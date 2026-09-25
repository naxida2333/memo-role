"""人设卡片数据结构测试。"""

from __future__ import annotations

import pytest

from memo_role.persona.card import (
    MAX_EXAMPLES,
    PersonaCard,
    normalize_persona_id,
)


# ----------------------------------------------------------------------
# id 校验
# ----------------------------------------------------------------------
def test_normalize_id_accepts_safe_values() -> None:
    assert normalize_persona_id("default") == "default"
    assert normalize_persona_id("my-role_2.0") == "my-role_2.0"


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "_leading", "-dash", "a/b", "../etc/passwd", "有中文", "a" * 65],
)
def test_normalize_id_rejects_unsafe_values(bad: str) -> None:
    """id 会作为文件名，必须挡掉路径穿越等危险输入。"""
    with pytest.raises(ValueError):
        normalize_persona_id(bad)


# ----------------------------------------------------------------------
# 构造与序列化
# ----------------------------------------------------------------------
def test_from_dict_fills_defaults() -> None:
    card = PersonaCard.from_dict({"name": "小明"}, default_id="xiaoming")
    assert card.id == "xiaoming"
    assert card.name == "小明"
    assert card.avatar  # 有默认头像
    assert card.created_at > 0


def test_from_dict_ignores_unknown_fields() -> None:
    card = PersonaCard.from_dict({"id": "a", "name": "A", "unknown": 123})
    assert not hasattr(card, "unknown")


def test_from_dict_requires_id_or_default() -> None:
    with pytest.raises(ValueError):
        PersonaCard.from_dict({"name": "无 id"})


def test_to_dict_roundtrip() -> None:
    card = PersonaCard.from_dict(
        {
            "id": "role",
            "name": "角色",
            "tags": ["a", "b", "a"],
            "extra": {"theme": "#fff"},
        }
    )
    again = PersonaCard.from_dict(card.to_dict())
    assert again.id == card.id
    assert again.tags == ["a", "b"]  # 去重
    assert again.extra == {"theme": "#fff"}


# ----------------------------------------------------------------------
# 对话示例规范化
# ----------------------------------------------------------------------
def test_examples_normalized_from_various_shapes() -> None:
    card = PersonaCard.from_dict(
        {
            "id": "a",
            "name": "A",
            "example_dialogue": [
                {"user": "你好", "assistant": "你好呀"},
                ["在吗", "在的"],
                {"user": "只有用户"},
                "无效",
            ],
        }
    )
    assert card.example_dialogue[0] == {"user": "你好", "assistant": "你好呀"}
    assert card.example_dialogue[1] == {"user": "在吗", "assistant": "在的"}
    assert card.example_dialogue[2] == {"user": "只有用户", "assistant": ""}
    assert len(card.example_dialogue) == 3  # "无效" 被丢弃


def test_examples_truncated_at_limit() -> None:
    card = PersonaCard.from_dict(
        {
            "id": "a",
            "name": "A",
            "example_dialogue": [{"user": f"u{i}", "assistant": "a"} for i in range(50)],
        }
    )
    assert len(card.example_dialogue) == MAX_EXAMPLES


# ----------------------------------------------------------------------
# 复制与派生
# ----------------------------------------------------------------------
def test_copy_isolates_mutable_fields() -> None:
    card = PersonaCard.from_dict(
        {"id": "a", "name": "A", "tags": ["x"], "extra": {"k": 1}}
    )
    clone = card.copy()
    clone.tags.append("y")
    clone.extra["k"] = 2
    assert card.tags == ["x"]
    assert card.extra == {"k": 1}


def test_with_id_resets_timestamps() -> None:
    card = PersonaCard.from_dict({"id": "a", "name": "A", "created_at": 1.0})
    clone = card.with_id("b", new_name="B")
    assert clone.id == "b"
    assert clone.name == "B"
    assert clone.created_at > 1.0


# ----------------------------------------------------------------------
# 提示词渲染
# ----------------------------------------------------------------------
def test_render_includes_all_sections() -> None:
    card = PersonaCard.from_dict(
        {
            "id": "cat",
            "name": "喵酱",
            "description": "一只猫娘",
            "personality": "活泼",
            "speaking_style": "句尾带喵",
            "scenario": "住在手机里",
            "example_dialogue": [{"user": "在吗", "assistant": "在喵"}],
        }
    )
    prompt = card.render_system_prompt()
    assert "喵酱" in prompt
    assert "一只猫娘" in prompt
    assert "【性格】" in prompt
    assert "【说话风格】" in prompt
    assert "【背景设定】" in prompt
    assert "用户：在吗" in prompt
    assert "喵酱：在喵" in prompt
    assert "【行为准则】" in prompt


def test_render_skips_empty_sections() -> None:
    card = PersonaCard.from_dict({"id": "a", "name": "A"})
    prompt = card.render_system_prompt()
    assert "【性格】" not in prompt
    assert "【对话示例】" not in prompt
    assert "A" in prompt


def test_custom_system_prompt_overrides_template() -> None:
    card = PersonaCard.from_dict(
        {"id": "a", "name": "A", "personality": "不该出现", "system_prompt": "只要这句话"}
    )
    assert card.render_system_prompt() == "只要这句话"


def test_describe_excludes_full_prompt() -> None:
    card = PersonaCard.from_dict(
        {"id": "a", "name": "A", "system_prompt": "很长的提示词", "tags": ["t"]}
    )
    summary = card.describe()
    assert summary["id"] == "a"
    assert summary["tags"] == ["t"]
    assert "system_prompt" not in summary