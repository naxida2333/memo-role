"""关键信息提取。

把原始对话文本抽成结构化记忆条目。提供两种实现：

- :class:`RuleBasedExtractor` 纯正则，零额外开销。默认选择，
  在 i3 2/3 代与安卓设备上不会带来可感知的 CPU 压力。
- :class:`LlmExtractor` 让本地模型输出 JSON 记忆条目，抽取更灵活，
  但每轮对话要多跑一次推理（低配设备需权衡）。

两者都产出 :class:`ExtractedMemory`，并且都会在内部**去重 + 截断**，
避免同一句话反复写入。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern, Sequence, Tuple

from ..logging_setup import get_logger
from .store import KIND_CORE, KIND_EPISODIC

logger = get_logger(__name__)

#: 单条记忆内容的长度上限（超过就截断，避免长文污染召回）
MAX_CONTENT_LENGTH = 80

#: 一次提取最多产出的条目数
MAX_ITEMS_PER_TEXT = 8


@dataclass
class ExtractedMemory:
    """提取出来的一条记忆。"""

    content: str
    kind: str = KIND_EPISODIC
    importance: float = 0.5
    meta: Dict[str, Any] = field(default_factory=dict)


class MemoryExtractor(ABC):
    """提取器接口。"""

    name: str = "base"

    @abstractmethod
    def extract(self, text: str, *, speaker_name: str = "") -> List[ExtractedMemory]:
        """从一段文本中提取记忆。"""

    def describe(self) -> Dict[str, Any]:
        return {"extractor": self.name}


# ----------------------------------------------------------------------
# 规则提取
# ----------------------------------------------------------------------
#: (规则名, 正则, 生成模板, 种类, 重要度)
#: 模板中用 ``{g1}``/``{g2}`` 引用正则的捕获组
RULES: Sequence[Tuple[str, str, str, str, float]] = (
    # --- 核心记忆：身份与长期设定 ---
    (
        "nickname",
        r"(?:我叫|我的名字是|我的昵称是|叫我)\s*([^\s，。,！!？?；;：:吧吗呢啊呀哦嘛]{1,20})",
        "用户的称呼是「{g1}」",
        KIND_CORE,
        0.85,
    ),
    (
        "birthday",
        r"(?:我的)?生日(?:是|在|：|:)?\s*(\d{1,4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?)",
        "用户的生日是 {g1}",
        KIND_CORE,
        0.85,
    ),
    (
        "age",
        r"我(?:今年)?\s*(\d{1,2})\s*岁",
        "用户今年 {g1} 岁",
        KIND_CORE,
        0.7,
    ),
    (
        "identity",
        r"我(?:是|是一名|是一个|是个)\s*([^\s，。,！!？?；;：:]{2,15})",
        "用户的身份/职业是 {g1}",
        KIND_CORE,
        0.6,
    ),
    (
        "location",
        r"我(?:住在|居住在|在)\s*([^\s，。,！!？?；;：:]{2,15}?)(?:工作|生活|上学|住|$)",
        "用户在 {g1} 生活/工作",
        KIND_CORE,
        0.6,
    ),
    (
        "contact",
        r"((?:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})|(?:1[3-9]\d{9}))",
        "用户的联系方式是 {g1}",
        KIND_CORE,
        0.8,
    ),
    # --- 情节记忆：偏好、事件、显式要求 ---
    (
        "explicit_remember",
        r"(?:请|一定要|帮我)?记住[：:，,\s]*(.{2,60})",
        "{g1}",
        KIND_EPISODIC,
        0.9,
    ),
    (
        "like",
        r"我(?:最|很|超|特别)?喜欢\s*([^\s，。,！!？?；;：:]{1,25})",
        "用户喜欢 {g1}",
        KIND_EPISODIC,
        0.55,
    ),
    (
        "dislike",
        r"我(?:不喜欢|讨厌|最讨厌|不爱)\s*([^\s，。,！!？?；;：:]{1,25})",
        "用户不喜欢 {g1}",
        KIND_EPISODIC,
        0.55,
    ),
    (
        "possession",
        r"我的([^\s，。,！!？?；;：:]{1,10})(?:是|叫)\s*([^\s，。,！!？?；;：:]{1,25})",
        "用户的{g1}是 {g2}",
        KIND_EPISODIC,
        0.6,
    ),
    (
        "wish",
        r"我(?:希望|想要|想)\s*([^\s，。,！!？?；;：:]{2,30})",
        "用户希望/想要 {g1}",
        KIND_EPISODIC,
        0.5,
    ),
)

#: 预编译，避免每次提取重复编译
_COMPILED_RULES: Sequence[Tuple[str, Pattern[str], str, str, float]] = tuple(
    (name, re.compile(pattern), template, kind, importance)
    for name, pattern, template, kind, importance in RULES
)


def _normalize(text: str) -> str:
    """清洗捕获到的片段：去掉首尾标点与空白，并截断。"""
    cleaned = text.strip().strip("。，,、！!？?；;：: 　")
    if len(cleaned) > MAX_CONTENT_LENGTH:
        cleaned = cleaned[:MAX_CONTENT_LENGTH]
    return cleaned


class RuleBasedExtractor(MemoryExtractor):
    """基于正则的提取器（零依赖、确定性、可测试）。"""

    name = "rule"

    def __init__(self, rules: Optional[Sequence[Any]] = None) -> None:
        self.rules = rules if rules is not None else _COMPILED_RULES

    def extract(self, text: str, *, speaker_name: str = "") -> List[ExtractedMemory]:
        if not text or not text.strip():
            return []

        results: List[ExtractedMemory] = []
        seen: set[str] = set()

        for name, pattern, template, kind, importance in self.rules:
            for match in pattern.finditer(text):
                groups = match.groups()
                content = template
                for index, value in enumerate(groups, start=1):
                    content = content.replace(f"{{g{index}}}", _normalize(value or ""))
                content = _normalize(content)

                # 模板替换后仍残留占位符（组为空）则丢弃
                if "{g" in content or len(content) < 2:
                    continue
                if content in seen:
                    continue

                seen.add(content)
                results.append(
                    ExtractedMemory(
                        content=content,
                        kind=kind,
                        importance=importance,
                        meta={"rule": name, "speaker_name": speaker_name},
                    )
                )
                if len(results) >= MAX_ITEMS_PER_TEXT:
                    return results

        return results


# ----------------------------------------------------------------------
# LLM 提取
# ----------------------------------------------------------------------
EXTRACTION_PROMPT = """你是一个记忆提取器。请从下面的对话中提取值得长期记住的用户信息。

要求：
1. 只输出 JSON 数组，不要任何解释文字或 Markdown 代码块
2. 每项格式：{{"content": "一句话描述", "kind": "core|episodic", "importance": 0.0~1.0}}
3. kind 取值：core = 身份与长期设定（称呼、生日、职业、所在地）；
   episodic = 偏好、事件、明确要求记住的事
4. 没有值得记住的信息就输出 []
5. content 用第三人称陈述，不要照抄原句

对话：
{text}"""


class LlmExtractor(MemoryExtractor):
    """用本地模型做提取。

    需要注入一个 :class:`~memo_role.inference.base.ChatBackend`。解析失败时
    自动回退到规则提取，保证「提取环节永远不会让对话失败」。
    """

    name = "llm"

    def __init__(self, backend: Any, *, fallback: Optional[MemoryExtractor] = None) -> None:
        self.backend = backend
        self.fallback = fallback or RuleBasedExtractor()

    def extract(self, text: str, *, speaker_name: str = "") -> List[ExtractedMemory]:
        if not text or not text.strip():
            return []

        from ..inference.base import ChatMessage, GenParams

        prompt = EXTRACTION_PROMPT.format(text=text[:2000])
        try:
            raw = self.backend.chat(
                [ChatMessage("user", prompt)],
                GenParams(temperature=0.1, max_tokens=512),
            )
        except Exception as exc:  # noqa: BLE001 - 任何推理异常都回退
            logger.warning("LLM 提取失败，回退到规则提取：%s", exc)
            return self.fallback.extract(text, speaker_name=speaker_name)

        items = _parse_llm_items(raw)
        if items is None:
            logger.warning("LLM 提取结果无法解析，回退到规则提取")
            return self.fallback.extract(text, speaker_name=speaker_name)
        return items


def _parse_llm_items(raw: str) -> Optional[List[ExtractedMemory]]:
    """解析模型输出的 JSON 数组；失败返回 ``None``。"""
    text = (raw or "").strip()
    if not text:
        return []
    # 容忍模型加了 ```json 包裹
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None

    results: List[ExtractedMemory] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        content = _normalize(str(item.get("content", "")))
        if len(content) < 2:
            continue
        kind = str(item.get("kind", KIND_EPISODIC)).strip().lower()
        if kind not in {KIND_CORE, KIND_EPISODIC}:
            kind = KIND_EPISODIC
        try:
            importance = float(item.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        results.append(
            ExtractedMemory(
                content=content,
                kind=kind,
                importance=min(max(importance, 0.0), 1.0),
                meta={"extractor": "llm"},
            )
        )
        if len(results) >= MAX_ITEMS_PER_TEXT:
            break
    return results


def build_extractor(cfg: Any, backend: Any = None) -> MemoryExtractor:
    """按 ``memory.extractor`` 构造提取器。"""
    name = (cfg.memory.extractor or "rule").strip().lower()
    if name == "llm":
        if backend is None:
            logger.warning("配置为 llm 提取器但未提供推理后端，回退到规则提取")
            return RuleBasedExtractor()
        return LlmExtractor(backend)
    return RuleBasedExtractor()