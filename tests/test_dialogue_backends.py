"""推理后端池（运行时模型切换）测试。"""

from __future__ import annotations

import pytest

from memo_role.dialogue.backends import BackendPool, default_backend_factory
from memo_role.inference.base import ChatMessage

from conftest import FakeBackend


class RecordingFactory:
    """记录每次构造请求，便于断言「何时重建」。"""

    def __init__(self) -> None:
        self.calls = []
        self.backends = []

    def __call__(self, cfg, model_id):
        self.calls.append((cfg.inference.backend, model_id))
        backend = FakeBackend(reply="ok", model=model_id or "default")
        self.backends.append(backend)
        return backend


@pytest.fixture
def factory() -> RecordingFactory:
    return RecordingFactory()


@pytest.fixture
def pool(cfg, factory: RecordingFactory) -> BackendPool:
    return BackendPool(cfg, factory=factory)


def test_get_builds_once_and_reuses(cfg, pool: BackendPool, factory: RecordingFactory) -> None:
    first = pool.get()
    second = pool.get()
    assert first is second
    assert len(factory.calls) == 1
    assert pool.current is first


def test_get_with_same_model_reuses(cfg, pool: BackendPool, factory: RecordingFactory) -> None:
    pool.get(model_id="m1")
    pool.get(model_id="m1")
    assert len(factory.calls) == 1


def test_switch_model_rebuilds_and_closes_old(
    pool: BackendPool, factory: RecordingFactory
) -> None:
    first = pool.get(model_id="m1")
    second = pool.get(model_id="m2")
    assert first is not second
    assert first.closed is True  # 旧后端被卸载，避免低配设备内存堆积
    assert factory.calls == [(pool.cfg.inference.backend, "m1"), (pool.cfg.inference.backend, "m2")]


def test_switch_backend_override_changes_config(pool: BackendPool, factory: RecordingFactory) -> None:
    pool.get(backend="openai_api")
    assert factory.calls[-1][0] == "openai_api"


def test_explicit_switch_forces_rebuild(pool: BackendPool, factory: RecordingFactory) -> None:
    pool.get()
    pool.switch()  # 即便 key 相同也强制重建
    assert len(factory.calls) == 2


def test_close_releases_and_clears(pool: BackendPool) -> None:
    backend = pool.get()
    pool.close()
    assert backend.closed is True
    assert pool.current is None
    assert pool.current_key is None


def test_close_is_idempotent(pool: BackendPool) -> None:
    pool.close()  # 未构造过也不应报错
    pool.get()
    pool.close()
    pool.close()


def test_describe_reports_active(pool: BackendPool) -> None:
    assert pool.describe()["active"] is None
    pool.get(model_id="m1")
    info = pool.describe()
    assert info["active_key"] == [pool.cfg.inference.backend, "m1"]
    assert info["active"]["model"] == "m1"


def test_available_models_lists_builtin(cfg, pool: BackendPool) -> None:
    models = pool.available_models()
    assert any(m["id"] == "smollm2-135m" for m in models)


# ----------------------------------------------------------------------
# 默认工厂：openai 后端用 model_id 覆盖模型名
# ----------------------------------------------------------------------
def test_default_factory_overrides_openai_model(cfg, monkeypatch) -> None:
    cfg.inference.backend = "openai_api"
    captured = {}

    def fake_build(config, **kwargs):
        captured["model"] = config.inference.openai.model
        return FakeBackend()

    monkeypatch.setattr(
        "memo_role.dialogue.backends.build_backend", fake_build
    )
    default_backend_factory(cfg, "gpt-4o")
    assert captured["model"] == "gpt-4o"


def test_default_factory_passes_model_id_to_local(cfg, monkeypatch) -> None:
    cfg.inference.backend = "llama_server"
    captured = {}

    def fake_build(config, **kwargs):
        captured["model_id"] = kwargs.get("model_id")
        return FakeBackend()

    monkeypatch.setattr(
        "memo_role.dialogue.backends.build_backend", fake_build
    )
    default_backend_factory(cfg, "qwen2.5-0.5b")
    assert captured["model_id"] == "qwen2.5-0.5b"


def test_fake_backend_usable_as_chat_backend() -> None:
    from memo_role.inference.base import ChatBackend

    assert isinstance(FakeBackend(), ChatBackend)
    assert FakeBackend().chat([ChatMessage("user", "hi")]) == "好的，我明白了"