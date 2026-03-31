from typing import Dict, List, Optional

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
    global_indices: List[int] = Field(default_factory=list, description="全局占位符索引列表")
    local_tag_map: Dict[str, str] = Field(default_factory=dict, description="局部占位符到标签的映射")
