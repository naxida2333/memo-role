"""模型管理 API：清单、默认模型、推理后端切换、第三方 API、模型下载。

三种「用哪个模型」的层级（从低到高）：

1. 配置里的 ``inference.model`` —— 启动默认值
2. ``data/runtime.json`` 里的全局默认 —— 管理后台改的，重启仍生效
3. 会话绑定（``session.model_id``，由聊天里的 ``/model`` 指令写入）—— 只影响该会话

本模块负责第 2 层，外加两件手机上绕不开的事：

- **换推理后端**：容器里还没有 llama-server，唯一能立刻聊起来的办法是接第三方
  OpenAI 兼容 API；而这不该要求用户进文件管理页手改 config.yaml。
- **把模型弄进手机**：给服务一个链接让它自己下，比「先下到手机再上传」省事得多。

后端切换与模型切换一样是**立即生效**的：低配设备上是「卸载旧的再装载新的」，
若只改配置不重建后端，下一条消息仍会用旧后端，后台显示与实际不一致。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...files import clean_filename
from ...inference.base import ChatMessage, GenParams
from ...inference.downloader import download_url, known_source_options
from ...inference.factory import SUPPORTED_BACKENDS
from ...logging_setup import get_logger
from ..errors import BadRequestError, ConflictError
from ..state import AppState
from .deps import get_state

logger = get_logger(__name__)

router = APIRouter(prefix="/models", tags=["models"])

#: 测试第三方 API 时的最大等待秒数（连不上就该尽快报错）
MAX_PROBE_TIMEOUT = 60.0


class ModelDefault(BaseModel):
    """设置全局默认模型；``model_id`` 传空串表示恢复「跟随注册表默认」。"""

    model_id: str = ""


class BackendPayload(BaseModel):
    """切换推理后端；``openai_api`` 时带上第三方 API 的参数。"""

    backend: str
    base_url: str = ""
    model: str = ""
    #: ``None``（不传）表示沿用已保存的密钥，空数组表示清空。前端留空即不传，
    #: 否则「只想改模型名」会被当成「把密钥删了」。
    api_keys: Optional[List[str]] = None
    timeout: float = 60.0


class ApiProbe(BaseModel):
    """测试一组第三方 API 参数（只试连，不落盘）。"""

    base_url: str
    model: str
    api_keys: List[str] = Field(default_factory=list)
    timeout: float = 30.0


class SourcePayload(BaseModel):
    """设置模型下载源。"""

    source: str = ""


class DownloadPayload(BaseModel):
    """下载模型：给 ``model_id`` 用内置源，或给 ``url`` 自行指定。"""

    model_id: str = ""
    url: str = ""
    filename: str = ""


@router.get("")
def list_models(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """可用模型清单（含本地是否已下载）+ 后端状态 + 下载状态。"""
    inf = state.cfg.inference
    return {
        "backend": inf.backend,
        "default_model": inf.model,
        "openai_style": state.is_openai_backend(),
        "models": state.backends.available_models(),
        "active": state.backends.describe(),
        "model_dir": str(state.model_dir()),
        "inference": {
            "backends": list(SUPPORTED_BACKENDS),
            "openai": {
                "base_url": inf.openai.base_url,
                "model": inf.openai.model,
                "timeout": inf.openai.timeout,
                "key_count": len([k for k in inf.openai.api_keys if k]),
            },
        },
        "download": {
            "source": state.download_source(),
            "sources": known_source_options(),
            "task": state.downloads.describe(),
        },
    }


@router.post("/default")
def set_default_model(
    payload: ModelDefault, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """切换全局默认模型并立即重载后端。

    模型不存在或装载失败时回滚配置，避免留下「配置指向坏模型」的状态。
    """
    state.set_default_model(payload.model_id)
    return {
        "default_model": state.cfg.inference.model,
        "active": state.backends.describe(),
    }


@router.post("/backend")
def set_backend(
    payload: BackendPayload, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """切换推理后端（本地 llama_server / llama_cpp / 第三方 openai_api）。

    切到 ``openai_api`` 时校验必填项并要求 ``base_url`` 是 http(s)：
    这类地址是用户手输的，写错了应当立刻被拦下，而不是等到第一次对话才报错。
    """
    backend = (payload.backend or "").strip()
    if backend not in SUPPORTED_BACKENDS:
        raise BadRequestError(
            f"未知的推理后端 {backend!r}，可选：{', '.join(SUPPORTED_BACKENDS)}"
        )

    openai: Dict[str, Any] = {}
    if backend == "openai_api":
        base_url = payload.base_url.strip()
        model = payload.model.strip()
        if not base_url.startswith(("http://", "https://")):
            raise BadRequestError("第三方 API 的 base_url 必须以 http:// 或 https:// 开头")
        if not model:
            raise BadRequestError("第三方 API 需要填模型名，例如 deepseek-chat")
        openai = {
            "base_url": base_url.rstrip("/"),
            "model": model,
            "timeout": max(5.0, min(float(payload.timeout or 60.0), 600.0)),
        }
        if payload.api_keys is not None:
            openai["api_keys"] = [
                k.strip() for k in payload.api_keys if k and k.strip()
            ]

    active = state.set_inference(backend=backend, openai=openai or None)
    logger.info("推理后端已切换为 %s", backend)
    return {
        "backend": state.cfg.inference.backend,
        "default_model": state.cfg.inference.model,
        "openai_style": state.is_openai_backend(),
        "active": active,
    }


@router.post("/api-test")
def test_api(payload: ApiProbe) -> Dict[str, Any]:
    """用一个极小的请求试连第三方 API，把真实报错原样带回来。

    不落盘、不碰当前后端：用户先把地址密钥填对，再点「保存并切换」。
    会真的发一次 chat 请求（约 16 token），因为「能不能连上」只有真调用才知道。
    """
    base_url = payload.base_url.strip()
    model = payload.model.strip()
    if not base_url.startswith(("http://", "https://")):
        raise BadRequestError("base_url 必须以 http:// 或 https:// 开头")
    if not model:
        raise BadRequestError("请先填模型名")
    timeout = max(5.0, min(float(payload.timeout or 30.0), MAX_PROBE_TIMEOUT))

    from ...config import OpenAIConfig
    from ...inference.base import InferenceError
    from ...inference.openai_api import OpenAICompatBackend

    cfg = OpenAIConfig(
        base_url=base_url.rstrip("/"),
        model=model,
        api_keys=[k.strip() for k in payload.api_keys if k and k.strip()],
        timeout=timeout,
    )
    backend = OpenAICompatBackend(
        cfg, default_params=GenParams(temperature=0.0, max_tokens=16)
    )
    started = time.time()
    try:
        reply = backend.chat([ChatMessage(role="user", content="ping")])
    except InferenceError as exc:
        raise BadRequestError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 第三方服务的错五花八门，原样带出
        raise BadRequestError(f"连接失败：{exc}") from exc
    finally:
        backend.close()

    return {
        "ok": True,
        "latency_ms": int((time.time() - started) * 1000),
        "model": model,
        "reply": (reply or "")[:200],
        "key_count": len(cfg.api_keys),
    }


@router.post("/source")
def set_source(
    payload: SourcePayload, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """设置模型下载源（HF 官方 / 镜像 / 自建反代）。"""
    source = (payload.source or "").strip().rstrip("/")
    if source and not source.startswith(("http://", "https://")):
        raise BadRequestError("下载源必须以 http:// 或 https:// 开头")
    state.runtime.update(model_source=source)
    return {"source": state.download_source()}


@router.post("/download", status_code=202)
def start_download(
    payload: DownloadPayload, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """开始下载一个模型文件（后台线程，进度另查）。"""
    if state.downloads.is_running():
        raise ConflictError("已有下载在进行，请等它结束或先取消")

    if payload.model_id:
        spec = state.backends.registry_instance().get(payload.model_id.strip())
        if not (spec.hf_repo and spec.hf_file):
            raise BadRequestError(
                f"模型「{spec.id}」没有内置下载源，"
                "请改用「从链接导入」或手动放置 GGUF 文件"
            )
        url = download_url(state.download_source(), spec)
        dest = state.model_dir() / spec.filename
        # 有些 CDN 不给 Content-Length，先拿目录里的实测大小当预期值，进度条才有得走
        expected_total = spec.approx_size_mb * 1024 * 1024
    else:
        url = payload.url.strip()
        if not url.startswith(("http://", "https://")):
            raise BadRequestError("下载链接必须以 http:// 或 https:// 开头")
        name = payload.filename.strip() or _filename_from_url(url)
        if not name:
            raise BadRequestError("无法从链接里看出文件名，请填写「保存为」")
        dest = state.model_dir() / clean_filename(name)
        expected_total = 0

    task = state.downloads.start(url, dest, expected_total=expected_total)
    return {"task": task.to_dict()}


@router.get("/download")
def download_status(
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """最近一次下载的状态（没有任务时 ``task`` 为 null）。"""
    return {"task": state.downloads.describe()}


@router.post("/download/cancel")
def cancel_download(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """取消正在进行的下载（已下载的分块会被删掉）。"""
    task = state.downloads.cancel()
    return {"task": task.to_dict() if task is not None else None}


def _filename_from_url(url: str) -> str:
    """从链接里猜文件名（去掉查询串）。"""
    path = urlparse(url).path
    return path.rpartition("/")[2]
