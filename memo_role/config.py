"""配置模块。

负责加载、合并与校验项目配置。

配置优先级（后面的覆盖前面的）：

1. 代码内置默认值（本文件中的 dataclass 默认值）
2. 配置文件（默认 ``config.yaml``，不存在时回退到 ``config.example.yaml``）
3. 环境变量 ``MEMO_ROLE_<段>__<字段>``，例如 ``MEMO_ROLE_SERVER__PORT=9000``
   多级字段用双下划线分隔，例如 ``MEMO_ROLE_INFERENCE__LLAMA_SERVER__PORT=9001``
4. 调用方显式传入的 ``overrides`` 字典

设计取舍：配置全部落在 dataclass 上，便于类型提示与默认值集中管理；
文件与环境变量只是「补齐 / 覆盖」这一层结构。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, get_type_hints

# 环境变量前缀
ENV_PREFIX = "MEMO_ROLE_"

# 仓库根目录：本文件位于 <root>/memo_role/config.py
DEFAULT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ServerConfig:
    """Web 服务配置。"""

    host: str = "127.0.0.1"
    port: int = 8000
    # 是否开启热重载（开发用；安卓 PRoot 上建议关闭）
    reload: bool = False


@dataclass
class StorageConfig:
    """持久化存储配置。

    仅使用 SQLite 单文件数据库，避免在低配设备上引入额外服务。
    """

    # 相对路径会基于 root 解析
    db_path: str = "data/memo_role.db"


@dataclass
class MemoryConfig:
    """三层记忆系统配置。"""

    # 工作记忆：注入给模型的最近对话轮数
    working_window: int = 12
    # 情节记忆召回条数
    recall_top_k: int = 6
    # 召回相似度下限（低于该值不注入，避免噪声）
    recall_min_score: float = 0.15
    # 低于该重要度的信息不写入情节记忆
    min_importance: float = 0.2
    # 向量后端：hashing | llama_server | openai
    embedding_backend: str = "hashing"
    # 向量维度（仅 hashing 后端生效；其它后端以模型实际输出为准）
    embedding_dim: int = 256
    # embedding 模型名；llama_server 留空表示使用其已加载的模型，openai 需显式指定
    embedding_model: str = ""
    # 记忆提取器：rule | llm
    extractor: str = "rule"


@dataclass
class LlamaServerConfig:
    """llama.cpp 的 llama-server 子进程配置。"""

    # llama-server 可执行文件路径；留空则在 PATH 中查找
    bin_path: str = ""
    # 子进程监听地址（仅本机）
    host: str = "127.0.0.1"
    port: int = 8081
    # 启动后等待服务就绪的最长秒数
    startup_timeout: float = 120.0
    # 追加给 llama-server 的原始参数
    extra_args: List[str] = field(default_factory=list)


@dataclass
class OpenAIConfig:
    """OpenAI 兼容 API 配置（支持多密钥轮询）。"""

    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    # 多个密钥按顺序轮询，失败自动切换下一个
    api_keys: List[str] = field(default_factory=list)
    timeout: float = 60.0
    # 单次请求最多尝试的密钥数
    max_key_attempts: int = 0  # 0 表示等于密钥总数


@dataclass
class InferenceConfig:
    """推理层配置。"""

    # 后端：llama_server | llama_cpp | openai_api
    backend: str = "llama_server"
    # 当前选中的模型 id（对应 models 目录中的条目）
    model: str = ""
    # 生成参数
    temperature: float = 0.8
    top_p: float = 0.9
    max_tokens: int = 512
    # 上下文长度
    n_ctx: int = 2048
    # CPU 线程数（低配 i3 建议 2~4）
    n_threads: int = 2
    # 卸载到 GPU 的层数（0 表示纯 CPU）
    n_gpu_layers: int = 0
    # 模型文件存放目录（相对 root）
    model_dir: str = "models"
    llama_server: LlamaServerConfig = field(default_factory=LlamaServerConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)


@dataclass
class PersonaConfig:
    """人设卡片配置。"""

    # 人设卡片目录（相对 root），每张卡一个 JSON 文件，便于文件管理页直接编辑
    dir: str = "data/personas"
    # 默认人设 id
    default_persona: str = "default"


@dataclass
class DialogueConfig:
    """对话调度配置。

    群聊里机器人不能「有消息就回」——既扰民也浪费低配设备的算力，
    因此这里集中定义「什么时候该回复」的策略。
    """

    # 群聊未 @ 机器人时的随机回复概率（0~1）；设为 0 表示只回应被叫到的话
    group_reply_probability: float = 0.15
    # 被 @ 或被叫到名字时是否必定回复
    group_reply_when_mentioned: bool = True
    # 指令前缀：消息以此开头时必定回复（前缀本身会从文本中剥离）
    command_prefixes: List[str] = field(default_factory=lambda: ["/"])
    # 群聊上下文是否给每条发言加「昵称：」前缀，帮助模型区分发言人
    label_group_speakers: bool = True
    # 注入的历史消息条数上限；0 表示沿用 memory.working_window
    history_limit: int = 0


@dataclass
class LoggingConfig:
    """日志配置。"""

    level: str = "INFO"
    # 日志文件（相对 root）；留空则只输出到控制台
    file: str = "data/logs/memo_role.log"
    # 单文件最大字节数与保留份数
    max_bytes: int = 1_000_000
    backup_count: int = 3


@dataclass
class NapCatConfig:
    """NapCat（OneBot v11 反向 WebSocket）适配器配置。"""

    enabled: bool = False
    # 反向 WS 监听路径，需与 NapCat 配置中的 URL 一致
    ws_path: str = "/onebot/v11/ws"
    # 访问令牌；留空表示不校验
    access_token: str = ""
    # 默认使用的人设 id
    persona: str = "default"
    # 机器人在群里的称呼（除默认人设名外，用于识别纯文本里「叫名字」的消息）
    bot_names: List[str] = field(default_factory=list)
    # 管理员 QQ 号列表（可执行管理指令）
    admins: List[str] = field(default_factory=list)


@dataclass
class AppConfig:
    """应用总配置。"""

    # 项目根目录（也是文件管理页沙箱的根）
    root: Path = DEFAULT_ROOT
    # 数据目录（相对 root）
    data_dir: str = "data"
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    dialogue: DialogueConfig = field(default_factory=DialogueConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    napcat: NapCatConfig = field(default_factory=NapCatConfig)

    # ------------------------------------------------------------------
    # 路径工具
    # ------------------------------------------------------------------
    def resolve_path(self, value: str) -> Path:
        """把配置中的相对路径解析为绝对路径（相对 ``root``）。"""
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.root / p)

    @property
    def db_path(self) -> Path:
        return self.resolve_path(self.storage.db_path)

    @property
    def persona_dir(self) -> Path:
        return self.resolve_path(self.persona.dir)

    @property
    def model_dir(self) -> Path:
        return self.resolve_path(self.inference.model_dir)

    @property
    def log_file(self) -> Optional[Path]:
        return self.resolve_path(self.logging.file) if self.logging.file else None

    def ensure_dirs(self) -> None:
        """创建运行所需目录（幂等）。"""
        for p in (
            self.resolve_path(self.data_dir),
            self.persona_dir,
            self.model_dir,
            self.db_path.parent,
        ):
            p.mkdir(parents=True, exist_ok=True)
        if self.log_file is not None:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> Dict[str, Any]:
        """导出为可序列化字典（用于管理后台展示）。"""
        return _to_plain(self)


# ----------------------------------------------------------------------
# dataclass 构造与合并
# ----------------------------------------------------------------------
def _to_plain(obj: Any) -> Any:
    """递归把 dataclass / Path 转成普通可 JSON 序列化的结构。"""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj


def _build(cls: type, data: Optional[Dict[str, Any]]) -> Any:
    """依据 dataclass 字段定义，从字典构造实例（忽略未知键）。

    遇到嵌套 dataclass 字段会递归构造，保证「部分配置」也能生效。

    注意：本模块启用了 ``from __future__ import annotations``，dataclass 的
    ``field.type`` 是字符串，因此这里必须用 ``get_type_hints`` 解析出真实类型。
    """
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise TypeError(f"配置段 {cls.__name__} 需要是字典，实际为 {type(data).__name__}")

    hints = get_type_hints(cls)
    kwargs: Dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        kwargs[f.name] = _coerce_value(hints.get(f.name, f.type), data[f.name])
    return cls(**kwargs)


def _coerce_value(ftype: Any, value: Any) -> Any:
    """把配置值强制转换成声明的类型；嵌套 dataclass 递归构造。"""
    if isinstance(ftype, type) and is_dataclass(ftype):
        # 段类型写错时必须显式报错，避免静默回退到默认值掩盖配置问题
        if value is None:
            return _build(ftype, None)
        if not isinstance(value, dict):
            raise TypeError(
                f"配置段 {ftype.__name__} 需要是字典，实际为 {type(value).__name__}"
            )
        return _build(ftype, value)

    # 常见标量类型转换（YAML / 环境变量可能给到字符串）
    if ftype is bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if ftype is int and not isinstance(value, int):
        return int(value)
    if ftype is float and not isinstance(value, float):
        return float(value)
    if ftype is str and not isinstance(value, str):
        return str(value)
    if ftype is Path:
        return Path(value)
    return value


# ----------------------------------------------------------------------
# 环境变量覆盖
# ----------------------------------------------------------------------
def _parse_env_value(raw: str) -> Any:
    """把环境变量字符串解析成合适类型：优先 JSON，失败则当普通字符串。"""
    text = raw.strip()
    if text == "":
        return ""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return raw


def collect_env_overrides(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """收集 ``MEMO_ROLE_`` 前缀的环境变量，展开成嵌套字典。

    例：``MEMO_ROLE_SERVER__PORT=9000`` → ``{"server": {"port": 9000}}``
    """
    env = os.environ if environ is None else environ
    result: Dict[str, Any] = {}
    for key, raw in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX) :].split("__")
        if not path or not path[0]:
            continue
        cursor = result
        for part in path[:-1]:
            cursor = cursor.setdefault(part.lower(), {})
            if not isinstance(cursor, dict):  # pragma: no cover - 冲突配置
                break
        else:
            cursor[path[-1].lower()] = _parse_env_value(raw)
    return result


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并字典，``patch`` 覆盖 ``base``。"""
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ----------------------------------------------------------------------
# 配置文件读取
# ----------------------------------------------------------------------
def _load_config_file(path: Path) -> Dict[str, Any]:
    """读取 YAML 或 JSON 配置文件。"""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml  # 延迟导入，避免无配置场景下的硬依赖

        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"配置文件根节点必须是字典：{path}")
    return data


def find_config_file(root: Path, explicit: Optional[str] = None) -> Optional[Path]:
    """定位配置文件：显式指定 > config.yaml > config.yml > config.example.yaml。"""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = root / p
        if not p.exists():
            raise FileNotFoundError(f"指定的配置文件不存在：{p}")
        return p
    for name in ("config.yaml", "config.yml", "config.example.yaml"):
        candidate = root / name
        if candidate.exists():
            return candidate
    return None


def load_config(
    path: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    root: Optional[Path] = None,
    environ: Optional[Dict[str, str]] = None,
) -> AppConfig:
    """加载配置。

    :param path: 配置文件路径（相对 ``root`` 或绝对路径）
    :param overrides: 最高优先级的覆盖字典
    :param root: 项目根目录，默认取仓库根
    :param environ: 用于测试注入的环境变量字典
    """
    base_root = Path(root).resolve() if root else DEFAULT_ROOT

    data: Dict[str, Any] = {}
    config_file = find_config_file(base_root, path)
    if config_file is not None:
        data = _load_config_file(config_file)

    data = _deep_merge(data, collect_env_overrides(environ))
    if overrides:
        data = _deep_merge(data, overrides)

    # root 由参数决定，不允许被配置文件里的同名键覆盖（避免越出沙箱）
    data.pop("root", None)
    cfg: AppConfig = _build(AppConfig, data)
    cfg.root = base_root
    return cfg