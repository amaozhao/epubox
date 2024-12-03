from fastapi import APIRouter
from . import routes, wechat, google, github, token

router = APIRouter(prefix="/auth", tags=["auth"])
router.include_router(routes.router, tags=["auth"])
router.include_router(wechat.router, prefix="/oauth", tags=["wechat"])
router.include_router(google.router, prefix="/oauth", tags=["google"])
router.include_router(github.router, prefix="/oauth", tags=["github"])
router.include_router(token.router, prefix="/token", tags=["token"])

__all__ = ["router"]
