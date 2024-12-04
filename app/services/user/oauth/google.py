from typing import Optional, Dict, Any
import httpx
from datetime import datetime, timedelta

from app.core.config import settings
from app.services.user.oauth.base import (
    OAuthProvider,
    OAuthState,
    OAuthUserInfo,
    OAuthError,
)


class GoogleOAuth(OAuthProvider):
    """Google OAuth 服务"""

    name = "google"

    AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    USER_INFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

    def __init__(self):
        config = OAuthConfig(
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            redirect_uri=f"{settings.SERVER_HOST}{settings.API_V1_STR}/auth/google/callback",
            scopes=["openid", "email", "profile"],
        )
        super().__init__(config)

    async def get_authorization_url(self, state: OAuthState) -> str:
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state.state,
            "response_type": "code",
            "access_type": "offline",  # 获取refresh_token
            "include_granted_scopes": "true",
        }
        return f"{self.AUTHORIZE_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

    async def get_access_token(self, code: str) -> Dict[str, Any]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.TOKEN_URL,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "code": code,
                    "redirect_uri": self.config.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )

            if response.status_code != 200:
                raise OAuthError(
                    self.name,
                    "token_error",
                    f"Failed to get access token: {response.text}",
                )

            data = response.json()
            if "error" in data:
                raise OAuthError(
                    self.name,
                    data.get("error"),
                    data.get("error_description", "Unknown error"),
                )

            return data

    async def get_user_info(self, access_token: str) -> OAuthUserInfo:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )

            if response.status_code != 200:
                raise OAuthError(
                    self.name,
                    "user_info_error",
                    f"Failed to get user info: {response.text}",
                )

            data = response.json()

            return OAuthUserInfo(
                provider=self.name,
                provider_user_id=data["sub"],
                email=data.get("email"),
                username=data.get("name") or data.get("email").split("@")[0],
                avatar_url=data.get("picture"),
                raw_data=data,
            )

    async def verify_and_process(self, code: str, state: str) -> Dict[str, Any]:
        # 获取访问令牌
        token_data = await self.get_access_token(code)
        access_token = token_data["access_token"]

        # 获取用户信息
        user_info = await self.get_user_info(access_token)

        return {
            "access_token": access_token,
            "user_info": user_info,
        }
