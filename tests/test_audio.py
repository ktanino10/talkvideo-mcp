import struct

import pytest

from talkvideo_mcp.audio import DiagnosticTone, assemble_wav, encode_wav, parse_wav
from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.models import AudioSettings


@pytest.mark.parametrize("data", [b"", b"<html>not audio</html>", b"RIFF", bytes(100)])
def test_invalid_audio(data):
    with pytest.raises(TalkVideoError, match="invalid_wav"):
        parse_wav(data)


async def test_pcm_assembly_exact_frames_gaps_and_samples():
    settings = AudioSettings(gap_ms=125)
    tone = DiagnosticTone()
    parts = [await tone.synthesize(text, settings) for text in ["甲", "長めの説明", "終わり"]]
    result, info, offsets = assemble_wav(parts, settings)
    chunks = [parse_wav(part) for part in parts]
    expected_frames = sum(item.frames for item, _ in chunks) + 2 * 2000
    assert info.frames == expected_frames
    assert info.duration_seconds == expected_frames / 16000
    assert offsets[0] == (0, chunks[0][0].frames, 2000)
    assert offsets[-1][1] == expected_frames and offsets[-1][2] == 0
    _, pcm = parse_wav(result)
    assert pcm == chunks[0][1] + bytes(4000) + chunks[1][1] + bytes(4000) + chunks[2][1]


async def test_reject_truncation_lying_sizes_and_trailing_bytes():
    wav = await DiagnosticTone().synthesize("example", AudioSettings())
    for invalid in [wav[:-1], wav + b"extra", wav[:40], wav[:4] + struct.pack("<I", 20) + wav[8:]]:
        with pytest.raises(TalkVideoError, match="invalid_wav"):
            parse_wav(invalid)
    with pytest.raises(TalkVideoError, match="invalid_wav"):
        parse_wav(encode_wav(b"", rate=16000, channels=1, width=2))


async def test_format_mismatch_and_duration_budget():
    wav = await DiagnosticTone().synthesize("x", AudioSettings())
    other_rate = encode_wav(bytes(400), rate=8000, channels=1, width=2)
    with pytest.raises(TalkVideoError, match="audio_format_mismatch"):
        assemble_wav([wav, other_rate], AudioSettings())
    long = encode_wav(bytes(16000 * 2 * 100), rate=16000, channels=1, width=2)
    with pytest.raises(TalkVideoError, match="audio_duration_limit"):
        assemble_wav([long, long], AudioSettings())
