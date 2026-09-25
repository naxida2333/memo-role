"""Web 层运行时状态：部件装配 + 可持久化的运行时设置。

为什么要有这一层
----------------

路由函数需要拿到「对话引擎 / 记忆 / 人设 / 后端池 / 文件沙箱」，而这些部件
必须**全局唯一**：

- 后端池同一时刻只保留一个活跃模型（低配设备的硬约束），每次请求重建会把
  模型反复装载卸载，等于把设备拖垮；
- 文件沙箱的受保护清单（数据库文件）只应在启动时确定一次。

因此统一由 :class:`AppState` 持有，在 :mod:`memo_role.web.app` 里挂到
``app.state.memo``，路由通过 ``Depends(get_state)`` 取用。

运行时设置（``data/runtime.json``）
-----------------------------------

有少量改动「重启后希望保留，但不属于静态配置」：全局默认模型、群聊回复概率。
写回 ``config.yaml`` 会让程序改写用户的配置文件（注释丢失、难以 review），
所以单独放一个 JSON，启动时覆盖到 ``cfg`` 上，改动后立即生效。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional

from ..dialogue.backends import BackendFactory, BackendPool
from ..dialogue.context import GroupReplyPolicy
from ..dialogue.engine import DialogueEngine
from ..files import FileSandbox
from ..inference.downloader import DEFAULT_SOURCE, DownloadManager
from ..logging_setup import get_logger
from ..memory.manager import MemoryManager
from ..persona.manager import PersonaManager

logger = get_logger(__name__)

#: 允许运行时热改的对话策略字段。其余字段（如指令前缀）改了要重启，
#: 因为指令前缀已被 CommandHandler 记在实例上，热更会造成前后不一致。
DIALOGUE_OVERRIDABLE = (
    "group_reply_probability",
    "group_reply_when_mentioned",
    "label_group_speakers",
)

#: 允许运行时覆盖的 ``inference.openai`` 字段。白名单而非「照单全收」：
#: runtime.json 是可以被文件管理页手改的，写进别的键不该影响配置对象。
OPENAI_OVERRIDABLE = (
    "base_url",
    "model",
    "api_keys",
    "timeout",
    "max_key_attempts",
)

#: 运行时设置文件名（相对 ``data_dir``）
RUNTIME_FILENAME = "runtime.json"


@dataclass
class RuntimeSettings:
    """运行时可改、需持久化的少量设置。"""

    path: Path
    #: 全局默认模型 id；空串表示沿用配置
    model_id: str = ""
    #: 覆盖到 ``cfg.dialogue`` 上的字段
    dialogue: Dict[str, Any] = field(default_factory=dict)
    #: 覆盖到 ``cfg.inference`` 上的字段：``{"backend": ..., "openai": {...}}``。
    #: 存在的理由是「手机上换后端」——改 config.yaml 要进文件管理页手改再重启，
    #: 而第三方 API 的地址与密钥本来就随时会变。
    inference: Dict[str, Any] = field(default_factory=dict)
    #: 模型下载源；空串表示用 ``downloader.DEFAULT_SOURCE``
    model_source: str = ""

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str) -> "RuntimeSettings":
        """读取设置文件；文件缺失或损坏时回退到空设置（不影响启动）。"""
        target = Path(path)
        if not target.exists():
            return cls(target)
        try:
            data = json.loads(target.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("运行时设置读取失败，已忽略：%s", exc)
            return cls(target)
        if not isinstance(data, dict):
            logger.warning("运行时设置根节点应为对象，已忽略：%s", target)
            return cls(target)

        dialogue = data.get("dialogue")
        inference = data.get("inference")
        return cls(
            target,
            model_id=str(data.get("model_id") or ""),
            dialogue=dialogue if isinstance(dialogue, dict) else {},
            inference=inference if isinstance(inference, dict) else {},
            model_source=str(data.get("model_source") or ""),
        )

    def save(self) -> None:
        """落盘（先写临时文件再替换，避免写一半断电留下半个 JSON）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model_id": self.model_id,
            "dialogue": dict(self.dialogue),
            "inference": dict(self.inference),
            "model_source": self.model_source,
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    def update(
        self,
        *,
        model_id: Optional[str] = None,
        dialogue: Optional[Dict[str, Any]] = None,
        inference: Optional[Dict[str, Any]] = None,
        model_source: Optional[str] = None,
    ) -> None:
        """修改并立即落盘；传 ``None`` 表示该字段不变。"""
        if model_id is not None:
            self.model_id = str(model_id).strip()
        for key, value in (dialogue or {}).items():
            if key in DIALOGUE_OVERRIDABLE and value is not None:
                self.dialogue[key] = value
        if inference is not None:
            self.inference = {**self.inference, **inference}
        if model_source is not None:
            self.model_source = str(model_source).strip()
        self.save()

    # ------------------------------------------------------------------
    # 生效
    # ------------------------------------------------------------------
    def apply_to(self, cfg: Any) -> None:
        """把设置覆盖到配置对象上（幂等，可重复调用）。"""
        if self.model_id:
            cfg.inference.model = self.model_id
        if self.inference.get("backend"):
            cfg.inference.backend = str(self.inference["backend"])
        openai = self.inference.get("openai")
        if isinstance(openai, dict):
            for key, value in openai.items():
                if key in OPENAI_OVERRIDABLE:
                    setattr(cfg.inference.openai, key, value)
        for key, value in self.dialogue.items():
            if key in DIALOGUE_OVERRIDABLE and value is not None:
                setattr(cfg.dialogue, key, value)

    def describe(self) -> Dict[str, Any]:
        """供接口展示；**密钥只给条数**，避免后台或日志回显明文。"""
        openai = self.inference.get("openai")
        openai = openai if isinstance(openai, dict) else {}
        return {
            "model_id": self.model_id,
            "dialogue": dict(self.dialogue),
            "model_source": self.model_source,
            "inference": {
                "backend": str(self.inference.get("backend") or ""),
                "openai": {
                    "base_url": str(openai.get("base_url") or ""),
                    "model": str(openai.get("model") or ""),
                    "key_count": len([k for k in (openai.get("api_keys") or []) if k]),
                },
            },
        }


@dataclass
class AppState:
    """Web 应用的全部依赖。"""

    cfg: Any
    engine: DialogueEngine
    memory: MemoryManager
    persona: PersonaManager
    backends: BackendPool
    sandbox: FileSandbox
    runtime: RuntimeSettings
    #: NapCat 适配器；未启用时为 ``None``
    napcat: Optional[Any] = None
    #: 模型下载任务（进程内单例，同时只跑一个）
    downloads: DownloadManager = field(default_factory=DownloadManager)
    started_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------
    # 运行时设置
    # ------------------------------------------------------------------
    def apply_runtime(self) -> None:
        """把运行时设置同步到配置与引擎策略上。"""
        self.runtime.apply_to(self.cfg)
        # 引擎在构造时把策略快照了下来，热改配置后必须同步重建
        self.engine.policy = GroupReplyPolicy.from_config(self.cfg)

    def set_default_model(self, model_id: str) -> Dict[str, Any]:
        """切换全局默认模型：校验 → 重载后端 → 落盘。

        顺序很重要：**只有重载成功才落盘**。否则配置里留下一个装不起来的模型 id，
        下次启动直接起不来，而用户并没有改过配置文件。

        :raises ModelNotFoundError: 本地后端下模型不在注册表里
        :raises InferenceError: 后端装载失败（如模型文件缺失）
        """
        model_id = (model_id or "").strip()
        if model_id and not self.is_openai_backend() and model_id not in self.backends.model_ids():
            from ..inference.base import ModelNotFoundError

            available = ", ".join(self.backends.model_ids()) or "（无）"
            raise ModelNotFoundError(f"模型 {model_id!r} 不在目录中，可用：{available}")

        previous = self.cfg.inference.model
        self.cfg.inference.model = model_id
        try:
            # 立刻装载，避免「后台显示已切换、下一条消息仍是旧模型」
            self.backends.switch(model_id=model_id or None)
        except Exception:
            # 后端池此时已被 close()，恢复配置让下一次 get() 按旧模型重建
            self.cfg.inference.model = previous
            raise
        self.runtime.update(model_id=model_id)
        return self.backends.describe()

    def update_dialogue(self, values: Dict[str, Any]) -> None:
        """热更对话策略并落盘。"""
        self.runtime.update(dialogue=values)
        self.apply_runtime()

    def is_openai_backend(self) -> bool:
        """``openai_api`` 后端可指定任意模型名，不做注册表校验。"""
        return (self.cfg.inference.backend or "") == "openai_api"

    # ------------------------------------------------------------------
    # 推理后端 / 第三方 API
    # ------------------------------------------------------------------
    def set_inference(
        self, *, backend: str, openai: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """切换推理后端（并可同时更新第三方 API 配置），立即重建后端。

        顺序与 :meth:`set_default_model` 一致：**重建成功才落盘**。否则
        runtime.json 里会留下一个起不来的后端，下次启动直接连不上模型，
        而用户并没有动过配置。

        ``openai_api`` 下 ``inference.model`` 的含义会从「注册表里的模型 id」
        变成「API 的模型名」。两者混在一起必然出错（切过去以后默认模型还是
        本地那个 id，请求就会被换成 ``model=qwen2.5-0.5b`` 打到对方服务器），
        所以这里统一清空它，让模型名只由 ``openai.model`` 决定；会话级仍可以
        用 ``/model`` 指定别的名字。

        :raises InferenceError: 后端装载失败（配置已回滚）
        """
        previous_backend = self.cfg.inference.backend
        previous_openai = replace(self.cfg.inference.openai)
        previous_model = self.cfg.inference.model

        self.cfg.inference.backend = backend
        for key, value in (openai or {}).items():
            if key in OPENAI_OVERRIDABLE:
                setattr(self.cfg.inference.openai, key, value)
        if backend == "openai_api" or (
            self.cfg.inference.model not in self.backends.model_ids()
        ):
            self.cfg.inference.model = ""

        try:
            self.backends.switch()
        except Exception:
            self.cfg.inference.backend = previous_backend
            self.cfg.inference.openai = previous_openai
            self.cfg.inference.model = previous_model
            raise

        block: Dict[str, Any] = {"backend": backend}
        if openai:
            block["openai"] = {
                k: v for k, v in openai.items() if k in OPENAI_OVERRIDABLE
            }
        self.runtime.update(
            inference=block,
            # 模型 id 被清掉时，运行时里的那份也要清，否则下次启动又会被盖回来
            model_id="" if not self.cfg.inference.model else None,
        )
        return self.backends.describe()

    # ------------------------------------------------------------------
    # 模型下载
    # ------------------------------------------------------------------
    def download_source(self) -> str:
        """当前模型下载源。"""
        return self.runtime.model_source or DEFAULT_SOURCE

    def model_dir(self) -> Path:
        return Path(self.cfg.model_dir)

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        """状态摘要，供管理后台 / 健康检查使用。"""
        info: Dict[str, Any] = {
            "uptime": max(0.0, time.time() - self.started_at),
            "engine": self.engine.describe(),
            "runtime": self.runtime.describe(),
            "files": self.sandbox.describe(),
        }
        if self.napcat is not None:
            info["napcat"] = self.napcat.describe()
        return info

    # ------------------------------------------------------------------
    # 释放
    # ------------------------------------------------------------------
    def close(self) -> None:
        """释放后端与记忆资源（幂等，异常只记日志）。"""
        try:
            self.engine.close()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不应中断退出流程
            logger.warning("释放对话引擎失败：%s", exc)


def build_state(
    cfg: Any,
    *,
    backend_factory: Optional[BackendFactory] = None,
    napcat: Optional[Any] = None,
    sandbox: Optional[FileSandbox] = None,
    runtime: Optional[RuntimeSettings] = None,
) -> AppState:
    """按配置装配全部部件。

    :param backend_factory: 注入自定义后端工厂（测试用假后端）
    :param napcat: 注入已构造的适配器；``None`` 时按配置自动创建
    :param sandbox: 注入文件沙箱；``None`` 时以项目根为沙箱根
    :param runtime: 注入运行时设置；``None`` 时读 ``<data_dir>/runtime.json``
    """
    cfg.ensure_dirs()

    # 注意用 resolve_path：cfg.data_dir 默认是相对路径（"data"），直接拼会落到
    # 进程的当前目录而不是项目根 —— 测试里就曾把 runtime.json 写进仓库。
    runtime = runtime or RuntimeSettings.load(
        cfg.resolve_path(cfg.data_dir) / RUNTIME_FILENAME
    )
    runtime.apply_to(cfg)

    memory = MemoryManager.build(cfg)
    persona = PersonaManager.build(cfg)
    backends = BackendPool(cfg, factory=backend_factory)
    engine = DialogueEngine(cfg, memory, persona, backends)

    if sandbox is None:
        sandbox = FileSandbox(Path(cfg.root), protected=_protected_paths(cfg))

    state = AppState(
        cfg=cfg,
        engine=engine,
        memory=memory,
        persona=persona,
        backends=backends,
        sandbox=sandbox,
        runtime=runtime,
        napcat=napcat,
    )
    if napcat is None and cfg.napcat.enabled:
        from ..adapters.napcat import NapCatAdapter

        state.napcat = NapCatAdapter.build(cfg, engine)
        logger.info("NapCat 适配器已启用（WS 路径 %s）", state.napcat.ws_path)
    return state


def _protected_paths(cfg: Any) -> list:
    """受保护文件：正在使用的 SQLite 数据库及其 WAL / SHM 附属文件。

    允许浏览（便于排查），但禁止在线改写或删除 —— 记忆数据不该被文件管理页
    顺手删掉。
    """
    db = Path(cfg.db_path)
    return [db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")]