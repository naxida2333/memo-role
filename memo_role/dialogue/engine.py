"""对话编排引擎：把「人设 + 记忆 + 模型」串成一次完整回复。

一次 :meth:`DialogueEngine.reply` 的完整流程::

    收到消息
      ↓
    会话 / 发言人登记
      ↓
    写入工作记忆（原始消息）
      ↓
    记忆提取（关键信息 → 情节 / 核心记忆，全局共享）
      ↓
    群聊？→ 判定是否该回复（未命中则到此为止，但消息与记忆已保留）
      ↓
    召回三层记忆 → 组装系统提示 + 历史 + 当前消息
      ↓
    调用推理后端生成回复
      ↓
    写入助手回复 → 返回结果

两个刻意的设计：

- **不回也要记**：群里「旁观」到的消息同样入库并提取记忆，保证群聊里的信息
  在私聊中也能被用到（全局统一记忆）。
- **生成串行化**：低配设备同时跑两次推理会卡死，因此用锁把生成阶段串起来。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from ..inference.base import ChatMessage, ChatBackend, GenParams
from ..inference.factory import gen_params
from ..logging_setup import get_logger
from ..memory.manager import MemoryManager
from ..memory.store import MemoryRecord
from ..persona.card import PersonaCard
from ..persona.manager import PersonaManager
from .backends import BackendPool
from .commands import CommandContext, CommandHandler
from .context import (
    GroupReplyPolicy,
    ReplyDecision,
    compose_messages,
    group_instruction,
)

logger = get_logger(__name__)

#: 用首条消息自动命名会话时的长度上限
AUTO_TITLE_MAX = 24


@dataclass
class TurnRequest:
    """一次对话请求。"""

    session_id: str
    user_text: str
    #: private | group | web
    session_kind: str = "web"
    #: qq | local | web
    platform: str = "local"
    peer_id: str = ""
    session_title: str = ""
    #: 人设 id；留空用默认人设
    persona_id: str = ""
    #: 模型 id；留空用配置默认模型
    model_id: str = ""
    #: 发言人标识与昵称（群聊必需，用于区分不同人）
    speaker_uid: str = ""
    speaker_name: str = ""
    #: 机器人可能的昵称（用于群聊「叫名字」识别）
    bot_names: Sequence[str] = ()
    #: 渠道层是否已识别出 @机器人
    is_mentioned: bool = False
    #: 追加到系统提示的额外规则（渠道相关，如禁言提醒）
    extra_rules: str = ""
    #: 覆盖生成参数；留空用配置
    gen: Optional[GenParams] = None


@dataclass
class TurnResult:
    """一次对话的结果。"""

    reply: str = ""
    persona_id: str = ""
    model_id: str = ""
    #: 本轮新写入的记忆
    new_memories: List[MemoryRecord] = field(default_factory=list)
    #: 是否被策略跳过（群聊未命中）
    skipped: bool = False
    decision: Optional[ReplyDecision] = None
    #: 实际发送给模型的消息（便于日志排查；生产环境可不下发）
    messages: List[ChatMessage] = field(default_factory=list)
    #: 是否由内置指令直接产生（未调用模型）
    is_command: bool = False
    #: 指令动作标识（如 persona_switch / model_switch / reset）
    action: str = ""
    #: 指令的结构化附加数据
    command_data: Dict[str, Any] = field(default_factory=dict)


class DialogueEngine:
    """对话编排引擎。"""

    def __init__(
        self,
        cfg,
        memory: MemoryManager,
        persona: PersonaManager,
        backends: BackendPool,
        *,
        policy: Optional[GroupReplyPolicy] = None,
        commands: Optional[CommandHandler] = None,
    ) -> None:
        self.cfg = cfg
        self.memory = memory
        self.persona = persona
        self.backends = backends
        self.policy = policy or GroupReplyPolicy.from_config(cfg)
        self.commands = commands or CommandHandler(cfg, memory, persona, backends)
        # 生成阶段串行化：低配设备同时推理会互相拖慢甚至 OOM
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    @classmethod
    def build(
        cls,
        cfg,
        *,
        memory: Optional[MemoryManager] = None,
        persona: Optional[PersonaManager] = None,
        backends: Optional[BackendPool] = None,
        policy: Optional[GroupReplyPolicy] = None,
    ) -> "DialogueEngine":
        """按配置组装引擎（各部件可注入，便于测试）。"""
        memory = memory or MemoryManager.build(cfg)
        persona = persona or PersonaManager.build(cfg)
        backends = backends or BackendPool(cfg)
        return cls(cfg, memory, persona, backends, policy=policy)

    # ------------------------------------------------------------------
    # 回复判定
    # ------------------------------------------------------------------
    def should_reply(self, request: TurnRequest) -> ReplyDecision:
        """判定是否回复（私聊恒为「回」）。"""
        return self.policy.should_reply(
            request.user_text,
            session_kind=request.session_kind,
            is_mentioned=request.is_mentioned,
            bot_names=request.bot_names,
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def reply(self, request: TurnRequest) -> TurnResult:
        """同步生成一次回复。"""
        decision = self.should_reply(request)
        text = decision.text if decision.text else request.user_text
        persona_id = self._resolve_persona_id(request)
        card = self.persona.get(persona_id)
        model_id = self._resolve_model_id(request)

        # 内置指令优先拦截：不加载模型、不进推理、不提取记忆（省算力也不污染记忆）
        if decision.is_command:
            command = self._run_command(decision.text, request, card, model_id, decision)
            if command is not None:
                return command

        backend = self.backends.get(model_id=model_id)
        speaker_id, new_memories = self._record_incoming(request, text, card, model_id)

        if not decision.should_reply:
            logger.debug("群聊未命中回复策略，已记录信息：%s", decision.reason)
            return TurnResult(
                persona_id=card.id,
                model_id=self._model_of(backend, model_id),
                new_memories=new_memories,
                skipped=True,
                decision=decision,
            )

        messages = self._build_messages(request, text, card)
        params = request.gen or gen_params(self.cfg)

        with self._lock:
            reply_text = (backend.chat(messages, params) or "").strip()

        self.memory.record_message(request.session_id, "assistant", reply_text)
        return TurnResult(
            reply=reply_text,
            persona_id=card.id,
            model_id=self._model_of(backend, model_id),
            new_memories=new_memories,
            decision=decision,
            messages=messages,
        )

    def stream_reply(
        self,
        request: TurnRequest,
        *,
        on_complete: Optional[Callable[[TurnResult], None]] = None,
    ) -> Iterator[str]:
        """流式生成回复，逐段产出文本。

        :param on_complete: 生成结束（或跳过）后回调最终结果，便于调用方落库 / 记日志
        """
        decision = self.should_reply(request)
        text = decision.text if decision.text else request.user_text
        card = self.persona.get(self._resolve_persona_id(request))
        model_id = self._resolve_model_id(request)

        # 指令结果一次性产出（本身不经过模型，没有「流」的必要）
        if decision.is_command:
            command = self._run_command(decision.text, request, card, model_id, decision)
            if command is not None:
                if on_complete is not None:
                    on_complete(command)
                if command.reply:
                    yield command.reply
                return

        backend = self.backends.get(model_id=model_id)
        speaker_id, new_memories = self._record_incoming(request, text, card, model_id)

        if not decision.should_reply:
            if on_complete is not None:
                on_complete(
                    TurnResult(
                        persona_id=card.id,
                        model_id=self._model_of(backend, model_id),
                        new_memories=new_memories,
                        skipped=True,
                        decision=decision,
                    )
                )
            return

        messages = self._build_messages(request, text, card)
        params = request.gen or gen_params(self.cfg)

        chunks: List[str] = []
        # 用 try/finally 保证即使消费者中途放弃，锁也能释放
        self._lock.acquire()
        try:
            for piece in backend.stream(messages, params):
                if not piece:
                    continue
                chunks.append(piece)
                yield piece
        finally:
            self._lock.release()

        reply_text = "".join(chunks).strip()
        self.memory.record_message(request.session_id, "assistant", reply_text)
        if on_complete is not None:
            on_complete(
                TurnResult(
                    reply=reply_text,
                    persona_id=card.id,
                    model_id=self._model_of(backend, model_id),
                    new_memories=new_memories,
                    decision=decision,
                    messages=messages,
                )
            )

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    def _run_command(
        self,
        text: str,
        request: TurnRequest,
        card: PersonaCard,
        model_id: Optional[str],
        decision: ReplyDecision,
    ) -> Optional[TurnResult]:
        """尝试按内置指令处理；未识别返回 ``None``（交回模型）。"""
        ctx = CommandContext(
            session_id=request.session_id,
            session_kind=request.session_kind,
            persona_id=card.id,
            speaker_name=request.speaker_name,
        )
        result = self.commands.handle(text, ctx)
        if result is None:
            return None

        logger.info("执行内置指令 %s（会话 %s）", result.action, request.session_id)
        return TurnResult(
            reply=result.reply,
            persona_id=card.id,
            model_id=model_id or self.cfg.inference.model,
            decision=decision,
            is_command=True,
            action=result.action,
            command_data=result.data,
        )

    # ------------------------------------------------------------------
    # 解析：会话级人设 / 模型绑定
    # ------------------------------------------------------------------
    def _resolve_persona_id(self, request: TurnRequest) -> Optional[str]:
        """人设解析顺序：本次请求 → 会话绑定 → 默认人设（返回 None 交由调用方取默认）。"""
        if request.persona_id:
            return request.persona_id
        session = self.memory.get_session(request.session_id)
        if session is not None and session.persona_id:
            return session.persona_id
        return None

    def _resolve_model_id(self, request: TurnRequest) -> Optional[str]:
        """模型解析顺序：本次请求 → 会话绑定 → 配置默认（返回 None 交由后端池解析）。"""
        if request.model_id:
            return request.model_id
        session = self.memory.get_session(request.session_id)
        if session is not None and session.model_id:
            return session.model_id
        return None

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------
    def _record_incoming(
        self,
        request: TurnRequest,
        text: str,
        card: PersonaCard,
        model_id: Optional[str] = None,
    ) -> tuple[Optional[int], List[MemoryRecord]]:
        """登记会话 / 发言人，写入原始消息，并提取记忆。

        ``model_id`` 只在**显式指定**（请求或会话绑定）时才写入会话，
        否则传空串以保留原有绑定（``upsert_session`` 只补非空字段），
        避免用配置默认值覆盖掉用户 ``/model`` 的选择。同理，标题只在
        渠道没给、会话也还没有时才由首条消息补上。
        """
        self.memory.ensure_session(
            request.session_id,
            request.session_kind,
            platform=request.platform,
            peer_id=request.peer_id,
            title=request.session_title or self._auto_title(request.session_id, text),
            persona_id=card.id,
            model_id=model_id or "",
        )

        speaker_id: Optional[int] = None
        if request.speaker_uid:
            speaker_id = self.memory.register_speaker(
                request.platform, request.speaker_uid, request.speaker_name
            )

        self.memory.record_message(
            request.session_id,
            "user",
            text,
            speaker_id=speaker_id,
            speaker_name=request.speaker_name,
        )

        # 群聊里别人说的话同样提取记忆，才能真正做到「全局统一记忆」
        new_memories = self.memory.extract_and_store(
            text,
            session_id=request.session_id,
            speaker_id=speaker_id,
            speaker_name=request.speaker_name,
            persona_id=card.id,
        )
        return speaker_id, new_memories

    def _auto_title(self, session_id: str, text: str) -> str:
        """会话还没有标题时，用首条用户消息命名（便于在列表里辨认）。

        已有标题一律不覆盖：既避免把用户手动改过的名字冲掉，也避免群聊侧
        （已按群名登记）被第一条消息顶替。返回空串表示「保持原标题」。
        指令消息不会走到这里（在 :meth:`reply` 里就提前返回了），
        所以标题不会变成一条 ``/help``。
        """
        session = self.memory.get_session(session_id)
        if session is not None and session.title:
            return ""
        title = " ".join((text or "").split())
        if not title:
            return ""
        if len(title) > AUTO_TITLE_MAX:
            return title[:AUTO_TITLE_MAX] + "…"
        return title

    def _build_messages(
        self, request: TurnRequest, text: str, card: PersonaCard
    ) -> List[ChatMessage]:
        """组装系统提示 + 工作记忆 + 当前消息。"""
        memory_context = self.memory.build_memory_context(
            text, session_id=request.session_id, persona_id=card.id
        )

        rules: List[str] = []
        group = request.session_kind == "group"
        if group and self.cfg.dialogue.label_group_speakers:
            rules.append(group_instruction())
        if request.extra_rules.strip():
            rules.append(request.extra_rules.strip())

        system_prompt = self.persona.render_system_prompt(
            card.id,
            memory_context=memory_context,
            extra_rules="\n\n".join(rules),
        )

        # 当前消息已写入工作记忆，因此取历史时排除最后一条，避免重复注入
        history = self.memory.working_messages(
            request.session_id,
            limit=self._history_limit(),
            exclude_last=1,
        )
        return compose_messages(
            system_prompt,
            history,
            text,
            user_name=request.speaker_name,
            group=group and self.cfg.dialogue.label_group_speakers,
        )

    def _history_limit(self) -> int:
        return int(self.cfg.dialogue.history_limit) or int(self.cfg.memory.working_window)

    @staticmethod
    def _model_of(backend: Optional[ChatBackend], model_id: Optional[str] = "") -> str:
        """当前实际使用的模型标识（用于结果回传与后台展示）。"""
        if backend is not None:
            try:
                info = backend.describe()
            except Exception:  # noqa: BLE001 - 描述失败不应影响主流程
                info = {}
            found = info.get("model") or info.get("model_path")
            if found:
                return str(found)
        return model_id or ""

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def describe(self) -> dict:
        """引擎状态摘要，供管理后台展示。"""
        return {
            "persona": self.persona.describe(),
            "memory": self.memory.stats(),
            "backend": self.backends.describe(),
            "policy": {
                "group_reply_probability": self.policy.probability,
                "reply_when_mentioned": self.policy.reply_when_mentioned,
                "command_prefixes": list(self.policy.command_prefixes),
            },
        }

    def close(self) -> None:
        """释放后端与记忆资源。"""
        self.backends.close()
        self.memory.close()