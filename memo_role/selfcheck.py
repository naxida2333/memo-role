"""启动自检：一次性回答「这台机器上现在能测什么」。

为什么单独做一个命令
--------------------

部署到安卓（Termux / PRoot）时，出错的地方往往不在代码里，而在环境：
依赖没装全、模型没下载、llama-server 不在 PATH、端口被占、目录没写权限。
这些问题的报错散落在各处，用户很难自己判断「到底缺哪一步」。

``python -m memo_role --check`` 把它们一次性检查完并打印成一份可复制的报告，
方便把结果直接贴给开发者，而不是反复猜。

约定：``ok`` 为 ``True`` / ``False`` / ``None``（None 表示「提示，不算失败」）。
只有出现 ``False`` 时进程才以 1 退出 —— 比如「没下载模型」属于提示，
因为界面、指令、人机设、文件管理这些功能不装模型也能测。
"""

from __future__ import annotations

import importlib
import platform
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

#: 运行时必需依赖（与 requirements.txt 的「运行依赖」一致）
REQUIRED_MODULES = ("fastapi", "uvicorn", "yaml", "pydantic", "httpx", "numpy")

#: Web 静态资源（缺任何一个页面都会 404）
REQUIRED_STATIC = (
    "index.html",
    "admin.html",
    "files.html",
    "style.css",
    "common.js",
    "app.js",
)


@dataclass
class CheckResult:
    """单项检查结果。"""

    name: str
    ok: Optional[bool]
    detail: str
    hint: str = ""

    @property
    def mark(self) -> str:
        if self.ok is True:
            return "[ OK ]"
        if self.ok is False:
            return "[失败]"
        return "[提示]"


def run_checks(
    cfg: Any,
    *,
    config_path: Optional[str] = None,
    platform_name: Optional[str] = None,
) -> List[CheckResult]:
    """按顺序执行全部检查项。

    :param config_path: ``--config`` 指定的路径（``None`` 时自动查找）
    """
    results = [
        _check_python(platform_name),
        _check_deps(),
        _check_config(cfg, config_path),
        _check_dirs(cfg),
        _check_database(cfg),
    ]
    models = _check_models(cfg)
    results.append(models)
    results.append(_check_backend(cfg))
    results.append(_check_port(cfg))
    results.append(_check_static())
    results.append(_check_napcat(cfg))
    return results


def format_report(results: List[CheckResult]) -> str:
    """把结果渲染成便于复制粘贴的文本报告。"""
    lines = ["memo-role 启动自检", "=" * 52]
    for item in results:
        lines.append(f"{item.mark} {item.name}：{item.detail}")
        if item.hint:
            lines.append(f"       → {item.hint}")

    failed = [r for r in results if r.ok is False]
    lines.append("-" * 52)
    if failed:
        lines.append(f"有 {len(failed)} 项失败，请先按上面的提示处理。")
    # 无论有没有失败，都告诉用户「现在能测到哪一步」——这才是最有用的结论
    lines.append(f"现在可以做的：{_verdict(results)}")
    return "\n".join(lines)


def failed(results: List[CheckResult]) -> List[CheckResult]:
    return [r for r in results if r.ok is False]


# ----------------------------------------------------------------------
# 各项检查
# ----------------------------------------------------------------------
def _check_python(platform_name: Optional[str]) -> CheckResult:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return CheckResult(
        "Python",
        None,
        f"{version}（{platform_name or platform.platform()}）",
    )


def _check_deps() -> CheckResult:
    """逐个导入运行时依赖，缺哪个就报哪个。"""
    missing = []
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    if missing:
        return CheckResult(
            "依赖",
            False,
            f"缺少：{', '.join(missing)}",
            "在项目根目录执行：pip install -r requirements.txt",
        )
    return CheckResult("依赖", True, f"齐全（{len(REQUIRED_MODULES)} 个）")


def _check_config(cfg: Any, config_path: Optional[str]) -> CheckResult:
    """报告生效的配置来源与关键取值。"""
    from .config import find_config_file

    found = find_config_file(cfg.root, config_path)
    where = str(found) if found else "未找到配置文件（全部走默认值）"
    detail = (
        f"{where}｜根目录 {cfg.root}｜后端 {cfg.inference.backend}｜"
        f"模型目录 {cfg.inference.model_dir}"
    )
    hint = "" if found else "需要自定义配置时：cp config.example.yaml config.yaml"
    return CheckResult("配置", None, detail, hint)


def _check_dirs(cfg: Any) -> CheckResult:
    """确认数据 / 模型目录可写（写不进去后面全部会失败）。"""
    bad = []
    for label, path in (("数据", cfg.data_dir), ("模型", cfg.inference.model_dir)):
        target = Path(path)
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".memo_role_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            bad.append(f"{label}目录 {target}（{exc}）")
    if bad:
        return CheckResult("目录可写", False, "；".join(bad), "检查目录权限或改用可写路径")
    return CheckResult("目录可写", True, f"{cfg.data_dir}、{cfg.inference.model_dir}")


def _check_database(cfg: Any) -> CheckResult:
    """尝试建表：既验证权限，也验证 sqlite 可用。"""
    try:
        from .db import Database

        db = Database(cfg.db_path)
        db.init_schema()
    except Exception as exc:  # noqa: BLE001 - 任何异常都说明数据库不可用
        return CheckResult("数据库", False, f"{cfg.db_path}（{exc}）", "确认数据目录可写后重试")
    return CheckResult("数据库", True, str(cfg.db_path))


def _check_models(cfg: Any) -> CheckResult:
    """列出模型目录里已就绪 / 缺失的模型。

    没下载模型时给 **提示** 而不是失败：界面、指令、人设、文件管理不依赖模型。
    """
    try:
        from .inference.registry import ModelRegistry

        registry = ModelRegistry.load(cfg)
        ready = registry.downloaded()
    except Exception as exc:  # noqa: BLE001 - 目录不可读等
        return CheckResult("模型", False, f"读取模型目录失败：{exc}")

    if not ready:
        # 提示里给出「最小的那个模型」的完整落点，照着放文件即可
        first = registry.get(registry.ids()[0])
        target = registry.path_of(first)
        return CheckResult(
            "模型",
            None,
            f"未下载任何模型（目录 {registry.model_dir}）",
            "把 GGUF 放到该目录即可，例如最小的那个：\n"
            f"       {target}\n"
            "       下载后可在管理后台「模型」页设为默认；"
            "中文效果建议用 qwen2.5-0.5b（约 400MB）",
        )
    names = "、".join(s.id for s in ready)
    return CheckResult("模型", True, f"已就绪 {len(ready)} 个：{names}")


def _check_backend(cfg: Any) -> CheckResult:
    """检查所选推理后端的「能不能用」。"""
    name = (cfg.inference.backend or "").strip()
    if name == "openai_api":
        api = cfg.inference.openai
        if not api.api_keys:
            return CheckResult(
                "推理后端", False, "openai_api 未配置 api_keys", "在 config.yaml 填入密钥"
            )
        return CheckResult("推理后端", True, f"openai_api → {api.base_url}")
    if name == "llama_cpp":
        try:
            importlib.import_module("llama_cpp")
        except ImportError:
            return CheckResult(
                "推理后端",
                False,
                "llama_cpp 不可用（未安装）",
                "pip install llama-cpp-python；安卓上编译较难，建议改用 llama_server",
            )
        return CheckResult("推理后端", True, "llama_cpp 已安装")
    if name != "llama_server":
        return CheckResult(
            "推理后端",
            False,
            f"未知后端 {name!r}",
            "可选：llama_server / llama_cpp / openai_api",
        )

    try:
        from .inference.llama_server import LlamaServerBackend

        backend = LlamaServerBackend(cfg.inference.llama_server)
        binary = backend.resolve_binary()
    except Exception as exc:  # noqa: BLE001 - 找不到二进制属于预期失败
        return CheckResult(
            "推理后端",
            False,
            f"llama-server 不可用：{exc}",
            "只影响真实聊天；界面 / 指令 / 人设 / 文件 / 后台不依赖它，可以先测",
        )
    return CheckResult("推理后端", True, f"llama_server → {binary}")


def _check_port(cfg: Any) -> CheckResult:
    """试绑监听端口：被占用时换端口比事后排查容易得多。"""
    host, port = cfg.server.host, cfg.server.port
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
    except OSError as exc:
        return CheckResult(
            "监听端口",
            False,
            f"{host}:{port} 无法绑定（{exc}）",
            "端口被占用：改 server.port 或用 --port 指定别的端口"
            "（若服务已在运行，可忽略本项）",
        )
    finally:
        probe.close()
    return CheckResult("监听端口", True, f"{host}:{port} 可用")


def _check_static() -> CheckResult:
    """静态资源齐全性检查（缺文件时页面会白屏 / 404）。"""
    static_dir = Path(__file__).resolve().parent / "web" / "static"
    missing = [f for f in REQUIRED_STATIC if not (static_dir / f).exists()]
    if missing:
        return CheckResult(
            "Web 静态资源", False, f"缺少：{', '.join(missing)}", f"目录：{static_dir}"
        )
    return CheckResult("Web 静态资源", True, f"{len(REQUIRED_STATIC)} 个文件齐全")


def _check_napcat(cfg: Any) -> CheckResult:
    """NapCat 是可选通道，未启用不算问题。"""
    napcat = cfg.napcat
    if not napcat.enabled:
        return CheckResult("NapCat", None, "未启用（只跑网页对话时无需开启）")
    if not napcat.access_token:
        return CheckResult(
            "NapCat",
            None,
            f"已启用（{napcat.ws_path}），但 access_token 为空",
            "建议与 NapCat 侧配置一致的 token，否则任何人都能连上这个 WS",
        )
    return CheckResult("NapCat", True, f"已启用（{napcat.ws_path}）")


def _verdict(results: List[CheckResult]) -> str:
    """根据结果给出「现在能测到哪一步」的结论。"""
    by_name = {r.name: r for r in results}
    model = by_name.get("模型")
    backend = by_name.get("推理后端")
    if model is not None and model.ok is None and "未下载" in model.detail:
        return (
            "可以开始第一轮测试（界面 / 指令 / 人设 / 记忆 / 文件 / 管理后台）；"
            "真实聊天需要先下载模型。"
        )
    if backend is not None and backend.ok is False:
        return "推理后端不可用，聊天会失败；其余功能仍可测试。"
    return "环境齐备，可以做完整的端到端测试。"