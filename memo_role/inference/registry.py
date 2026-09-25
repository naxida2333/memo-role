"""模型注册表。

职责：把「内置目录 + 用户自定义 ``catalog.json``」合并成一份可用模型清单，
并负责把模型 id 解析成实际的 GGUF 文件路径。

``catalog.json`` 格式（放在 ``inference.model_dir`` 下，即默认 ``models/catalog.json``）::

    [
      {"id": "qwen2.5-0.5b", "hf_repo": "真实仓库名", "hf_file": "真实文件名"},
      {"id": "my-custom", "name": "我的微调模型", "params": "1B",
       "quant": "Q4_K_M", "approx_size_mb": 700, "filename": "my-model.gguf"}
    ]

- 与内置条目 **id 相同** → 覆盖（只写需要改的字段即可）
- id 不同 → 追加为新条目
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..logging_setup import get_logger
from .base import ModelNotFoundError
from .catalog import BUILTIN_MODELS, ModelSpec, default_model_id

logger = get_logger(__name__)

#: catalog.json 文件名
CATALOG_FILENAME = "catalog.json"


class ModelRegistry:
    """模型清单与路径解析。"""

    def __init__(self, specs: Sequence[ModelSpec], model_dir: Path):
        self.model_dir = Path(model_dir)
        self._specs: List[ModelSpec] = list(specs)
        self._by_id: Dict[str, ModelSpec] = {s.id: s for s in self._specs}

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, cfg: Any) -> "ModelRegistry":
        """按配置加载：内置目录 + ``catalog.json`` 覆盖。"""
        specs: Dict[str, ModelSpec] = {s.id: s for s in BUILTIN_MODELS}
        catalog_file = Path(cfg.model_dir) / CATALOG_FILENAME

        if catalog_file.exists():
            try:
                entries = json.loads(catalog_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("读取 %s 失败，忽略自定义模型目录：%s", catalog_file, exc)
                entries = []
            if isinstance(entries, list):
                for entry in entries:
                    spec = _merge_entry(specs, entry)
                    if spec is not None:
                        specs[spec.id] = spec
            else:
                logger.warning("%s 根节点应为数组，已忽略", catalog_file)

        return cls(list(specs.values()), Path(cfg.model_dir))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def model_dir_path(self) -> Path:
        return self.model_dir

    def list_specs(self) -> List[ModelSpec]:
        """全部模型（保持插入顺序）。"""
        return list(self._specs)

    def ids(self) -> List[str]:
        return [s.id for s in self._specs]

    def get(self, model_id: str) -> ModelSpec:
        """按 id 取模型，不存在时抛 ``ModelNotFoundError``。"""
        try:
            return self._by_id[model_id]
        except KeyError:
            raise ModelNotFoundError(
                f"模型 {model_id!r} 不在目录中，可用：{', '.join(self.ids())}"
            ) from None

    def default_id(self) -> str:
        """默认模型：优先第一个内置条目，否则取目录第一项。"""
        builtin_default = default_model_id()
        if builtin_default in self._by_id:
            return builtin_default
        return self._specs[0].id

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def path_of(self, spec: ModelSpec) -> Path:
        """模型文件在本地的预期路径。"""
        return self.model_dir / spec.filename

    def is_downloaded(self, spec: ModelSpec) -> bool:
        """模型文件是否已存在于本地。"""
        return self.path_of(spec).exists()

    def file_size(self, spec: ModelSpec) -> int:
        """本地文件大小；不存在时返回 0（界面据此显示「未下载」）。"""
        try:
            return self.path_of(spec).stat().st_size
        except OSError:
            return 0

    def downloaded(self) -> List[ModelSpec]:
        """列出本地已就绪的模型。"""
        return [s for s in self._specs if self.is_downloaded(s)]

    def resolve(self, model_id: Optional[str] = None) -> tuple[ModelSpec, Path]:
        """把模型 id 解析为 ``(元信息, 本地路径)``。

        文件不存在时也会返回路径（由调用方决定是否报错 / 触发下载），
        这样后端可以先校验再给出明确提示。
        """
        spec = self.get(model_id or self.default_id())
        return spec, self.path_of(spec)

    def describe(self) -> List[Dict[str, Any]]:
        """带本地状态的清单，供管理后台展示。"""
        return [
            {
                **s.to_dict(),
                "path": str(self.path_of(s)),
                "downloaded": self.is_downloaded(s),
                "size": self.file_size(s),
            }
            for s in self._specs
        ]


def _merge_entry(
    existing: Dict[str, ModelSpec], entry: Any
) -> Optional[ModelSpec]:
    """把 catalog.json 中的一条记录合并成 ``ModelSpec``。

    - 已存在同 id：在旧条目基础上覆盖给定字段
    - 新 id：要求字段完整到能构造 ``ModelSpec``
    """
    if not isinstance(entry, dict) or not entry.get("id"):
        logger.warning("catalog.json 条目缺少 id，已跳过：%s", entry)
        return None

    model_id = str(entry["id"])
    valid_keys = {f.name for f in fields(ModelSpec)}

    if model_id in existing:
        base = existing[model_id].to_dict()
        base.update({k: v for k, v in entry.items() if k in valid_keys})
    else:
        base = {k: v for k, v in entry.items() if k in valid_keys}
        missing = {"name", "params", "quant", "approx_size_mb", "filename"} - base.keys()
        if missing:
            logger.warning("自定义模型 %s 缺少字段 %s，已跳过", model_id, sorted(missing))
            return None

    try:
        return ModelSpec(**base)
    except TypeError as exc:  # pragma: no cover - 字段过滤后基本不会发生
        logger.warning("自定义模型 %s 构造失败：%s", model_id, exc)
        return None