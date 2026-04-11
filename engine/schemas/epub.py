from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .chunk import Chunk

CHECKPOINT_SCHEMA_VERSION = 2


class EpubBook(BaseModel):
    name: str
    path: str
    extract_path: str
    checkpoint_schema_version: int = CHECKPOINT_SCHEMA_VERSION
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
    preserved_pre: Optional[List[str]] = None  # 用于存储提取的 pre 标签
    preserved_code: Optional[List[str]] = None  # 用于存储提取的 code 标签
    preserved_style: Optional[List[str]] = None  # 用于存储提取的 style 标签
