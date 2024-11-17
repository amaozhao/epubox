import asyncio


class TranslationManager:
    """
    管理并发的翻译任务，支持批量翻译。
    """

    def __init__(self, translator, max_concurrent_tasks=2):
        """
        :param translator: 翻译服务实例
        :param max_concurrent_tasks: 最大并发任务数量
        """
        self.translator = translator
        self.semaphore = asyncio.Semaphore(max_concurrent_tasks)  # 限制并发数量

    async def translate_batch(self, texts):
        """
        批量翻译任务，使用并发请求进行翻译，并限制并发数。
        :param texts: 要翻译的文本列表
        :return: 翻译后的文本列表
        """
        tasks = [self._translate_with_semaphore(text) for text in texts]
        return await asyncio.gather(*tasks)

    async def _translate_with_semaphore(self, text):
        """
        包装翻译任务，确保每个翻译任务在 Semaphore 限制内执行。
        :param text: 需要翻译的文本
        :return: 翻译后的文本
        """
        async with self.semaphore:  # 限制并发请求数量
            return await self.translator.translate(text)
