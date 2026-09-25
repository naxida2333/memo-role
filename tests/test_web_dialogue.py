"""对话 API 测试：会话生命周期、消息收发、指令拦截、SSE 流式。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple


def _parse_sse(body: str) -> List[Tuple[str, Dict[str, Any]]]:
    """把 SSE 文本解析成 ``[(event, data), ...]``（与前端解析逻辑一致）。"""
    events: List[Tuple[str, Dict[str, Any]]] = []
    for block in body.split("\n\n"):
        name = ""
        data_lines: List[str] = []
        for line in block.splitlines():
            if line.startswith(":"):  # 注释帧（用于尽早建立连接）
                continue
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if name and data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events


def _new_session(client, **fields) -> Dict[str, Any]:
    resp = client.post("/api/dialogue/sessions", json=fields)
    assert resp.status_code == 201
    return resp.json()["session"]


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------
def test_create_session_generates_id(web_client) -> None:
    session = _new_session(web_client, title="打招呼")
    assert session["id"].startswith("web:")
    assert session["kind"] == "web"
    assert session["title"] == "打招呼"

    listed = web_client.get("/api/dialogue/sessions").json()["sessions"]
    assert [s["id"] for s in listed] == [session["id"]]
    assert listed[0]["message_count"] == 0


def test_create_session_with_explicit_id(web_client) -> None:
    session = _new_session(web_client, session_id="web:fixed")
    assert session["id"] == "web:fixed"


def test_create_session_rejects_bad_id(web_client) -> None:
    resp = web_client.post("/api/dialogue/sessions", json={"session_id": "bad id/x"})
    assert resp.status_code == 400


def test_missing_session_returns_404(web_client) -> None:
    assert web_client.get("/api/dialogue/sessions/nope").status_code == 404
    assert web_client.delete("/api/dialogue/sessions/nope").status_code == 404
    assert web_client.post("/api/dialogue/sessions/nope/reset").status_code == 404
    assert (
        web_client.post(
            "/api/dialogue/sessions/nope/messages", json={"text": "hi"}
        ).status_code
        == 404
    )


def test_delete_session_removes_it(web_client) -> None:
    session = _new_session(web_client)
    sid = session["id"]
    web_client.post(f"/api/dialogue/sessions/{sid}/messages", json={"text": "你好"})

    assert web_client.delete(f"/api/dialogue/sessions/{sid}").json()["deleted"] is True
    assert web_client.get(f"/api/dialogue/sessions/{sid}").status_code == 404


def test_rename_session_updates_title(web_client) -> None:
    session = _new_session(web_client, title="旧名字")
    sid = session["id"]

    resp = web_client.patch(f"/api/dialogue/sessions/{sid}", json={"title": "  新名字\n"})
    assert resp.status_code == 200
    # 前后空白与换行会被压成单行，避免会话列表被撑成两行
    assert resp.json()["session"]["title"] == "新名字"
    assert web_client.get(f"/api/dialogue/sessions/{sid}").json()["session"]["title"] == "新名字"
    # 重命名不应动到别的东西
    assert web_client.get(f"/api/dialogue/sessions/{sid}").json()["session"]["persona_id"] == (
        session["persona_id"]
    )


def test_rename_session_rejects_empty_title(web_client) -> None:
    sid = _new_session(web_client)["id"]
    assert web_client.patch(f"/api/dialogue/sessions/{sid}", json={"title": "   "}).status_code == 400
    assert web_client.patch("/api/dialogue/sessions/nope", json={"title": "x"}).status_code == 404


def test_first_message_names_the_session(web_client) -> None:
    """首条用户消息自动成为标题，避免会话列表里全是 web:xxxx。"""
    sid = _new_session(web_client)["id"]
    assert web_client.get(f"/api/dialogue/sessions/{sid}").json()["session"]["title"] == ""

    web_client.post(f"/api/dialogue/sessions/{sid}/messages", json={"text": "帮我记一下下周要交报告"})
    assert (
        web_client.get(f"/api/dialogue/sessions/{sid}").json()["session"]["title"]
        == "帮我记一下下周要交报告"
    )

    # 后续消息不再改标题（否则标题会跟着最后一句乱跳）
    web_client.post(f"/api/dialogue/sessions/{sid}/messages", json={"text": "另外买点牛奶"})
    assert (
        web_client.get(f"/api/dialogue/sessions/{sid}").json()["session"]["title"]
        == "帮我记一下下周要交报告"
    )


def test_first_message_title_is_truncated(web_client) -> None:
    from memo_role.dialogue.engine import AUTO_TITLE_MAX

    sid = _new_session(web_client)["id"]
    web_client.post(f"/api/dialogue/sessions/{sid}/messages", json={"text": "长" * 50})
    title = web_client.get(f"/api/dialogue/sessions/{sid}").json()["session"]["title"]
    assert title == "长" * AUTO_TITLE_MAX + "…"


def test_command_does_not_name_the_session(web_client) -> None:
    """指令在进推理前就被拦截，也不该变成会话标题。"""
    sid = _new_session(web_client)["id"]
    web_client.post(f"/api/dialogue/sessions/{sid}/messages", json={"text": "/help"})
    assert web_client.get(f"/api/dialogue/sessions/{sid}").json()["session"]["title"] == ""


def test_manual_title_survives_new_messages(web_client) -> None:
    """用户手动命名的会话，不会被后续首条（此处为第一条）消息覆盖。"""
    sid = _new_session(web_client, title="我自己起的")["id"]
    web_client.post(f"/api/dialogue/sessions/{sid}/messages", json={"text": "你好"})
    assert (
        web_client.get(f"/api/dialogue/sessions/{sid}").json()["session"]["title"] == "我自己起的"
    )


# ----------------------------------------------------------------------
# 消息
# ----------------------------------------------------------------------
def test_send_message_roundtrip(web_client, fake_backend) -> None:
    session = _new_session(web_client)
    sid = session["id"]

    data = web_client.post(
        f"/api/dialogue/sessions/{sid}/messages", json={"text": "你好"}
    ).json()
    assert data["reply"] == fake_backend.reply
    assert data["is_command"] is False
    assert data["skipped"] is False

    detail = web_client.get(f"/api/dialogue/sessions/{sid}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "你好"
    # 首条消息会把会话绑定到解析出的人设，后续消息才有稳定角色
    assert detail["session"]["persona_id"] == data["persona_id"]


def test_send_message_rejects_empty_text(web_client) -> None:
    session = _new_session(web_client)
    resp = web_client.post(
        f"/api/dialogue/sessions/{session['id']}/messages", json={"text": ""}
    )
    assert resp.status_code == 422


def test_command_is_intercepted_without_calling_model(web_client, fake_backend) -> None:
    session = _new_session(web_client)
    sid = session["id"]
    fake_backend.calls.clear()

    data = web_client.post(
        f"/api/dialogue/sessions/{sid}/messages", json={"text": "/help"}
    ).json()
    assert data["is_command"] is True
    assert data["action"] == "help"
    assert "指令" in data["reply"]

    assert fake_backend.calls == []  # 指令不进推理，省算力
    assert web_client.get(f"/api/dialogue/sessions/{sid}").json()["messages"] == []


def test_persona_command_binds_session(web_client) -> None:
    session = _new_session(web_client)
    sid = session["id"]

    data = web_client.post(
        f"/api/dialogue/sessions/{sid}/messages", json={"text": "/persona catgirl"}
    ).json()
    assert data["is_command"] is True
    assert data["action"] == "persona_switch"

    detail = web_client.get(f"/api/dialogue/sessions/{sid}").json()
    assert detail["session"]["persona_id"] == "catgirl"


def test_reset_clears_working_memory_only(web_client, web_state) -> None:
    session = _new_session(web_client)
    sid = session["id"]
    web_client.post(f"/api/dialogue/sessions/{sid}/messages", json={"text": "你好"})
    web_state.memory.store.add_memory("用户叫小明", kind="core", importance=0.9)

    assert web_client.post(f"/api/dialogue/sessions/{sid}/reset").json()["removed"] == 2
    assert web_client.get(f"/api/dialogue/sessions/{sid}").json()["messages"] == []
    # 长期记忆是全局共享的，清空上下文不应把它一起删掉
    contents = [m["content"] for m in web_client.get("/api/memories").json()["memories"]]
    assert "用户叫小明" in contents


# ----------------------------------------------------------------------
# 流式
# ----------------------------------------------------------------------
def test_stream_sse_emits_deltas_then_done(web_client, fake_backend) -> None:
    session = _new_session(web_client)
    sid = session["id"]

    with web_client.stream(
        "POST", f"/api/dialogue/sessions/{sid}/stream", json={"text": "你好"}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())

    events = _parse_sse(body)
    deltas = [d for name, d in events if name == "delta"]
    assert "".join(d["text"] for d in deltas) == fake_backend.reply

    done = [d for name, d in events if name == "done"]
    assert done and done[0]["reply"] == fake_backend.reply
    assert events[-1][0] == "end"

    # 流式同样要落库（否则刷新页面后这一轮对话就丢了）
    detail = web_client.get(f"/api/dialogue/sessions/{sid}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_stream_command_returns_single_delta(web_client) -> None:
    session = _new_session(web_client)
    sid = session["id"]

    with web_client.stream(
        "POST", f"/api/dialogue/sessions/{sid}/stream", json={"text": "/status"}
    ) as resp:
        body = "".join(resp.iter_text())

    events = _parse_sse(body)
    done = [d for name, d in events if name == "done"]
    assert done and done[0]["is_command"] is True
    assert events[-1][0] == "end"


# ----------------------------------------------------------------------
# 指令清单
# ----------------------------------------------------------------------
def test_commands_endpoint_lists_builtin_commands(web_client) -> None:
    data = web_client.get("/api/dialogue/commands").json()
    assert data["prefix"] == "/"
    assert "可用指令" in data["help"]
    assert {"help", "persona", "model", "reset", "memory", "status"} <= {
        c["name"] for c in data["commands"]
    }