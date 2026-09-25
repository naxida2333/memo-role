"""模型注册表与后端工厂测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memo_role.config import load_config
from memo_role.inference.base import InferenceError, ModelNotFoundError
from memo_role.inference.catalog import BUILTIN_MODELS
from memo_role.inference.factory import SUPPORTED_BACKENDS, build_backend, gen_params
from memo_role.inference.llama_cpp import LlamaCppBackend
from memo_role.inference.llama_server import LlamaServerBackend
from memo_role.inference.openai_api import OpenAICompatBackend
from memo_role.inference.registry import ModelRegistry


def write_catalog(cfg, entries) -> None:
    cfg.model_dir.mkdir(parents=True, exist_ok=True)
    (cfg.model_dir / "catalog.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )


# ----------------------------------------------------------------------
# 内置目录
# ----------------------------------------------------------------------
def test_builtin_catalog_ids_are_unique() -> None:
    ids = [s.id for s in BUILTIN_MODELS]
    assert len(ids) == len(set(ids))
    assert "smollm2-135m" in ids


def test_load_without_custom_catalog(cfg) -> None:
    registry = ModelRegistry.load(cfg)
    assert set(registry.ids()) == {s.id for s in BUILTIN_MODELS}
    assert registry.default_id() == "smollm2-135m"


def test_resolve_returns_path_in_model_dir(cfg) -> None:
    registry = ModelRegistry.load(cfg)
    spec, path = registry.resolve("qwen2.5-0.5b")
    assert spec.params == "0.5B"
    assert path == cfg.model_dir / "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    assert registry.is_downloaded(spec) is False


def test_resolve_unknown_id_raises(cfg) -> None:
    registry = ModelRegistry.load(cfg)
    with pytest.raises(ModelNotFoundError, match="不在目录中"):
        registry.resolve("no-such-model")


def test_resolve_none_uses_default(cfg) -> None:
    registry = ModelRegistry.load(cfg)
    spec, _ = registry.resolve(None)
    assert spec.id == registry.default_id()


def test_downloaded_detection(cfg) -> None:
    registry = ModelRegistry.load(cfg)
    cfg.model_dir.mkdir(parents=True, exist_ok=True)
    spec, path = registry.resolve("smollm2-135m")
    path.write_bytes(b"GGUF")
    assert registry.is_downloaded(spec) is True
    assert [s.id for s in registry.downloaded()] == ["smollm2-135m"]


# ----------------------------------------------------------------------
# 自定义 catalog.json
# ----------------------------------------------------------------------
def test_catalog_json_overrides_builtin_fields(cfg) -> None:
    """同 id 条目应只覆盖给定字段，其余保留内置值。"""
    write_catalog(cfg, [{"id": "smollm2-135m", "hf_repo": "Real/Repo-Name"}])
    registry = ModelRegistry.load(cfg)
    spec = registry.get("smollm2-135m")
    assert spec.hf_repo == "Real/Repo-Name"
    assert spec.name == "SmolLM2 135M Instruct"  # 未被覆盖
    assert spec.filename == "smollm2-135m-instruct-q4_k_m.gguf"


def test_catalog_json_appends_custom_model(cfg) -> None:
    write_catalog(
        cfg,
        [
            {
                "id": "my-model",
                "name": "我的模型",
                "params": "1B",
                "quant": "Q4_K_M",
                "approx_size_mb": 700,
                "filename": "my-model.gguf",
            }
        ],
    )
    registry = ModelRegistry.load(cfg)
    assert "my-model" in registry.ids()
    spec, path = registry.resolve("my-model")
    assert spec.name == "我的模型"
    assert path.name == "my-model.gguf"


def test_catalog_entry_missing_required_fields_skipped(cfg) -> None:
    write_catalog(cfg, [{"id": "incomplete", "name": "缺字段"}])
    registry = ModelRegistry.load(cfg)
    assert "incomplete" not in registry.ids()
    assert len(registry.ids()) == len(BUILTIN_MODELS)


def test_catalog_entry_without_id_skipped(cfg) -> None:
    write_catalog(cfg, [{"name": "无 id"}])
    registry = ModelRegistry.load(cfg)
    assert len(registry.ids()) == len(BUILTIN_MODELS)


def test_broken_catalog_json_falls_back_to_builtin(cfg) -> None:
    cfg.model_dir.mkdir(parents=True, exist_ok=True)
    (cfg.model_dir / "catalog.json").write_text("{ not json", encoding="utf-8")
    registry = ModelRegistry.load(cfg)
    assert len(registry.ids()) == len(BUILTIN_MODELS)


def test_catalog_non_list_root_ignored(cfg) -> None:
    cfg.model_dir.mkdir(parents=True, exist_ok=True)
    (cfg.model_dir / "catalog.json").write_text('{"id": "x"}', encoding="utf-8")
    registry = ModelRegistry.load(cfg)
    assert len(registry.ids()) == len(BUILTIN_MODELS)


def test_describe_includes_local_status(cfg) -> None:
    registry = ModelRegistry.load(cfg)
    entry = next(e for e in registry.describe() if e["id"] == "smollm2-135m")
    assert entry["downloaded"] is False
    assert entry["path"].endswith(".gguf")


# ----------------------------------------------------------------------
# 后端工厂
# ----------------------------------------------------------------------
def test_gen_params_from_config(cfg) -> None:
    cfg.inference.temperature = 0.33
    cfg.inference.top_p = 0.77
    cfg.inference.max_tokens = 99
    params = gen_params(cfg)
    assert (params.temperature, params.top_p, params.max_tokens) == (0.33, 0.77, 99)


def test_build_openai_backend(cfg) -> None:
    cfg.inference.backend = "openai_api"
    backend = build_backend(cfg)
    assert isinstance(backend, OpenAICompatBackend)
    assert backend.name == "openai_api"


def test_build_llama_server_backend_resolves_model_path(cfg) -> None:
    cfg.inference.backend = "llama_server"
    cfg.inference.model = "qwen2.5-0.5b"
    backend = build_backend(cfg)
    assert isinstance(backend, LlamaServerBackend)
    assert backend.model_path == cfg.model_dir / "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    assert backend.n_threads == cfg.inference.n_threads


def test_build_llama_server_with_explicit_model_id(cfg) -> None:
    cfg.inference.backend = "llama_server"
    backend = build_backend(cfg, model_id="gemma3-1b")
    assert backend.model_path.name == "gemma-3-1b-it-q4_k_m.gguf"


def test_build_llama_cpp_backend(cfg) -> None:
    cfg.inference.backend = "llama_cpp"
    backend = build_backend(cfg, model_id="smollm2-135m", llm_factory=lambda: None)
    assert isinstance(backend, LlamaCppBackend)
    assert backend.model_path.name == "smollm2-135m-instruct-q4_k_m.gguf"


def test_build_backend_unknown_raises(cfg) -> None:
    cfg.inference.backend = "quantum"
    with pytest.raises(InferenceError, match="未知的推理后端"):
        build_backend(cfg)


def test_supported_backends_matches_docs() -> None:
    assert set(SUPPORTED_BACKENDS) == {"llama_server", "llama_cpp", "openai_api"}


def test_registry_loaded_from_config_model_dir(tmp_path: Path) -> None:
    """model_dir 应随配置解析（相对 root）。"""
    cfg = load_config(
        root=tmp_path, environ={}, overrides={"inference": {"model_dir": "gguf"}}
    )
    registry = ModelRegistry.load(cfg)
    assert registry.model_dir_path == tmp_path / "gguf"