"""人设卡片数据结构。

一张人设卡 = 一个可扮演的角色。为了适配「可视化编辑 + 导入导出 + 文件管理页
直接改 JSON」三种使用方式，卡片被设计成**纯数据对象**：

- 字段全部是可 JSON 序列化的基础类型，直接落成 ``<persona_dir>/<id>.json``
- :meth:`PersonaCard.render_system_prompt` 负责把结构化字段拼成给模型看的系统提示，
  这样前端只要改字段，提示词结构由代码统一保证

字段设计遵循「角色扮演最需要什么」：名称 / 简介 / 性格 / 说话风格 / 背景 /
开场白 / 对话示例。若用户想完全自定义提示词，直接填 ``system_prompt``，
它会**整体覆盖**自动拼接的结果。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

#: 人设 id 允许的字符：字母 / 数字开头，随后可含字母数字、下划线、点、连字符。
#: 该 id 会直接作为文件名，因此必须严格校验，杜绝 ``../`` 之类的路径穿越。
PERSONA_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: 单个文本字段的长度上限，避免低配设备上提示词过大拖慢推理
MAX_TEXT_LENGTH = 2000

#: 对话示例条数上限
MAX_EXAMPLES = 20


def normalize_persona_id(value: str) -> str:
    """校验并规范化人设 id；非法时抛 ``ValueError``。"""
    text = (value or "").strip()
    if not text:
        raise ValueError("人设 id 不能为空")
    if not PERSONA_ID_PATTERN.match(text):
        raise ValueError(
            f"人设 id {text!r} 非法：只允许字母 / 数字 / 下划线 / 点 / 连字符，"
            "且需以字母或数字开头（最长 64 字符）"
        )
    return text


def _clip(text: Any, limit: int = MAX_TEXT_LENGTH) -> str:
    """把任意值转成字符串并截断（None → 空串）。"""
    if text is None:
        return ""
    value = text if isinstance(text, str) else str(text)
    return value[:limit]


def _string_list(value: Any) -> List[str]:
    """把任意值规范成字符串列表（去空、去重、保持顺序）。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    result: List[str] = []
    for item in items:
        text = _clip(item, 64).strip()
        if text and text not in result:
            result.append(text)
    return result


def _normalize_examples(value: Any) -> List[Dict[str, str]]:
    """规范化对话示例，统一成 ``[{"user": ..., "assistant": ...}, ...]``。

    容忍三种输入：字典、``[用户, 助手]`` 二元组、含 ``user``/``assistant`` 键的字典。
    """
    if not value:
        return []
    if not isinstance(value, (list, tuple)):
        return []

    result: List[Dict[str, str]] = []
    for item in value:
        user_text = assistant_text = ""
        if isinstance(item, dict):
            user_text = _clip(item.get("user", ""))
            assistant_text = _clip(item.get("assistant", ""))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            user_text = _clip(item[0])
            assistant_text = _clip(item[1])
        if not user_text and not assistant_text:
            continue
        result.append({"user": user_text.strip(), "assistant": assistant_text.strip()})
        if len(result) >= MAX_EXAMPLES:
            break
    return result


@dataclass
class PersonaCard:
    """一张人设卡片。"""

    id: str
    name: str
    #: 头像：emoji 或图片路径 / URL，前端直接展示
    avatar: str = "🙂"
    #: 一句话简介，展示在角色卡片上
    description: str = ""
    #: 性格
    personality: str = ""
    #: 说话风格（口癖、语气、称呼方式等）
    speaking_style: str = ""
    #: 背景 / 场景设定
    scenario: str = ""
    #: 开场白，新会话时可直接插入
    greeting: str = ""
    #: 对话示例，帮助小模型模仿语气
    example_dialogue: List[Dict[str, str]] = field(default_factory=list)
    #: 完全自定义的系统提示；非空时覆盖自动拼接结果
    system_prompt: str = ""
    #: 标签，便于前端筛选
    tags: List[str] = field(default_factory=list)
    #: 扩展字段：前端可放主题色、语音等自定义配置
    extra: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """导出为可 JSON 序列化的字典。"""
        return {
            "id": self.id,
            "name": self.name,
            "avatar": self.avatar,
            "description": self.description,
            "personality": self.personality,
            "speaking_style": self.speaking_style,
            "scenario": self.scenario,
            "greeting": self.greeting,
            "example_dialogue": [dict(t) for t in self.example_dialogue],
            "system_prompt": self.system_prompt,
            "tags": list(self.tags),
            "extra": dict(self.extra),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], *, default_id: str = "") -> "PersonaCard":
        """从字典构造卡片（忽略未知字段，缺省字段用默认值补齐）。

        :param default_id: 数据缺少 ``id`` 时使用的 id（导入场景常用）
        """
        if not isinstance(data, dict):
            raise ValueError("人设数据必须是字典")
        raw_id = data.get("id") or default_id
        persona_id = normalize_persona_id(str(raw_id))

        now = time.time()
        return cls(
            id=persona_id,
            name=_clip(data.get("name") or persona_id, 64),
            avatar=_clip(data.get("avatar") or "🙂", 512),
            description=_clip(data.get("description")),
            personality=_clip(data.get("personality")),
            speaking_style=_clip(data.get("speaking_style")),
            scenario=_clip(data.get("scenario")),
            greeting=_clip(data.get("greeting")),
            example_dialogue=_normalize_examples(data.get("example_dialogue")),
            system_prompt=_clip(data.get("system_prompt")),
            tags=_string_list(data.get("tags")),
            extra=data.get("extra") if isinstance(data.get("extra"), dict) else {},
            created_at=float(data.get("created_at") or now),
            updated_at=float(data.get("updated_at") or now),
        )

    def copy(self) -> "PersonaCard":
        """浅拷贝（``extra`` 独立），用于派生新卡片。"""
        return replace(
            self,
            example_dialogue=[dict(t) for t in self.example_dialogue],
            tags=list(self.tags),
            extra=dict(self.extra),
        )

    def with_id(self, new_id: str, *, new_name: Optional[str] = None) -> "PersonaCard":
        """复制一份并改 id（导入时为避免覆盖已有角色使用）。"""
        card = self.copy()
        card.id = normalize_persona_id(new_id)
        if new_name is not None:
            card.name = _clip(new_name, 64)
        now = time.time()
        card.created_at = now
        card.updated_at = now
        return card

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def render_system_prompt(self) -> str:
        """把卡片渲染成系统提示词。

        若填写了 ``system_prompt``，直接返回之（用户完全自定义）；
        否则按固定模板拼接各字段，保证小模型也能拿到结构清晰的设定。
        """
        if self.system_prompt.strip():
            return self.system_prompt.strip()

        name = self.name or self.id
        lines: List[str] = [
            f"你现在扮演角色「{name}」，请全程以该角色的身份、语气和立场与我对话。"
        ]

        sections = (
            ("角色简介", self.description),
            ("性格", self.personality),
            ("说话风格", self.speaking_style),
            ("背景设定", self.scenario),
        )
        for title, body in sections:
            if body.strip():
                lines.append(f"\n【{title}】\n{body.strip()}")

        if self.example_dialogue:
            lines.append("\n【对话示例】")
            for turn in self.example_dialogue:
                user_text = turn.get("user", "").strip()
                assistant_text = turn.get("assistant", "").strip()
                if user_text:
                    lines.append(f"用户：{user_text}")
                if assistant_text:
                    lines.append(f"{name}：{assistant_text}")

        lines.append(
            "\n【行为准则】\n"
            "- 始终保持角色设定，不要跳出角色，也不要自称 AI 或语言模型\n"
            "- 回复简洁自然，符合角色的语气与用词习惯，避免长篇大论\n"
            "- 如果对方的问题超出角色设定范围，用符合角色的方式自然回应"
        )
        return "\n".join(lines)

    def describe(self) -> Dict[str, Any]:
        """摘要信息，供列表页展示（不含完整提示词）。"""
        return {
            "id": self.id,
            "name": self.name,
            "avatar": self.avatar,
            "description": self.description,
            "tags": list(self.tags),
            "greeting": self.greeting,
            "updated_at": self.updated_at,
        }