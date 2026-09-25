"""人设卡片持久化层。

存储形态刻意选择「**一张卡一个 JSON 文件**」，而不是塞进 SQLite：

- 文件管理页可以直接浏览 / 编辑 / 上传下载人设，符合「安卓打包 APK 后也能改数据」的诉求
- 用户把自己的角色卡分享给别人，只要拷一个 JSON 文件
- 损坏单张卡不会影响其他人设

目录结构::

    <persona_dir>/
        _meta.json          # 内部元信息：默认人设、是否已播种（前缀下划线，前端可隐藏）
        default.json        # 人设卡
        catgirl.json

本层只做文件读写，不做提示词拼接（那是 :mod:`memo_role.persona.manager` 的事）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..logging_setup import get_logger
from .card import PersonaCard, normalize_persona_id

logger = get_logger(__name__)

#: 内部元信息文件名
META_FILENAME = "_meta.json"

#: 导出格式
FORMAT_JSON = "json"


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再替换，避免写一半断电导致文件损坏（安卓上很常见）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class PersonaStore:
    """人设卡片的文件读写。"""

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def ensure_dir(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_of(self, persona_id: str) -> Path:
        """人设 id → 文件路径（id 非法时抛 ``ValueError``）。"""
        return self.directory / f"{normalize_persona_id(persona_id)}.json"

    def exists(self, persona_id: str) -> bool:
        try:
            return self.path_of(persona_id).exists()
        except ValueError:
            return False

    def list_ids(self) -> List[str]:
        """列出全部人设 id（按文件修改时间倒序，最近改过的排前面）。"""
        if not self.directory.exists():
            return []
        files = [p for p in self.directory.glob("*.json") if p.name != META_FILENAME]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [p.stem for p in files]

    def list_cards(self) -> List[PersonaCard]:
        """加载全部人设卡；单张损坏时跳过并记录警告，不影响其余。"""
        cards: List[PersonaCard] = []
        for persona_id in self.list_ids():
            card = self.load(persona_id)
            if card is not None:
                cards.append(card)
        return cards

    def load(self, persona_id: str) -> Optional[PersonaCard]:
        """读取单张卡；文件不存在或解析失败返回 ``None``。"""
        try:
            path = self.path_of(persona_id)
        except ValueError as exc:
            logger.warning("人设 id 非法：%s", exc)
            return None
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取人设 %s 失败，已跳过：%s", path, exc)
            return None
        try:
            # 文件名是权威 id，避免文件内 id 被改乱后与文件名不一致
            return PersonaCard.from_dict(data, default_id=path.stem)
        except ValueError as exc:
            logger.warning("人设 %s 内容非法，已跳过：%s", path, exc)
            return None

    # ------------------------------------------------------------------
    # 写入 / 删除
    # ------------------------------------------------------------------
    def save(self, card: PersonaCard, *, new: bool = False) -> PersonaCard:
        """保存卡片。

        :param new: 新建场景。为 ``True`` 时刷新 ``created_at``；
            否则保留原有 ``created_at``（若文件已存在）。
        """
        self.ensure_dir()
        now = time.time()
        card.id = normalize_persona_id(card.id)
        card.updated_at = now

        if new:
            card.created_at = now
        elif not card.created_at:
            existing = self.load(card.id)
            card.created_at = existing.created_at if existing else now

        _atomic_write(
            self.path_of(card.id),
            json.dumps(card.to_dict(), ensure_ascii=False, indent=2),
        )
        return card

    def delete(self, persona_id: str) -> bool:
        """删除人设文件；返回是否真的删掉了。"""
        try:
            path = self.path_of(persona_id)
        except ValueError:
            return False
        if not path.exists():
            return False
        path.unlink()
        # 删掉的正好是默认人设时，清空默认标记
        if self.get_default() == persona_id:
            self._update_meta(default="")
        return True

    # ------------------------------------------------------------------
    # 元信息：默认人设 / 播种标记
    # ------------------------------------------------------------------
    def read_meta(self) -> Dict[str, Any]:
        """读取元信息，文件缺失或损坏时返回空字典。"""
        path = self.directory / META_FILENAME
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _update_meta(self, **changes: Any) -> None:
        self.ensure_dir()
        meta = self.read_meta()
        meta.update(changes)
        _atomic_write(
            self.directory / META_FILENAME,
            json.dumps(meta, ensure_ascii=False, indent=2),
        )

    def get_default(self) -> Optional[str]:
        """当前默认人设 id；未设置返回 ``None``。"""
        value = self.read_meta().get("default") or ""
        return value or None

    def set_default(self, persona_id: Optional[str]) -> None:
        """设置默认人设；传 ``None`` 表示清除。"""
        if persona_id:
            self._update_meta(default=normalize_persona_id(persona_id))
        else:
            self._update_meta(default="")

    def is_seeded(self) -> bool:
        """是否已经播过种（用于避免用户删光后又被自动重建）。"""
        return bool(self.read_meta().get("seeded"))

    def mark_seeded(self) -> None:
        self._update_meta(seeded=True)

    # ------------------------------------------------------------------
    # 导入 / 导出
    # ------------------------------------------------------------------
    def export_str(self, persona_id: str, *, indent: int = 2) -> str:
        """导出为 JSON 字符串。"""
        card = self.load(persona_id)
        if card is None:
            raise FileNotFoundError(f"人设 {persona_id!r} 不存在")
        return json.dumps(card.to_dict(), ensure_ascii=False, indent=indent)

    def export_all(self) -> List[Dict[str, Any]]:
        """导出全部人设（用于整体备份）。"""
        return [c.to_dict() for c in self.list_cards()]

    def import_card(
        self,
        data: Union[str, Dict[str, Any]],
        *,
        new_id: Optional[str] = None,
        overwrite: bool = False,
        save: bool = True,
    ) -> PersonaCard:
        """从 JSON 字符串或字典导入一张卡。

        :param new_id: 指定新 id（留空则沿用卡片内 id）
        :param overwrite: 已存在同 id 时是否覆盖；``False`` 会抛 ``FileExistsError``
        :param save: 为 ``False`` 时只解析不落盘（便于先预览 / 校验）
        """
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError as exc:
                raise ValueError(f"人设 JSON 解析失败：{exc}") from exc
        else:
            parsed = data

        card = PersonaCard.from_dict(parsed)
        if new_id:
            card = card.with_id(new_id)

        if save:
            if self.exists(card.id) and not overwrite:
                raise FileExistsError(
                    f"人设 {card.id!r} 已存在；如需覆盖请显式指定 overwrite=True"
                )
            card = self.save(card, new=new_id is not None)
        return card

    def import_many(
        self, items: List[Any], *, overwrite: bool = False
    ) -> List[PersonaCard]:
        """批量导入（忽略单条失败，尽量把能导的都导进来）。"""
        imported: List[PersonaCard] = []
        for item in items:
            try:
                imported.append(self.import_card(item, overwrite=overwrite))
            except (ValueError, FileExistsError) as exc:
                logger.warning("导入人设失败，已跳过：%s", exc)
        return imported

    def describe(self) -> Dict[str, Any]:
        """目录状态摘要，供管理后台展示。"""
        return {
            "directory": str(self.directory),
            "count": len(self.list_ids()),
            "default": self.get_default(),
            "seeded": self.is_seeded(),
        }