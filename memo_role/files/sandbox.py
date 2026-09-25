"""可视化文件管理：受限于项目根目录的沙箱。

设计取舍
--------

**只暴露相对 root 的 POSIX 路径**，不接受绝对路径。所有操作都先规范化、
再 ``resolve()`` 后二次校验是否仍在 root 内 —— 后者专门用来挡住
「软链接指向 root 之外」这类绕过（只做字符串前缀判断是不够的）。

**动数据库的保护**：SQLite 数据库（含 ``-wal`` / ``-shm``）被列为受保护文件，
可以浏览但不可写、不可删；删除目录时若目录内含受保护文件也会被拒绝，
避免「顺手删掉 data 目录」把记忆一起带走。

本模块不依赖任何 Web 框架，因此可以独立单测；HTTP 路由在
:mod:`memo_role.web.api.files`。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional

from ..logging_setup import get_logger

logger = get_logger(__name__)

#: 可在线编辑的文本大小上限（低配设备上编辑大文件既占内存也没意义）
MAX_TEXT_BYTES = 512 * 1024
#: 上传大小上限。定得比「编辑器」大得多是有意的：把量化模型（GGUF）拷进
#: 手机最现实的路子就是「别处下好 → 上传」，而最小的中文可用模型也有 400 MB。
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
#: 流式上传的分块大小
UPLOAD_CHUNK_BYTES = 1 * 1024 * 1024
#: 单目录返回条数上限，避免超大目录把内存打满
MAX_ENTRIES = 1000
#: 判定「是否文本」时采样的字节数
TEXT_SNIFF_BYTES = 4096


# ----------------------------------------------------------------------
# 异常
# ----------------------------------------------------------------------
class SandboxError(Exception):
    """文件操作失败（路径非法、目标不存在、超限等）。"""


class PathEscapeError(SandboxError):
    """路径越出沙箱根目录。"""


class SandboxNotFoundError(SandboxError):
    """目标不存在。"""


class ProtectedPathError(SandboxError):
    """目标是受保护文件（如正在使用的数据库）。"""


class TooLargeError(SandboxError):
    """文件超出可处理大小。"""


# ----------------------------------------------------------------------
# 路径工具
# ----------------------------------------------------------------------
def clean_rel_path(rel: Any) -> str:
    """规范化相对路径；非法时抛 :class:`PathEscapeError`。

    规则：拒绝绝对路径与 ``..``，去掉 ``.`` 与空段，统一用 ``/`` 分隔。
    """
    text = str(rel or "").strip().replace("\\", "/")
    if not text:
        return ""
    if text.startswith("/"):
        raise PathEscapeError("路径必须是相对于项目根目录的相对路径")
    parts: List[str] = []
    for part in PurePosixPath(text).parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise PathEscapeError("路径不允许包含 ..")
        parts.append(part)
    return "/".join(parts)


def clean_filename(name: Any) -> str:
    """只取文件名部分（丢掉任何目录前缀），拒绝 ``..`` 之类的名字。

    注意：``a/b.txt`` 会被取成 ``b.txt``（浏览器传来的路径前缀很常见），
    但 ``a/`` 这种「以分隔符结尾」的输入会被拒绝 —— 它本意是目录，
    当成文件名保存会静默落到别处。
    """
    text = str(name or "").strip().replace("\\", "/")
    if not text or text.endswith("/"):
        raise PathEscapeError(f"非法的文件名：{name!r}")
    base = PurePosixPath(text).name
    if base in ("", ".", ".."):
        raise PathEscapeError(f"非法的文件名：{name!r}")
    return base


def _within(root: Path, path: Path) -> bool:
    """``path`` 是否位于 ``root`` 之内（两者都应为已解析的绝对路径）。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """先写临时文件再替换，避免写一半断电导致文件损坏（安卓上很常见）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _sort_key(path: Path) -> tuple:
    """排序：目录在前，然后按名字不区分大小写。"""
    try:
        is_dir = path.is_dir()
    except OSError:
        is_dir = False
    return (0 if is_dir else 1, path.name.casefold())


# ----------------------------------------------------------------------
# 沙箱
# ----------------------------------------------------------------------
class FileSandbox:
    """把文件操作限制在 ``root`` 之内。"""

    def __init__(
        self,
        root: Path | str,
        *,
        protected: Iterable[Path | str] = (),
        max_text_bytes: int = MAX_TEXT_BYTES,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_text_bytes = int(max_text_bytes)
        self.max_upload_bytes = int(max_upload_bytes)
        self.max_entries = int(max_entries)
        self._protected = {str(Path(p).resolve()) for p in protected}

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def resolve(self, rel: Any, *, must_exist: bool = False) -> Path:
        """把相对路径解析为 root 内的绝对路径。

        :param must_exist: 为 ``True`` 时目标不存在则抛 :class:`SandboxNotFoundError`
        """
        clean = clean_rel_path(rel)
        target = (self.root / clean).resolve() if clean else self.root
        if not _within(self.root, target):
            raise PathEscapeError(f"路径越出项目目录：{rel!r}")
        if must_exist and not target.exists():
            raise SandboxNotFoundError(f"目标不存在：{clean or '.'}")
        return target

    def rel_of(self, path: Path) -> str:
        """绝对路径 → 相对 root 的 POSIX 路径（root 本身返回空串）。"""
        return "" if path == self.root else path.relative_to(self.root).as_posix()

    # ------------------------------------------------------------------
    # 保护
    # ------------------------------------------------------------------
    def is_protected(self, path: Path) -> bool:
        return str(path) in self._protected

    def _contains_protected(self, path: Path) -> bool:
        """目录内（含自身）是否存在受保护文件。"""
        if not path.is_dir():
            return False
        prefix = str(path) + os.sep
        return any(item == str(path) or item.startswith(prefix) for item in self._protected)

    def _ensure_editable(self, path: Path) -> None:
        """确认目标可写可删。"""
        if self.is_protected(path):
            raise ProtectedPathError(f"「{self.rel_of(path)}」是受保护文件，不可修改或删除")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def entry(self, path: Path) -> Dict[str, Any]:
        """单个文件 / 目录的描述信息。"""
        try:
            stat = path.stat()
        except OSError as exc:
            raise SandboxNotFoundError(f"无法读取 {path.name}：{exc}") from exc

        is_dir = path.is_dir()
        protected = self.is_protected(path)
        return {
            "name": path.name,
            "path": self.rel_of(path),
            "type": "dir" if is_dir else "file",
            "size": 0 if is_dir else int(stat.st_size),
            "mtime": float(stat.st_mtime),
            "protected": protected,
            "editable": (not is_dir) and (not protected) and self._looks_text(path),
        }

    def _looks_text(self, path: Path) -> bool:
        """是否可按 UTF-8 文本在线编辑（大小与内容都需满足）。"""
        try:
            if path.stat().st_size > self.max_text_bytes:
                return False
            with path.open("rb") as handle:
                sample = handle.read(TEXT_SNIFF_BYTES)
        except OSError:
            return False
        if b"\x00" in sample:
            return False
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            # 采样可能正好切断多字节字符，末尾不完整时放宽一次
            try:
                sample[:-4].decode("utf-8")
                return True
            except UnicodeDecodeError:
                return False
        return True

    def list_dir(self, rel: Any = "") -> Dict[str, Any]:
        """列出目录内容。"""
        directory = self.resolve(rel, must_exist=True)
        if not directory.is_dir():
            raise SandboxError(f"「{self.rel_of(directory)}」不是目录")

        children = sorted(directory.iterdir(), key=_sort_key)
        truncated = len(children) > self.max_entries
        entries = [self.entry(child) for child in children[: self.max_entries]]
        return {
            "path": self.rel_of(directory),
            "parent": None if directory == self.root else self.rel_of(directory.parent),
            "entries": entries,
            "truncated": truncated,
        }

    def read_text(self, rel: Any) -> Dict[str, Any]:
        """读取文本文件内容。"""
        path = self.resolve(rel, must_exist=True)
        if path.is_dir():
            raise SandboxError("目录不能以文本方式读取")
        size = path.stat().st_size
        if size > self.max_text_bytes:
            raise TooLargeError(
                f"文件 {size} 字节，超过在线编辑上限 {self.max_text_bytes} 字节"
            )
        data = path.read_bytes()
        # 含 NUL 的几乎一定是二进制（图片 / 数据库 / 可执行文件），
        # 这类字节在 UTF-8 下也「能解码」，所以必须单独挡一次。
        if b"\x00" in data[:TEXT_SNIFF_BYTES]:
            raise SandboxError("该文件是二进制文件，无法在线编辑")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SandboxError("该文件不是 UTF-8 文本，无法在线编辑") from exc
        return {
            "path": self.rel_of(path),
            "content": content,
            "size": size,
            "protected": self.is_protected(path),
        }

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def write_text(self, rel: Any, content: Any) -> Dict[str, Any]:
        """写入文本文件（父目录不存在时自动创建）。"""
        if not clean_rel_path(rel):
            raise SandboxError("未指定文件路径")
        path = self.resolve(rel)
        if path.is_dir():
            raise SandboxError("目标是目录，不能写入")
        self._ensure_editable(path)

        data = str(content if content is not None else "").encode("utf-8")
        if len(data) > self.max_text_bytes:
            raise TooLargeError(f"内容超过 {self.max_text_bytes} 字节上限")
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(path, data)
        return self.entry(path)

    def save_bytes(self, rel: Any, data: bytes) -> Dict[str, Any]:
        """写入二进制内容（上传用）。"""
        if not clean_rel_path(rel):
            raise SandboxError("未指定文件路径")
        if len(data) > self.max_upload_bytes:
            raise TooLargeError(f"文件超过 {self.max_upload_bytes} 字节上限")
        path = self.resolve(rel)
        if path.is_dir():
            raise SandboxError("目标是目录，不能覆盖")
        self._ensure_editable(path)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.entry(path)

    def save_upload(self, directory: Any, filename: Any, data: bytes) -> Dict[str, Any]:
        """把上传文件保存到指定目录下（文件名只取 basename，杜绝路径穿越）。"""
        base = clean_rel_path(directory)
        name = clean_filename(filename)
        target = f"{base}/{name}" if base else name
        return self.save_bytes(target, data)

    def save_upload_stream(
        self, directory: Any, filename: Any, stream: Any, *, chunk: int = UPLOAD_CHUNK_BYTES
    ) -> Dict[str, Any]:
        """流式保存上传文件：边读边写，几 GB 的 GGUF 也不会整个进内存。

        先写 ``<名字>.part`` 再改名：中途超限或断线时，模型目录里不会留下
        一个「看起来完整、其实只有一半」的文件被当成模型加载。
        """
        base = clean_rel_path(directory)
        name = clean_filename(filename)
        target = f"{base}/{name}" if base else name
        if not clean_rel_path(target):
            raise SandboxError("未指定文件路径")
        path = self.resolve(target)
        if path.is_dir():
            raise SandboxError("目标是目录，不能覆盖")
        self._ensure_editable(path)

        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        written = 0
        try:
            with part.open("wb") as out:
                while True:
                    data = stream.read(chunk)
                    if not data:
                        break
                    written += len(data)
                    if written > self.max_upload_bytes:
                        raise TooLargeError(
                            f"文件超过 {self.max_upload_bytes} 字节上限"
                        )
                    out.write(data)
            part.replace(path)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        logger.info("文件管理上传：%s（%d 字节）", self.rel_of(path), written)
        return self.entry(path)

    def make_dir(self, rel: Any) -> Dict[str, Any]:
        """新建目录（可多级）。"""
        if not clean_rel_path(rel):
            raise SandboxError("未指定目录路径")
        path = self.resolve(rel)
        if path.exists():
            raise SandboxError(f"「{self.rel_of(path)}」已存在")
        path.mkdir(parents=True)
        return self.entry(path)

    def rename(self, rel: Any, new_rel: Any) -> Dict[str, Any]:
        """重命名 / 移动。"""
        source = self.resolve(rel, must_exist=True)
        if source == self.root:
            raise PathEscapeError("不允许移动项目根目录")
        target = self.resolve(new_rel)
        if target == self.root:
            raise PathEscapeError("不允许覆盖项目根目录")
        if target.exists():
            raise SandboxError(f"「{self.rel_of(target)}」已存在")
        self._ensure_editable(source)
        self._ensure_editable(target)
        if self._contains_protected(source):
            raise ProtectedPathError("目录中包含受保护文件，不可移动")

        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        return self.entry(target)

    def delete(self, rel: Any) -> Dict[str, Any]:
        """删除文件或目录（目录递归删除）。"""
        path = self.resolve(rel, must_exist=True)
        if path == self.root:
            raise PathEscapeError("不允许删除项目根目录")
        self._ensure_editable(path)
        if self._contains_protected(path):
            raise ProtectedPathError("目录中包含受保护文件，已拒绝删除")

        relative = self.rel_of(path)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        logger.info("删除文件管理中移除：%s", relative)
        return {"path": relative, "deleted": True}

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    def download_path(self, rel: Any) -> Path:
        """返回可下载的文件绝对路径（目录不允许下载）。"""
        path = self.resolve(rel, must_exist=True)
        if path.is_dir():
            raise SandboxError("目录无法直接下载")
        return path

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "max_text_bytes": self.max_text_bytes,
            "max_upload_bytes": self.max_upload_bytes,
            "protected": sorted(self._protected),
        }