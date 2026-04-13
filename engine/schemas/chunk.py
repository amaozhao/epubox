from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from .translator import TranslationStatus


class NavTextTarget(BaseModel):
    """导航文本节点写回定位信息。"""

    marker: str = Field(..., description="导航文本片段在 chunk 文本中的唯一标记")
    xpath: str = Field(..., description="文本节点父元素 xpath")
    text_index: int = Field(..., description="父元素中可翻译文本节点序号（从 0 开始）")
    original_text: str = Field(..., description="提取时的原始文本（用于调试和兜底）")


class Chunk(BaseModel):
    """
    用于翻译的文本分块数据模型。
    """

    name: str = Field(..., description="在章节中的唯一占位符名字")
    original: str = Field(..., description="需要翻译的原始文本")
    translated: Optional[str] = Field(None, description="翻译后的文本")
    status: Optional[TranslationStatus] = TranslationStatus.PENDING
    tokens: int = Field(0, description="当前chunk估算的token数")
    chunk_mode: Literal["html_fragment", "nav_text"] = Field(
        "html_fragment",
        description="chunk 载荷模式：常规 HTML 片段 或 导航文本模式",
    )
    # chunk 内各元素在原始 DOM 中的 xpath 路径，用于追踪翻译结果回写位置
    xpaths: List[str] = Field(default_factory=list, description="chunk 内各元素的 xpath 路径列表")
    nav_targets: List[NavTextTarget] = Field(default_factory=list, description="导航文本模式下的写回目标列表")
