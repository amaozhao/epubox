from typing import List, Optional

from pydantic import BaseModel, Field

from .translator import TranslationStatus


class Chunk(BaseModel):
    """
    用于翻译的文本分块数据模型。
    """

    name: str = Field(..., description="在章节中的唯一占位符名字")
    original: str = Field(..., description="需要翻译的原始文本")
    translated: Optional[str] = Field(None, description="翻译后的文本")
    status: Optional[TranslationStatus] = TranslationStatus.PENDING
    tokens: int = Field(0, description="当前chunk估算的token数")
    # chunk 内各元素在原始 DOM 中的 xpath 路径，用于追踪翻译结果回写位置
    xpaths: List[str] = Field(default_factory=list, description="chunk 内各元素的 xpath 路径列表")
