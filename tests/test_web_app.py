"""Web 应用骨架测试：页面 / 静态资源 / 系统 API / CLI 入口。"""

from __future__ import annotations

from pathlib import Path

import pytest


# ----------------------------------------------------------------------
# 页面与静态资源
# ----------------------------------------------------------------------
@pytest.mark.parametrize("route", ["/", "/admin", "/files"])
def test_pages_are_served(web_client, route: str) -> None:
    resp = web_client.get(route)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.parametrize("asset", ["style.css", "common.js", "app.js"])
def test_static_assets_are_served(web_client, asset: str) -> None:
    resp = web_client.get(f"/static/{asset}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].split(";")[0] in {
        "text/css",
        "text/javascript",
        "application/javascript",
    }


# ----------------------------------------------------------------------
# 系统
# ----------------------------------------------------------------------
def test_status_reports_engine_and_policy(web_client) -> None:
    data = web_client.get("/api/system/status").json()
    assert data["version"]
    assert data["engine"]["policy"]["command_prefixes"] == ["/"]
    assert data["files"]["root"]


def test_config_masks_secrets(web_state, web_client) -> None:
    web_state.cfg.inference.openai.api_keys = ["sk-secret"]
    web_state.cfg.napcat.access_token = "token-123"

    data = web_client.get("/api/system/config").json()
    assert data["inference"]["openai"]["api_keys"] == ["********"]
    assert data["inference"]["openai"]["api_key_count"] == 1
    assert data["napcat"]["access_token"] == "********"


def test_dialogue_policy_hot_update_and_persist(web_state, web_client) -> None:
    resp = web_client.put("/api/system/dialogue", json={"group_reply_probability": 0.5})
    assert resp.status_code == 200

    # 立即生效：引擎的策略在构造时被快照，热更后必须同步
    assert web_state.engine.policy.probability == 0.5
    assert web_state.runtime.path.exists()

    from memo_role.web.state import RuntimeSettings

    assert RuntimeSettings.load(web_state.runtime.path).dialogue == {
        "group_reply_probability": 0.5
    }


def test_dialogue_policy_rejects_out_of_range(web_client) -> None:
    resp = web_client.put("/api/system/dialogue", json={"group_reply_probability": 2.0})
    assert resp.status_code == 422


def test_dialogue_policy_rejects_empty_payload(web_client) -> None:
    assert web_client.put("/api/system/dialogue", json={}).status_code == 400


def test_runtime_settings_live_under_project_root(web_state, tmp_root) -> None:
    """runtime.json 必须落在项目根下的 data/，不能是进程当前目录。

    ``cfg.data_dir`` 默认是相对路径，曾经直接拼进 Path 用过 —— 结果是测试把
    运行时设置写进了仓库的 data/，本地开发与测试互相污染。
    """
    assert web_state.runtime.path == tmp_root / "data" / "runtime.json"


def test_logs_tail(web_state, web_client) -> None:
    log_path = Path(web_state.cfg.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(f"line-{i}" for i in range(10)), encoding="utf-8")

    data = web_client.get("/api/system/logs", params={"lines": 3}).json()
    assert data["exists"] is True
    assert data["lines"] == ["line-7", "line-8", "line-9"]


def test_logs_without_file_returns_empty(tmp_root, backend_factory) -> None:
    from fastapi.testclient import TestClient

    from memo_role.config import load_config
    from memo_role.web.app import create_app
    from memo_role.web.state import build_state

    cfg = load_config(root=tmp_root, environ={})
    cfg.logging.file = ""  # 只输出控制台
    state = build_state(cfg, backend_factory=backend_factory)
    with TestClient(create_app(state)) as client:
        data = client.get("/api/system/logs").json()
        assert data["exists"] is False
        assert data["lines"] == []


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def test_cli_starts_uvicorn_with_overrides(monkeypatch, tmp_root) -> None:
    import uvicorn

    from memo_role import __main__ as cli
    from memo_role.config import load_config

    cfg = load_config(root=tmp_root, environ={})
    cfg.logging.file = ""  # 避免测试往临时目录挂日志 handler
    monkeypatch.setattr(cli, "_load", lambda args: cfg)

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))

    assert cli.main(["--root", str(tmp_root), "--port", "9123", "--host", "0.0.0.0"]) == 0
    assert captured["port"] == 9123
    assert captured["host"] == "0.0.0.0"
    assert captured["app"] is not None
    # 非热重载分支不应把 reload 打开（uvicorn 默认即为 False）
    assert captured.get("reload") is not True


def test_cli_defaults_root_to_cwd(monkeypatch, tmp_path) -> None:
    """不传 ``--root`` 时以当前工作目录为项目根（便于「拷到哪都能跑」）。"""
    from memo_role import __main__ as cli

    monkeypatch.chdir(tmp_path)
    cfg = cli._load(cli._parse_args([]))
    assert Path(cfg.root) == Path(tmp_path).resolve()