import httpx
import urllib.parse


class GoogleTranslator:
    """
    Google Translate
    """

    def __init__(self, language, **kwargs) -> None:
        self.api_url = "https://translate.google.com/translate_a/single?client=it&dt=qca&dt=t&dt=rmt&dt=bd&dt=rms&dt=sos&dt=md&dt=gt&dt=ld&dt=ss&dt=ex&otf=2&dj=1&hl=en&ie=UTF-8&oe=UTF-8&sl=auto&tl=zh-CN"
        self.headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "GoogleTranslate/6.29.59279 (iPhone; iOS 15.4; en; iPhone14,2)",
        }
        self.session = (
            httpx.AsyncClient()
        )  # Use httpx Client instead of requests.session
        self.language = language

    async def translate(self, text):
        """
        Perform the translation via Google Translate API.
        :param text: The text to be translated
        :return: Translated text
        """
        return await self._retry_translate(text)

    async def _retry_translate(self, text, timeout=3):
        """
        Retry translating with timeout support.
        :param text: The text to translate
        :param timeout: Timeout in seconds
        :return: Translated text or the original if failed
        """
        time = 0
        while time <= timeout:
            time += 1
            try:
                # Make async request using httpx.AsyncClient
                r = await self.session.post(
                    self.api_url,
                    headers=self.headers,
                    data=f"q={urllib.parse.quote_plus(text)}",
                    timeout=3,
                )
                if r.status_code == 200:
                    print(text)
                    print(r.text)
                    t_text = "".join(
                        [
                            sentence.get("trans", "")
                            for sentence in r.json()["sentences"]
                        ],
                    )
                    return t_text
            except httpx.RequestError as e:
                print(f"Request failed: {e}")
                await asyncio.sleep(3)  # Sleep a bit before retry
        text = text.replace("您", "你")
        text = text.replace("覆盖", "封面")
        text = text.replace("法学硕士", "LLM")
        return text


async def main():
    google_translator = GoogleTranslator(language="zh-CN")
    translated_text = await google_translator.translate("Hello, this is a test.")
    print(translated_text)


if __name__ == "__main__":
    # If you need to call the translate function, use an event loop to run the async function
    import asyncio

    # Run the async main function
    asyncio.run(main())
