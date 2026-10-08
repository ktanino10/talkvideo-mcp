import asyncio
import json
import struct
import subprocess
import sys

import pytest

from talkvideo_mcp.audio import DiagnosticTone, encode_wav, parse_wav
from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.media import (
    inspect_video,
    media_tools,
    normalize_source_wav,
    render_diagnostic_video,
    run_bounded,
)
from talkvideo_mcp.models import AudioSettings


@pytest.mark.skipif(media_tools() is None, reason="Local FFmpeg/ffprobe not installed")
async def test_real_local_diagnostic_mux_and_probe():
    wav = await DiagnosticTone().synthesize("synthetic diagnostic only", AudioSettings())
    info, _ = parse_wav(wav)
    video, probe = await render_diagnostic_video(wav, info.duration_seconds)
    assert len(video) > 1000
    assert {stream.codec_type for stream in probe.streams} == {"audio", "video"}
    assert probe.decode is not None and probe.decode.decoded_frames > 0
    assert probe.decode.frame_rate == "25/1"


@pytest.mark.skipif(media_tools() is None, reason="Local FFmpeg/ffprobe not installed")
async def test_probeable_header_does_not_hide_corrupt_media_payload():
    wav = await DiagnosticTone().synthesize("synthetic diagnostic pattern " * 3, AudioSettings())
    info, _ = parse_wav(wav)
    video, _ = await render_diagnostic_video(wav, info.duration_seconds)
    damaged = bytearray(video)
    offset = 0
    found = False
    while offset + 8 <= len(damaged):
        size = struct.unpack_from(">I", damaged, offset)[0]
        kind = damaged[offset + 4 : offset + 8]
        assert size >= 8
        if kind == b"mdat":
            start = offset + 8 + (size - 8) // 4
            damaged[start : offset + size] = bytes(offset + size - start)
            found = True
            break
        offset += size
    assert found
    raw_header = await run_bounded(
        [
            media_tools()[1],
            "-v",
            "quiet",
            "-protocol_whitelist",
            "pipe",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            "pipe:0",
        ],
        data=bytes(damaged),
        max_stdout=32768,
        wall_seconds=15,
    )
    assert len(json.loads(raw_header)["streams"]) == 2
    with pytest.raises(TalkVideoError, match="video_decode_failed"):
        await inspect_video(bytes(damaged), info.duration_seconds)


@pytest.mark.skipif(media_tools() is None, reason="Local FFmpeg/ffprobe not installed")
async def test_explicit_source_wav_normalization_has_exact_frame_evidence():
    source = encode_wav(bytes(4410 * 4), rate=44100, channels=2, width=2)
    with pytest.raises(TalkVideoError, match="audio_normalization_required"):
        await normalize_source_wav(source, AudioSettings(), allow_normalization=False)
    normalized, raw_info, info, method = await normalize_source_wav(
        source, AudioSettings(), allow_normalization=True
    )
    assert raw_info.frames == 4410 and raw_info.sample_rate == 44100
    assert raw_info.channels == 2
    assert info.frames == 1600 and info.sample_rate == 16000 and info.channels == 1
    assert parse_wav(normalized)[0] == info
    assert method == "ffmpeg_pcm_s16le" and normalized != source


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
