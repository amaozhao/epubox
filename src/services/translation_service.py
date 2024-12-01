from abc import ABC, abstractmethod


class TranslationService(ABC):
    """翻译服务的抽象基类"""

    @abstractmethod
    async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """
        翻译文本

        Args:
            text: 要翻译的文本
            source_lang: 源语言代码 (如 'zh', 'en')
            target_lang: 目标语言代码

        Returns:
            翻译后的文本

        Raises:
            TranslationError: 翻译过程中的错误
        """
        pass

    @abstractmethod
    def get_token_limit(self) -> int:
        """
        获取翻译服务的token限制

        Returns:
            单次翻译请求的最大token数
        """
        pass


class TranslationError(Exception):
    """翻译过程中的错误"""

    pass
