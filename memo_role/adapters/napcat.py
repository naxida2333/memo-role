"""NapCat 适配器：QQ 消息 ↔ 对话引擎。

职责边界
--------

- **收**：NapCat 通过 OneBot v11 反向 WebSocket 把 QQ 消息推过来
- **转**：把 :class:`~memo_role.adapters.onebot.OneBotEvent` 翻译成
  :class:`~memo_role.dialogue.engine.TurnRequest`（会话 id、发言人、@识别）
- **回**：把引擎产出的回复通过同一条连接发回 QQ

刻意不做的事
------------

- **不做回复判定**：群聊该不该回由 :class:`~memo_role.dialogue.context.GroupReplyPolicy`
  决定，适配器只负责把 ``is_mentioned`` 等事实如实上报
- **不覆盖会话绑定**：``napcat.persona`` 只在会话尚未绑定人设时作为初始值，
  这样群里用 ``/persona`` 切过的角色不会被配置默认值顶掉
- **不静默丢消息**：群聊里没 @ 机器人也会照常进引擎「记一笔」，保证全局记忆完整

异步与阻塞
----------

推理是同步阻塞调用（低配设备上可能跑很久），因此用
:func:`asyncio.to_thread` 放到线程里执行，避免卡住 WebSocket 事件循环。
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence

import asyncio
import contextlib
import json

from ..logging_setup import get_logger
from ..dialogue.engine import DialogueEngine, TurnRequest, TurnResult
from .hub import ConnectionHub, NotConnectedError, OneBotConnection, OneBotTransport
from .onebot import PLATFORM, OneBotEvent

logger = get_logger(__name__)

try:  # starlette 随 fastapi 一起安装；缺失时（未启用 Web 层）退化为不捕获
    from starlette.websockets import WebSocketDisconnect as _WebSocketDisconnect

    _DISCONNECT_ERRORS: tuple = (_WebSocketDisconnect,)
except ImportError:  # pragma: no cover - 仅在未安装 Web 依赖时发生
    _DISCONNECT_ERRORS = ()


class NapCatAdapter:
    """NapCat（OneBot v11 反向 WebSocket）适配器。"""

    def __init__(
        self,
        cfg: Any,
        engine: DialogueEngine,
        *,
        hub: Optional[ConnectionHub] = None,
        bot_names: Sequence[str] = (),
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.hub = hub or ConnectionHub()
        #: 额外的「机器人称呼」，用于识别纯文本里直接叫名字的情况
        self._extra_names = _dedup(bot_names)

    @classmethod
    def build(
        cls,
        cfg: Any,
        engine: DialogueEngine,
        *,
        hub: Optional[ConnectionHub] = None,
        bot_names: Sequence[str] = (),
    ) -> "NapCatAdapter":
        return cls(cfg, engine, hub=hub, bot_names=bot_names)

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @property
    def ws_path(self) -> str:
        """反向 WS 路径，需与 NapCat 侧配置一致。"""
        return self.cfg.napcat.ws_path or "/onebot/v11/ws"

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.napcat.enabled)

    def verify_token(self, token: str) -> bool:
        """校验访问令牌；配置留空表示不校验。"""
        expected = self.cfg.napcat.access_token or ""
        return not expected or token == expected

    def is_admin(self, user_id: Any) -> bool:
        """是否为配置的管理员 QQ。"""
        admins = {str(a) for a in (self.cfg.napcat.admins or [])}
        return bool(admins) and str(user_id) in admins

    def bot_names(self) -> List[str]:
        """机器人可能的称呼：配置项 + 默认人设名（含 ``extra.nicknames``）。"""
        names = list(self._extra_names)
        persona_id = self.cfg.napcat.persona or self.engine.persona.current_default()
        card = self.engine.persona.get_or_none(persona_id)
        if card is not None:
            names.append(card.name)
            extra = card.extra.get("nicknames") if isinstance(card.extra, dict) else None
            if isinstance(extra, (list, tuple, str)):
                names.extend([extra] if isinstance(extra, str) else list(extra))
        return _dedup(names)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    async def handle_event(self, event: OneBotEvent) -> Optional[TurnResult]:
        """处理一条 OneBot 事件：非消息 / 空消息直接忽略。

        整个处理过程都兜住异常并记日志：单条消息出问题不应把 WebSocket
        连接（以及负责消费事件的 worker）弄挂。
        """
        if not event.is_message:
            logger.debug("忽略非消息事件：%s", event.post_type or event.raw.get("post_type"))
            return None

        text = event.text
        if not text:
            logger.debug("空消息，已忽略（会话 %s）", event.session_id)
            return None

        try:
            request = self._build_request(event, text)
            result = await asyncio.to_thread(self.engine.reply, request)
            if result.skipped or not result.reply:
                return result
            await self.send_reply(event, result.reply)
            return result
        except Exception as exc:  # noqa: BLE001 - 推理失败不应中断连接
            logger.exception("处理 QQ 消息失败（会话 %s）：%s", event.session_id, exc)
            return None

    def _build_request(self, event: OneBotEvent, text: str) -> TurnRequest:
        """把 OneBot 事件翻译成对话请求。"""
        return TurnRequest(
            session_id=event.session_id,
            user_text=text,
            session_kind=event.session_kind,
            platform=PLATFORM,
            peer_id=event.peer_id,
            persona_id=self._persona_for(event.session_id),
            speaker_uid=event.user_id,
            speaker_name=event.nickname,
            bot_names=self.bot_names(),
            is_mentioned=event.mentioned_self or event.at_all,
        )

    def _persona_for(self, session_id: str) -> str:
        """仅在会话尚未绑定人设时用配置人设兜底。"""
        session = self.engine.memory.get_session(session_id)
        if session is not None and session.persona_id:
            return ""
        return self.cfg.napcat.persona or ""

    async def send_reply(self, event: OneBotEvent, text: str) -> bool:
        """把回复发回 QQ；没有在线连接时返回 ``False``。"""
        try:
            if event.is_group:
                response = await self.hub.send_group(event.group_id, text)
            else:
                response = await self.hub.send_private(event.user_id, text)
        except NotConnectedError as exc:
            logger.warning("回复未发出：%s", exc)
            return False
        if not response.ok:
            logger.warning("发送 QQ 消息失败：%s %s", response.retcode, response.message)
            return False
        return True

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------
    async def serve_connection(self, websocket: Any) -> None:
        """驱动一条反向 WS 连接直到断开。

        ``websocket`` 只需具备 ``accept`` / ``receive_text`` / ``close`` /
        ``headers`` / ``query_params``，因此可用 FastAPI 的 WebSocket，
        也可在测试里用假对象替代。

        **读与处理必须解耦**：发出消息后要等 NapCat 在同一连接上回响应，
        而事件处理（含推理）可能很久。若「读一帧 → 处理完 → 再读下一帧」，
        响应就永远等不到，形成自我死锁。因此这里让读取循环只负责
        「解析 + 立刻关联 API 响应 + 排队」，另起一个 worker 串行消费事件，
        既不死锁，又保证同一会话的消息按到达顺序处理。
        """
        if not self.verify_token(self._extract_token(websocket)):
            logger.warning("OneBot 反向连接鉴权失败，已拒绝")
            await websocket.close(code=1008)
            return

        await websocket.accept()
        conn = OneBotConnection(_WebSocketTransport(websocket))
        self.hub.register(conn)
        logger.info("OneBot 反向连接已建立（当前 %d 条）", self.hub.count)

        events: "asyncio.Queue[dict]" = asyncio.Queue()
        worker = asyncio.create_task(self._consume_events(conn, events))
        try:
            while True:
                raw = await websocket.receive_text()
                self._enqueue(conn, events, raw)
        except _DISCONNECT_ERRORS:
            logger.info("OneBot 反向连接已断开")
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            self.hub.unregister(conn)
            await conn.close()

    async def _consume_events(self, conn: OneBotConnection, events: "asyncio.Queue[dict]") -> None:
        """串行消费事件队列。"""
        while True:
            payload = await events.get()
            await self.dispatch(conn, payload)

    def _enqueue(self, conn: OneBotConnection, events: "asyncio.Queue[dict]", raw: Any) -> None:
        """解析一帧报文：API 响应立即关联，其余排队等 worker 处理。"""
        payload = _parse_payload(raw)
        if payload is None or conn.resolve(payload):
            return
        events.put_nowait(payload)

    async def on_message(self, conn: OneBotConnection, raw: Any) -> None:
        """直接处理一帧报文（不排队）；便于单测与非队列场景调用。"""
        payload = _parse_payload(raw)
        if payload is None or conn.resolve(payload):
            return
        await self.dispatch(conn, payload)

    async def dispatch(self, conn: OneBotConnection, payload: dict) -> None:
        """处理一条已解析的非 API 响应报文。"""
        if str(payload.get("post_type") or "") == "meta_event":
            if str(payload.get("meta_event_type") or "") in {"lifecycle", "connect"}:
                conn.self_id = str(payload.get("self_id") or conn.self_id)
                logger.info("NapCat 连接就绪（机器人 %s）", conn.self_id)
            return
        await self.handle_event(OneBotEvent.from_dict(payload))

    def mount(self, app: Any) -> None:
        """把反向 WS 路由挂到 FastAPI 应用上。"""
        from fastapi import WebSocket  # 延迟导入，未启用 Web 层时不引入依赖

        adapter = self

        @app.websocket(self.ws_path)
        async def onebot_ws(websocket: WebSocket) -> None:  # pragma: no cover - 由框架调用
            await adapter.serve_connection(websocket)

    @staticmethod
    def _extract_token(websocket: Any) -> str:
        """从 ``Authorization: Bearer`` 或查询参数里取访问令牌。"""
        headers = getattr(websocket, "headers", None) or {}
        auth = str(headers.get("authorization") or "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        query = getattr(websocket, "query_params", None) or {}
        try:
            return str(query.get("access_token") or "")
        except AttributeError:  # pragma: no cover - 非映射实现
            return ""

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def describe(self) -> dict:
        """适配器状态摘要，供管理后台展示。"""
        return {
            "enabled": self.enabled,
            "ws_path": self.ws_path,
            "token_required": bool(self.cfg.napcat.access_token),
            "persona": self.cfg.napcat.persona,
            "admins": list(self.cfg.napcat.admins or []),
            "bot_names": self.bot_names(),
            "connection": self.hub.describe(),
        }


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
class _WebSocketTransport(OneBotTransport):
    """把 FastAPI 的 WebSocket 包成 :class:`OneBotTransport`。"""

    def __init__(self, websocket: Any) -> None:
        self._ws = websocket

    async def send_text(self, payload: str) -> None:
        await self._ws.send_text(payload)

    async def close(self) -> None:
        await self._ws.close()


def _parse_payload(raw: Any) -> Optional[dict]:
    """把一帧原始文本解析成字典；解析失败返回 ``None``。"""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("无法解析 OneBot 报文：%s", str(raw)[:200])
        return None
    return payload if isinstance(payload, dict) else None


def _dedup(values: Iterable[Any]) -> List[str]:
    """去空、去重并保持顺序。"""
    result: List[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result