"""One command to prepare, translate, verify, and publish an EPUB."""

# Typer declares arguments and options with callable defaults.
# ruff: noqa: B008

import json
from pathlib import Path
from typing import Any, cast

import typer

from engine.cli import RunOutcome, resume_book, translate_book
from engine.services import state
from engine.services.atomic import StoreLocked
from engine.services.resume import plan_resume

app = typer.Typer()


@app.command("translate", help="完整翻译一本 EPUB，未完成时自动续跑，最多 5 轮（含首次）。")
def translate(
    ctx: typer.Context,
    epub_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True),
    language: str = typer.Option("Chinese", "--language", "-lg"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    work_root: Path | None = typer.Option(None, "--work-root", help="工作资料目录，默认在原书旁创建同名目录。"),
    glossary: Path | None = typer.Option(None, "--glossary", exists=True, dir_okay=False),
    auto_extract: bool = typer.Option(True, "--auto-extract/--no-auto-extract"),
    provider: str = typer.Option("agnes", "--provider"),
    context_tokens: int = typer.Option(32768, "--context-tokens", min=1),
    max_input_tokens: int = typer.Option(50000, "--max-input-tokens", min=1),
    max_output_tokens: int = typer.Option(8192, "--max-output-tokens", min=1),
    limit: int | None = typer.Option(
        None, "--limit", min=1, help="单个源片段可翻译正文的最大 token 数；默认读取环境配置。"
    ),
    http_limit: int = typer.Option(0, "--http-limit", min=0),
    concurrency: int = typer.Option(2, "--concurrency", min=1),
    epubcheck: str | None = typer.Option(None, "--epubcheck-command"),
    overwrite: bool = typer.Option(False, "--overwrite"),
    repair_terms: bool = typer.Option(False, "--repair-terms"),
) -> None:
    if language.lower().replace("_", "-") not in {"chinese", "zh", "zh-cn", "zh-hans", "simplified chinese"}:
        raise typer.BadParameter("目前仅支持译为简体中文（zh-Hans）")
    explicit = frozenset(
        name
        for name in (
            "glossary",
            "auto_extract",
            "provider",
            "context_tokens",
            "max_input_tokens",
            "max_output_tokens",
            "limit",
            "http_limit",
            "concurrency",
            "repair_terms",
        )
        if getattr(ctx.get_parameter_source(name), "name", None) == "COMMANDLINE"
    )
    for attempt in range(1, 6):
        try:
            result = translate_book(
                epub_path,
                output=output,
                work_root=work_root,
                glossary=glossary,
                auto_extract=auto_extract,
                provider=provider,
                context_tokens=context_tokens,
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
                limit=limit,
                http_limit=http_limit,
                concurrency=concurrency,
                epubcheck=epubcheck,
                overwrite=overwrite,
                repair_terms=repair_terms,
                progress=_progress_printer(),
                explicit_options=explicit,
            )
        except StoreLocked as error:
            typer.echo("同一本书已有翻译进程在运行；请等它退出后，用相同命令继续。", err=True)
            raise typer.Exit(1) from error
        except Exception as error:
            if attempt == 5:
                typer.echo(f"翻译未完成：{error}", err=True)
                raise typer.Exit(1) from error
        else:
            explicit = frozenset()  # Later rounds resume the configuration validated by the first result.
            if result.status == "completed" or attempt == 5:
                _print_result(result)
                return
        typer.echo(f"自动续跑：第 {attempt + 1}/5 轮，复用已保存的进度。")


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
        if result.report_path:
            report = json.loads(state.read(result.work_dir / "report.json"))
            verification = report.get("publication_verification") or {}
            baseline = verification.get("baseline")
            if isinstance(baseline, dict) and baseline.get("inherited_errors"):
                typer.echo(
                    f"成品保留原书 {baseline['inherited_errors']} 个 EPUBCheck 问题，未新增问题；"
                    "EPUBCheck 仍未完全通过。详情见报告。"
                )
        return
    if result.reason and not (result.status == "needs_attention" and result.phase == "translation"):
        typer.echo(result.reason, err=True)
    raise typer.Exit(1)


def _progress_printer():
    last: tuple[object, ...] | None = None

    def show(report: dict[str, Any]) -> None:
        nonlocal last
        phase = str(report.get("phase", "running"))
        if phase == "waiting" or "result_item_id" in report or report.get("event") in {"request", "response"}:
            return
        if isinstance(report.get("notice"), str):
            typer.echo(report["notice"])
            return
        if "request_id" in report:
            values = tuple(
                report.get(key)
                for key in (
                    "request_id",
                    "result_item_id",
                    "result_status",
                    "batch_status",
                    "decision",
                    "revised",
                    "batch_issues",
                    "reason",
                    "accepted_units",
                    "required_items",
                    "translated_items",
                    "reviewed_items",
                    "needs_attention_units",
                    "http_attempts",
                    "input_tokens",
                    "output_tokens",
                    "actual_input_tokens",
                    "actual_output_tokens",
                    "elapsed_seconds",
                )
            )
            key = (phase, report.get("execution_state"), *values)
            if key == last:
                return
            last = key
            decision = report.get("decision")
            result = {
                "no_change": "通过",
                "pass": "通过",
                "replace": "修订",
                "needs_attention": "待处理",
            }.get(
                str(decision),
                report.get("result_status", report.get("batch_status", report.get("execution_state", "-"))),
            )
            issues = report.get("batch_issues")
            reason = report.get("reason")
            if isinstance(reason, dict):
                reason = reason.get("message") or reason.get("code") or str(reason)
            reason = reason or ("；".join(map(str, issues)) if isinstance(issues, (list, tuple)) else None)
            actual = ""
            if type(report.get("actual_input_tokens")) is int or type(report.get("actual_output_tokens")) is int:
                actual = (
                    f"，本次实际输入={report.get('actual_input_tokens', '-')} tokens，"
                    f"本次实际输出={report.get('actual_output_tokens', '-')} tokens"
                )
            source_label = {"body": "正文", "metadata": "元数据", "navigation": "导航", "attribute": "属性"}.get(
                str(report.get("source_channel", "body")), "文本"
            )
            typer.echo(
                f"{phase}: 批次={report.get('request_id', '-')}，结果="
                f"{result}，修订={'是' if report.get('revised') else '否'}，"
                f"完成单元={report.get('accepted_units', 0)}，总项={report.get('required_items', 0)}，"
                f"初译={report.get('translated_items', 0)}，校对={report.get('reviewed_items', 0)}，"
                f"待处理单元={report.get('needs_attention_units', 0)}，"
                f"耗时={float(report.get('elapsed_seconds', 0)):.1f}秒；"
                f"{source_label}估算={report.get('source_tokens', '-')} tokens，"
                f"输入估算={report.get('estimated_input_tokens', '-')} tokens，"
                f"输入预留={report.get('reserved_input_tokens', '-')} tokens，"
                f"输出预留={report.get('reserved_output_tokens', '-')} tokens，"
                f"实际累计输入={report.get('input_tokens', 0)} tokens，"
                f"实际累计输出={report.get('output_tokens', 0)} tokens，HTTP={report.get('http_attempts', 0)}"
                + actual
                + (f"；原因={reason}" if reason else "")
            )
            return
        planned = report.get("planned", report.get("required_units", 0))
        succeeded = report.get("succeeded", report.get("accepted_units", 0))
        failed = report.get("failed", report.get("needs_attention_units", 0))
        attempts = report.get("http_attempts", 0)
        if any(type(value) is not int for value in (planned, succeeded, failed, attempts)):
            return
        details = tuple(
            report.get(key)
            for key in (
                "translated_items",
                "reviewed_items",
                "required_items",
                "retrying_items",
                "waiting_derived_units",
            )
        )
        if all(type(value) is int for value in details):
            translated, reviewed, required_items, retrying, waiting = cast(tuple[int, int, int, int, int], details)
            key = (
                phase,
                succeeded,
                translated,
                reviewed,
                failed,
                attempts,
                report.get("execution_state"),
            )
            if key != last:
                last = key
                typer.echo(
                    f"{phase}: 接受={succeeded}/{planned} 单元，初译={translated}/{required_items} 项，"
                    f"校对={reviewed}/{required_items} 项，导航待依赖={waiting}，重试中={retrying}，"
                    f"局部问题={failed}，累计HTTP={attempts}"
                )
            return
        key = (phase, succeeded, planned, failed, attempts, report.get("execution_state"))
        if key == last:
            return
        last = key
        typer.echo(f"{phase}: {succeeded}/{planned} 完成，局部问题={failed}，HTTP={attempts}")

    return show


if __name__ == "__main__":
    app()
