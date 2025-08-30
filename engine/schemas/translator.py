from enum import Enum


# 定义翻译状态的枚举
class TranslationStatus(str, Enum):
    """
    表示文本块的翻译状态。
    """

    PENDING = "pending"  # 待处理
    IN_PROGRESS = "in_progress"  # 正在翻译
    TRANSLATED = "translated"  # 已翻译
    FAILED = "failed"  # 翻译失败
    SKIPPED = "skipped"  # 跳过
    COMPLETED = "completed"  # 完成
