from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from asyncio.subprocess import Process
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from talkvideo_mcp.audio import encode_wav, parse_source_wav, parse_wav, wave_chunks
from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.models import (
    MAX_AUDIO_SECONDS,
    MAX_FILE_BYTES,
    MAX_JSON_BYTES,
    AudioSettings,
    RawWavInfo,
    VideoDecodeEvidence,
    WavInfo,
)


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
    r_frame_rate: str | None = None
    avg_frame_rate: str | None = None
    time_base: str | None = None
    index: int
    duration_ts: int
    nb_read_packets: int


class ProbePacket(BaseModel):
    model_config = ConfigDict(extra="ignore")
    stream_index: int
    pts: int = Field(ge=0)
    dts: int = Field(ge=0)
    duration: int = Field(gt=0)


class ProbeFormat(BaseModel):
    model_config = ConfigDict(extra="ignore")
    duration: float = Field(gt=0, le=MAX_AUDIO_SECONDS + 1, allow_inf_nan=False)


class VideoInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    streams: list[ProbeStream] = Field(min_length=2, max_length=2)
    format: ProbeFormat
    decode: VideoDecodeEvidence | None = None
    packets: list[ProbePacket] = Field(min_length=2, max_length=8192, exclude=True)


async def decode_video(
    executable: str, data: bytes, expected_duration: float, time_base: str, average_rate: str
) -> VideoDecodeEvidence:
    try:
        progress = await run_bounded(
            [
                executable,
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-max_error_rate",
                "0",
                "-protocol_whitelist",
                "pipe",
                "-i",
                "pipe:0",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-fps_mode",
                "passthrough",
                "-progress",
                "pipe:1",
                "-stats_period",
                "10",
                "-f",
                "null",
                "-",
            ],
            data=data,
            max_stdout=65_536,
            wall_seconds=60,
        )
    except TalkVideoError as exc:
        if exc.problem.code == "process_cleanup_timeout":
            raise
        raise TalkVideoError(
            "video_decode_failed",
            "The complete local video could not be decoded without errors.",
            "A readable header is insufficient. Preserve failed output for inspection.",
            needs_user_action=True,
        ) from exc
    try:
        values = dict(
            line.split("=", 1) for line in progress.decode("ascii").splitlines() if "=" in line
        )
        frames = int(values["frame"])
        end = int(values["out_time_us"]) / 1_000_000
        expected_frames = (round(expected_duration * 16000) * 25 + 15999) // 16000
        if (
            values.get("progress") != "end"
            or frames != expected_frames
            or abs(end - expected_duration) > 0.15
        ):
            raise ValueError("Inconsistent complete decode timeline.")
    except (ValueError, KeyError) as exc:
        raise TalkVideoError(
            "video_timeline_mismatch",
            "Decoded frames or timestamps do not match the expected 25 fps audio timeline.",
            "Inspect the local output; this is not a perceptual lip-sync assessment.",
        ) from exc
    return VideoDecodeEvidence(
        decoded_frames=frames,
        frame_rate="25/1",
        time_base=time_base,
        decoded_end_seconds=end,
        source_audio_seconds=expected_duration,
        average_frame_rate=average_rate,
    )


def packet_timing_valid(
    info: VideoInfo, video: ProbeStream, audio: ProbeStream, source_frames: int
) -> bool:
    clock = Fraction(video.time_base or "0")
    if clock <= 0:
        return False
    period_ticks = Fraction(1, 25) / clock
    expected_video_frames = (source_frames * 25 + 15999) // 16000
    video_packets = [packet for packet in info.packets if packet.stream_index == video.index]
    audio_packets = [packet for packet in info.packets if packet.stream_index == audio.index]
    if (
        video.index == audio.index
        or len(video_packets) + len(audio_packets) != len(info.packets)
        or len(video_packets) != expected_video_frames
        or not audio_packets
        or period_ticks.denominator != 1
        or audio.duration_ts != source_frames + 1024
        or Fraction(video.avg_frame_rate or "0")
        != Fraction(len(video_packets), 1) / (video.duration_ts * clock)
    ):
        return False
    for stream, packets in ((video, video_packets), (audio, audio_packets)):
        cursor = 0
        for packet in packets:
            if packet.pts != cursor or packet.dts != cursor:
                return False
            cursor += packet.duration
        if cursor != stream.duration_ts or len(packets) != stream.nb_read_packets:
            return False
    # Fragmented MP4 extends the first video packet by the AAC encoder's priming interval.
    priming = (video_packets[0].duration - period_ticks) * clock
    return (
        abs(priming - Fraction(1024, 16000)) <= clock
        and all(packet.duration == period_ticks for packet in video_packets[1:])
        and all(packet.duration == 1024 for packet in audio_packets[:-1])
        and audio_packets[-1].duration <= 1024
    )


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
            "-count_packets",
            "-show_entries",
            "format=duration:stream=index,codec_name,codec_type,width,height,sample_rate,channels,"
            "r_frame_rate,avg_frame_rate,time_base,duration_ts,nb_read_packets:"
            "packet=stream_index,pts,dts,duration",
            "-of",
            "json=compact=1",
            "pipe:0",
        ],
        data=data,
        max_stdout=MAX_JSON_BYTES,
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
    try:
        clock = Fraction(videos[0].time_base or "0") if len(videos) == 1 else Fraction(0)
        rate_ok = (
            len(videos) == 1
            and Fraction(videos[0].r_frame_rate or "0") == 25
            and clock > 0
            and (Fraction(1, 25) / clock).denominator == 1
            and len(audios) == 1
            and Fraction(audios[0].time_base or "0") == Fraction(1, 16000)
            and packet_timing_valid(info, videos[0], audios[0], round(expected_duration * 16000))
        )
    except (ValueError, ZeroDivisionError):
        rate_ok = False
    if (
        len(videos) != 1
        or len(audios) != 1
        or videos[0].codec_name != "h264"
        or (videos[0].width, videos[0].height) != (640, 360)
        or audios[0].codec_name != "aac"
        or audios[0].sample_rate != "16000"
        or audios[0].channels != 1
        or not rate_ok
        or abs(info.format.duration - expected_duration) > 0.15
    ):
        raise TalkVideoError(
            "video_format_mismatch",
            "The diagnostic video format or duration does not match its source audio.",
            "Inspect local codec compatibility; do not claim lip-sync quality from duration alone.",
        )
    info.decode = await decode_video(
        tools[0],
        data,
        expected_duration,
        videos[0].time_base or "",
        videos[0].avg_frame_rate or "",
    )
    return info


async def normalize_source_wav(
    raw: bytes, settings: AudioSettings, *, allow_normalization: bool
) -> tuple[bytes, RawWavInfo, WavInfo, Literal["identity", "pcm_rewrap", "ffmpeg_pcm_s16le"]]:
    source, samples = parse_source_wav(raw)
    compatible = (
        source.encoding == "pcm"
        and source.sample_rate == settings.sample_rate
        and source.channels == settings.channels
        and source.bits_per_sample == settings.sample_width * 8
    )
    if compatible and len(wave_chunks(raw)[0]) == 16:
        info, _ = parse_wav(raw)
        return raw, source, info, "identity"
    if not allow_normalization:
        raise TalkVideoError(
            "audio_normalization_required",
            "Source WAV differs from the required PCM format; it has not been relabeled.",
            "The operator may explicitly enable bounded local WAV normalization. Resume the "
            "cached source instead of regenerating speech.",
            needs_user_action=True,
        )
    method: Literal["identity", "pcm_rewrap", "ffmpeg_pcm_s16le"] = "pcm_rewrap"
    if not compatible:
        tools = media_tools()
        if tools is None:
            raise TalkVideoError(
                "media_tools_unavailable",
                "WAV conversion needs compatible local FFmpeg/ffprobe.",
                "Keep the cached raw audio; install local tooling before retrieval-only resume.",
                needs_user_action=True,
            )
        samples = await run_bounded(
            [
                tools[0],
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-protocol_whitelist",
                "pipe",
                "-f",
                "wav",
                "-i",
                "pipe:0",
                "-map",
                "0:a:0",
                "-vn",
                "-ar",
                str(settings.sample_rate),
                "-ac",
                str(settings.channels),
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                "pipe:1",
            ],
            data=raw,
            max_stdout=MAX_AUDIO_SECONDS * settings.sample_rate * settings.channels * 2,
            wall_seconds=30,
        )
        method = "ffmpeg_pcm_s16le"
    if not samples or len(samples) % (settings.channels * settings.sample_width):
        raise TalkVideoError(
            "invalid_normalized_audio",
            "The converter returned incomplete PCM.",
            "Keep the raw source.",
        )
    result = encode_wav(
        samples, rate=settings.sample_rate, channels=settings.channels, width=settings.sample_width
    )
    info, _ = parse_wav(result)
    if abs(info.duration_seconds - source.duration_seconds) > 1 / settings.sample_rate + 1e-9:
        raise TalkVideoError(
            "normalization_timing_mismatch",
            "Normalized PCM timing differs from the source by more than one output frame.",
            "Inspect the cached source/converter; do not regenerate or accept mismatched audio.",
        )
    return result, source, info, method


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
    frame_count = (round(duration * 16000) * 25 + 15999) // 16000
    pattern_duration = frame_count / 25
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
            f"testsrc2=size=640x360:rate=25:duration={pattern_duration:.8f}",
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
            "-profile:a",
            "aac_low",
            "-b:a",
            "96k",
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
