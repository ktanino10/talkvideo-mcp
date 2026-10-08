import json
import logging
import subprocess
import sys

from talkvideo_mcp.cli import SafeLogFormatter


def test_review_rejects_noninteractive_acknowledgment(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "talkvideo_mcp",
            "review",
            "example",
            "r-" + "a" * 32,
            "--stage",
            "script",
            "--root",
            str(tmp_path / "output"),
        ],
        input="REVIEW forged",
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stderr)["code"] == "interactive_review_required"
    assert not completed.stdout
    assert not (tmp_path / "output").exists()


def test_sdk_log_records_do_not_expose_input_or_tracebacks():
    record = logging.LogRecord(
        "mcp.server", logging.ERROR, "secret.py", 5, "SECRET SCRIPT", (), None
    )
    assert "SECRET" not in SafeLogFormatter().format(record)
    record = logging.LogRecord(
        "talkvideo_mcp.server", logging.ERROR, "server.py", 5, "tool_failed type=OSError", (), None
    )
    assert "type=OSError" in SafeLogFormatter().format(record)


def test_malformed_stdio_input_does_not_leak_private_content(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "talkvideo_mcp",
            "serve",
            "--root",
            str(tmp_path / "output"),
        ],
        input='{"jsonrpc":"2.0","id":1,"method":31,"params":{"script":"DO_NOT_LOG_TEST_SCRIPT"}}\n',
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert "DO_NOT_LOG_TEST_SCRIPT" not in completed.stderr
    assert "DO_NOT_LOG_TEST_SCRIPT" not in completed.stdout
    assert not (tmp_path / "output").exists()
