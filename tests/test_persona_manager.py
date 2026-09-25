"""人设管理器测试：播种、切换、增删改、导入导出、提示词组装。"""

from __future__ import annotations

import pytest

from memo_role.persona.builtin import BUILTIN_PERSONAS
from memo_role.persona.card import PersonaCard
from memo_role.persona.manager import PersonaManager, PersonaNotFoundError
from memo_role.persona.store import PersonaStore


@pytest.fixture
def store(cfg) -> PersonaStore:
    store = PersonaStore(cfg.persona_dir)
    store.ensure_dir()
    return store


@pytest.fixture
def manager(cfg, store: PersonaStore) -> PersonaManager:
    """不自动播种，便于精确控制测试内容。"""
    return PersonaManager.build(cfg, store=store, seed=False)


# ----------------------------------------------------------------------
# 播种
# ----------------------------------------------------------------------
def test_seed_creates_builtin_cards(cfg, store: PersonaStore) -> None:
    manager = PersonaManager.build(cfg, store=store)
    ids = [c.id for c in manager.list_cards()]
    assert len(ids) == len(BUILTIN_PERSONAS)
    assert "default" in ids
    assert manager.current_default() == "default"


def test_seed_runs_only_once(cfg, store: PersonaStore) -> None:
    manager = PersonaManager.build(cfg, store=store)
    # 用户删光所有角色
    for persona_id in list(store.list_ids()):
        store.delete(persona_id)
    # 再次构建不应重新生成（尊重用户选择）
    PersonaManager.build(cfg, store=store)
    assert store.list_ids() == []


def test_seed_respects_existing_user_cards(cfg, store: PersonaStore) -> None:
    store.save(PersonaCard.from_dict({"id": "mine", "name": "我的角色"}), new=True)
    manager = PersonaManager.build(cfg, store=store)
    assert [c.id for c in manager.list_cards()] == ["mine"]


# ----------------------------------------------------------------------
# 取卡与默认切换
# ----------------------------------------------------------------------
def test_get_by_id(manager: PersonaManager, store: PersonaStore) -> None:
    store.save(PersonaCard.from_dict({"id": "a", "name": "角色A"}), new=True)
    assert manager.get("a").name == "角色A"


def test_get_missing_raises(manager: PersonaManager) -> None:
    with pytest.raises(PersonaNotFoundError):
        manager.get("nope")


def test_get_without_id_uses_default(manager: PersonaManager, store: PersonaStore) -> None:
    store.save(PersonaCard.from_dict({"id": "a", "name": "角色A"}), new=True)
    store.set_default("a")
    assert manager.get().id == "a"


def test_get_or_none(manager: PersonaManager, store: PersonaStore) -> None:
    store.save(PersonaCard.from_dict({"id": "a", "name": "A"}), new=True)
    assert manager.get_or_none("a") is not None
    assert manager.get_or_none("nope") is None
    assert manager.get_or_none(None) is None


def test_current_default_falls_back_to_config(manager: PersonaManager, store: PersonaStore) -> None:
    store.save(PersonaCard.from_dict({"id": "cfg_role", "name": "配置默认"}), new=True)
    manager.cfg.persona.default_persona = "cfg_role"
    assert manager.current_default() == "cfg_role"


def test_current_default_falls_back_to_first_card(
    cfg, store: PersonaStore
) -> None:
    cfg.persona.default_persona = "不存在"
    store.save(PersonaCard.from_dict({"id": "only", "name": "唯一"}), new=True)
    manager = PersonaManager.build(cfg, store=store, seed=False)
    assert manager.current_default() == "only"


def test_set_default_switches(manager: PersonaManager, store: PersonaStore) -> None:
    store.save(PersonaCard.from_dict({"id": "a", "name": "A"}), new=True)
    store.save(PersonaCard.from_dict({"id": "b", "name": "B"}), new=True)
    manager.set_default("b")
    assert manager.current_default() == "b"
    assert manager.get().id == "b"


def test_set_default_missing_raises(manager: PersonaManager) -> None:
    with pytest.raises(PersonaNotFoundError):
        manager.set_default("nope")


def test_list_summaries_marks_default(manager: PersonaManager, store: PersonaStore) -> None:
    store.save(PersonaCard.from_dict({"id": "a", "name": "A"}), new=True)
    manager.set_default("a")
    summaries = manager.list_summaries()
    assert summaries[0]["is_default"] is True


# ----------------------------------------------------------------------
# 增删改
# ----------------------------------------------------------------------
def test_create_with_explicit_id(manager: PersonaManager) -> None:
    card = manager.create(name="新角色", persona_id="fresh", personality="活泼")
    assert card.id == "fresh"
    assert manager.get("fresh").personality == "活泼"


def test_create_autogenerates_id(manager: PersonaManager) -> None:
    card = manager.create(name="自动 id")
    assert card.id.startswith("persona_")


def test_create_duplicate_raises(manager: PersonaManager) -> None:
    manager.create(name="A", persona_id="dup")
    with pytest.raises(FileExistsError):
        manager.create(name="B", persona_id="dup")


def test_update_partial_fields(manager: PersonaManager) -> None:
    manager.create(name="原名", persona_id="u", personality="旧")
    updated = manager.update("u", personality="新", speaking_style="简短")
    assert updated.personality == "新"
    assert updated.speaking_style == "简短"
    assert updated.name == "原名"  # 未传字段不变
    assert manager.get("u").personality == "新"


def test_update_cannot_change_id(manager: PersonaManager) -> None:
    manager.create(name="A", persona_id="keep")
    updated = manager.update("keep", id="hacked", name="新名")
    assert updated.id == "keep"


def test_update_preserves_created_at(manager: PersonaManager) -> None:
    card = manager.create(name="A", persona_id="u")
    updated = manager.update("u", description="改了")
    assert updated.created_at == pytest.approx(card.created_at)


def test_delete_switches_default(manager: PersonaManager, store: PersonaStore) -> None:
    manager.create(name="A", persona_id="a")
    manager.create(name="B", persona_id="b")
    manager.set_default("a")
    assert manager.delete("a") is True
    assert manager.current_default() == "b"


def test_delete_missing_returns_false(manager: PersonaManager) -> None:
    assert manager.delete("nope") is False


def test_duplicate_card(manager: PersonaManager) -> None:
    manager.create(name="原角色", persona_id="src")
    clone = manager.duplicate("src")
    assert clone.id == "src_copy"
    assert clone.name == "原角色（副本）"


def test_duplicate_avoids_name_collision(manager: PersonaManager) -> None:
    manager.create(name="A", persona_id="src")
    manager.duplicate("src")  # 生成 src_copy
    second = manager.duplicate("src")  # 应自动变成 src_copy2
    assert second.id == "src_copy2"


# ----------------------------------------------------------------------
# 导入 / 导出
# ----------------------------------------------------------------------
def test_export_dict_and_json(manager: PersonaManager) -> None:
    manager.create(name="导出", persona_id="exp")
    data = manager.export("exp", fmt="dict")
    assert data["id"] == "exp"
    text = manager.export("exp", fmt="json")
    assert '"exp"' in text


def test_export_all_backup_and_reimport(manager: PersonaManager, cfg, store: PersonaStore) -> None:
    manager.create(name="A", persona_id="a")
    manager.create(name="B", persona_id="b")
    backup = manager.export_all(fmt="json")
    assert '"personas"' in backup

    # 导入到全新目录
    other = PersonaManager.build(cfg, store=PersonaStore(cfg.root / "other"), seed=False)
    imported = other.import_data(backup)
    assert {c.id for c in imported} == {"a", "b"}


def test_import_single_card(manager: PersonaManager) -> None:
    cards = manager.import_data({"id": "single", "name": "单卡"})
    assert len(cards) == 1
    assert cards[0].id == "single"


def test_import_list(manager: PersonaManager) -> None:
    cards = manager.import_data([{"id": "x", "name": "X"}, {"id": "y", "name": "Y"}])
    assert {c.id for c in cards} == {"x", "y"}


def test_import_with_new_id(manager: PersonaManager) -> None:
    cards = manager.import_data({"id": "orig", "name": "O"}, new_id="renamed")
    assert cards[0].id == "renamed"


def test_import_overwrite(manager: PersonaManager) -> None:
    manager.create(name="原", persona_id="dup")
    with pytest.raises(FileExistsError):
        manager.import_data({"id": "dup", "name": "新"})
    cards = manager.import_data({"id": "dup", "name": "新"}, overwrite=True)
    assert cards[0].name == "新"


def test_import_invalid_payload_raises(manager: PersonaManager) -> None:
    with pytest.raises(ValueError):
        manager.import_data("不是 json")
    with pytest.raises(ValueError):
        manager.import_data(123)


# ----------------------------------------------------------------------
# 提示词组装
# ----------------------------------------------------------------------
def test_render_system_prompt_includes_persona(manager: PersonaManager) -> None:
    manager.create(name="角色", persona_id="p", description="简介")
    prompt = manager.render_system_prompt("p")
    assert "角色" in prompt
    assert "简介" in prompt


def test_render_system_prompt_appends_memory(manager: PersonaManager) -> None:
    manager.create(name="角色", persona_id="p")
    prompt = manager.render_system_prompt("p", memory_context="【长期设定】\n- 用户叫小明")
    assert "【关于对方的记忆】" in prompt
    assert "用户叫小明" in prompt


def test_render_system_prompt_appends_extra_rules(manager: PersonaManager) -> None:
    manager.create(name="角色", persona_id="p")
    prompt = manager.render_system_prompt("p", extra_rules="这是群聊，注意区分发言人")
    assert "这是群聊" in prompt


def test_render_system_prompt_skips_empty_blocks(manager: PersonaManager) -> None:
    manager.create(name="角色", persona_id="p")
    prompt = manager.render_system_prompt("p")
    assert "【关于对方的记忆】" not in prompt


def test_describe(manager: PersonaManager) -> None:
    manager.create(name="A", persona_id="a")
    info = manager.describe()
    assert info["count"] == 1
    assert info["current_default"] == "a"