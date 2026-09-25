"""人设卡片管理：保存、切换、导入导出。

存储形态是「一张卡一个 JSON 文件」（见 :mod:`memo_role.persona.store`），
这样文件管理页能直接编辑，用户也能方便地分享角色卡。
"""

from __future__ import annotations

from .builtin import BUILTIN_BY_ID, BUILTIN_PERSONAS, BuiltinPersona, seed_cards
from .card import (
    MAX_EXAMPLES,
    MAX_TEXT_LENGTH,
    PERSONA_ID_PATTERN,
    PersonaCard,
    normalize_persona_id,
)
from .manager import PersonaManager, PersonaNotFoundError
from .store import (
    FORMAT_JSON,
    META_FILENAME,
    PersonaStore,
)

__all__ = [
    "BUILTIN_BY_ID",
    "BUILTIN_PERSONAS",
    "BuiltinPersona",
    "seed_cards",
    "MAX_EXAMPLES",
    "MAX_TEXT_LENGTH",
    "PERSONA_ID_PATTERN",
    "PersonaCard",
    "normalize_persona_id",
    "PersonaManager",
    "PersonaNotFoundError",
    "FORMAT_JSON",
    "META_FILENAME",
    "PersonaStore",
]