from __future__ import annotations

import unicodedata

import regex

from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.models import (
    MAX_CHUNKS,
    MAX_TEXT_BYTES,
    MAX_TEXT_CODEPOINTS,
    Chunk,
    ChunkLimits,
    CueInput,
    PreparedCue,
    PreparedScript,
    ScriptInput,
    canonical_bytes,
    digest_bytes,
    text_digest,
)

SENTENCE_END = frozenset("。！？.!?\n")
CLAUSE_END = frozenset("、，,；;：: \t")
CLOSERS = frozenset("」』】）》〉”’\"')]}")  # Keep trailing quotation marks with a sentence.


def graphemes(text: str) -> list[str]:
    return regex.findall(r"\X", text)


def split_text(text: str, limits: ChunkLimits) -> list[str]:
    clusters = graphemes(text)
    chunks: list[str] = []
    start = 0
    while start < len(clusters):
        stop = start
        codepoints = 0
        utf8_bytes = 0
        sentence = 0
        clause = 0
        while stop < len(clusters) and stop - start < limits.graphemes:
            cluster = clusters[stop]
            size = len(cluster.encode("utf-8"))
            if (
                codepoints + len(cluster) > limits.codepoints
                or utf8_bytes + size > limits.utf8_bytes
            ):
                break
            codepoints += len(cluster)
            utf8_bytes += size
            stop += 1
            numeric_period = (
                cluster == "."
                and stop > 1
                and stop < len(clusters)
                and clusters[stop - 2][-1].isdecimal()
                and clusters[stop][0].isdecimal()
            )
            if cluster[-1] in SENTENCE_END and not numeric_period:
                sentence = stop
            elif all(char in CLOSERS for char in cluster) and sentence == stop - 1:
                sentence = stop
            elif cluster[-1] in CLAUSE_END:
                clause = stop
        if stop == start:
            raise TalkVideoError(
                "grapheme_exceeds_limit",
                "A single grapheme exceeds the configured codepoint or UTF-8 byte limit.",
                "Increase the host chunk limit or explicitly revise this cue; no text was dropped.",
            )
        end = stop if stop == len(clusters) else sentence or clause or stop
        chunks.append("".join(clusters[start:end]))
        if len(chunks) > MAX_CHUNKS:
            raise TalkVideoError(
                "too_many_chunks",
                "The script exceeds the 512-chunk host limit.",
                "Increase chunk size or prepare a shorter, explicitly approved script.",
            )
        start = end
    return chunks


def prepare_script(script: ScriptInput) -> PreparedScript:
    tracks = [
        [cue.display_text for cue in script.cues],
        [
            cue.spoken_text if cue.spoken_text is not None else cue.display_text
            for cue in script.cues
        ],
    ]
    for track in tracks:
        if (
            sum(map(len, track)) > MAX_TEXT_CODEPOINTS
            or sum(len(value.encode("utf-8")) for value in track) > MAX_TEXT_BYTES
        ):
            raise TalkVideoError(
                "script_too_large",
                "A combined text track exceeds the host's codepoint or UTF-8 byte limit.",
                "Prepare a shorter script; no input was truncated.",
            )
    normalize = (
        (lambda value: unicodedata.normalize("NFC", value))
        if script.normalization == "NFC"
        else (lambda value: value)
    )
    used_ids: set[str] = set()
    cues: list[PreparedCue] = []
    total_chunks = 0
    total_graphemes = 0
    for index, cue in enumerate(script.cues, start=1):
        cue_id = cue.cue_id or f"c{index:04d}"
        if cue_id in used_ids:
            raise TalkVideoError(
                "duplicate_cue_id",
                "Cue IDs must be unique, including automatically assigned IDs.",
                "Give each cue a distinct stable ID.",
            )
        used_ids.add(cue_id)
        display = normalize(cue.display_text)
        spoken = normalize(cue.spoken_text if cue.spoken_text is not None else cue.display_text)
        chunks = [
            Chunk(
                chunk_id=f"{cue_id}-p{part:04d}",
                text=text,
                sha256=text_digest(text),
                graphemes=len(graphemes(text)),
                codepoints=len(text),
                utf8_bytes=len(text.encode("utf-8")),
            )
            for part, text in enumerate(split_text(spoken, script.limits), start=1)
        ]
        if "".join(chunk.text for chunk in chunks) != spoken:
            raise RuntimeError("Segmentation violated the exact-rejoin invariant.")
        cues.append(
            PreparedCue(cue_id=cue_id, display_text=display, spoken_text=spoken, chunks=chunks)
        )
        total_chunks += len(chunks)
        total_graphemes += sum(chunk.graphemes for chunk in chunks)
    display_text = "".join(cue.display_text for cue in cues)
    spoken_text = "".join(cue.spoken_text for cue in cues)
    for value in (display_text, spoken_text):
        if len(value) > MAX_TEXT_CODEPOINTS or len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            raise TalkVideoError(
                "script_too_large",
                "Each combined display/spoken track is limited to 20000 codepoints / 80000 bytes.",
                "Prepare a shorter script; inputs are never truncated automatically.",
            )
    if total_chunks > MAX_CHUNKS:
        raise TalkVideoError(
            "too_many_chunks",
            "The script exceeds the 512-chunk host limit.",
            "Increase chunk size or prepare a shorter script.",
        )
    normalized = ScriptInput(
        cues=[
            CueInput(cue_id=cue.cue_id, display_text=cue.display_text, spoken_text=cue.spoken_text)
            for cue in cues
        ],
        normalization=script.normalization,
        limits=script.limits,
    )
    changed = any(
        left.display_text != right.display_text
        or (left.spoken_text if left.spoken_text is not None else left.display_text)
        != right.spoken_text
        for left, right in zip(script.cues, cues, strict=True)
    )
    return PreparedScript(
        normalization=script.normalization,
        limits=script.limits,
        cues=cues,
        display_text=display_text,
        spoken_text=spoken_text,
        source_digest=digest_bytes(canonical_bytes(script)),
        normalized_digest=digest_bytes(canonical_bytes(normalized)),
        normalization_changed=changed,
        total_chunks=total_chunks,
        total_graphemes=total_graphemes,
        total_codepoints=len(spoken_text),
        total_utf8_bytes=len(spoken_text.encode("utf-8")),
        plan_digest=digest_bytes(canonical_bytes(normalized)),
    )
