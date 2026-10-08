import unicodedata

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.models import ChunkLimits, CueInput, ScriptInput
from talkvideo_mcp.text import graphemes, prepare_script, split_text


@pytest.mark.parametrize("limit", [8, 240, 1000])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_exact_boundary(limit, delta):
    text = "あ" * (limit + delta)
    chunks = split_text(text, ChunkLimits(graphemes=limit))
    assert "".join(chunks) == text
    assert all(len(graphemes(chunk)) <= limit for chunk in chunks)
    assert len(chunks) == (1 if delta <= 0 else 2)


@pytest.mark.parametrize(
    "text",
    [
        "「これは例です。」次は『否定ではない。』です！\n続く。",
        " 👨‍👩‍👧‍👦🇯🇵e\u0301か\u3099👍🏽\t終わり ",
        "A " + " " * 50 + "B",
        "分割されても 12.5% は減少していません。\r\n改行も保存。",
    ],
)
def test_unicode_and_whitespace_are_lossless(text):
    limits = ChunkLimits(graphemes=8, codepoints=24, utf8_bytes=64)
    chunks = split_text(text, limits)
    assert "".join(chunks) == text
    assert [g for chunk in chunks for g in graphemes(chunk)] == graphemes(text)
    assert all(len(chunk) <= 24 and len(chunk.encode()) <= 64 for chunk in chunks)


def test_prefer_sentence_with_closing_quote():
    text = "「例です。」次の文です。長い続きもあります。"
    chunks = split_text(text, ChunkLimits(graphemes=10))
    assert chunks[0] == "「例です。」"
    assert "".join(chunks) == text


@pytest.mark.parametrize(
    "text,limit,first",
    [
        ("価格は 3.14 円です。続き", 10, "価格は 3.14 "),
        ("版は v1.2.3 です。続き", 11, "版は v1.2.3 "),
    ],
)
def test_numeric_periods_do_not_beat_later_clause_boundaries(text, limit, first):
    chunks = split_text(text, ChunkLimits(graphemes=limit))
    assert chunks[0] == first
    assert "".join(chunks) == text
    assert all(len(graphemes(chunk)) <= limit for chunk in chunks)


def test_single_grapheme_never_silently_split():
    with pytest.raises(TalkVideoError, match="grapheme_exceeds_limit"):
        split_text("👨‍👩‍👧‍👦", ChunkLimits(graphemes=1, codepoints=6))


def test_nfc_is_explicit_and_cues_rejoin_both_tracks():
    text = " cafe\u0301\nか\u3099 "
    script = ScriptInput(
        cues=[CueInput(display_text=text, spoken_text="１つ。"), CueInput(display_text="続く。")]
    )
    plain = prepare_script(script)
    normalized = prepare_script(
        ScriptInput(cues=script.cues, normalization="NFC", limits=script.limits)
    )
    assert plain.display_text == text + "続く。"
    assert normalized.display_text == unicodedata.normalize("NFC", text) + "続く。"
    assert normalized.normalization_changed
    assert plain.spoken_text == "１つ。続く。"
    assert "".join(c.display_text for c in plain.cues) == plain.display_text
    assert "".join(part.text for c in plain.cues for part in c.chunks) == plain.spoken_text
    assert [c.cue_id for c in plain.cues] == ["c0001", "c0002"]
    assert plain.plan_digest == prepare_script(script).plan_digest


def test_duplicate_auto_id_rejected():
    with pytest.raises(TalkVideoError, match="duplicate_cue_id"):
        prepare_script(
            ScriptInput(
                cues=[CueInput(display_text="a"), CueInput(cue_id="c0001", display_text="b")]
            )
        )


@pytest.mark.parametrize("text", ["", " \t", "\ud800", "hello\x00there", "\x1b[2J"])
def test_invalid_text(text):
    with pytest.raises(ValidationError):
        CueInput(display_text=text)


def test_total_and_chunk_limits_are_explicit():
    with pytest.raises(TalkVideoError, match="script_too_large"):
        prepare_script(
            ScriptInput(
                cues=[CueInput(display_text="a" * 11_000), CueInput(display_text="b" * 11_000)]
            )
        )
    with pytest.raises(TalkVideoError, match="too_many_chunks"):
        prepare_script(
            ScriptInput(cues=[CueInput(display_text="a" * 513)], limits=ChunkLimits(graphemes=1))
        )


@settings(max_examples=100)
@given(
    st.text(alphabet=st.characters(exclude_categories=("Cc", "Cs")), min_size=1, max_size=100),
    st.integers(min_value=1, max_value=32),
)
def test_property_exact_rejoining(text, limit):
    chunks = split_text(text, ChunkLimits(graphemes=limit))
    assert "".join(chunks) == text
    assert [g for chunk in chunks for g in graphemes(chunk)] == graphemes(text)
