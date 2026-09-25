"""内置模型目录。

列出适合「超低配 i3 / 安卓」的 GGUF Q4 小模型候选。用户可在
``<model_dir>/catalog.json`` 中覆盖同名条目或追加自定义模型，无需改代码。

⚠️ 关于 ``hf_repo`` / ``hf_file``（重要）：

    开发环境无法访问 HuggingFace（SSL 连接失败），因此**这些下载源未经核实**，
    仅作为「默认猜测值」提供，字段含义是「预期可用的仓库名」，不保证当前有效。
    若下载失败，请到 HuggingFace 搜索对应模型名后修改 ``catalog.json``，
    或手动把 GGUF 文件放入 ``models/`` 并保持 ``filename`` 一致。

``approx_size_mb`` 为 Q4 量化的**估算体积**，用于展示与预警，非精确值。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Tuple


@dataclass(frozen=True)
class ModelSpec:
    """单个模型的元信息。"""

    id: str  # 稳定标识，配置文件与前端用它引用模型
    name: str  # 展示名
    params: str  # 参数规模
    quant: str  # 量化方式
    approx_size_mb: int  # 估算体积（MB）
    filename: str  # 期望的本地文件名（放在 inference.model_dir 下）
    context: int = 2048  # 训练/推荐上下文长度
    hf_repo: str = ""  # 预期下载源（未核实）
    hf_file: str = ""  # 预期下载文件名（未核实）
    note: str = ""  # 备注

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


#: 内置候选模型（按体量从小到大排序，便于低配设备优先选小模型）
BUILTIN_MODELS: Tuple[ModelSpec, ...] = (
    ModelSpec(
        id="smollm2-135m",
        name="SmolLM2 135M Instruct",
        params="135M",
        quant="Q4_K_M",
        approx_size_mb=100,
        context=2048,
        filename="smollm2-135m-instruct-q4_k_m.gguf",
        hf_repo="HuggingFaceTB/SmolLM2-135M-Instruct-GGUF",
        hf_file="smollm2-135m-instruct-q4_k_m.gguf",
        note="体量最小，仅适合验证链路是否跑通，角色扮演质量有限",
    ),
    ModelSpec(
        id="qwen2.5-0.5b",
        name="Qwen2.5 0.5B Instruct",
        params="0.5B",
        quant="Q4_K_M",
        approx_size_mb=400,
        context=4096,
        filename="qwen2.5-0.5b-instruct-q4_k_m.gguf",
        hf_repo="Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        hf_file="qwen2.5-0.5b-instruct-q4_k_m.gguf",
        note="中文能力相对可用，低配首选梯队",
    ),
    ModelSpec(
        id="tinyllama-1.1b",
        name="TinyLlama 1.1B Chat",
        params="1.1B",
        quant="Q4_K_M",
        approx_size_mb=670,
        context=2048,
        filename="tinyllama-1.1b-chat-v1.0-q4_k_m.gguf",
        hf_repo="TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
        hf_file="tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
        note="英文为主，中文表现一般",
    ),
    ModelSpec(
        id="gemma3-1b",
        name="Gemma 3 1B Instruct",
        params="1B",
        quant="Q4_K_M",
        approx_size_mb=810,
        context=4096,
        filename="gemma-3-1b-it-q4_k_m.gguf",
        hf_repo="ggml-org/gemma-3-1b-it-GGUF",
        hf_file="gemma-3-1b-it-Q4_K_M.gguf",
        note="综合表现较好；注意 Gemma 系列许可证与 MIT 不同，商用前请自行确认",
    ),
    ModelSpec(
        id="smollm2-1.7b",
        name="SmolLM2 1.7B Instruct",
        params="1.7B",
        quant="Q4_K_M",
        approx_size_mb=1060,
        context=2048,
        filename="smollm2-1.7b-instruct-q4_k_m.gguf",
        hf_repo="HuggingFaceTB/SmolLM2-1.7B-Instruct-GGUF",
        hf_file="smollm2-1.7b-instruct-q4_k_m.gguf",
        note="1.7B 档，内存吃紧时建议配合较小 n_ctx",
    ),
    ModelSpec(
        id="qwen3.5-0.8b",
        name="Qwen3.5 0.8B（待确认）",
        params="0.8B",
        quant="Q4_K_M",
        approx_size_mb=500,
        context=4096,
        filename="qwen3.5-0.8b-q4_k_m.gguf",
        hf_repo="",  # 未确认存在对应仓库，留空避免误导
        hf_file="",
        note=(
            "未能确认 Qwen3.5-0.8B 这一模型是否已发布，故不提供下载源。"
            "请在 HuggingFace 核实准确名称后，通过 models/catalog.json 补充 "
            "hf_repo / hf_file，或手动放置 GGUF 文件。"
        ),
    ),
)

#: 供快速查表
BUILTIN_BY_ID: Dict[str, ModelSpec] = {spec.id: spec for spec in BUILTIN_MODELS}


def default_model_id() -> str:
    """默认模型：取第一个内置条目。"""
    return BUILTIN_MODELS[0].id