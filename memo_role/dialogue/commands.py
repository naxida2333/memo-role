"""内置指令：在对话里直接切换人设 / 模型、清理上下文、查看记忆。

为什么要有指令
--------------

低配设备上「让模型自己理解要切角色」既慢又不靠谱。指令在**调用模型之前**
就被拦截处理，因此既省算力（不进推理）、又不会被当成聊天内容提取进记忆
（避免「/reset」变成一条情节记忆）。

会话级绑定
----------

``/persona`` 与 ``/model`` 写的是**当前会话**的绑定（``session.persona_id`` /
``session.model_id``），而不是全局默认值。这样私聊和群聊可以各自用不同角色，
同时继续共享同一份全局记忆 —— 对应需求里的「不同会话可绑定同一人设」。

未知指令不会报错，而是交回给模型当普通消息处理（``handle`` 返回 ``None``），
这样「/ 开头但其实是聊天」的情况也不会中断对话。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..logging_setup import get_logger
from ..memory.manager import MemoryManager
from ..persona.manager import PersonaManager
from .backends import BackendPool

logger = get_logger(__name__)


@dataclass
class CommandContext:
    """指令执行的上下文。"""

    session_id: str
    session_kind: str = "web"
    #: 当前会话已解析出的人设 id
    persona_id: str = ""
    speaker_name: str = ""


@dataclass
class CommandResult:
    """指令执行结果。"""

    #: 动作标识：help / persona_switch / model_switch / reset / memory / status ...
    action: str
    #: 返回给用户的文本
    reply: str
    #: 是否已处理（目前恒为 True；返回 None 表示未识别）
    handled: bool = True
    #: 结构化附加数据，便于 Web / 后台复用
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandSpec:
    """指令说明（同时用于帮助文本与路由）。"""

    name: str
    aliases: Tuple[str, ...]
    usage: str
    description: str


#: 内置指令清单（``name`` 与 aliases 都可触发）
COMMANDS: Tuple[CommandSpec, ...] = (
    CommandSpec("help", ("帮助", "?", "？"), "/help", "显示所有可用指令"),
    CommandSpec(
        "persona",
        ("角色", "人设", "char"),
        "/persona [人设id|list]",
        "查看可用人设，或把当前会话切到指定人设",
    ),
    CommandSpec(
        "model",
        ("模型", "model"),
        "/model [模型id|list]",
        "查看可用模型，或把当前会话切到指定模型",
    ),
    CommandSpec(
        "reset",
        ("清空", "重置", "clear"),
        "/reset",
        "清空当前会话的上下文（长期记忆保留）",
    ),
    CommandSpec(
        "memory", ("记忆", "remember"), "/memory [关键词]", "查看长期记忆；带关键词则按相关度检索"
    ),
    CommandSpec("status", ("状态", "status"), "/status", "查看当前人设 / 模型 / 记忆概况"),
)

#: 触发「列出清单」的参数写法
_LIST_ARGS = {"list", "ls", "列表", "所有", "全部"}


class CommandHandler:
    """内置指令解析与执行。"""

    def __init__(
        self,
        cfg: Any,
        memory: MemoryManager,
        persona: PersonaManager,
        backends: BackendPool,
    ) -> None:
        self.cfg = cfg
        self.memory = memory
        self.persona = persona
        self.backends = backends
        prefixes = tuple(cfg.dialogue.command_prefixes or ("/",))
        self.prefix = prefixes[0] if prefixes else "/"

        # 名称 / 别名 → 规范名
        self._routes: Dict[str, str] = {}
        for spec in COMMANDS:
            for key in (spec.name, *spec.aliases):
                self._routes[key.casefold()] = spec.name

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def handle(self, text: str, ctx: CommandContext) -> Optional[CommandResult]:
        """处理一条已去掉前缀的指令文本；未识别返回 ``None``。"""
        raw = (text or "").strip()
        if not raw:
            return None
        name, _, args = raw.partition(" ")
        target = self._routes.get(name.casefold())
        if target is None:
            logger.debug("未识别指令：%s", name)
            return None
        handler = getattr(self, f"_cmd_{target}")
        return handler(args.strip(), ctx)

    def help_text(self) -> str:
        """帮助文本（按配置的指令前缀渲染，避免写死的 ``/`` 与实际不符）。"""
        lines = ["可用指令："]
        for spec in COMMANDS:
            usage = spec.usage
            if usage.startswith("/"):
                usage = f"{self.prefix}{usage[1:]}"
            lines.append(f"  {usage}\n      {spec.description}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 各指令实现
    # ------------------------------------------------------------------
    def _cmd_help(self, args: str, ctx: CommandContext) -> CommandResult:
        return CommandResult("help", self.help_text())

    def _cmd_persona(self, args: str, ctx: CommandContext) -> CommandResult:
        current = ctx.persona_id or self.persona.current_default() or ""

        # 查看清单
        if not args or args.casefold() in _LIST_ARGS:
            summaries = self.persona.list_summaries()
            lines = [f"当前人设：{self._persona_label(current)}", "可用人设："]
            for item in summaries:
                mark = "（当前）" if item["id"] == current else ""
                lines.append(f"  {item['id']} — {item['name']}{mark}")
            lines.append(f"切换方式：{self.prefix}persona <人设id>")
            return CommandResult(
                "persona_list",
                "\n".join(lines),
                data={"current": current, "personas": [i["id"] for i in summaries]},
            )

        # 切换（写到会话绑定上）
        card = self.persona.get_or_none(args)
        if card is None:
            ids = [i["id"] for i in self.persona.list_summaries()]
            return CommandResult(
                "persona_not_found",
                f"没有找到人设「{args}」。可用人设：{', '.join(ids) or '（无）'}",
                data={"requested": args},
            )
        self._bind(ctx, persona_id=card.id)
        return CommandResult(
            "persona_switch",
            f"已把当前会话的人设切换为「{card.name}」",
            data={"persona_id": card.id, "name": card.name},
        )

    def _cmd_model(self, args: str, ctx: CommandContext) -> CommandResult:
        backend_name = self.cfg.inference.backend or ""
        is_openai = backend_name == "openai_api"

        if not args or args.casefold() in _LIST_ARGS:
            lines = [f"当前推理后端：{backend_name}"]
            if is_openai:
                lines.append(f"当前 API 模型：{self.cfg.inference.openai.model or '（未设置）'}")
                lines.append("API 后端可直接指定任意模型名。")
                options: List[str] = []
            else:
                options = self.backends.model_ids()
                lines.append("可用本地模型：")
                for item in self.backends.available_models():
                    mark = "✓已下载" if item.get("downloaded") else "未下载"
                    lines.append(f"  {item['id']} — {item.get('name', '')} [{mark}]")
            lines.append(f"切换方式：{self.prefix}model <模型id>")
            return CommandResult(
                "model_list", "\n".join(lines), data={"backend": backend_name, "models": options}
            )

        # 校验模型名：本地后端必须存在于注册表，API 后端接受任意名字
        if not is_openai and args not in self.backends.model_ids():
            options = self.backends.model_ids()
            return CommandResult(
                "model_not_found",
                f"没有找到模型「{args}」。可用模型：{', '.join(options) or '（无）'}",
                data={"requested": args},
            )

        self._bind(ctx, model_id=args)
        return CommandResult(
            "model_switch",
            f"已把当前会话的模型切换为「{args}」，下一条消息生效",
            data={"model_id": args},
        )

    def _cmd_reset(self, args: str, ctx: CommandContext) -> CommandResult:
        removed = self.memory.clear_working(ctx.session_id)
        return CommandResult(
            "reset",
            f"已清空当前会话的 {removed} 条上下文消息（长期记忆保留）。",
            data={"removed": removed},
        )

    def _cmd_memory(self, args: str, ctx: CommandContext) -> CommandResult:
        persona_id = ctx.persona_id or None
        if args:
            records = self.memory.recall(
                args, session_id=ctx.session_id, persona_id=persona_id
            )
            if not records:
                return CommandResult("memory", f"没有找到与「{args}」相关的长期记忆。")
            lines = [f"与「{args}」相关的记忆："]
            lines += [f"  - {r.content}" for r in records]
            return CommandResult("memory", "\n".join(lines), data={"count": len(records)})

        core = self.memory.core_memories(persona_id=persona_id)
        if not core:
            return CommandResult("memory", "还没有长期记忆。多聊几句我就会记住啦。")
        lines = ["长期记忆（核心）："]
        lines += [f"  - {r.content}" for r in core]
        return CommandResult("memory", "\n".join(lines), data={"count": len(core)})

    def _cmd_status(self, args: str, ctx: CommandContext) -> CommandResult:
        stats = self.memory.stats()
        active = self.backends.current
        model_label = self.cfg.inference.model or "默认"
        if active is not None:
            try:
                model_label = active.describe().get("model") or model_label
            except Exception:  # noqa: BLE001 - 描述失败不影响状态展示
                pass
        lines = [
            "运行状态：",
            f"  人设：{self._persona_label(ctx.persona_id)}",
            f"  人设绑定：{ctx.persona_id or '（跟随默认）'}",
            f"  模型：{model_label}（{self.cfg.inference.backend}）",
            f"  会话消息数：{self.memory.count_messages(ctx.session_id)}",
            f"  记忆条数：{stats.get('memories', stats.get('memory_count', '-'))}",
        ]
        return CommandResult("status", "\n".join(lines), data=stats)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _bind(self, ctx: CommandContext, *, persona_id: str = "", model_id: str = "") -> None:
        """把会话绑定到指定人设 / 模型（只写入非空字段，不影响其它会话）。"""
        self.memory.ensure_session(
            ctx.session_id,
            ctx.session_kind,
            persona_id=persona_id,
            model_id=model_id,
        )

    def _persona_label(self, persona_id: Optional[str]) -> str:
        card = self.persona.get_or_none(persona_id)
        if card is not None:
            return f"{card.name}（{card.id}）"
        default = self.persona.current_default()
        return f"默认（{default}）" if default else "（未设置）"