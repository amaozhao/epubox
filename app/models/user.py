# app/models/user.py
from fastapi_users.db import SQLAlchemyBaseUserTableUUID
from app.core.database import BaseModel


class User(SQLAlchemyBaseUserTableUUID, BaseModel):
    pass
