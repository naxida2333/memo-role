"""人设管理器：人设卡片的统一入口。

在 :class:`~memo_role.persona.store.PersonaStore`（文件读写）之上，补齐
业务语义：

- **切换**：维护「当前默认人设」，未指定时自动回退到配置默认值 / 第一张卡
- **播种**：首次运行时写入内置人设，保证开箱即用
- **提示词组装**：把「人设提示 + 记忆上下文 + 额外规则」拼成最终系统提示，
  供 :mod:`memo_role.dialogue` 直接使用
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..logging_setup import get_logger
from .builtin import seed_cards
from .card import PersonaCard, normalize_persona_id
from .store import PersonaStore

logger = get_logger(__name__)


class PersonaNotFoundError(KeyError):
    """请求的人设不存在。"""


class PersonaManager:
    """人设卡片管理。"""

    def __init__(self, cfg: Any, store: PersonaStore):
        self.cfg = cfg
        self.store = store

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    @classmethod
    def build(
        cls,
        cfg: Any,
        *,
        store: Optional[PersonaStore] = None,
        seed: bool = True,
    ) -> "PersonaManager":
        """按配置组装管理器。

        :param store: 注入存储（测试用）
        :param seed: 是否在首次运行时写入内置人设
        """
        store = store or PersonaStore(Path(cfg.persona_dir))
        manager = cls(cfg, store)
        if seed:
            manager.ensure_seed()
        return manager

    def ensure_seed(self) -> List[PersonaCard]:
        """播种内置人设。

        仅在「从未播种过」且「目录里没有任何卡片」时执行，因此用户
        主动删掉内置角色后不会被反复重建。
        """
        store = self.store
        if store.is_seeded():
            return []
        if store.list_ids():
            # 目录已有用户自备的角色，尊重用户选择，只打标记
            store.mark_seeded()
            return []

        created: List[PersonaCard] = []
        for data in seed_cards():
            card = PersonaCard.from_dict(data)
            created.append(store.save(card, new=True))
        store.mark_seeded()

        # 默认人设：优先配置值，否则用第一张内置卡
        config_default = getattr(self.cfg.persona, "default_persona", "") or ""
        if config_default and store.exists(config_default):
            store.set_default(config_default)
        elif created:
            store.set_default(created[0].id)
        logger.info("已写入 %d 张内置人设卡", len(created))
        return created

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_cards(self) -> List[PersonaCard]:
        """全部人设卡。"""
        return self.store.list_cards()

    def list_summaries(self) -> List[Dict[str, Any]]:
        """列表页用的摘要（含「是否当前默认」标记）。"""
        default_id = self.current_default()
        return [
            {**card.describe(), "is_default": card.id == default_id}
            for card in self.list_cards()
        ]

    def get_or_none(self, persona_id: Optional[str]) -> Optional[PersonaCard]:
        """按 id 取卡；id 为空或不存在返回 ``None``。"""
        if not persona_id:
            return None
        return self.store.load(persona_id)

    def get(self, persona_id: Optional[str] = None) -> PersonaCard:
        """取卡：id 为空时回退到默认人设；仍取不到则抛 ``PersonaNotFoundError``。"""
        if persona_id:
            card = self.store.load(persona_id)
            if card is None:
                raise PersonaNotFoundError(f"人设 {persona_id!r} 不存在")
            return card

        resolved = self.current_default()
        card = self.get_or_none(resolved)
        if card is None:
            raise PersonaNotFoundError(
                "没有可用的人设：请先在管理后台创建，或调用 ensure_seed() 写入内置人设"
            )
        return card

    def exists(self, persona_id: str) -> bool:
        return self.store.exists(persona_id)

    # ------------------------------------------------------------------
    # 默认人设（切换）
    # ------------------------------------------------------------------
    def current_default(self) -> Optional[str]:
        """当前默认人设 id 的解析顺序：

        1. ``_meta.json`` 中记录的用户选择
        2. 配置 ``persona.default_persona``
        3. 目录里第一张卡
        """
        stored = self.store.get_default()
        if stored and self.store.exists(stored):
            return stored

        config_default = getattr(self.cfg.persona, "default_persona", "") or ""
        if config_default and self.store.exists(config_default):
            return config_default

        ids = self.store.list_ids()
        return ids[0] if ids else None

    def set_default(self, persona_id: str) -> PersonaCard:
        """切换默认人设（人设不存在时报错）。"""
        card = self.get(persona_id)
        self.store.set_default(card.id)
        return card

    # ------------------------------------------------------------------
    # 增删改
    # ------------------------------------------------------------------
    def create(
        self,
        *,
        name: str,
        persona_id: Optional[str] = None,
        **fields: Any,
    ) -> PersonaCard:
        """新建一张人设卡。

        :param persona_id: 留空时自动生成（``persona_<时间戳>``）
        :param fields: 其它卡片字段（avatar / personality / ...）
        """
        pid = persona_id or f"persona_{int(time.time())}"
        pid = normalize_persona_id(pid)
        if self.store.exists(pid):
            raise FileExistsError(f"人设 {pid!r} 已存在")

        data: Dict[str, Any] = {"id": pid, "name": name, **fields}
        card = PersonaCard.from_dict(data, default_id=pid)
        return self.store.save(card, new=True)

    def update(self, persona_id: str, **fields: Any) -> PersonaCard:
        """局部更新一张卡（只改传入字段，``id`` 不可改）。"""
        card = self.get(persona_id)
        fields.pop("id", None)
        fields.pop("created_at", None)

        merged = card.to_dict()
        merged.update(fields)
        merged["id"] = card.id
        merged["created_at"] = card.created_at
        updated = PersonaCard.from_dict(merged)
        return self.store.save(updated)

    def save(self, card: PersonaCard) -> PersonaCard:
        """整卡保存（存在则更新，不存在则新建）。"""
        is_new = not self.store.exists(card.id)
        return self.store.save(card, new=is_new)

    def delete(self, persona_id: str) -> bool:
        """删除人设；若删掉的是默认人设，自动切到剩余第一张卡。"""
        if not self.store.delete(persona_id):
            return False
        if self.store.get_default() in (None, ""):
            remaining = self.store.list_ids()
            if remaining:
                self.store.set_default(remaining[0])
        return True

    def duplicate(self, persona_id: str, *, new_id: Optional[str] = None) -> PersonaCard:
        """复制一张卡（默认新 id 为 ``<原 id>_copy``）。"""
        card = self.get(persona_id)
        target = new_id or f"{card.id}_copy"
        target = normalize_persona_id(target)
        index = 1
        base = target
        while self.store.exists(target):
            index += 1
            target = normalize_persona_id(f"{base}{index}")
        clone = card.with_id(target, new_name=f"{card.name}（副本）")
        return self.store.save(clone, new=True)

    # ------------------------------------------------------------------
    # 导入 / 导出
    # ------------------------------------------------------------------
    def export(self, persona_id: str, *, fmt: str = "dict") -> Union[Dict[str, Any], str]:
        """导出单张卡：``dict`` 或 ``json`` 字符串。"""
        card = self.get(persona_id)
        return card.to_dict() if fmt == "dict" else self.store.export_str(persona_id)

    def export_all(self, *, fmt: str = "list") -> Union[List[Dict[str, Any]], str]:
        """导出全部人设：``list`` 或 ``json`` 字符串（整体备份）。"""
        import json

        cards = self.store.export_all()
        if fmt == "list":
            return cards
        return json.dumps(
            {"version": 1, "personas": cards}, ensure_ascii=False, indent=2
        )

    def import_data(
        self,
        data: Union[str, Dict[str, Any], List[Any]],
        *,
        overwrite: bool = False,
        new_id: Optional[str] = None,
    ) -> List[PersonaCard]:
        """导入人设。

        支持三种输入：

        - 单张卡（字典 / JSON 字符串）
        - 卡片数组
        - :meth:`export_all` 产出的整体备份 ``{"version": 1, "personas": [...]}``
        """
        parsed = _parse_import_payload(data)
        if isinstance(parsed, dict):
            return [self.store.import_card(parsed, overwrite=overwrite, new_id=new_id)]
        return self.store.import_many(parsed, overwrite=overwrite)

    # ------------------------------------------------------------------
    # 提示词组装
    # ------------------------------------------------------------------
    def render_system_prompt(
        self,
        persona_id: Optional[str] = None,
        *,
        memory_context: str = "",
        extra_rules: str = "",
    ) -> str:
        """组装最终系统提示词：人设 + 记忆 + 额外规则。

        :param memory_context: 由记忆系统渲染的文本块（``build_memory_context``）
        :param extra_rules: 渠道相关的补充规则（如群聊礼仪、禁言提醒）
        """
        card = self.get(persona_id)
        parts: List[str] = [card.render_system_prompt()]

        memory_text = (memory_context or "").strip()
        if memory_text:
            parts.append(
                "【关于对方的记忆】\n"
                "以下是你记得的、与对方相关的信息，请自然地运用，不要直接逐条复述：\n"
                f"{memory_text}"
            )
        rules = (extra_rules or "").strip()
        if rules:
            parts.append(rules)
        return "\n\n".join(parts)

    def describe(self) -> Dict[str, Any]:
        """状态摘要，供管理后台展示。"""
        return {
            **self.store.describe(),
            "current_default": self.current_default(),
        }


def _parse_import_payload(
    data: Union[str, Dict[str, Any], List[Any]]
) -> Union[Dict[str, Any], List[Any]]:
    """把导入输入统一解析成「单卡字典」或「卡片列表」。"""
    import json

    payload: Any = data
    if isinstance(data, str):
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"导入内容不是合法 JSON：{exc}") from exc

    if isinstance(payload, dict):
        # 整体备份格式：{"version": 1, "personas": [...]}
        personas = payload.get("personas")
        if isinstance(personas, list):
            return personas
        return payload
    if isinstance(payload, list):
        return payload
    raise ValueError("导入内容必须是卡片对象、卡片数组或备份文件")