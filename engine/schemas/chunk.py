from typing import Optional

from pydantic import BaseModel, Field

from .translator import TranslationStatus


class Chunk(BaseModel):
    """
    用于翻译的文本分块数据模型。
    """

    name: str = Field(..., description="Chunk名称/xpath")
    original: str = Field(..., description="需要翻译的原始HTML")
    translated: Optional[str] = Field(None, description="翻译后的HTML")
    status: Optional[TranslationStatus] = TranslationStatus.PENDING
    tokens: int = Field(0, description="当前chunk估算的token数")
