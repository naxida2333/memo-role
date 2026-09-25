"""文件管理 API：项目目录的可视化浏览与编辑。

这一层刻意做得很薄 —— 路径校验、受保护文件、大小上限全部在
:class:`~memo_role.files.sandbox.FileSandbox` 里，路由只负责
「参数绑定 + 调沙箱 + 映射异常」。这样安全规则只有一处实现，不会出现
「路由忘了校验」的漏洞。

异常到状态码的映射见 ``app.py``：不存在 → 404，越界 / 受保护 → 400，超限 → 413。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..state import AppState
from .deps import get_state

router = APIRouter(prefix="/files", tags=["files"])


class PathPayload(BaseModel):
    """只需一个路径的请求体。"""

    path: str = ""


class WritePayload(BaseModel):
    """写入文本文件。"""

    path: str
    content: str = ""


class RenamePayload(BaseModel):
    """重命名 / 移动。"""

    path: str
    new_path: str


@router.get("/meta")
def file_meta(state: AppState = Depends(get_state)) -> Dict[str, Any]:
    """沙箱信息：根目录、大小上限、受保护文件清单。"""
    return state.sandbox.describe()


@router.get("/list")
def list_dir(
    path: str = Query(""), state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """列出目录内容（目录在前，其次按名称）。"""
    return state.sandbox.list_dir(path)


@router.get("/content")
def read_file(
    path: str = Query(...), state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """读取文本文件内容（供在线编辑）。"""
    return state.sandbox.read_text(path)


@router.put("/content")
def write_file(
    payload: WritePayload, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """写入文本文件（父目录不存在时自动创建）。"""
    return state.sandbox.write_text(payload.path, payload.content)


@router.post("/dir", status_code=201)
def make_dir(
    payload: PathPayload, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """新建目录（可多级）。"""
    return state.sandbox.make_dir(payload.path)


@router.post("/rename")
def rename(
    payload: RenamePayload, state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """重命名 / 移动文件或目录。"""
    return state.sandbox.rename(payload.path, payload.new_path)


@router.delete("")
def delete(
    path: str = Query(...), state: AppState = Depends(get_state)
) -> Dict[str, Any]:
    """删除文件或目录（目录递归；含受保护文件时拒绝）。"""
    return state.sandbox.delete(path)


@router.post("/upload", status_code=201)
def upload(
    dir: str = Form(""),
    file: UploadFile = File(...),
    state: AppState = Depends(get_state),
) -> Dict[str, Any]:
    """上传文件到指定目录（文件名只取 basename，杜绝路径穿越）。

    刻意是同步路由 + 流式落盘：模型文件动辄几百 MB，``await file.read()``
    会把它整个读进内存，而同步路由跑在线程池里，可以边读边写、有多少写多少。
    """
    return state.sandbox.save_upload_stream(
        dir, file.filename or "upload.bin", file.file
    )


@router.get("/download")
def download(path: str = Query(...), state: AppState = Depends(get_state)) -> FileResponse:
    """下载文件（目录不可下载）。"""
    target = state.sandbox.download_path(path)
    return FileResponse(target, filename=target.name)