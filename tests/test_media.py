import asyncio
import subprocess
import sys

import pytest

from talkvideo_mcp.audio import DiagnosticTone, parse_wav
from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.media import media_tools, render_diagnostic_video, run_bounded
from talkvideo_mcp.models import AudioSettings


@pytest.mark.skipif(media_tools() is None, reason="Local FFmpeg/ffprobe not installed")
async def test_real_local_diagnostic_mux_and_probe():
    wav = await DiagnosticTone().synthesize("synthetic diagnostic only", AudioSettings())
    info, _ = parse_wav(wav)
    video, probe = await render_diagnostic_video(wav, info.duration_seconds)
    assert len(video) > 1000
    assert {stream.codec_type for stream in probe.streams} == {"audio", "video"}


async def test_subprocess_output_and_time_are_bounded():
    with pytest.raises(TalkVideoError, match="process_output_limit"):
        await run_bounded([sys.executable, "-c", "print('x' * 100000)"], max_stdout=50)
    with pytest.raises(TalkVideoError, match="media_timeout"):
        await run_bounded([sys.executable, "-c", "import time; time.sleep(10)"], wall_seconds=0.05)


async def test_flooding_sigterm_resistant_child_keeps_cleanup_bounded():
    child = (
        "import os,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "while True: os.write(1,b'x'*65536)\n"
    )
    async with asyncio.timeout(8):
        with pytest.raises(TalkVideoError, match="process_output_limit"):
            await run_bounded([sys.executable, "-c", child], max_stdout=32, wall_seconds=20)


async def test_cancel_reaps_child(monkeypatch):
    processes = []
    started = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def observe(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe)
    task = asyncio.create_task(run_bounded([sys.executable, "-c", "import time; time.sleep(10)"]))
    async with asyncio.timeout(5):
        await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None


async def test_parent_exit_cannot_leave_descendant_holding_pipes(tmp_path):
    marker = tmp_path / "child.pid"
    child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
    parent = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid))"
    )
    with pytest.raises(TalkVideoError, match="media_timeout"):
        await run_bounded([sys.executable, "-c", parent, str(marker), child], wall_seconds=0.3)
    pid = int(marker.read_text())
    # An orphan can briefly be a zombie until the OS reaps it, but must not remain running.
    completed = await asyncio.to_thread(
        subprocess.run,
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        timeout=5,
        check=False,
    )
    result = completed.stdout
    assert not result.strip() or result.strip().startswith(b"Z")
