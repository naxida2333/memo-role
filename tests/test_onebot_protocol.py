"""OneBot v11 协议原语测试（纯函数，无网络无模型）。"""

from __future__ import annotations

from memo_role.adapters.onebot import (
    ACTION_SEND_PRIVATE,
    AT_ALL_QQ,
    SEG_AT,
    SEG_TEXT,
    ApiResponse,
    MessageSegment,
    OneBotEvent,
    build_api_request,
    parse_segments,
    render_text_message,
    text_segment,
)


def private_payload(**kwargs) -> dict:
    payload = {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 11,
        "user_id": 10001,
        "self_id": 999,
        "time": 1700000000,
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


# ----------------------------------------------------------------------
# 消息段解析
# ----------------------------------------------------------------------
def test_parse_segments_array() -> None:
    segments = parse_segments([{"type": "text", "data": {"text": "hi"}}])
    assert segments == [MessageSegment(SEG_TEXT, {"text": "hi"})]


def test_parse_segments_cq_string() -> None:
    segments = parse_segments("[CQ:at,qq=999] 你好[CQ:image,file=a.jpg]")
    assert segments[0] == MessageSegment(SEG_AT, {"qq": "999"})
    assert segments[1].type == SEG_TEXT
    assert segments[1].text == " 你好"
    assert segments[2].type == "image"
    assert segments[2].data["file"] == "a.jpg"


def test_parse_segments_plain_string() -> None:
    assert parse_segments("就一句话") == [text_segment("就一句话")]


def test_parse_segments_none_and_unknown() -> None:
    assert parse_segments(None) == []
    assert parse_segments(123) == [text_segment("123")]


def test_render_text_message_is_segment_array() -> None:
    assert render_text_message("hi") == [{"type": "text", "data": {"text": "hi"}}]


# ----------------------------------------------------------------------
# 事件解析
# ----------------------------------------------------------------------
def test_private_event_fields() -> None:
    event = OneBotEvent.from_dict(private_payload())
    assert event.is_message and event.is_private
    assert event.session_kind == "private"
    assert event.session_id == "private:10001"
    assert event.peer_id == "10001"
    assert event.nickname == "小明"
    assert event.text == "你好"


def test_group_event_fields_prefer_card() -> None:
    event = OneBotEvent.from_dict(group_payload())
    assert event.is_group
    assert event.session_id == "group:555"
    assert event.peer_id == "555"
    assert event.nickname == "小明"  # 群名片优先于 QQ 昵称
    assert event.mentioned_self is True


def test_group_without_card_falls_back_to_nickname() -> None:
    event = OneBotEvent.from_dict(
        group_payload(sender={"nickname": "nick", "card": ""})
    )
    assert event.nickname == "nick"


def test_mention_self_is_excluded_from_text() -> None:
    event = OneBotEvent.from_dict(group_payload())
    assert "@999" not in event.text
    assert event.text == "早上好"


def test_mention_others_is_preserved() -> None:
    payload = group_payload(
        message=[
            {"type": "text", "data": {"text": "看看"}},
            {"type": "at", "data": {"qq": "888"}},
        ]
    )
    event = OneBotEvent.from_dict(payload)
    assert event.text == "看看@888"
    assert event.mentioned_self is False


def test_at_all_counts_as_mentioned() -> None:
    payload = group_payload(message=[{"type": "at", "data": {"qq": AT_ALL_QQ}}])
    event = OneBotEvent.from_dict(payload)
    assert event.at_all is True
    assert event.text == "@全体成员"


def test_image_and_face_become_placeholders() -> None:
    payload = private_payload(
        message=[
            {"type": "image", "data": {"file": "a.jpg"}},
            {"type": "face", "data": {"id": "1"}},
        ]
    )
    assert OneBotEvent.from_dict(payload).text == "[图片][表情]"


def test_text_falls_back_to_raw_message() -> None:
    payload = private_payload(message=[], raw_message="只给了 raw")
    assert OneBotEvent.from_dict(payload).text == "只给了 raw"


def test_message_sent_is_not_a_message_event() -> None:
    """机器人自己发的消息会以 ``message_sent`` 回灌，不能再当成用户消息处理。"""
    event = OneBotEvent.from_dict(private_payload(post_type="message_sent"))
    assert event.is_message is False


def test_from_dict_tolerates_garbage() -> None:
    event = OneBotEvent.from_dict("not a dict")
    assert event.is_message is False
    assert event.text == ""


def test_event_without_self_id_never_mentioned() -> None:
    event = OneBotEvent.from_dict(group_payload(self_id=0))
    assert event.mentioned_self is False


# ----------------------------------------------------------------------
# API 报文
# ----------------------------------------------------------------------
def test_build_api_request_includes_echo() -> None:
    payload = build_api_request(ACTION_SEND_PRIVATE, {"user_id": 1}, echo="memo-1")
    assert payload == {"action": ACTION_SEND_PRIVATE, "params": {"user_id": 1}, "echo": "memo-1"}


def test_build_api_request_omits_empty_echo() -> None:
    assert "echo" not in build_api_request("get_login_info")


def test_api_response_ok_for_zero_and_one() -> None:
    assert ApiResponse.from_dict({"status": "ok", "retcode": 0, "echo": "e"}).ok is True
    assert ApiResponse.from_dict({"status": "ok", "retcode": 1, "echo": "e"}).ok is True
    assert ApiResponse.from_dict({"status": "failed", "retcode": 100}).ok is False


def test_api_response_reads_wording_as_message() -> None:
    response = ApiResponse.from_dict({"status": "failed", "retcode": 100, "wording": "群不存在"})
    assert response.message == "群不存在"