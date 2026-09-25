"""NapCat 适配器测试：OneBot 报文 → 对话引擎 → 回发 QQ。

用假连接（:class:`FakeTransport` / :class:`FakeWebSocket`）覆盖全链路，
因此不需要真的连 NapCat，也不需要真的跑模型。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Dict, List, Optional

import pytest
from starlette.websockets import WebSocketDisconnect

from memo_role.adapters.hub import (
    ConnectionHub,
    OneBotConnection,
    OneBotTransport,
)
from memo_role.adapters.napcat import NapCatAdapter
from memo_role.adapters.onebot import OneBotEvent
from memo_role.dialogue.backends import BackendPool
from memo_role.dialogue.engine import DialogueEngine

from conftest import FakeBackend


# ----------------------------------------------------------------------
# 夹具与工具
# ----------------------------------------------------------------------
@pytest.fixture
def engine(cfg, memory_manager, persona_manager, fake_backend) -> DialogueEngine:
    """群聊随机回复概率设为 0，保证判定确定可测。"""
    cfg.dialogue.group_reply_probability = 0.0
    pool = BackendPool(cfg, factory=lambda c, mid: fake_backend)
    return DialogueEngine.build(
        cfg, memory=memory_manager, persona=persona_manager, backends=pool
    )


class FakeTransport(OneBotTransport):
    """记录发出的报文，并立刻回一条成功响应，模拟 NapCat 的行为。"""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.closed = False
        self.conn: Optional[OneBotConnection] = None

    async def send_text(self, payload: str) -> None:
        data = json.loads(payload)
        self.sent.append(data)
        if self.conn is not None:
            self.conn.resolve(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": len(self.sent)},
                    "echo": data.get("echo"),
                }
            )

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def adapter(cfg, engine, transport) -> NapCatAdapter:
    hub = ConnectionHub()
    conn = OneBotConnection(transport, self_id="999")
    transport.conn = conn
    hub.register(conn)
    return NapCatAdapter.build(cfg, engine, hub=hub)


def private_payload(**kwargs) -> dict:
    payload = {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 11,
        "user_id": 10001,
        "self_id": 999,
        "raw_message": "你好",
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "sender": {"nickname": "小明", "card": ""},
    }
    payload.update(kwargs)
    return payload


def group_payload(**kwargs) -> dict:
    payload = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 12,
        "user_id": 10001,
        "group_id": 555,
        "self_id": 999,
        "raw_message": "@小忆 早上好",
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " 早上好"}},
        ],
        "sender": {"nickname": "nick", "card": "小明"},
    }
    payload.update(kwargs)
    return payload


def actions(transport: FakeTransport) -> List[str]:
    return [item["action"] for item in transport.sent]


def parsed(payload: dict) -> OneBotEvent:
    return OneBotEvent.from_dict(payload)


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    """轮询等待条件成立（推理跑在线程里，不能靠固定 sleep）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时：条件始终未成立")


# ----------------------------------------------------------------------
# 会话与消息收发
# ----------------------------------------------------------------------
async def test_private_message_replies_and_records(adapter, transport, engine, fake_backend) -> None:
    result = await adapter.handle_event(parsed(private_payload()))
    assert result is not None and result.reply == fake_backend.reply
    assert actions(transport) == ["send_private_msg"]

    params = transport.sent[0]["params"]
    assert params["user_id"] == 10001
    assert params["message"][0]["data"]["text"] == fake_backend.reply
    # 工作记忆落库（用户 + 助手）
    roles = [m.role for m in engine.memory.working_messages("private:10001")]
    assert roles == ["user", "assistant"]


async def test_group_unmentioned_is_skipped_but_remembered(adapter, transport, engine) -> None:
    """群里没被 @ 时不回复，但消息与记忆都要留下（全局统一记忆）。"""
    event = parsed(
        group_payload(
            message=[{"type": "text", "data": {"text": "我叫小红"}}],
            raw_message="我叫小红",
        )
    )
    result = await adapter.handle_event(event)
    assert result is not None and result.skipped is True
    assert transport.sent == []  # 没有调用模型，也没有发消息
    assert engine.memory.count_messages("group:555") == 1


async def test_group_mention_replies(adapter, transport) -> None:
    result = await adapter.handle_event(parsed(group_payload()))
    assert result is not None and result.skipped is False
    assert actions(transport) == ["send_group_msg"]
    params = transport.sent[0]["params"]
    assert params["group_id"] == 555


async def test_group_called_by_name_replies(adapter, transport) -> None:
    """@ 之外的「直接叫名字」也要能识别（bot_names 含默认人设名）。"""
    event = parsed(
        group_payload(
            message=[{"type": "text", "data": {"text": "小忆你觉得呢"}}],
            raw_message="小忆你觉得呢",
        )
    )
    result = await adapter.handle_event(event)
    assert result is not None and result.skipped is False
    assert actions(transport) == ["send_group_msg"]


async def test_empty_message_is_ignored(adapter, transport) -> None:
    event = parsed(private_payload(message=[], raw_message=""))
    assert await adapter.handle_event(event) is None
    assert transport.sent == []


async def test_non_message_event_is_ignored(adapter) -> None:
    event = parsed({"post_type": "notice", "notice_type": "group_increase"})
    assert await adapter.handle_event(event) is None


# ----------------------------------------------------------------------
# 指令
# ----------------------------------------------------------------------
async def test_private_command_skips_model(adapter, transport, fake_backend) -> None:
    event = parsed(
        private_payload(
            message=[{"type": "text", "data": {"text": "/reset"}}], raw_message="/reset"
        )
    )
    result = await adapter.handle_event(event)
    assert result is not None and result.is_command is True
    assert fake_backend.calls == []  # 指令不进推理
    assert actions(transport) == ["send_private_msg"]


async def test_group_persona_command_binds_session(adapter, transport, engine) -> None:
    command = parsed(
        group_payload(
            message=[
                {"type": "at", "data": {"qq": "999"}},
                {"type": "text", "data": {"text": "/persona catgirl"}},
            ],
            raw_message="@小忆 /persona catgirl",
        )
    )
    await adapter.handle_event(command)
    session = engine.memory.get_session("group:555")
    assert session is not None and session.persona_id == "catgirl"

    # 会话已绑定，配置里的默认人设不能把它顶掉
    later = await adapter.handle_event(parsed(group_payload()))
    assert later is not None and later.persona_id == "catgirl"


# ----------------------------------------------------------------------
# 容错与连接
# ----------------------------------------------------------------------
async def test_backend_failure_is_swallowed(
    cfg, memory_manager, persona_manager, transport
) -> None:
    """单条消息推理失败只记日志，不能把 WebSocket 连接打断。"""

    class BoomBackend(FakeBackend):
        def chat(self, messages, params=None):  # type: ignore[override]
            raise RuntimeError("模型炸了")

    engine = DialogueEngine.build(
        cfg,
        memory=memory_manager,
        persona=persona_manager,
        backends=BackendPool(cfg, factory=lambda c, mid: BoomBackend()),
    )
    hub = ConnectionHub()
    conn = OneBotConnection(transport, self_id="999")
    transport.conn = conn
    hub.register(conn)

    adapter = NapCatAdapter.build(cfg, engine, hub=hub)
    assert await adapter.handle_event(parsed(private_payload())) is None
    assert transport.sent == []


async def test_send_reply_without_connection(cfg, engine) -> None:
    adapter = NapCatAdapter.build(cfg, engine)  # 空 hub
    assert await adapter.send_reply(parsed(private_payload()), "你好") is False


def test_verify_token(cfg, engine) -> None:
    adapter = NapCatAdapter.build(cfg, engine)
    assert adapter.verify_token("任意值") is True  # 未配置 = 不校验

    cfg.napcat.access_token = "secret"
    assert adapter.verify_token("secret") is True
    assert adapter.verify_token("bad") is False


def test_is_admin(cfg, engine) -> None:
    adapter = NapCatAdapter.build(cfg, engine)
    assert adapter.is_admin("10001") is False  # 未配置管理员时恒为 False

    cfg.napcat.admins = ["10001"]
    assert adapter.is_admin(10001) is True  # 数字也能匹配
    assert adapter.is_admin("10002") is False


def test_bot_names_include_persona_name(cfg, engine) -> None:
    adapter = NapCatAdapter.build(cfg, engine, bot_names=["小忆酱", "小忆"])
    card = engine.persona.get("default")
    names = adapter.bot_names()
    assert card.name in names
    assert "小忆酱" in names
    assert names.count("小忆") == 1  # 去重


def test_describe_reports_connection(adapter) -> None:
    info = adapter.describe()
    assert info["ws_path"] == adapter.ws_path
    assert info["connection"] == {"online": True, "connections": 1, "self_id": "999"}


# ----------------------------------------------------------------------
# 连接生命周期
# ----------------------------------------------------------------------
class FakeWebSocket:
    """够用的假 WebSocket：可灌入报文，并把 API 调用自动回灌成成功响应。"""

    def __init__(self, headers: Optional[dict] = None, query_params: Optional[dict] = None) -> None:
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.accepted = False
        self.closed = False
        self.close_code: Optional[int] = None
        self.sent: List[Dict[str, Any]] = []
        self._incoming: "asyncio.Queue[str]" = asyncio.Queue()
        self._closed = asyncio.Event()

    def push(self, payload: dict) -> None:
        self._incoming.put_nowait(json.dumps(payload, ensure_ascii=False))

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        if self.closed:
            raise WebSocketDisconnect(1000)
        getter = asyncio.ensure_future(self._incoming.get())
        closer = asyncio.ensure_future(self._closed.wait())
        done, pending = await asyncio.wait(
            {getter, closer}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if getter in done:
            return getter.result()
        raise WebSocketDisconnect(1000)

    async def send_text(self, payload: str) -> None:
        data = json.loads(payload)
        self.sent.append(data)
        if "echo" in data:
            self.push(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": len(self.sent)},
                    "echo": data["echo"],
                }
            )

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self.close_code = code
        self._closed.set()


async def test_serve_connection_end_to_end(cfg, engine) -> None:
    adapter = NapCatAdapter.build(cfg, engine)
    ws = FakeWebSocket()
    ws.push(private_payload())

    task = asyncio.create_task(adapter.serve_connection(ws))
    await wait_until(lambda: any(p["action"] == "send_private_msg" for p in ws.sent))
    assert ws.accepted is True
    assert adapter.hub.online is True

    await ws.close()
    await asyncio.wait_for(task, 2)
    assert adapter.hub.online is False  # 断开后注销
    assert engine.memory.count_messages("private:10001") == 2


async def test_serve_connection_handles_meta_event(cfg, engine) -> None:
    adapter = NapCatAdapter.build(cfg, engine)
    ws = FakeWebSocket()
    ws.push(
        {
            "post_type": "meta_event",
            "meta_event_type": "lifecycle",
            "sub_type": "connect",
            "self_id": 4242,
        }
    )
    task = asyncio.create_task(adapter.serve_connection(ws))
    await wait_until(lambda: adapter.hub.primary() is not None and adapter.hub.primary().self_id == "4242")
    await ws.close()
    await asyncio.wait_for(task, 2)


async def test_serve_connection_rejects_bad_token(cfg, engine) -> None:
    cfg.napcat.access_token = "secret"
    adapter = NapCatAdapter.build(cfg, engine)
    ws = FakeWebSocket()
    await adapter.serve_connection(ws)
    assert ws.accepted is False
    assert ws.close_code == 1008


async def test_serve_connection_accepts_bearer_token(cfg, engine) -> None:
    cfg.napcat.access_token = "secret"
    adapter = NapCatAdapter.build(cfg, engine)
    ws = FakeWebSocket(headers={"authorization": "Bearer secret"})
    ws.push(private_payload())

    task = asyncio.create_task(adapter.serve_connection(ws))
    await wait_until(lambda: ws.accepted)
    await ws.close()
    await asyncio.wait_for(task, 2)
    assert ws.accepted is True


def test_extract_token_sources() -> None:
    assert NapCatAdapter._extract_token(FakeWebSocket(headers={"authorization": "Bearer abc"})) == "abc"
    assert NapCatAdapter._extract_token(FakeWebSocket(query_params={"access_token": "xyz"})) == "xyz"
    assert NapCatAdapter._extract_token(FakeWebSocket(headers={"authorization": "Basic abc"})) == ""


async def test_on_message_ignores_unmatched_response(adapter, engine) -> None:
    conn = adapter.hub.primary()
    assert conn is not None
    await adapter.on_message(conn, '{"status":"ok","retcode":0,"echo":"nobody"}')
    assert engine.memory.count_messages() == 0


async def test_on_message_survives_invalid_json(adapter, engine) -> None:
    conn = adapter.hub.primary()
    assert conn is not None
    await adapter.on_message(conn, "{不是 JSON")
    assert engine.memory.count_messages() == 0


def test_mount_registers_websocket_route(cfg, engine) -> None:
    from fastapi import FastAPI

    app = FastAPI()
    adapter = NapCatAdapter.build(cfg, engine)
    adapter.mount(app)
    paths = [getattr(route, "path", "") for route in app.routes]
    assert adapter.ws_path in paths