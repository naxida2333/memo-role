"""启动自检测试：报告内容、结论与退出码。

自检的价值在于「远程排障」：用户把报告贴出来就能定位环境问题，
所以这里断言的重点是**每条结论都要能指明下一步怎么做**。
"""

from __future__ import annotations

import socket
from pathlib import Path

from memo_role.selfcheck import (
    CheckResult,
    failed,
    format_report,
    run_checks,
)


def _by_name(results, name: str) -> CheckResult:
    return {r.name: r for r in results}[name]


def _free_port() -> int:
    """要一个当前空闲的端口（测试不假设 8000 一定可用）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_report_shape(cfg) -> None:
    results = run_checks(cfg, platform_name="TestOS")
    assert [r.name for r in results] == [
        "Python",
        "依赖",
        "配置",
        "目录可写",
        "数据库",
        "模型",
        "推理后端",
        "监听端口",
        "Web 静态资源",
        "NapCat",
    ]
    text = format_report(results)
    assert "memo-role 启动自检" in text
    assert "现在可以做的：" in text


def test_missing_model_is_a_hint_not_a_failure(cfg) -> None:
    """没下载模型只算提示：不装模型也能测界面 / 指令 / 文件 / 后台。"""
    result = _by_name(run_checks(cfg), "模型")
    assert result.ok is None
    assert "未下载任何模型" in result.detail
    # 提示里必须给出「文件该放哪」，否则用户无从下手
    expected = cfg.resolve_path(cfg.inference.model_dir) / "smollm2-135m-instruct-q4_k_m.gguf"
    assert str(expected) in result.hint


def test_downloaded_model_is_reported(cfg) -> None:
    models = cfg.resolve_path(cfg.inference.model_dir)
    models.mkdir(parents=True, exist_ok=True)
    (models / "smollm2-135m-instruct-q4_k_m.gguf").write_bytes(b"fake")

    result = _by_name(run_checks(cfg), "模型")
    assert result.ok is True
    assert "smollm2-135m" in result.detail


def test_dirs_and_database_pass(cfg) -> None:
    results = run_checks(cfg)
    assert _by_name(results, "目录可写").ok is True
    assert _by_name(results, "数据库").ok is True


def test_port_conflict_is_detected(cfg) -> None:
    """端口被占用要能测出来：手机上「起不来」最常见的原因之一。"""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    cfg.server.port = holder.getsockname()[1]
    try:
        result = _by_name(run_checks(cfg), "监听端口")
        assert result.ok is False
        assert "无法绑定" in result.detail
    finally:
        holder.close()


def test_missing_binary_is_a_failure(cfg) -> None:
    """默认配置走 llama_server，测试机上没有 llama-server，应判失败并给出说明。"""
    cfg.inference.llama_server.bin_path = "definitely-not-a-real-binary-xyz"
    result = _by_name(run_checks(cfg), "推理后端")
    assert result.ok is False
    assert "llama-server 不可用" in result.detail
    # 关键：要告诉用户「这不影响先测界面」
    assert "可以先测" in result.hint


def test_openai_backend_without_keys_fails(cfg) -> None:
    cfg.inference.backend = "openai_api"
    cfg.inference.openai.api_keys = []
    result = _by_name(run_checks(cfg), "推理后端")
    assert result.ok is False
    assert "api_keys" in result.detail


def test_unknown_backend_fails(cfg) -> None:
    cfg.inference.backend = "not_a_backend"
    result = _by_name(run_checks(cfg), "推理后端")
    assert result.ok is False
    assert "未知后端" in result.detail


def test_static_assets_are_complete(cfg) -> None:
    assert _by_name(run_checks(cfg), "Web 静态资源").ok is True


def test_verdict_mentions_first_round_when_no_model(cfg) -> None:
    """没模型时的结论必须说清「第一轮能测什么」。"""
    text = format_report(run_checks(cfg))
    assert "第一轮" in text or "真实聊天需要先下载模型" in text


def test_failed_only_counts_hard_failures(cfg) -> None:
    results = run_checks(cfg)
    assert all(r.ok is False for r in failed(results))


def test_napcat_disabled_is_only_a_hint(cfg) -> None:
    result = _by_name(run_checks(cfg), "NapCat")
    assert result.ok is None
    assert "未启用" in result.detail


def test_napcat_enabled_without_token_warns(cfg) -> None:
    cfg.napcat.enabled = True
    result = _by_name(run_checks(cfg), "NapCat")
    assert result.ok is None
    assert "access_token" in result.detail


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def test_cli_check_prints_report_and_exits(monkeypatch, tmp_root, capsys) -> None:
    from memo_role import __main__ as cli
    from memo_role.config import load_config

    cfg = load_config(root=tmp_root, environ={})
    cfg.server.port = _free_port()
    cfg.inference.llama_server.bin_path = "definitely-not-a-real-binary-xyz"
    monkeypatch.setattr(cli, "_load", lambda args: cfg)

    code = cli.main(["--check", "--root", str(tmp_root)])
    out = capsys.readouterr().out
    assert "memo-role 启动自检" in out
    assert code == 1  # 有失败项（缺 llama-server）时退出码非 0

    # 补齐可执行文件后应通过（用 python 自身充当占位）
    import sys

    cfg.inference.llama_server.bin_path = sys.executable
    assert cli.main(["--check", "--root", str(tmp_root)]) == 0


def test_cli_check_does_not_start_server(monkeypatch, tmp_root) -> None:
    """自检不应顺手把服务拉起来。"""
    import uvicorn

    from memo_role import __main__ as cli
    from memo_role.config import load_config

    cfg = load_config(root=tmp_root, environ={})
    cfg.server.port = _free_port()
    monkeypatch.setattr(cli, "_load", lambda args: cfg)
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: started.append(kw))

    cli.main(["--check", "--root", str(tmp_root)])
    assert started == []


def test_config_path_is_reported(cfg, tmp_root: Path) -> None:
    """指定了配置文件时，报告里要能看出用的是哪一份。"""
    target = tmp_root / "custom.yaml"
    target.write_text("server:\n  port: 8123\n", encoding="utf-8")

    from memo_role.config import load_config

    loaded = load_config(path=str(target), root=tmp_root, environ={})
    result = _by_name(run_checks(loaded, config_path=str(target)), "配置")
    assert "custom.yaml" in result.detail
    assert "8123" not in result.detail  # 端口不在这一项里展示