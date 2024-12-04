from fastapi_users import FastAPIUsers

from app.auth.backend import auth_backend
from app.auth.manager import get_user_manager
from app.db.models import User

# FastAPI Users 实例
fastapi_users = FastAPIUsers[User, int](
    get_user_manager,
    [auth_backend],
)

# 常用依赖
current_active_user = fastapi_users.current_user(active=True)
current_superuser = fastapi_users.current_user(active=True, superuser=True)
