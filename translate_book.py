import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn

from services.epub_book.pipeline import PipelineConfig
from services.epub_book.translator import TranslationConfig, TranslationProvider
from utils.logging import get_logger, setup_logging

logger = get_logger(__name__)
console = Console()

def create_layout() -> Layout:
    """Create the layout for the translation progress display."""
    layout = Layout()
    layout.split_column(
        Layout(name="header"),
        Layout(name="body"),
        Layout(name="footer")
    )
    return layout

async def translate_book(input_file: str, output_dir: Path):
    """Translate an EPUB book with progress display."""
    # Pipeline configuration
    pipeline_config = PipelineConfig(
        max_concurrent_requests=1,
        output_dir=output_dir,
        translation_provider=TranslationProvider.MISTRAL,
        source_lang="en",
        target_lang="zh",
        max_chars=2000,
        separator='\n'
    )
    
    # Translation configuration
    translation_config = TranslationConfig(
        provider=TranslationProvider.MISTRAL,
        source_lang="en",
        target_lang="zh",
        max_chars=2000,
        temperature=0.3,
        top_p=0.9
    )
    
    layout = create_layout()
    layout["header"].update(Panel("EPUBox Translation Pipeline", style="bold cyan"))
    
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
    )
    
    layout["body"].update(progress)
    layout["footer"].update(Panel("Press Ctrl+C to cancel", style="italic"))
    
    with Live(layout, refresh_per_second=10, console=console):
        try:
            # Create and run the pipeline
            pipeline = TranslationPipeline(pipeline_config)
            await pipeline.translate_book(input_file)
            
            layout["footer"].update(Panel("✅ Translation completed successfully!", style="bold green"))
            
        except Exception as e:
            logger.error("translation_failed", error=str(e), exc_info=True)
            layout["footer"].update(Panel(f"❌ Translation failed: {str(e)}", style="bold red"))
            raise

def main(
    input_file: str = typer.Argument(..., help="Input EPUB file path"),
    output_dir: str = typer.Option("translated_books", help="Output directory for translated files"),
    log_level: str = typer.Option("INFO", help="Log level (DEBUG, INFO, WARNING, ERROR)"),
):
    """
    Translate an EPUB book from one language to another.
    """
    # Setup logging
    setup_logging(log_level)
    logger.info("starting_translation", input_file=input_file, output_dir=output_dir)
    
    try:
        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(exist_ok=True)
        asyncio.run(translate_book(input_file, output_dir_path))
    except Exception as e:
        logger.error("translation_failed", error=str(e), exc_info=True)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        logger.info("translation_cancelled")
        console.print("\n[yellow]Translation cancelled by user[/yellow]")
        raise typer.Exit(130)

if __name__ == "__main__":
    typer.run(main)
