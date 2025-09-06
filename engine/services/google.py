import re
import urllib.parse

import httpx


class GoogleTranslator:
    """
    Google Translate using httpx async
    """

    def __init__(self) -> None:
        self.api_url = (
            "https://translate.google.com/translate_a/single"
            "?client=it&dt=qca&dt=t&dt=rmt&dt=bd&dt=rms&dt=sos&dt=md&dt=gt"
            "&dt=ld&dt=ss&dt=ex&otf=2&dj=1&hl=en&ie=UTF-8&oe=UTF-8&sl=auto&tl=zh-CN"
        )
        self.headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "GoogleTranslate/6.29.59279 (iPhone; iOS 15.4; en; iPhone14,2)",
        }
        self.client = httpx.AsyncClient()

    async def translate(self, text):
        r = await self.client.post(self.api_url, headers=self.headers, data={"q": urllib.parse.quote(text)})
        if not r.is_success:
            return text
        t_text = "".join([sentence.get("trans", "") for sentence in r.json()["sentences"]])
        return re.sub("\n{3,}", "\n\n", t_text)
