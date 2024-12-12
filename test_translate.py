import asyncio
import sys
from pathlib import Path

from app.db.models import LimitType
from app.db.models import TranslationProvider as TranslationProviderModel
from app.html.processor import HTMLProcessor
from app.translation.providers.mistral import MistralProvider


async def main():
    # 创建 MistralProvider 实例
    provider_model = TranslationProviderModel(
        name="mistral",
        limit_type=LimitType.TOKENS,
        limit_value=6000,
        config={
            "api_key": "Hmpty6LRYAgJ28YIDr837aNgLg5JVfnD",  # 替换为你的 API key
            "model": "mistral-large-latest",
        },
    )
    translator = MistralProvider(provider_model)

    # 创建 HTMLProcessor 实例
    processor = HTMLProcessor(translator, "en", "zh")

    # 读取测试 HTML 文件
    html_path = Path("test.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    try:
        # 处理 HTML 内容
        translated_content = await processor.process(html_content)
        print("Translation successful!")

    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        raise


if __name__ == "__main__":
    asyncio.run(main())
