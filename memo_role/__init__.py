"""memo-role：本地离线多模型角色扮演机器人。

模块划分（按核心对话 → 扩展功能逐步迭代）：

- ``config``    配置加载与合并
- ``logging_setup`` 统一日志
- ``db``        SQLite 连接与建表（轻量、单文件、适合安卓 PRoot）
- ``inference``  推理层（llama-server / llama-cpp-python / OpenAI 兼容 API）
- ``memory``     三层记忆系统（工作记忆 / 情节记忆 / 核心记忆）+ 向量召回
- ``persona``    人设卡片管理（保存、切换、导入导出）
- ``dialogue``   对话编排（人设 + 记忆 + 模型 → 回复）
- ``adapters``   外部渠道适配（NapCat / OneBot v11）
- ``web``        Web 对话页与管理后台（FastAPI + 原生 HTML/JS）
- ``files``      可视化文件管理（受限于项目根目录的沙箱）
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]