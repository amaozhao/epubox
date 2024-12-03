from datetime import datetime
from typing import Any, Dict

from sqlalchemy import Index, func
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    declared_attr,
    mapped_column,
    column_property,
)


class Base(DeclarativeBase):
    """基础模型类"""

    # 主键
    id: Mapped[int] = mapped_column(primary_key=True)

    # 时间戳
    created: Mapped[datetime] = mapped_column(
        server_default=func.now(), comment="创建时间"
    )
    updated: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )

    # 状态
    actived: Mapped[bool] = mapped_column(default=True, comment="是否可用")

    # 表名自动生成
    @declared_attr.directive
    def __tablename__(cls) -> str:
        """将驼峰命名转换为下划线命名作为表名"""
        return cls.__name__.lower()

    # 索引和表配置
    __table_args__ = (
        Index("ix_created", "created"),
        Index("ix_updated", "updated"),
    )

    # 默认查询过滤器
    @classmethod
    def __init_subclass__(cls) -> None:
        """为所有子类添加默认过滤条件"""
        super().__init_subclass__()

        # 添加默认的查询条件
        if hasattr(cls, "__mapper__"):
            cls.__mapper__.add_property(
                "actived_filter", column_property(cls.actived.is_(True), deferred=True)
            )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，用于日志等场景"""
        return {
            column.name: getattr(self, column.name) for column in self.__table__.columns
        }


# 导入所有模型，用于 Alembic
# 当我们创建新的模型时，需要在这里导入
# from app.models.user import User
