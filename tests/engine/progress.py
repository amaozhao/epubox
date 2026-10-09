from main import _progress_printer


def test_global_stop_is_visible_once_without_dumping_content_failures(capsys):
    printer = _progress_printer()
    stopped = {
        "phase": "translation",
        "execution_state": "stopped",
        "accepted_units": 3537,
        "required_units": 15408,
        "http_attempts": 1509,
        "stop_reason": "ready dependencies changed during workflow verification",
    }
    printer(stopped)
    printer(stopped)
    output = capsys.readouterr().out
    assert output.count("ready dependencies changed during workflow verification") == 1
    assert "已暂停" in output and "3537/15408" in output
    printer({"phase": "translation", "execution_state": "stopped", "reason": "bulk per-item failures"})
    assert "bulk per-item failures" not in capsys.readouterr().out


def test_completed_workflow_reports_key_health_parallelism_and_wait_breakdown(capsys):
    printer = _progress_printer()
    printer(
        {
            "phase": "workflow",
            "request_id": "tx-test",
            "batch_status": "completed",
            "elapsed_seconds": 1000,
            "runtime": {
                "key_count": 5,
                "verified_keys": 1,
                "enabled_keys": 1,
                "http_active": 0,
                "http_peak": 5,
                "workflow_capacity": 1,
            },
            "workflow_timing": {
                "keys_used": ["agnes-1"],
                "key_wait_seconds": 800,
                "rate_wait_seconds": 10,
                "http_seconds": 120,
                "http_attempts": 3,
            },
        }
    )
    output = capsys.readouterr().out
    assert "key=agnes-1" in output
    assert "已验证key=1/5" in output and "可尝试key=1" in output
    assert "当前接口并发=0/1" in output and "累计接口峰值=5" in output
    assert "等待key=800.0秒" in output and "限速等待=10.0秒" in output
    assert "接口等待=120.0秒（3次）" in output and "本地/调度=70.0秒" in output
    assert len(output.splitlines()) == 1


def test_key_disabled_notice_is_visible_without_per_request_spam(capsys):
    printer = _progress_printer()
    printer({"phase": "translate", "event": "key_state", "notice": "密钥：agnes-2 HTTP 401 已停用。"})
    for event in ("request", "response", "http_start", "http_end"):
        printer({"phase": "translate", "event": event, "request_id": "tx-test"})
    output = capsys.readouterr().out
    assert output.strip() == "密钥：agnes-2 HTTP 401 已停用。"
