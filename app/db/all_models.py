"""
此文件用于 Alembic 自动发现所有模型。
在创建新的模型时，需要在这里导入。
"""

from app.db.base import Base  # noqa
from app.models.user import User  # noqa

# 导入所有模型，使其对 Alembic 可见
