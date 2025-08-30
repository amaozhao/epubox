from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .chunk import Chunk


class EpubBook(BaseModel):
    name: str
    path: str
    extract_path: str
    items: List["EpubItem"] = Field(default_factory=list)


class EpubItem(BaseModel):
    """
    EPUB 中单个文档的元数据。
    """

    id: str
    path: str
    content: str
    translated: Optional[str] = None
    placeholder: Optional[Dict[str, str]] = None  # 用于存储占位符信息
    chunks: Optional[List[Chunk]] = None  # 用于存储分块信息
