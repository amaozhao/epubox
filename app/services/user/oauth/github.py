from typing import Optional, Dict, Any
import httpx
from datetime import datetime, timedelta

from app.core.config import settings
from app.services.user.oauth.base import OAuthConfig, OAuthProvider, OAuthState, OAuthUserInfo, OAuthError


class GitHubOAuth(OAuthProvider):
    """GitHub OAuth 服务"""

    name = "github"

    AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    TOKEN_URL = "https://github.com/login/oauth/access_token"
    USER_INFO_URL = "https://api.github.com/user"
    USER_EMAIL_URL = "https://api.github.com/user/emails"

    def __init__(self):
        config = OAuthConfig(
            client_id=settings.GITHUB_CLIENT_ID,
            client_secret=settings.GITHUB_CLIENT_SECRET,
            redirect_uri=f"{settings.SERVER_HOST}{settings.API_V1_STR}/auth/github/callback",
            scopes=["read:user", "user:email"],
        )
        super().__init__(config)

    async def get_authorization_url(self, state: OAuthState) -> str:
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state.state,
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
                },
                headers={"Accept": "application/json"},
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
            # 获取用户基本信息
            headers = {
                "Authorization": f"token {access_token}",
                "Accept": "application/json",
            }
            response = await client.get(self.USER_INFO_URL, headers=headers)

            if response.status_code != 200:
                raise OAuthError(
                    self.name,
                    "user_info_error",
                    f"Failed to get user info: {response.text}",
                )

            user_data = response.json()

            # 获取用户邮箱
            email_response = await client.get(self.USER_EMAIL_URL, headers=headers)
            email = None
            if email_response.status_code == 200:
                emails = email_response.json()
                # 获取主要的且已验证的邮箱
                primary_email = next(
                    (e for e in emails if e.get("primary") and e.get("verified")),
                    None,
                )
                if primary_email:
                    email = primary_email["email"]

            return OAuthUserInfo(
                provider=self.name,
                provider_user_id=str(user_data["id"]),
                email=email,
                username=user_data["login"],
                avatar_url=user_data.get("avatar_url"),
                raw_data=user_data,
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
