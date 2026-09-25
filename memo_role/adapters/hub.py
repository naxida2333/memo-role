"""OneBot 反向 WebSocket 连接管理。

反向 WS 的特点是「**NapCat 连过来，而不是我们连过去**」：HTTP 服务端同时也是
WebSocket 服务端，NapCat 作为客户端接入。因此需要维护「当前有哪些连接在线」，
并借同一条连接 **回发 API 调用**（发消息）。

调用与响应靠 ``echo`` 关联：每次 :meth:`OneBotConnection.call` 生成唯一 echo，
把未来放进 ``_pending``；收到响应报文时用 :meth:`OneBotConnection.resolve`
唤醒对应的等待者。这样多个调用可以并发在一条连接上跑而不会串号。
"""

from __future__ import annotations

import asyncio
import itertools
import json
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from .onebot import (
    ACTION_SEND_GROUP,
    ACTION_SEND_PRIVATE,
    ApiResponse,
    build_api_request,
    render_text_message,
)

logger = get_logger(__name__)

#: 单次 API 调用的默认超时（NapCat 未响应时不应把对话卡死）
DEFAULT_CALL_TIMEOUT = 15.0


class NotConnectedError(RuntimeError):
    """当前没有在线的 OneBot 连接，无法发送消息。"""


def _as_int_id(value: Any) -> Any:
    """QQ 号 / 群号转 int（OneBot 更偏好数字）；转不了就原样返回。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class OneBotTransport(ABC):
    """一条底层连接的抽象，便于用假实现做单测。"""

    @abstractmethod
    async def send_text(self, payload: str) -> None:
        """发送一条 JSON 文本报文。"""

    @abstractmethod
    async def close(self) -> None:
        """关闭连接。"""


class OneBotConnection:
    """一条在线连接：负责 API 调用与响应关联。"""

    def __init__(self, transport: OneBotTransport, *, self_id: str = "") -> None:
        self.transport = transport
        #: 机器人自身 QQ（由 meta_event 补齐）
        self.self_id = self_id
        self._pending: Dict[str, "asyncio.Future[ApiResponse]"] = {}
        self._counter = itertools.count(1)

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    def new_echo(self) -> str:
        return f"memo-{next(self._counter)}"

    async def call(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> ApiResponse:
        """发起一次 API 调用并等待响应；超时返回失败结果而非抛异常。"""
        echo = self.new_echo()
        future: "asyncio.Future[ApiResponse]" = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            payload = build_api_request(action, params, echo)
            await self.transport.send_text(json.dumps(payload, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            logger.warning("OneBot 调用 %s 超时（%.0fs 未收到响应）", action, timeout)
            return ApiResponse(status="failed", retcode=-1, echo=echo, message="timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 发送失败不应中断对话
            logger.warning("OneBot 调用 %s 发送失败：%s", action, exc)
            return ApiResponse(status="failed", retcode=-1, echo=echo, message=str(exc))
        finally:
            self._pending.pop(echo, None)

    def resolve(self, payload: Any) -> bool:
        """尝试把报文当作 API 响应处理；是响应返回 ``True``。"""
        if not isinstance(payload, dict):
            return False
        if "echo" not in payload or "status" not in payload:
            return False
        echo = str(payload.get("echo") or "")
        future = self._pending.get(echo)
        if future is None or future.done():
            logger.debug("收到无法关联的 OneBot 响应（echo=%s）", echo)
            return False
        future.set_result(ApiResponse.from_dict(payload))
        return True

    # ------------------------------------------------------------------
    # 便捷发送
    # ------------------------------------------------------------------
    async def send_private(self, user_id: Any, text: str) -> ApiResponse:
        """发送私聊消息。"""
        return await self.call(
            ACTION_SEND_PRIVATE,
            {"user_id": _as_int_id(user_id), "message": render_text_message(text)},
        )

    async def send_group(self, group_id: Any, text: str) -> ApiResponse:
        """发送群聊消息。"""
        return await self.call(
            ACTION_SEND_GROUP,
            {"group_id": _as_int_id(group_id), "message": render_text_message(text)},
        )

    # ------------------------------------------------------------------
    # 释放
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """取消未完成的调用并关闭底层连接（幂等）。"""
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()
        try:
            await self.transport.close()
        except Exception as exc:  # noqa: BLE001 - 关闭失败无需上抛
            logger.debug("关闭 OneBot 连接失败：%s", exc)


class ConnectionHub:
    """在线连接登记处：始终取「最近接入」的连接发消息。"""

    def __init__(self) -> None:
        self._connections: List[OneBotConnection] = []

    def register(self, conn: OneBotConnection) -> None:
        if conn not in self._connections:
            self._connections.append(conn)

    def unregister(self, conn: OneBotConnection) -> None:
        if conn in self._connections:
            self._connections.remove(conn)

    @property
    def online(self) -> bool:
        return bool(self._connections)

    @property
    def count(self) -> int:
        return len(self._connections)

    def primary(self) -> Optional[OneBotConnection]:
        """最近接入的连接；NapCat 重连时新连接自然接管。"""
        return self._connections[-1] if self._connections else None

    async def call(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> ApiResponse:
        return await self._require().call(action, params, timeout=timeout)

    async def send_private(
        self, user_id: Any, text: str, *, timeout: float = DEFAULT_CALL_TIMEOUT
    ) -> ApiResponse:
        """给指定用户发私聊消息。"""
        return await self._require().send_private(user_id, text)

    async def send_group(
        self, group_id: Any, text: str, *, timeout: float = DEFAULT_CALL_TIMEOUT
    ) -> ApiResponse:
        """给指定群发消息。"""
        return await self._require().send_group(group_id, text)

    def _require(self) -> OneBotConnection:
        conn = self.primary()
        if conn is None:
            raise NotConnectedError("没有在线的 OneBot 连接，无法发送消息")
        return conn

    def describe(self) -> Dict[str, Any]:
        conn = self.primary()
        return {
            "online": self.online,
            "connections": self.count,
            "self_id": conn.self_id if conn else "",
        }