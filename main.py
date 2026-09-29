"""One command to prepare, translate, verify, and publish an EPUB."""

# Typer declares arguments and options with callable defaults.
# ruff: noqa: B008

from pathlib import Path
from typing import Any

import typer

from engine.cli import RunOutcome, resume_book, translate_book
from engine.services.resume_plan import plan_resume

app = typer.Typer()


@app.command("translate", help="完整翻译一本 EPUB，并保存可恢复的 JSON 进度。")
def translate(
    epub_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True),
    language: str = typer.Option("Chinese", "--language", "-lg"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    work_root: Path = typer.Option(Path("work"), "--work-root"),
    glossary: Path | None = typer.Option(None, "--glossary", exists=True, dir_okay=False),
    auto_extract: bool = typer.Option(True, "--auto-extract/--no-auto-extract"),
    provider: str = typer.Option("agnes", "--provider"),
    context_tokens: int = typer.Option(32768, "--context-tokens", min=1),
    max_output_tokens: int = typer.Option(4096, "--max-output-tokens", min=1),
    http_limit: int = typer.Option(0, "--http-limit", min=0),
    concurrency: int = typer.Option(2, "--concurrency", min=1),
    epubcheck: str | None = typer.Option(None, "--epubcheck-command"),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    if language.lower().replace("_", "-") not in {"chinese", "zh", "zh-cn", "zh-hans", "simplified chinese"}:
        raise typer.BadParameter("目前仅支持译为简体中文（zh-Hans）")
    try:
        result = translate_book(
            epub_path,
            output=output,
            work_root=work_root,
            glossary=glossary,
            auto_extract=auto_extract,
            provider=provider,
            context_tokens=context_tokens,
            max_output_tokens=max_output_tokens,
            http_limit=http_limit,
            concurrency=concurrency,
            epubcheck=epubcheck,
            overwrite=overwrite,
            progress=_progress_printer(),
        )
    except Exception as error:
        typer.echo(f"翻译未完成：{error}", err=True)
        raise typer.Exit(1) from error
    _print_result(result)


@app.command("resume", help="从工作目录中的 JSON 继续同一轮翻译。")
def resume(
    work_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    output: Path = typer.Option(..., "--output", "-o"),
    epubcheck: str | None = typer.Option(None, "--epubcheck-command"),
    overwrite: bool = typer.Option(False, "--overwrite"),
    repair_file: Path | None = typer.Option(None, "--repair-file", exists=True, dir_okay=False),
    retry_unit: list[str] = typer.Option([], "--retry-unit"),
    add_unit_http: int = typer.Option(0, "--add-unit-http", min=0),
    add_run_http: int = typer.Option(0, "--add-run-http", min=0),
    retry_check: list[str] = typer.Option([], "--retry-check"),
    add_check_http: int = typer.Option(0, "--add-check-http", min=0),
    authorization_id: str | None = typer.Option(None, "--authorization-id"),
) -> None:
    try:
        result = resume_book(
            work_dir,
            output=output,
            epubcheck=epubcheck,
            overwrite=overwrite,
            progress=_progress_printer(),
            repair_file=repair_file,
            retry_units=tuple(retry_unit),
            add_unit_http=add_unit_http,
            add_run_http=add_run_http,
            retry_checks=tuple(retry_check),
            add_check_http=add_check_http,
            authorization_id=authorization_id,
        )
    except Exception as error:
        typer.echo(f"续跑未完成：{error}", err=True)
        raise typer.Exit(1) from error
    _print_result(result)


@app.command("plan-resume", help="只读查看工作目录的下一步动作，不发起模型请求。")
def preview_resume(work_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True)) -> None:
    try:
        preview = plan_resume(work_dir)
    except Exception as error:
        typer.echo(f"恢复预览失败：{error}", err=True)
        raise typer.Exit(1) from error
    typer.echo(f"阶段：{preview.phase}；状态：{preview.status}")
    for action in preview.actions:
        typer.echo(f"下一步：{action}")
    for reason in preview.reasons:
        typer.echo(f"原因：{reason}")


def _print_result(result: RunOutcome) -> None:
    typer.echo(f"状态：{result.status}；阶段：{result.phase}；工作目录：{result.work_dir}")
    if result.report_path:
        typer.echo(f"报告：{result.report_path}")
    if result.required_units:
        typer.echo(f"已接受：{result.accepted_units}/{result.required_units}；累计HTTP：{result.http_attempts}")
    if result.status == "completed":
        typer.echo(f"输出：{result.output_path}")
        return
    if result.reason:
        typer.echo(result.reason, err=True)
    raise typer.Exit(1)


def _progress_printer():
    last: tuple[object, ...] | None = None

    def show(report: dict[str, Any]) -> None:
        nonlocal last
        phase = str(report.get("phase", "running"))
        planned = report.get("planned", report.get("required_units", 0))
        succeeded = report.get("succeeded", report.get("accepted_units", 0))
        failed = report.get("failed", report.get("needs_attention_units", 0))
        attempts = report.get("http_attempts", 0)
        if any(type(value) is not int for value in (planned, succeeded, failed, attempts)):
            return
        bucket = succeeded * 20 // max(1, planned)
        key = (phase, bucket, failed, attempts // 50, report.get("execution_state"))
        if key == last:
            return
        last = key
        typer.echo(f"{phase}: {succeeded}/{planned} 完成，局部问题={failed}，HTTP={attempts}")

    return show


if __name__ == "__main__":
    app()
