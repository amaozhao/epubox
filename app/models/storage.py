from sqlalchemy.sql import func
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import BaseModel
from datetime import datetime


class EpubFile(BaseModel):
    __tablename__ = "epub_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="pending")
    created: Mapped[datetime] = mapped_column(server_default=func.now())
    updated: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    deleted: Mapped[bool] = mapped_column(default=False)

    def __repr__(self) -> str:
        return f"Epub(id={self.id})"
