from enum import Enum


# 定义翻译状态的枚举
class TranslationStatus(str, Enum):
    """
    表示文本块的翻译状态。
    """

    PENDING = "pending"  # 待翻译
    TRANSLATED = "translated"  # 已翻译（待校对）
    COMPLETED = "completed"  # 已完成（翻译+校对）
    UNTRANSLATED = "untranslated"  # 翻译失败，保留原文（可手动翻译）
