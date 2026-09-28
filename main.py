# Typer's declaration API intentionally constructs Argument/Option defaults.
# ruff: noqa: B008
from pathlib import Path

import typer

from engine.services.glossary import GlossaryExtractor

# 初始化 Typer 应用
app = typer.Typer()


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
    limit: int | None = typer.Option(
        None, "--limit", "-l", min=1, help="兼容旧命令；v23 自动规划切片，此参数已弃用。"
    ),
    language: str | None = typer.Option("Chinese", "--language", "-lg", help="目标翻译语言。"),
    preserve_fonts: bool = typer.Option(False, "--preserve-fonts", help="兼容旧命令；v23 默认保留字体。"),
    engine: str = typer.Option("v23", "--engine", help="仅支持 v23；旧翻译入口已移除。"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    work_root: Path = typer.Option(Path("work"), "--work-root"),
    context_tokens: int = typer.Option(32768, "--context-tokens", min=1),
    max_output_tokens: int = typer.Option(4096, "--max-output-tokens", min=1),
    http_limit: int = typer.Option(0, "--http-limit", min=0, help="v23 运行 HTTP 上限；0 按初始计划计算。"),
    concurrency: int = typer.Option(2, "--concurrency", min=1),
    provider: str = typer.Option("agnes", "--provider"),
    glossary: Path | None = typer.Option(None, "--glossary", exists=True, dir_okay=False),
    epubcheck: str | None = typer.Option(None, "--epubcheck-command"),
    overwrite: bool = typer.Option(False, "--overwrite"),
):
    """Translate a complete EPUB with the JSON-backed engine."""
    if engine != "v23":
        raise typer.BadParameter("旧翻译入口已移除；translate 只使用 v23")
    if (language or "Chinese").lower().replace("_", "-") not in {
        "chinese",
        "zh",
        "zh-cn",
        "zh-hans",
        "simplified chinese",
    }:
        raise typer.BadParameter("v23 currently supports English to simplified Chinese (zh-Hans) only")

    typer.echo(f"开始翻译 EPUB 文件: {epub_path.name}")
    typer.echo("翻译引擎: v23；目标语言: 简体中文")
    if limit is not None:
        typer.echo("提示：旧 --limit 已弃用；v23 按上下文预算规划切片。")
    typer.echo("-" * 50)

    from engine.cli_v23 import translate_v23

    try:
        result = translate_v23(
            epub_path,
            output=output,
            work_root=work_root,
            context_tokens=context_tokens,
            max_output_tokens=max_output_tokens,
            http_limit=http_limit,
            concurrency=concurrency,
            provider=provider,
            glossary=glossary,
            epubcheck=epubcheck,
            overwrite=overwrite,
            progress=_v23_progress,
            input_tokens=None,
        )
    except Exception as error:
        typer.echo(f"v23 未完成：{error}", err=True)
        raise typer.Exit(1) from error
    _print_v23_result(result)


def _v23_progress(report: dict) -> None:
    typer.echo(
        f"{report['execution_state']}: {report['completed_units']}/{report['required_units']} 单元完成，"
        f"HTTP={report['http_attempts']}，等待依赖={report['blocked_dependencies']}"
    )


def _print_v23_result(result) -> None:
    typer.echo(f"状态：{result.status}；工作目录：{result.work_dir}")
    if result.report_path:
        typer.echo(f"报告：{result.report_path}")
    if result.status == "completed":
        typer.echo(f"输出：{result.output_path}")
        return
    for issue in result.issues:
        typer.echo(issue.message, err=True)
    raise typer.Exit(1)


@app.command("resume", help="从 v23 工作目录中的完整 JSON 清单续跑。")
def resume(
    work_dir: Path = typer.Argument(..., exists=True, file_okay=False),
    output: Path = typer.Option(..., "--output", "-o"),
    epubcheck: str | None = typer.Option(None, "--epubcheck-command"),
    overwrite: bool = typer.Option(False, "--overwrite"),
    repair_file: Path | None = typer.Option(None, "--repair-file", exists=True, dir_okay=False),
    retry_unit: list[str] = typer.Option([], "--retry-unit"),
    add_unit_http: int = typer.Option(0, "--add-unit-http", min=0),
    add_run_http: int = typer.Option(0, "--add-run-http", min=0),
    retry_check: list[str] = typer.Option([], "--retry-check"),
    add_check_http: int = typer.Option(0, "--add-check-http", min=0),
    repair_document: list[str] = typer.Option([], "--repair-document"),
):
    from engine.cli_v23 import resume_v23

    try:
        result = resume_v23(
            work_dir,
            output=output,
            epubcheck=epubcheck,
            overwrite=overwrite,
            repair_file=repair_file,
            retry_units=retry_unit,
            add_unit_http=add_unit_http,
            add_run_http=add_run_http,
            progress=_v23_progress,
            retry_checks=retry_check,
            add_check_http=add_check_http,
            repair_documents=repair_document,
        )
    except Exception as error:
        typer.echo(f"v23 续跑未完成：{error}", err=True)
        raise typer.Exit(1) from error
    _print_v23_result(result)


@app.command("generate-glossary", help="为指定的 EPUB 文件生成可编辑的术语 JSON 文件。")
def generate_glossary(
    epub_path: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="要提取术语的 EPUB 文件路径。",
    ),
    output_path: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="输出术语表文件的路径（可选，会自动生成）。",
    ),
):
    """为指定的 EPUB 文件生成可编辑的术语 JSON 文件。"""
    typer.echo("-" * 50)
    if output_path:
        typer.echo(f"指定输出路径: {output_path}")

    extractor = GlossaryExtractor()
    generated = extractor.run(
        epub_path=str(epub_path),
        output_path=str(output_path) if output_path else None,
    )
    if not generated:
        typer.echo("术语表未生成；详情请查看上方日志。", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
