"""关键信息提取测试。"""

from __future__ import annotations

from typing import List, Sequence

from memo_role.inference.base import ChatMessage, ChatBackend, GenParams
from memo_role.memory.extractor import (
    MAX_CONTENT_LENGTH,
    LlmExtractor,
    RuleBasedExtractor,
    _parse_llm_items,
    build_extractor,
)
from memo_role.memory.store import KIND_CORE, KIND_EPISODIC


def contents(items) -> List[str]:
    return [i.content for i in items]


# ----------------------------------------------------------------------
# 规则提取
# ----------------------------------------------------------------------
def test_empty_text_returns_nothing() -> None:
    extractor = RuleBasedExtractor()
    assert extractor.extract("") == []
    assert extractor.extract("   ") == []


def test_extract_nickname_as_core() -> None:
    items = RuleBasedExtractor().extract("我叫小明")
    assert len(items) == 1
    assert items[0].content == "用户的称呼是「小明」"
    assert items[0].kind == KIND_CORE
    assert items[0].importance >= 0.8


def test_extract_nickname_variants() -> None:
    extractor = RuleBasedExtractor()
    assert "小美" in extractor.extract("我的名字是小美")[0].content
    assert "阿强" in extractor.extract("叫我阿强吧")[0].content


def test_extract_birthday() -> None:
    items = RuleBasedExtractor().extract("我的生日是 2001-03-15")
    assert any("生日" in c for c in contents(items))
    assert items[0].kind == KIND_CORE


def test_extract_age() -> None:
    items = RuleBasedExtractor().extract("我今年 24 岁")
    assert contents(items) == ["用户今年 24 岁"]


def test_extract_contact() -> None:
    items = RuleBasedExtractor().extract("我的邮箱是 test@example.com")
    assert any("test@example.com" in c for c in contents(items))
    items2 = RuleBasedExtractor().extract("电话 13800138000")
    assert any("13800138000" in c for c in contents(items2))


def test_extract_like_and_dislike() -> None:
    extractor = RuleBasedExtractor()
    liked = extractor.extract("我很喜欢猫")
    assert any("喜欢" in c and "猫" in c for c in contents(liked))
    disliked = extractor.extract("我讨厌拥挤的地铁")
    assert any("不喜欢" in c for c in contents(disliked))


def test_dislike_not_shadowed_by_like() -> None:
    """「我不喜欢」不应被「喜欢」规则误判为喜欢。"""
    items = RuleBasedExtractor().extract("我不喜欢香菜")
    assert all("用户喜欢" not in c for c in contents(items))
    assert any("不喜欢" in c for c in contents(items))


def test_explicit_remember_has_highest_importance() -> None:
    items = RuleBasedExtractor().extract("请记住我最怕打雷")
    assert items[0].importance == 0.9
    assert "打雷" in items[0].content


def test_extract_possession() -> None:
    items = RuleBasedExtractor().extract("我的猫叫布丁")
    assert any("猫" in c and "布丁" in c for c in contents(items))


def test_no_match_returns_empty() -> None:
    assert RuleBasedExtractor().extract("今天天气不错，出去走走吧。") == []


def test_deduplicates_within_one_text() -> None:
    """同一句话里重复出现的信息只保留一条。"""
    items = RuleBasedExtractor().extract("我叫小明，我叫小明")
    assert contents(items).count("用户的称呼是「小明」") == 1


def test_content_is_truncated() -> None:
    long_value = "啊" * 200
    items = RuleBasedExtractor().extract(f"记住{long_value}")
    assert items[0].content
    assert len(items[0].content) <= MAX_CONTENT_LENGTH + len("用户的称呼是「」")


def test_meta_records_rule_name() -> None:
    items = RuleBasedExtractor().extract("我叫小明")
    assert items[0].meta["rule"] == "nickname"


def test_describe() -> None:
    assert RuleBasedExtractor().describe() == {"extractor": "rule"}


# ----------------------------------------------------------------------
# LLM 提取
# ----------------------------------------------------------------------
class StubBackend(ChatBackend):
    """返回预设文本的假后端。"""

    name = "stub"

    def __init__(self, reply: str = "[]", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: List[Sequence[ChatMessage]] = []

    def chat(self, messages, params: GenParams | None = None) -> str:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.reply

    def is_available(self) -> bool:
        return True


def test_llm_extractor_parses_json() -> None:
    backend = StubBackend(
        '[{"content": "用户养了一只叫布丁的猫", "kind": "episodic", "importance": 0.7}]'
    )
    items = LlmExtractor(backend).extract("我的猫叫布丁")
    assert contents(items) == ["用户养了一只叫布丁的猫"]
    assert items[0].kind == KIND_EPISODIC
    assert items[0].importance == 0.7


def test_llm_extractor_handles_code_fence() -> None:
    backend = StubBackend(
        "```json\n[{\"content\": \"用户喜欢猫\", \"kind\": \"episodic\", \"importance\": 0.6}]\n```"
    )
    items = LlmExtractor(backend).extract("我喜欢猫")
    assert contents(items) == ["用户喜欢猫"]


def test_llm_extractor_clamps_importance_and_kind() -> None:
    backend = StubBackend('[{"content": "某事", "kind": "weird", "importance": 9.9}]')
    items = LlmExtractor(backend).extract("随便")
    assert items[0].kind == KIND_EPISODIC
    assert items[0].importance == 1.0


def test_llm_extractor_falls_back_on_bad_json() -> None:
    backend = StubBackend("这不是 JSON")
    items = LlmExtractor(backend).extract("我叫小明")
    # 回退到规则提取，仍能拿到昵称
    assert "小明" in items[0].content


def test_llm_extractor_falls_back_on_backend_error() -> None:
    backend = StubBackend(error=RuntimeError("模型炸了"))
    items = LlmExtractor(backend).extract("我叫小明")
    assert "小明" in items[0].content


def test_llm_extractor_empty_text_skips_backend() -> None:
    backend = StubBackend("[]")
    assert LlmExtractor(backend).extract("") == []
    assert backend.calls == []


def test_parse_llm_items_rejects_non_list() -> None:
    assert _parse_llm_items('{"content": "x"}') is None
    assert _parse_llm_items("") == []
    assert _parse_llm_items("没有方括号") is None


def test_parse_llm_items_skips_invalid_entries() -> None:
    items = _parse_llm_items('[{"no_content": 1}, {"content": "ok", "kind": "core"}]')
    assert items is not None
    assert contents(items) == ["ok"]


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------
def test_build_extractor_rule(tmp_root) -> None:
    from memo_role.config import load_config

    cfg = load_config(root=tmp_root, environ={})
    assert isinstance(build_extractor(cfg), RuleBasedExtractor)


def test_build_extractor_llm_without_backend_falls_back(tmp_root) -> None:
    from memo_role.config import load_config

    cfg = load_config(root=tmp_root, environ={}, overrides={"memory": {"extractor": "llm"}})
    assert isinstance(build_extractor(cfg, backend=None), RuleBasedExtractor)


def test_build_extractor_llm_with_backend(tmp_root) -> None:
    from memo_role.config import load_config

    cfg = load_config(root=tmp_root, environ={}, overrides={"memory": {"extractor": "llm"}})
    extractor = build_extractor(cfg, backend=StubBackend("[]"))
    assert isinstance(extractor, LlmExtractor)