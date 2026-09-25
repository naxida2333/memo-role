"""文件沙箱测试：路径越界防护与基本读写。"""

from __future__ import annotations

import pytest

from memo_role.files import (
    FileSandbox,
    PathEscapeError,
    ProtectedPathError,
    SandboxError,
    SandboxNotFoundError,
    TooLargeError,
    clean_filename,
    clean_rel_path,
)


@pytest.fixture
def sandbox(tmp_root) -> FileSandbox:
    return FileSandbox(tmp_root)


# ----------------------------------------------------------------------
# 路径规范化
# ----------------------------------------------------------------------
def test_clean_rel_path_normalizes() -> None:
    assert clean_rel_path("") == ""
    assert clean_rel_path(None) == ""
    assert clean_rel_path("a/b.txt") == "a/b.txt"
    assert clean_rel_path("./a//b") == "a/b"
    assert clean_rel_path("a\\b") == "a/b"
    assert clean_rel_path("  a/b  ") == "a/b"


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../x", ".."])
def test_clean_rel_path_rejects_escape(bad: str) -> None:
    with pytest.raises(PathEscapeError):
        clean_rel_path(bad)


def test_clean_filename_takes_basename() -> None:
    assert clean_filename("a/b.txt") == "b.txt"
    assert clean_filename("../../x.txt") == "x.txt"


@pytest.mark.parametrize("bad", ["", "..", ".", "a/"])
def test_clean_filename_rejects_bad(bad: str) -> None:
    with pytest.raises(PathEscapeError):
        clean_filename(bad)


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
def test_resolve_root_is_empty_path(sandbox: FileSandbox) -> None:
    assert sandbox.resolve("") == sandbox.root
    assert sandbox.rel_of(sandbox.root) == ""


def test_resolve_rejects_symlink_escape(tmp_root, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = tmp_root / "link"
    link.symlink_to(outside)

    sandbox = FileSandbox(tmp_root)
    with pytest.raises(PathEscapeError):
        sandbox.resolve("link/secret.txt")
    with pytest.raises(PathEscapeError):
        sandbox.list_dir("link")


def test_resolve_must_exist(sandbox: FileSandbox) -> None:
    with pytest.raises(SandboxNotFoundError):
        sandbox.resolve("nope.txt", must_exist=True)


# ----------------------------------------------------------------------
# 读写
# ----------------------------------------------------------------------
def test_write_then_read_roundtrip(sandbox: FileSandbox) -> None:
    info = sandbox.write_text("data/notes/a.txt", "你好\n世界")
    assert info["path"] == "data/notes/a.txt"
    assert info["type"] == "file"

    read = sandbox.read_text("data/notes/a.txt")
    assert read["content"] == "你好\n世界"
    assert read["size"] == len("你好\n世界".encode("utf-8"))


def test_write_text_requires_path(sandbox: FileSandbox) -> None:
    with pytest.raises(SandboxError):
        sandbox.write_text("", "x")


def test_write_text_rejects_oversize(sandbox: FileSandbox) -> None:
    small = FileSandbox(sandbox.root, max_text_bytes=8)
    with pytest.raises(TooLargeError):
        small.write_text("big.txt", "0123456789")


def test_read_text_rejects_binary(sandbox: FileSandbox) -> None:
    sandbox.save_bytes("blob.bin", b"\x00\x01\x02")
    with pytest.raises(SandboxError):
        sandbox.read_text("blob.bin")


def test_read_text_rejects_directory(sandbox: FileSandbox) -> None:
    sandbox.make_dir("sub")
    with pytest.raises(SandboxError):
        sandbox.read_text("sub")


# ----------------------------------------------------------------------
# 列表
# ----------------------------------------------------------------------
def test_list_dir_sorts_dirs_first(sandbox: FileSandbox) -> None:
    sandbox.make_dir("zeta")
    sandbox.write_text("alpha.txt", "a")
    listing = sandbox.list_dir("")

    assert listing["path"] == ""
    assert listing["parent"] is None
    names = [e["name"] for e in listing["entries"]]
    assert names == ["zeta", "alpha.txt"]  # 目录优先，其后按名字
    assert listing["entries"][0]["type"] == "dir"
    assert listing["entries"][1]["type"] == "file"


def test_list_dir_parent_link(sandbox: FileSandbox) -> None:
    sandbox.make_dir("a/b")
    assert sandbox.list_dir("a/b")["parent"] == "a"
    assert sandbox.list_dir("a")["parent"] == ""


def test_list_dir_flags_text_and_protected(sandbox: FileSandbox) -> None:
    sandbox.write_text("t.txt", "hello")
    sandbox.save_bytes("b.bin", b"\x00\x01")
    sandbox.write_text(".hidden", "x")

    entries = {e["name"]: e for e in sandbox.list_dir("")["entries"]}
    assert entries["t.txt"]["editable"] is True
    assert entries["b.bin"]["editable"] is False
    assert entries[".hidden"]["editable"] is True


def test_list_dir_not_a_directory(sandbox: FileSandbox) -> None:
    sandbox.write_text("f.txt", "x")
    with pytest.raises(SandboxError):
        sandbox.list_dir("f.txt")


def test_list_dir_truncates(sandbox: FileSandbox) -> None:
    tiny = FileSandbox(sandbox.root, max_entries=2)
    for i in range(5):
        tiny.write_text(f"f{i}.txt", "x")
    listing = tiny.list_dir("")
    assert len(listing["entries"]) == 2
    assert listing["truncated"] is True


# ----------------------------------------------------------------------
# 目录 / 改名 / 删除
# ----------------------------------------------------------------------
def test_make_dir_is_idempotent_guarded(sandbox: FileSandbox) -> None:
    assert sandbox.make_dir("x/y")["path"] == "x/y"
    with pytest.raises(SandboxError):
        sandbox.make_dir("x/y")


def test_rename_moves_file(sandbox: FileSandbox) -> None:
    sandbox.write_text("old.txt", "hi")
    info = sandbox.rename("old.txt", "sub/new.txt")
    assert info["path"] == "sub/new.txt"
    assert not (sandbox.root / "old.txt").exists()


def test_rename_rejects_existing_target(sandbox: FileSandbox) -> None:
    sandbox.write_text("a.txt", "a")
    sandbox.write_text("b.txt", "b")
    with pytest.raises(SandboxError):
        sandbox.rename("a.txt", "b.txt")


def test_rename_rejects_root(sandbox: FileSandbox) -> None:
    sandbox.write_text("a.txt", "a")
    with pytest.raises(PathEscapeError):
        sandbox.rename("", "b")


def test_delete_file_and_dir(sandbox: FileSandbox) -> None:
    sandbox.write_text("d/f.txt", "x")
    sandbox.delete("d")
    assert not (sandbox.root / "d").exists()
    with pytest.raises(SandboxNotFoundError):
        sandbox.delete("d")


def test_delete_rejects_root(sandbox: FileSandbox) -> None:
    with pytest.raises(PathEscapeError):
        sandbox.delete("")


# ----------------------------------------------------------------------
# 上传
# ----------------------------------------------------------------------
def test_save_upload_uses_basename_only(sandbox: FileSandbox) -> None:
    info = sandbox.save_upload("uploads", "../../evil.txt", b"data")
    assert info["path"] == "uploads/evil.txt"
    assert info["size"] == 4


def test_save_upload_rejects_oversize(sandbox: FileSandbox) -> None:
    tiny = FileSandbox(sandbox.root, max_upload_bytes=2)
    with pytest.raises(TooLargeError):
        tiny.save_upload("", "a.bin", b"12345")


# ----------------------------------------------------------------------
# 受保护文件
# ----------------------------------------------------------------------
@pytest.fixture
def guarded(tmp_root) -> FileSandbox:
    """把 data/app.db 设为受保护（模拟正在使用的数据库）。"""
    return FileSandbox(tmp_root, protected=[tmp_root / "data" / "app.db"])


def test_protected_file_cannot_be_written(guarded: FileSandbox) -> None:
    guarded.write_text("data/other.txt", "ok")  # 同目录其它文件不受影响
    (guarded.root / "data" / "app.db").write_bytes(b"db")
    with pytest.raises(ProtectedPathError):
        guarded.write_text("data/app.db", "hacked")


def test_protected_file_cannot_be_deleted(guarded: FileSandbox) -> None:
    (guarded.root / "data").mkdir(parents=True, exist_ok=True)
    (guarded.root / "data" / "app.db").write_bytes(b"db")
    with pytest.raises(ProtectedPathError):
        guarded.delete("data/app.db")


def test_directory_containing_protected_cannot_be_deleted(guarded: FileSandbox) -> None:
    (guarded.root / "data").mkdir(parents=True, exist_ok=True)
    (guarded.root / "data" / "app.db").write_bytes(b"db")
    with pytest.raises(ProtectedPathError):
        guarded.delete("data")
    assert (guarded.root / "data" / "app.db").exists()


def test_protected_file_is_browsable(guarded: FileSandbox) -> None:
    (guarded.root / "data").mkdir(parents=True, exist_ok=True)
    (guarded.root / "data" / "app.db").write_bytes(b"db")
    read = guarded.read_text("data/app.db")
    assert read["protected"] is True

    entries = {e["name"]: e for e in guarded.list_dir("data")["entries"]}
    assert entries["app.db"]["protected"] is True
    assert entries["app.db"]["editable"] is False


# ----------------------------------------------------------------------
# 下载与描述
# ----------------------------------------------------------------------
def test_download_path(sandbox: FileSandbox) -> None:
    sandbox.write_text("a.txt", "x")
    assert sandbox.download_path("a.txt") == sandbox.root / "a.txt"
    sandbox.make_dir("d")
    with pytest.raises(SandboxError):
        sandbox.download_path("d")


def test_describe(sandbox: FileSandbox) -> None:
    info = sandbox.describe()
    assert info["root"] == str(sandbox.root)
    assert info["max_text_bytes"] == sandbox.max_text_bytes