"""配置模块测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memo_role.config import (
    AppConfig,
    collect_env_overrides,
    find_config_file,
    load_config,
)


def test_defaults_when_no_config_file(tmp_root: Path) -> None:
    """无配置文件时应使用内置默认值。"""
    cfg = load_config(root=tmp_root, environ={})
    assert isinstance(cfg, AppConfig)
    assert cfg.server.port == 8000
    assert cfg.inference.backend == "llama_server"
    assert cfg.memory.embedding_backend == "hashing"
    assert cfg.root == tmp_root.resolve()


def test_yaml_file_overrides_defaults(tmp_root: Path) -> None:
    """配置文件（含嵌套段）应覆盖默认值。"""
    (tmp_root / "config.yaml").write_text(
        "server:\n"
        "  port: 9000\n"
        "inference:\n"
        "  backend: openai_api\n"
        "  llama_server:\n"
        "    port: 9001\n",
        encoding="utf-8",
    )
    cfg = load_config(root=tmp_root, environ={})
    assert cfg.server.port == 9000
    assert cfg.server.host == "127.0.0.1"  # 未覆盖的字段保留默认值
    assert cfg.inference.backend == "openai_api"
    assert cfg.inference.llama_server.port == 9001


def test_json_file_supported(tmp_root: Path) -> None:
    """非 .yaml 后缀按 JSON 解析。"""
    (tmp_root / "my.json").write_text(
        json.dumps({"server": {"port": 7777}}), encoding="utf-8"
    )
    cfg = load_config(path="my.json", root=tmp_root, environ={})
    assert cfg.server.port == 7777


def test_env_overrides_yaml(tmp_root: Path) -> None:
    """环境变量优先级高于配置文件，且支持多级字段。"""
    (tmp_root / "config.yaml").write_text("server:\n  port: 9000\n", encoding="utf-8")
    cfg = load_config(
        root=tmp_root,
        environ={
            "MEMO_ROLE_SERVER__PORT": "9123",
            "MEMO_ROLE_INFERENCE__LLAMA_SERVER__PORT": "9200",
            "MEMO_ROLE_UNKNOWN_SECTION__KEY": "1",  # 未知段应被忽略
            "UNRELATED": "1",
        },
    )
    assert cfg.server.port == 9123
    assert cfg.inference.llama_server.port == 9200


def test_explicit_overrides_win(tmp_root: Path) -> None:
    """显式传入的 overrides 优先级最高。"""
    cfg = load_config(
        root=tmp_root,
        environ={"MEMO_ROLE_SERVER__PORT": "9123"},
        overrides={"server": {"port": 1234}},
    )
    assert cfg.server.port == 1234


def test_type_coercion_from_string(tmp_root: Path) -> None:
    """YAML / 环境变量的字符串应被转换成字段声明的类型。"""
    cfg = load_config(
        root=tmp_root,
        environ={
            "MEMO_ROLE_SERVER__PORT": "8123",  # int
            "MEMO_ROLE_SERVER__RELOAD": "true",  # bool
            "MEMO_ROLE_INFERENCE__TEMPERATURE": "0.5",  # float
        },
    )
    assert cfg.server.port == 8123 and isinstance(cfg.server.port, int)
    assert cfg.server.reload is True
    assert cfg.inference.temperature == 0.5


def test_root_cannot_be_overridden_by_file(tmp_root: Path) -> None:
    """配置文件里的 root 键必须被忽略，避免文件管理沙箱逃逸。"""
    (tmp_root / "config.yaml").write_text("root: /etc\n", encoding="utf-8")
    cfg = load_config(root=tmp_root, environ={})
    assert cfg.root == tmp_root.resolve()


def test_resolve_path(tmp_root: Path) -> None:
    """相对路径基于 root 解析，绝对路径原样返回。"""
    cfg = load_config(root=tmp_root, environ={})
    assert cfg.resolve_path("data/x.db") == tmp_root / "data" / "x.db"
    assert cfg.resolve_path("/tmp/abs.db") == Path("/tmp/abs.db")
    assert cfg.db_path == tmp_root / "data" / "memo_role.db"
    assert cfg.persona_dir == tmp_root / "data" / "personas"
    assert cfg.model_dir == tmp_root / "models"


def test_ensure_dirs(tmp_root: Path) -> None:
    """ensure_dirs 应创建数据 / 人设 / 模型 / 日志目录。"""
    cfg = load_config(root=tmp_root, environ={})
    cfg.ensure_dirs()
    assert (tmp_root / "data").is_dir()
    assert (tmp_root / "data" / "personas").is_dir()
    assert (tmp_root / "models").is_dir()
    assert (tmp_root / "data" / "logs").is_dir()


def test_to_dict_is_json_serializable(tmp_root: Path) -> None:
    """to_dict 结果必须可 JSON 序列化（管理后台要直接返回）。"""
    cfg = load_config(root=tmp_root, environ={})
    payload = json.dumps(cfg.to_dict(), ensure_ascii=False)
    assert "llama_server" in payload


def test_missing_explicit_config_raises(tmp_root: Path) -> None:
    """显式指定不存在的配置文件应报错，而不是静默用默认值。"""
    with pytest.raises(FileNotFoundError):
        load_config(path="nope.yaml", root=tmp_root, environ={})


def test_invalid_config_section_type(tmp_root: Path) -> None:
    """段类型错误应给出清晰异常。"""
    (tmp_root / "bad.yaml").write_text("server: 123\n", encoding="utf-8")
    with pytest.raises(TypeError):
        load_config(path="bad.yaml", root=tmp_root, environ={})


def test_collect_env_overrides_ignores_unrelated() -> None:
    """只收集 MEMO_ROLE_ 前缀。"""
    result = collect_env_overrides(
        {"MEMO_ROLE_A__B": "1", "OTHER": "2", "MEMO_ROLE_": "3"}
    )
    assert result == {"a": {"b": 1}}


def test_find_config_file_prefers_config_yaml(tmp_root: Path) -> None:
    """同时存在 config.yaml 与 config.example.yaml 时优先前者。"""
    (tmp_root / "config.yaml").write_text("{}", encoding="utf-8")
    (tmp_root / "config.example.yaml").write_text("{}", encoding="utf-8")
    assert find_config_file(tmp_root).name == "config.yaml"