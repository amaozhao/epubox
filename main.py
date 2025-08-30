import asyncio
import os
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from engine.core.logger import engine_logger as logger
from engine.orchestrator import Orchestrator

# 初始化 Typer 应用和 Rich 控制台
app = typer.Typer()
console = Console()


@app.command("translate", help="翻译指定的 EPUB 文件")
def translate(
    epub_path: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="待翻译的 EPUB 文件路径。",
    ),
    limit: Optional[int] = typer.Option(1000, "--limit", "-l", help="每个分块的最大 token 数。"),
    language: Optional[str] = typer.Option("Chinese", "--language", "-lg", help="目标翻译语言。"),
):
    """
    翻译指定的 EPUB 文件。
    """
    # 打印开始信息
    console.print(f"[bold green]开始翻译 EPUB 文件: {epub_path.name}[/bold green]")
    console.print(f"[bold]目标语言:[/bold] {language}")
    console.print(f"[bold]分块大小:[/bold] {limit} tokens")
    console.print("-" * 50)

    try:
        # 实例化并运行 Orchestrator
        orchestrator = Orchestrator()
        asyncio.run(
            orchestrator.translate_epub(str(epub_path), limit=limit or 1000, target_language=language or "Chinese")
        )

        # 翻译成功，打印完成信息
        translated_path = os.path.join(os.path.dirname(epub_path), f"{epub_path.stem}-cn.epub")
        console.print("-" * 50)
        console.print(f"[bold green]翻译完成！[/bold green] 新文件已保存至 [bold]{translated_path}[/bold]")

    except Exception as e:
        # 捕获并打印错误
        logger.error(f"翻译过程中发生错误: {e}")
        console.print("-" * 50)
        console.print("[bold red]翻译失败！[/bold red] 详情请查看日志。")
        typer.Exit(1)


if __name__ == "__main__":
    app()
