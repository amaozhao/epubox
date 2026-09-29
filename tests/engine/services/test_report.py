import json
from pathlib import Path

from engine.services.report import write_report
from tests.engine.services.test_store import _prepare


def test_paused_preparation_report_does_not_invent_book_or_cost(tmp_path: Path) -> None:
    store, _ = _prepare(tmp_path)

    report_path = write_report(store, status="paused", phase="terms", reason="run limit")
    report = json.loads(report_path.read_text())

    assert report["status"] == "paused"
    assert report["source_units"] == 1
    assert report["required_units"] is None
    assert report["accepted_units"] == 0
    assert report["http"]["actual_attempts"] == 0
    assert report["output_path"] is None
    assert report["reader_check"] == "not_run"
