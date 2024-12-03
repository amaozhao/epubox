from typing import Dict, Any, Optional
import httpx
from enum import Enum
import base64
from Crypto.Cipher import AES
import json

from app.core.config import settings
from app.services.user.oauth.base import (
    OAuthProvider,
    OAuthConfig,
    OAuthError,
    OAuthUserInfo,
    OAuthState,
)


class WeChatAuthType(str, Enum):
    """微信授权类型"""

    QR_CODE = "qrcode"  # 扫码登录（开放平台）
    OFFICIAL = "official"  # 公众号授权
    MINI_PROGRAM = "mini"  # 小程序登录


class WeChatOAuth(OAuthProvider):
    name = "wechat"

    # 开放平台（扫码登录）
    QR_AUTHORIZE_URL = "https://open.weixin.qq.com/connect/qrconnect"
    # 公众号（网页授权）
    MP_AUTHORIZE_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"

    # 通用接口
    ACCESS_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
    REFRESH_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/refresh_token"
    USER_INFO_URL = "https://api.weixin.qq.com/sns/userinfo"

    # 小程序接口
    MINI_CODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"

    def __init__(self, auth_type: WeChatAuthType = WeChatAuthType.QR_CODE):
        self.auth_type = auth_type
        self.timeout = settings.WECHAT_API_TIMEOUT

        # 根据授权类型选择不同的配置
        if auth_type == WeChatAuthType.MINI_PROGRAM:
            app_id = settings.WECHAT_MINI_APP_ID
            app_secret = settings.WECHAT_MINI_APP_SECRET
            scopes = []  # 小程序不需要scope
            redirect_uri = None
        elif auth_type == WeChatAuthType.OFFICIAL:
            app_id = settings.WECHAT_MP_APP_ID
            app_secret = settings.WECHAT_MP_APP_SECRET
            scopes = ["snsapi_userinfo"]
            redirect_uri = (
                f"{settings.SERVER_HOST}{settings.API_V1_STR}/auth/wechat/callback"
            )
        else:  # WeChatAuthType.QR_CODE
            app_id = settings.WECHAT_APP_ID
            app_secret = settings.WECHAT_APP_SECRET
            scopes = ["snsapi_login"]
            redirect_uri = (
                settings.WECHAT_REDIRECT_URI
                or f"{settings.SERVER_HOST}{settings.API_V1_STR}/auth/wechat/callback"
            )

        config = OAuthConfig(
            client_id=app_id,
            client_secret=app_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )
        super().__init__(config)

    async def get_authorization_url(self, state: OAuthState) -> str:
        """获取授权URL（小程序模式下不需要）"""
        if self.auth_type == WeChatAuthType.MINI_PROGRAM:
            raise OAuthError(
                self.name,
                "invalid_auth_type",
                "Mini program doesn't need authorization url",
            )

        # 基础参数
        params = {
            "appid": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": self.config.scopes[0],  # 微信的scope只能传一个
            "state": state.state,
        }

        # 选择授权URL
        base_url = (
            self.QR_AUTHORIZE_URL
            if self.auth_type == WeChatAuthType.QR_CODE
            else self.MP_AUTHORIZE_URL
        )

        # 微信要求在URL最后加上 #wechat_redirect
        return f"{base_url}?{'&'.join(f'{k}={v}' for k, v in params.items())}#wechat_redirect"

    async def code2session(self, code: str) -> Dict[str, Any]:
        """小程序登录凭证校验"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                self.MINI_CODE2SESSION_URL,
                params={
                    "appid": self.config.client_id,
                    "secret": self.config.client_secret,
                    "js_code": code,
                    "grant_type": "authorization_code",
                },
            )

            if response.status_code != 200:
                raise OAuthError(
                    self.name,
                    "code2session_error",
                    f"Failed to get session info: {response.text}",
                )

            data = response.json()
            if "errcode" in data and data["errcode"] != 0:
                raise OAuthError(
                    self.name,
                    str(data["errcode"]),
                    data.get("errmsg", "Unknown error"),
                )

            return data

    def decrypt_user_info(
        self, session_key: str, encrypted_data: str, iv: str
    ) -> Dict[str, Any]:
        """解密小程序用户信息"""
        try:
            # Base64解码
            session_key = base64.b64decode(session_key)
            encrypted_data = base64.b64decode(encrypted_data)
            iv = base64.b64decode(iv)

            # 解密
            cipher = AES.new(session_key, AES.MODE_CBC, iv)
            decrypted = cipher.decrypt(encrypted_data)

            # 去除补位符
            pad = decrypted[-1]
            if isinstance(pad, int):
                pad_size = pad
            else:
                pad_size = ord(pad)
            decrypted = decrypted[:-pad_size]

            # 解析JSON
            user_info = json.loads(decrypted)

            # 校验appid
            if user_info["watermark"]["appid"] != self.config.client_id:
                raise OAuthError(
                    self.name,
                    "invalid_watermark",
                    "Invalid watermark in decrypted data",
                )

            return user_info

        except Exception as e:
            raise OAuthError(
                self.name,
                "decrypt_error",
                f"Failed to decrypt user info: {str(e)}",
            )

    async def get_access_token(self, code: str) -> Dict[str, Any]:
        """获取访问令牌（小程序模式下获取session_key）"""
        if self.auth_type == WeChatAuthType.MINI_PROGRAM:
            return await self.code2session(code)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                self.ACCESS_TOKEN_URL,
                params={
                    "appid": self.config.client_id,
                    "secret": self.config.client_secret,
                    "code": code,
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
            if "errcode" in data and data["errcode"] != 0:
                raise OAuthError(
                    self.name,
                    str(data["errcode"]),
                    data.get("errmsg", "Unknown error"),
                )

            return data

    async def refresh_access_token(self, refresh_token: str) -> Dict[str, Any]:
        """刷新访问令牌"""
        if self.auth_type == WeChatAuthType.MINI_PROGRAM:
            raise OAuthError(
                self.name,
                "invalid_auth_type",
                "Mini program doesn't support token refresh",
            )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                self.REFRESH_TOKEN_URL,
                params={
                    "appid": self.config.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )

            if response.status_code != 200:
                raise OAuthError(
                    self.name,
                    "refresh_token_error",
                    f"Failed to refresh access token: {response.text}",
                )

            data = response.json()
            if "errcode" in data and data["errcode"] != 0:
                raise OAuthError(
                    self.name,
                    str(data["errcode"]),
                    data.get("errmsg", "Unknown error"),
                )

            return data

    async def get_user_info(self, access_token: str, openid: str) -> OAuthUserInfo:
        """获取用户信息（小程序模式下不使用此方法）"""
        if self.auth_type == WeChatAuthType.MINI_PROGRAM:
            raise OAuthError(
                self.name,
                "invalid_auth_type",
                "Mini program should use decrypt_user_info instead",
            )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                self.USER_INFO_URL,
                params={
                    "access_token": access_token,
                    "openid": openid,
                    "lang": "zh_CN",
                },
            )

            if response.status_code != 200:
                raise OAuthError(
                    self.name,
                    "user_info_error",
                    f"Failed to get user info: {response.text}",
                )

            data = response.json()
            if "errcode" in data and data["errcode"] != 0:
                raise OAuthError(
                    self.name,
                    str(data["errcode"]),
                    data.get("errmsg", "Unknown error"),
                )

            return self._create_user_info(data)

    def _create_user_info(self, data: Dict[str, Any]) -> OAuthUserInfo:
        """创建统一的用户信息对象"""
        # 用户标识处理：优先使用unionid，没有则使用openid
        user_id = data.get("unionid") or data["openid"]

        return OAuthUserInfo(
            provider=self.name,
            provider_user_id=user_id,
            username=data.get("nickname", ""),
            avatar_url=data.get("avatarUrl") or data.get("headimgurl"),  # 兼容小程序
            raw_data=data,
            # 微信特有字段
            unionid=data.get("unionid"),
            openid=data["openid"],
        )

    async def verify_and_process(
        self, code: str, state: str = None, encrypted_data: str = None, iv: str = None
    ) -> Dict[str, Any]:
        """验证并处理OAuth回调"""
        if self.auth_type == WeChatAuthType.MINI_PROGRAM:
            if not encrypted_data or not iv:
                raise OAuthError(
                    self.name,
                    "missing_parameters",
                    "Mini program login requires encrypted_data and iv",
                )

            # 1. 获取session_key和openid
            session_data = await self.code2session(code)
            session_key = session_data["session_key"]

            # 2. 解密用户信息
            user_data = self.decrypt_user_info(session_key, encrypted_data, iv)
            user_info = self._create_user_info(user_data)

            return {
                "session_key": session_key,
                "user_info": user_info,
            }
        else:
            # 1. 获取访问令牌和OpenID
            token_data = await self.get_access_token(code)
            access_token = token_data["access_token"]
            openid = token_data["openid"]

            # 2. 获取用户信息
            user_info = await self.get_user_info(access_token, openid)

            # 3. 返回认证结果
            return {
                "access_token": access_token,
                "refresh_token": token_data.get("refresh_token"),
                "expires_in": token_data.get("expires_in"),
                "user_info": user_info,
            }
