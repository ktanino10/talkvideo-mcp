from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from asyncio.subprocess import Process

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.models import MAX_AUDIO_SECONDS, MAX_FILE_BYTES


def media_tools() -> tuple[str, str] | None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    return (ffmpeg, ffprobe) if ffmpeg and ffprobe else None


async def _stop_process(process: Process) -> None:
    # The process owns a new session; terminate its descendants as well as the direct child.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 0.5)
    except TimeoutError:
        pass
    # A descendant may hold the pipes open even after the direct child exits.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await asyncio.wait_for(process.wait(), 2)


async def run_bounded(
    args: list[str],
    *,
    data: bytes = b"",
    max_stdout: int = MAX_FILE_BYTES,
    wall_seconds: float = 120,
) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def write_input() -> None:
        if process.stdin is None:
            raise RuntimeError("Missing process input pipe.")
        try:
            process.stdin.write(data)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def read_output(stream: asyncio.StreamReader | None, limit: int) -> bytes:
        if stream is None:
            raise RuntimeError("Missing process output pipe.")
        result = bytearray()
        while chunk := await stream.read(64 * 1024):
            result.extend(chunk)
            if len(result) > limit:
                raise TalkVideoError(
                    "process_output_limit",
                    "A local media process exceeded its bounded output limit.",
                    "Use a shorter diagnostic job and inspect local tool compatibility.",
                )
        return bytes(result)

    tasks = [
        asyncio.create_task(write_input()),
        asyncio.create_task(read_output(process.stdout, max_stdout)),
        asyncio.create_task(read_output(process.stderr, 8192)),
    ]

    async def discard_output(stream: asyncio.StreamReader | None) -> None:
        if stream is not None:
            while await stream.read(64 * 1024):
                pass

    async def cleanup() -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A capped reader has stopped consuming. Drain/discard before waiting for exit,
        # otherwise asyncio can wait forever on a paused, full pipe after SIGKILL.
        drains = [
            asyncio.create_task(discard_output(process.stdout)),
            asyncio.create_task(discard_output(process.stderr)),
        ]
        try:
            await _stop_process(process)
            await asyncio.gather(*drains)
        finally:
            for drain in drains:
                if not drain.done():
                    drain.cancel()
            await asyncio.gather(*drains, return_exceptions=True)

    try:
        async with asyncio.timeout(wall_seconds):
            await asyncio.gather(*tasks)
            code = await process.wait()
        if code != 0:
            raise TalkVideoError(
                "media_process_failed",
                "A local media process failed; no output was accepted.",
                "Check that FFmpeg/ffprobe support libx264 and AAC. No cloud fallback is used.",
                needs_user_action=True,
            )
        stdout_task = tasks[1]
        result = stdout_task.result()
        if not isinstance(result, bytes):
            raise RuntimeError("Unexpected process output type.")
        return result
    except TimeoutError as exc:
        raise TalkVideoError(
            "media_timeout",
            "A local media process exceeded its wall-time limit.",
            "Use a shorter diagnostic preview or inspect local tool compatibility.",
        ) from exc
    finally:
        try:
            async with asyncio.timeout(4):
                await cleanup()
        except TimeoutError as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise TalkVideoError(
                "process_cleanup_timeout",
                "The OS did not finish local process cleanup within its bounded deadline.",
                "Stop further jobs and inspect the local process state; no output is accepted.",
                needs_user_action=True,
            ) from exc


class ProbeStream(BaseModel):
    model_config = ConfigDict(extra="ignore")
    codec_type: str
    codec_name: str
    width: int | None = None
    height: int | None = None
    sample_rate: str | None = None
    channels: int | None = None


class ProbeFormat(BaseModel):
    model_config = ConfigDict(extra="ignore")
    duration: float = Field(gt=0, le=MAX_AUDIO_SECONDS + 1, allow_inf_nan=False)


class VideoInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    streams: list[ProbeStream] = Field(min_length=2, max_length=2)
    format: ProbeFormat


async def inspect_video(data: bytes, expected_duration: float) -> VideoInfo:
    tools = media_tools()
    if tools is None:
        raise TalkVideoError(
            "media_tools_unavailable",
            "Local FFmpeg and ffprobe are required for diagnostic video.",
            "Provide compatible local tools; do not download models or use a cloud service.",
            needs_user_action=True,
        )
    raw = await run_bounded(
        [
            tools[1],
            "-v",
            "error",
            "-protocol_whitelist",
            "pipe",
            "-show_entries",
            "format=duration:stream=codec_name,codec_type,width,height,sample_rate,channels",
            "-of",
            "json",
            "pipe:0",
        ],
        data=data,
        max_stdout=32_768,
        wall_seconds=15,
    )
    try:
        info = VideoInfo.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise TalkVideoError(
            "invalid_video",
            "The local video output could not be validated.",
            "Inspect the diagnostic backend; no successful render is claimed.",
        ) from exc
    videos = [stream for stream in info.streams if stream.codec_type == "video"]
    audios = [stream for stream in info.streams if stream.codec_type == "audio"]
    if (
        len(videos) != 1
        or len(audios) != 1
        or videos[0].codec_name != "h264"
        or (videos[0].width, videos[0].height) != (640, 360)
        or audios[0].codec_name != "aac"
        or audios[0].sample_rate != "16000"
        or audios[0].channels != 1
        or abs(info.format.duration - expected_duration) > 0.15
    ):
        raise TalkVideoError(
            "video_format_mismatch",
            "The diagnostic video format or duration does not match its source audio.",
            "Inspect local codec compatibility; do not claim lip-sync quality from duration alone.",
        )
    return info


async def render_diagnostic_video(wav: bytes, duration: float) -> tuple[bytes, VideoInfo]:
    tools = media_tools()
    if tools is None:
        raise TalkVideoError(
            "media_tools_unavailable",
            "Local FFmpeg and ffprobe are not both available.",
            "Use prepare/audio diagnostics until compatible local tools are installed.",
            needs_user_action=True,
        )
    if not 0 < duration <= MAX_AUDIO_SECONDS:
        raise TalkVideoError(
            "audio_duration_limit", "Video duration is out of bounds.", "Use a shorter script."
        )
    data = await run_bounded(
        [
            tools[0],
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=25",
            "-protocol_whitelist",
            "pipe",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-t",
            f"{duration:.6f}",
            "-shortest",
            "-movflags",
            "frag_keyframe+empty_moov",
            "-f",
            "mp4",
            "pipe:1",
        ],
        data=wav,
    )
    info = await inspect_video(data, duration)
    return data, info
