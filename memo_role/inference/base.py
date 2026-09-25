"""推理层抽象。

三种后端共用同一套接口，上层（对话编排、Web、NapCat）只依赖 ``ChatBackend``：

- ``llama_server`` 启动 llama.cpp 的 ``llama-server`` 子进程，走 HTTP 调用
- ``llama_cpp``   进程内 ``import llama_cpp``（需自行编译，安卓上较难）
- ``openai_api``  OpenAI 兼容的第三方 API，支持多密钥轮询

这样设计的好处是：切换后端不需要改动业务代码，也方便测试时注入假后端。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Sequence

# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------
@dataclass
class ChatMessage:
    """一条对话消息。

    ``name`` 用于群聊场景标注具体发言人，便于模型区分不同人。
    """

    role: str  # system | user | assistant
    content: str
    name: str = ""

    def to_dict(self) -> Dict[str, str]:
        """转成 OpenAI 风格的消息字典（省略空 name）。"""
        data: Dict[str, str] = {"role": self.role, "content": self.content}
        if self.name:
            data["name"] = self.name
        return data


@dataclass
class GenParams:
    """单次生成的采样参数。"""

    temperature: float = 0.8
    top_p: float = 0.9
    max_tokens: int = 512
    stop: List[str] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        """转成 OpenAI 兼容的请求体字段。"""
        payload: Dict[str, Any] = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        if self.stop:
            payload["stop"] = list(self.stop)
        return payload


# ----------------------------------------------------------------------
# 异常
# ----------------------------------------------------------------------
class InferenceError(RuntimeError):
    """推理过程中的通用错误。"""


class BackendUnavailableError(InferenceError):
    """后端不可用（依赖缺失、服务未启动、模型文件不存在等）。"""


class ModelNotFoundError(InferenceError):
    """模型 id 在目录中不存在。"""


# ----------------------------------------------------------------------
# 后端接口
# ----------------------------------------------------------------------
class ChatBackend(ABC):
    """对话后端接口。

    子类只需实现 :meth:`chat`；流式与异步版本默认回退到同步实现，
    需要真正流式输出的后端再自行覆盖 :meth:`stream` / :meth:`astream`。
    """

    #: 后端标识，用于日志与 ``describe()``
    name: str = "base"
    #: 是否支持真正的流式输出
    supports_stream: bool = False

    @abstractmethod
    def chat(
        self,
        messages: Sequence[ChatMessage],
        params: Optional[GenParams] = None,
    ) -> str:
        """同步生成完整回复。"""

    def stream(
        self,
        messages: Sequence[ChatMessage],
        params: Optional[GenParams] = None,
    ) -> Iterator[str]:
        """流式生成。默认实现为「一次性返回完整结果」。"""
        yield self.chat(messages, params)

    async def achat(
        self,
        messages: Sequence[ChatMessage],
        params: Optional[GenParams] = None,
    ) -> str:
        """异步生成。默认放到线程池执行，避免阻塞事件循环。"""
        return await asyncio.to_thread(self.chat, messages, params)

    async def astream(
        self,
        messages: Sequence[ChatMessage],
        params: Optional[GenParams] = None,
    ) -> AsyncIterator[str]:
        """异步流式生成。默认回退为「一次产出完整结果」。"""
        yield await self.achat(messages, params)

    @abstractmethod
    def is_available(self) -> bool:
        """后端当前是否可用（不抛异常，只返回布尔值）。"""

    def describe(self) -> Dict[str, Any]:
        """描述后端状态，供管理后台展示。不得包含密钥明文。"""
        return {"backend": self.name, "available": self.is_available()}

    def close(self) -> None:
        """释放资源（子进程、HTTP 连接等）。默认无操作。"""


def messages_to_payload(messages: Sequence[ChatMessage]) -> List[Dict[str, str]]:
    """批量把消息对象转成字典（各后端共用）。"""
    return [m.to_dict() for m in messages]