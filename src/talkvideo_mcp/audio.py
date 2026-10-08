from __future__ import annotations

import asyncio
import math
import struct
from collections.abc import Sequence
from typing import Protocol

from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.models import (
    MAX_AUDIO_SECONDS,
    MAX_FILE_BYTES,
    AudioSettings,
    RawWavInfo,
    WavInfo,
)
from talkvideo_mcp.text import graphemes


def invalid_wav() -> TalkVideoError:
    return TalkVideoError(
        "invalid_wav",
        "Audio is not a complete, non-empty supported PCM RIFF/WAVE file.",
        "Do not complete the cue. Inspect the source; HTML, empty and partial audio are invalid.",
        needs_user_action=True,
    )


def wave_chunks(data: bytes) -> tuple[bytes, bytes]:
    if (
        len(data) < 44
        or len(data) > MAX_FILE_BYTES
        or data[:4] != b"RIFF"
        or data[8:12] != b"WAVE"
        or struct.unpack_from("<I", data, 4)[0] + 8 != len(data)
    ):
        raise invalid_wav()
    offset = 12
    fmt: bytes | None = None
    pcm: bytes | None = None
    chunks = 0
    while offset < len(data):
        chunks += 1
        if chunks > 1024:
            raise invalid_wav()
        if offset + 8 > len(data):
            raise invalid_wav()
        chunk = data[offset : offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        offset += 8
        if offset + size + size % 2 > len(data):
            raise invalid_wav()
        if chunk == b"fmt ":
            if fmt is not None:
                raise invalid_wav()
            fmt = data[offset : offset + size]
        elif chunk == b"data":
            if pcm is not None:
                raise invalid_wav()
            pcm = data[offset : offset + size]
        offset += size + size % 2
    if fmt is None or not pcm:
        raise invalid_wav()
    return fmt, pcm


def parse_wav(data: bytes) -> tuple[WavInfo, bytes]:
    fmt, pcm = wave_chunks(data)
    if len(fmt) != 16:
        raise invalid_wav()
    encoding, channels, rate, byte_rate, block_align, bits = struct.unpack("<HHIIHH", fmt)
    width = bits // 8
    if (
        encoding != 1
        or channels not in {1, 2}
        or bits not in {8, 16, 24, 32}
        or not 8000 <= rate <= 48000
        or block_align != channels * width
        or byte_rate != rate * block_align
        or len(pcm) % block_align
    ):
        raise invalid_wav()
    frames = len(pcm) // block_align
    if frames > MAX_AUDIO_SECONDS * rate:
        raise TalkVideoError(
            "audio_duration_limit",
            "Audio exceeds the 180-second host limit.",
            "Prepare a shorter explicitly approved script.",
        )
    return (
        WavInfo(
            sample_rate=rate,
            channels=channels,
            sample_width=width,
            frames=frames,
            duration_seconds=frames / rate,
        ),
        pcm,
    )


def parse_source_wav(data: bytes) -> tuple[RawWavInfo, bytes]:
    fmt, samples = wave_chunks(data)
    if len(fmt) not in {16, 18, 40}:
        raise invalid_wav()
    encoding, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", fmt)
    if len(fmt) == 18 and struct.unpack_from("<H", fmt, 16)[0] != 0:
        raise invalid_wav()
    if len(fmt) == 40:
        extension, valid_bits = struct.unpack_from("<HH", fmt, 16)
        if encoding != 65534 or extension != 22 or not 0 < valid_bits <= bits:
            raise invalid_wav()
        guid = fmt[24:40]
        if guid == bytes.fromhex("0100000000001000800000aa00389b71"):
            encoding = 1
        elif guid == bytes.fromhex("0300000000001000800000aa00389b71"):
            encoding = 3
        else:
            raise invalid_wav()
    if (
        encoding not in {1, 3}
        or channels not in {1, 2}
        or not 8000 <= rate <= 192000
        or (encoding == 1 and bits not in {8, 16, 24, 32})
        or (encoding == 3 and bits not in {32, 64})
        or align != channels * (bits // 8)
        or byte_rate != rate * align
        or len(samples) % align
    ):
        raise invalid_wav()
    frames = len(samples) // align
    if frames > MAX_AUDIO_SECONDS * rate:
        raise TalkVideoError(
            "audio_duration_limit",
            "Source WAV exceeds the duration limit.",
            "Use a shorter script.",
        )
    return RawWavInfo(
        sample_rate=rate,
        channels=channels,
        bits_per_sample=bits,
        encoding="pcm" if encoding == 1 else "float",
        frames=frames,
        duration_seconds=frames / rate,
    ), samples


def encode_wav(pcm: bytes, *, rate: int, channels: int, width: int) -> bytes:
    align = channels * width
    padding = b"\x00" if len(pcm) % 2 else b""
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm) + len(padding))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * align, align, width * 8)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
        + padding
    )


def validate_settings(info: WavInfo, settings: AudioSettings) -> None:
    if (info.sample_rate, info.channels, info.sample_width) != (
        settings.sample_rate,
        settings.channels,
        settings.sample_width,
    ):
        raise TalkVideoError(
            "audio_format_mismatch",
            "A cue's PCM format does not match the revision settings.",
            "Do not resample silently. Correct the backend or create a new revision.",
            needs_user_action=True,
        )


def assemble_wav(
    parts: Sequence[bytes], settings: AudioSettings
) -> tuple[bytes, WavInfo, list[tuple[int, int, int]]]:
    if not parts:
        raise invalid_wav()
    output = bytearray()
    offsets: list[tuple[int, int, int]] = []
    frames = 0
    gap_frames = settings.sample_rate * settings.gap_ms // 1000
    for index, part in enumerate(parts):
        info, pcm = parse_wav(part)
        validate_settings(info, settings)
        gap = gap_frames if index < len(parts) - 1 else 0
        end = frames + info.frames
        if end + gap > MAX_AUDIO_SECONDS * settings.sample_rate:
            raise TalkVideoError(
                "audio_duration_limit",
                "Assembled audio would exceed 180 seconds including gaps.",
                "Prepare a shorter script; no partial assembly is published.",
            )
        offsets.append((frames, end, gap))
        output.extend(pcm)
        output.extend(bytes(gap * settings.channels * settings.sample_width))
        frames = end + gap
    wav = encode_wav(
        bytes(output),
        rate=settings.sample_rate,
        channels=settings.channels,
        width=settings.sample_width,
    )
    info, _ = parse_wav(wav)
    if info.frames != frames:
        raise RuntimeError("Assembly frame count mismatch.")
    return wav, info, offsets


class SafeToRetry(Exception):
    """The backend guarantees submission did not occur."""


class AmbiguousSubmission(Exception):
    """Submission may have occurred: never automatically repeat the request."""


class SubmissionRejected(TalkVideoError):
    """An explicit response rejected this POST; never silently regenerate it."""


class AudioProvider(Protocol):
    fingerprint: str
    side_effects_possible: bool

    async def synthesize(self, text: str, settings: AudioSettings) -> bytes: ...


class DiagnosticTone:
    fingerprint = "diagnostic-tone-v1"
    side_effects_possible = False

    @staticmethod
    def frames(text: str, settings: AudioSettings) -> int:
        return max(400, min(settings.sample_rate * 2, len(graphemes(text)) * 160))

    async def synthesize(self, text: str, settings: AudioSettings) -> bytes:
        await asyncio.sleep(0)
        frames = self.frames(text, settings)
        pcm = bytearray()
        ramp = 80
        for frame in range(frames):
            envelope = min(1.0, frame / ramp, (frames - 1 - frame) / ramp)
            sample = round(
                4000 * envelope * math.sin(2 * math.pi * 440 * frame / settings.sample_rate)
            )
            pcm.extend(struct.pack("<h", sample))
        return encode_wav(bytes(pcm), rate=settings.sample_rate, channels=1, width=2)
