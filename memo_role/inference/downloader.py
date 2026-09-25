"""模型文件下载：一个后台线程 + 可查询的进度。

为什么要有它
------------

手机是唯一目标设备，而把 GGUF 弄进手机的路子本来只有两条：用文件管理页
上传（先得把文件放到手机上），或者自己想办法。既然服务本身就在容器里跑、
容器又能联网，那最省事的做法就是**让服务自己去下**：给一个链接就行。

设计取舍
--------

- **同一时刻只跑一个下载**。手机的上行/下行就那么多，并发只会让两个都慢，
  进度也没法用一句话说清。再点一次就提示「已有下载在进行」。
- **下完再改名**。先写 ``<文件名>.part``，成功才 replace：否则一个下到一半
  的 GGUF 会被注册表当成「已下载」，装载时报出一堆看不懂的错。
- **不实现断点续传**。HuggingFace 的下载链接是带签名、会过期的 CDN 地址，
  要续传就得先重新解析真实地址再拼 Range，收益（省一次重下）不值这个复杂度。
- 进度只在内存里，进程重启即丢（下载本来就随之终止）。
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..logging_setup import get_logger
from .catalog import ModelSpec

logger = get_logger(__name__)

#: 默认下载源。HF 官方在国内基本连不上，默认给一个可直连的镜像；
#: 管理后台里可以改（官方 / 镜像 / 自建反代都行）。
DEFAULT_SOURCE = "https://hf-mirror.com"

#: 管理后台下拉里的预置下载源
KNOWN_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("https://hf-mirror.com", "hf-mirror.com（国内可直连，推荐）"),
    ("https://huggingface.co", "huggingface.co（官方，国内通常连不上）"),
)

#: 仓库里文件的固定路径格式
_PATH_TEMPLATE = "{source}/{repo}/resolve/main/{file}"

#: 分块大小与单次 socket 超时（毫秒级的进度更新不值得，1MB 就够细了）
CHUNK_BYTES = 1 * 1024 * 1024
SOCKET_TIMEOUT = 30.0

#: 有些站点（含 HuggingFace）会对空 UA 直接 403
USER_AGENT = "memo-role/0.2 (+https://github.com/naxida2333/memo-role)"


def download_url(source: str, spec: ModelSpec) -> str:
    """把「下载源 + 模型条目」拼成可直接下载的地址。"""
    base = (source or DEFAULT_SOURCE).rstrip("/")
    return _PATH_TEMPLATE.format(source=base, repo=spec.hf_repo, file=spec.hf_file)


@dataclass
class DownloadTask:
    """一次下载的状态。字段都是给前端直接展示的。"""

    url: str
    filename: str
    dest: str
    #: running | done | failed | canceled
    status: str = "running"
    done: int = 0
    #: 0 表示服务端没给 Content-Length（进度只能显示已下载字节数）
    total: int = 0
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        ratio = (self.done / self.total) if self.total > 0 else 0.0
        return {
            "url": self.url,
            "filename": self.filename,
            "dest": self.dest,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "ratio": min(1.0, ratio),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class DownloadCancelled(Exception):
    """内部信号：用户取消了下载。"""


class DownloadManager:
    """下载任务的启动、查询与取消。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: Optional[DownloadTask] = None
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        with self._lock:
            return self._task is not None and self._task.status == "running"

    def current(self) -> Optional[DownloadTask]:
        """最近一次任务（可能已结束）。"""
        with self._lock:
            return self._task

    def describe(self) -> Optional[Dict[str, Any]]:
        task = self.current()
        return task.to_dict() if task is not None else None

    # ------------------------------------------------------------------
    # 启动 / 取消
    # ------------------------------------------------------------------
    def start(self, url: str, dest: Path, *, expected_total: int = 0) -> DownloadTask:
        """在后台线程里下载到 ``dest``（先写 .part，成功再改名）。

        :param expected_total: 服务端不给大小时的兜底总字节数（界面进度条用）；
            真正的大小一旦从响应头拿到就会被覆盖。
        :raises RuntimeError: 已有下载在跑
        """
        with self._lock:
            if self._task is not None and self._task.status == "running":
                raise RuntimeError("已有下载在进行，请等它结束或先取消")
            self._cancel = threading.Event()
            task = DownloadTask(
                url=url, filename=dest.name, dest=str(dest), total=max(0, expected_total)
            )
            self._task = task
            self._thread = threading.Thread(
                target=self._run,
                args=(task, Path(dest), self._cancel),
                name="model-download",
                daemon=True,
            )
            self._thread.start()
        return task

    def cancel(self) -> Optional[DownloadTask]:
        """请求取消（下载线程在下一个分块处退出）。"""
        self._cancel.set()
        return self.current()

    # ------------------------------------------------------------------
    # 线程主体
    # ------------------------------------------------------------------
    def _run(self, task: DownloadTask, dest: Path, cancel: threading.Event) -> None:
        part = dest.with_name(dest.name + ".part")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._download(task, part, cancel)
            part.replace(dest)
            task.status = "done"
            logger.info("模型下载完成：%s（%d 字节）", dest, task.done)
        except DownloadCancelled:
            task.status = "canceled"
            logger.info("模型下载已取消：%s", task.url)
        except Exception as exc:  # noqa: BLE001 - 任何失败都要如实回给界面
            task.status = "failed"
            task.error = _human_error(exc)
            logger.warning("模型下载失败：%s（%s）", task.url, exc)
        finally:
            task.finished_at = time.time()
            if task.status != "done":
                try:
                    part.unlink(missing_ok=True)
                except OSError as exc:  # pragma: no cover - 极少见
                    logger.warning("清理临时文件失败：%s", exc)

    def _download(self, task: DownloadTask, part: Path, cancel: threading.Event) -> None:
        # 带 Range 请求是为了拿到总大小：HuggingFace 的 CDN 有时只给
        # Content-Range 而不给 Content-Length，那时进度条就只能显示已下载字节数。
        request = urllib.request.Request(
            task.url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-"}
        )
        with urllib.request.urlopen(request, timeout=SOCKET_TIMEOUT) as response:
            task.total = _total_size(response.headers) or task.total
            with part.open("wb") as handle:
                while True:
                    if cancel.is_set():
                        raise DownloadCancelled()
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    handle.write(chunk)
                    task.done += len(chunk)
        if task.done == 0:
            raise IOError("下载到 0 字节，链接可能不是文件地址")


def _total_size(headers: Any) -> int:
    """期望的总字节数：优先 Content-Length，其次从 Content-Range 里取。

    ``Content-Range: bytes 0-15/105454432`` 里的最后一段就是文件总大小。
    """
    raw = headers.get("Content-Length")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    content_range = headers.get("Content-Range") or ""
    tail = content_range.rsplit("/", 1)[-1].strip()
    if tail.isdigit():
        return int(tail)
    return 0


def _human_error(exc: Exception) -> str:
    """把底层异常翻译成用户能看懂的一句话。"""
    if isinstance(exc, urllib.error.HTTPError):
        hints = {401: "需要鉴权", 403: "被拒绝（可能是空 UA 或需要登录）", 404: "链接不存在"}
        hint = hints.get(exc.code, "")
        return f"HTTP {exc.code}{('：' + hint) if hint else ''}"
    if isinstance(exc, urllib.error.URLError):
        return f"网络不可达：{exc.reason}"
    return str(exc)


def known_source_options() -> List[Dict[str, str]]:
    """给前端的下拉选项。"""
    return [{"value": value, "label": label} for value, label in KNOWN_SOURCES]
