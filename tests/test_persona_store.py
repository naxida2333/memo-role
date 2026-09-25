"""人设文件存储层测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memo_role.persona.card import PersonaCard
from memo_role.persona.store import META_FILENAME, PersonaStore


@pytest.fixture
def store(tmp_path: Path) -> PersonaStore:
    s = PersonaStore(tmp_path / "personas")
    s.ensure_dir()
    return s


def make_card(persona_id: str = "role", name: str = "角色") -> PersonaCard:
    return PersonaCard.from_dict({"id": persona_id, "name": name})


# ----------------------------------------------------------------------
# 读写
# ----------------------------------------------------------------------
def test_save_creates_json_file(store: PersonaStore) -> None:
    store.save(make_card(), new=True)
    path = store.directory / "role.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["id"] == "role"
    assert data["name"] == "角色"
    # 必须是非转义的 UTF-8，方便文件管理页直接阅读
    assert "角色" in path.read_text(encoding="utf-8")


def test_save_then_load(store: PersonaStore) -> None:
    store.save(make_card(), new=True)
    card = store.load("role")
    assert card is not None
    assert card.name == "角色"


def test_load_missing_returns_none(store: PersonaStore) -> None:
    assert store.load("nope") is None


def test_load_invalid_id_returns_none(store: PersonaStore) -> None:
    assert store.load("../evil") is None


def test_load_corrupted_file_is_skipped(store: PersonaStore) -> None:
    (store.directory / "broken.json").write_text("{ 不是 json", encoding="utf-8")
    store.save(make_card("good"), new=True)
    cards = store.list_cards()
    assert [c.id for c in cards] == ["good"]  # 坏文件被跳过，不影响好文件


def test_list_ids_excludes_meta(store: PersonaStore) -> None:
    store.save(make_card("a"), new=True)
    store.set_default("a")  # 会写 _meta.json
    assert (store.directory / META_FILENAME).exists()
    assert store.list_ids() == ["a"]


def test_save_preserves_created_at_on_update(store: PersonaStore) -> None:
    card = store.save(make_card(), new=True)
    first_created = card.created_at
    card.name = "改名了"
    saved = store.save(card)  # 非 new
    assert saved.created_at == pytest.approx(first_created)
    assert saved.updated_at >= saved.created_at


def test_save_no_tmp_file_left(store: PersonaStore) -> None:
    store.save(make_card(), new=True)
    assert not list(store.directory.glob("*.tmp"))


# ----------------------------------------------------------------------
# 删除
# ----------------------------------------------------------------------
def test_delete_removes_file(store: PersonaStore) -> None:
    store.save(make_card(), new=True)
    assert store.delete("role") is True
    assert store.load("role") is None
    assert store.delete("role") is False


def test_delete_default_clears_meta(store: PersonaStore) -> None:
    store.save(make_card("a"), new=True)
    store.set_default("a")
    store.delete("a")
    assert store.get_default() is None


# ----------------------------------------------------------------------
# 默认人设 / 播种标记
# ----------------------------------------------------------------------
def test_default_roundtrip(store: PersonaStore) -> None:
    assert store.get_default() is None
    store.set_default("cat")
    assert store.get_default() == "cat"
    store.set_default(None)
    assert store.get_default() is None


def test_default_rejects_invalid_id(store: PersonaStore) -> None:
    with pytest.raises(ValueError):
        store.set_default("../x")


def test_meta_corrupted_returns_empty(store: PersonaStore) -> None:
    (store.directory / META_FILENAME).write_text("坏了", encoding="utf-8")
    assert store.read_meta() == {}
    assert store.get_default() is None
    assert store.is_seeded() is False


def test_seeded_flag(store: PersonaStore) -> None:
    assert store.is_seeded() is False
    store.mark_seeded()
    assert store.is_seeded() is True


# ----------------------------------------------------------------------
# 导入 / 导出
# ----------------------------------------------------------------------
def test_export_str_roundtrip(store: PersonaStore) -> None:
    store.save(make_card("a", "角色A"), new=True)
    text = store.export_str("a")
    assert "角色A" in text
    restored = PersonaCard.from_dict(json.loads(text))
    assert restored.id == "a"


def test_export_missing_raises(store: PersonaStore) -> None:
    with pytest.raises(FileNotFoundError):
        store.export_str("nope")


def test_import_from_dict(store: PersonaStore) -> None:
    card = store.import_card({"id": "new", "name": "新角色"})
    assert card.id == "new"
    assert store.load("new").name == "新角色"


def test_import_conflict_requires_overwrite(store: PersonaStore) -> None:
    store.save(make_card("dup", "原"), new=True)
    with pytest.raises(FileExistsError):
        store.import_card({"id": "dup", "name": "覆盖"})
    card = store.import_card({"id": "dup", "name": "覆盖"}, overwrite=True)
    assert card.name == "覆盖"
    assert store.load("dup").name == "覆盖"


def test_import_with_new_id(store: PersonaStore) -> None:
    card = store.import_card({"id": "old", "name": "共享"}, new_id="copy")
    assert card.id == "copy"


def test_import_invalid_json_raises(store: PersonaStore) -> None:
    with pytest.raises(ValueError):
        store.import_card("不是 json")


def test_import_without_save_does_not_write(store: PersonaStore) -> None:
    card = store.import_card({"id": "preview", "name": "预览"}, save=False)
    assert card.id == "preview"
    assert store.load("preview") is None


def test_import_many_skips_bad_items(store: PersonaStore) -> None:
    imported = store.import_many(
        [{"id": "a", "name": "A"}, {"name": "无 id"}, {"id": "b", "name": "B"}]
    )
    assert [c.id for c in imported] == ["a", "b"]


def test_describe(store: PersonaStore) -> None:
    store.save(make_card("a"), new=True)
    info = store.describe()
    assert info["count"] == 1
    assert info["directory"].endswith("personas")