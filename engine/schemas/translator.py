from enum import Enum


# 定义翻译状态的枚举
class TranslationStatus(str, Enum):
    """
    表示文本块的翻译状态。
    """

    PENDING = "pending"  # 待翻译
    TRANSLATED = "translated"  # 已翻译（待校对）
    ACCEPTED_AS_IS = "accepted_as_is"  # 接受原文作为最终输出
    COMPLETED = "completed"  # 已完成（翻译+校对）
    TRANSLATION_FAILED = "translation_failed"  # 翻译失败，保留原文（可手动翻译）
    WRITEBACK_FAILED = "writeback_failed"  # 已翻译但无法安全回写到最终输出
