from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TEXT_CODEPOINTS = 20_000
MAX_TEXT_BYTES = 80_000
MAX_CUES = 128
MAX_CHUNKS = 512
MAX_AUDIO_SECONDS = 180
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_JOBS = 1000
MAX_REVISIONS = 200
MAX_QUEUED_JOBS = 8
MAX_ATTEMPTS = 3
PREVIEW_CHUNKS = 3

Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
CueId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$")]
RevisionId = Annotated[str, Field(pattern=r"^r-[0-9a-f]{32}$")]
JobId = Annotated[str, Field(pattern=r"^j-[0-9a-f]{32}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Stage = Literal["preview", "full"]
Kind = Literal["audio", "video"]
Backend = Literal["production", "diagnostic"]
ReviewStage = Literal["script", "audio_preview", "video_preview"]
JobStatus = Literal[
    "awaiting_review",
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "needs_user_action",
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_bytes(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def text_digest(value: str) -> str:
    return digest_bytes(value.encode("utf-8"))


def validate_text(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("Text must contain a non-whitespace character.")
    if len(value) > MAX_TEXT_CODEPOINTS:
        raise ValueError("Text exceeds the 20000-codepoint host limit.")
    if any(unicodedata.category(char) in {"Cc", "Cs"} and char not in "\n\r\t" for char in value):
        raise ValueError("Control characters and unpaired surrogates are not allowed.")
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("Text exceeds the 80000-byte UTF-8 host limit.")
    return value


class ChunkLimits(Model):
    """Host limits, not unverified limits of any speech provider."""

    graphemes: int = Field(default=240, ge=1, le=1000, strict=True)
    codepoints: int = Field(default=1000, ge=1, le=1000, strict=True)
    utf8_bytes: int = Field(default=4000, ge=1, le=4000, strict=True)


class CueInput(Model):
    cue_id: CueId | None = None
    display_text: str = Field(min_length=1, max_length=MAX_TEXT_CODEPOINTS)
    spoken_text: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT_CODEPOINTS)

    @field_validator("display_text", "spoken_text")
    @classmethod
    def text_is_valid(cls, value: str | None) -> str | None:
        return validate_text(value) if value is not None else None


class ScriptInput(Model):
    cues: list[CueInput] = Field(min_length=1, max_length=MAX_CUES)
    normalization: Literal["none", "NFC"] = "none"
    limits: ChunkLimits = Field(default_factory=ChunkLimits)


class Chunk(Model):
    chunk_id: str
    text: str
    sha256: Digest
    graphemes: int
    codepoints: int
    utf8_bytes: int


class PreparedCue(Model):
    cue_id: CueId
    display_text: str
    spoken_text: str
    chunks: list[Chunk]


class PreparedScript(Model):
    schema_version: Literal[1] = 1
    normalization: Literal["none", "NFC"]
    limits: ChunkLimits
    cues: list[PreparedCue]
    display_text: str
    spoken_text: str
    source_digest: Digest
    normalized_digest: Digest
    normalization_changed: bool
    total_chunks: int
    total_graphemes: int
    total_codepoints: int
    total_utf8_bytes: int
    plan_digest: Digest


class AudioSettings(Model):
    sample_rate: Literal[16000] = 16000
    channels: Literal[1] = 1
    sample_width: Literal[2] = 2
    gap_ms: int = Field(default=100, ge=0, le=1000, strict=True)

    @field_validator("sample_rate", "channels", "sample_width", mode="before")
    @classmethod
    def fixed_integers_are_not_booleans(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("PCM settings must be integers, not booleans.")
        return value


class RevisionRef(Model):
    video_name: Slug
    revision_id: RevisionId


class Revision(Model):
    schema_version: Literal[1] = 1
    ref: RevisionRef
    prepared: PreparedScript
    backend: Backend
    backend_fingerprint: str
    settings: AudioSettings
    settings_digest: Digest
    revision_digest: Digest
    created_at: str
    parent: RevisionRef | None = None
    changed_cues: list[CueId] = Field(default_factory=list)
    diagnostic_only: bool


class SaveRevisionInput(Model):
    video_name: Slug
    script: ScriptInput
    expected_plan_digest: Digest
    backend: Backend = "production"
    settings: AudioSettings = Field(default_factory=AudioSettings)


class CueEdit(Model):
    cue_id: CueId
    display_text: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT_CODEPOINTS)
    spoken_text: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT_CODEPOINTS)

    @field_validator("display_text", "spoken_text")
    @classmethod
    def text_is_valid(cls, value: str | None) -> str | None:
        return validate_text(value) if value is not None else None

    @model_validator(mode="after")
    def at_least_one_edit(self) -> CueEdit:
        if self.display_text is None and self.spoken_text is None:
            raise ValueError("Provide display_text or spoken_text.")
        return self


class ReviseInput(Model):
    ref: RevisionRef
    expected_revision_digest: Digest
    edits: list[CueEdit] = Field(min_length=1, max_length=MAX_CUES)


class WavInfo(Model):
    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    duration_seconds: float


class Artifact(Model):
    path: str
    sha256: Digest
    size_bytes: int
    media_type: str
    diagnostic_only: Literal[True] = True
    wav: WavInfo | None = None
    source_digest: Digest
    reused_from: RevisionRef | None = None
    input_artifacts: dict[str, Digest] = Field(default_factory=dict)


class Timing(Model):
    cue_id: CueId
    chunk_id: str
    start_frame: int
    end_frame: int
    gap_after_frames: int


class ArtifactIndex(Model):
    schema_version: Literal[1] = 1
    revision_digest: Digest
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    timings: dict[str, list[Timing]] = Field(default_factory=dict)


class Job(Model):
    schema_version: Literal[1] = 1
    job_id: JobId
    ref: RevisionRef
    revision_digest: Digest
    kind: Kind
    stage: Stage
    status: JobStatus
    created_at: str
    updated_at: str
    owner: str
    completed_chunks: int = 0
    total_chunks: int = 0
    attempts: dict[str, int] = Field(default_factory=dict)
    active_chunk: str | None = None
    submission_pending: bool = False
    problem_code: str | None = None
    next_action: str
    needs_user_action: bool = False
    diagnostic_only: Literal[True] = True


class ReviewReceipt(Model):
    schema_version: Literal[1] = 1
    revision_digest: Digest
    stage: ReviewStage
    subject_digest: Digest
    recorded_at: str
    meaning: Literal["local_review_acknowledgment_not_media_rights"] = (
        "local_review_acknowledgment_not_media_rights"
    )


class BackendCapability(Model):
    available: bool
    diagnostic_only: bool
    reason: str
    next_action: str


class Capabilities(Model):
    version: str
    sdk_version: str
    transport: Literal["stdio"] = "stdio"
    production_audio: BackendCapability
    real_person_lip_sync: BackendCapability
    diagnostic_audio: BackendCapability
    diagnostic_video: BackendCapability
    provider_limits_verified: Literal[False] = False
    host_limits: dict[str, int]
    normalization_default: Literal["none"] = "none"
    review_stages: list[ReviewStage]
    review_authority: str
    model_notes: list[str]
    uploads: Literal[False] = False
    support_url: str


class Inspection(Model):
    ref: RevisionRef
    revision_digest: Digest
    backend: Backend
    artifacts: list[Artifact]
    total: int
    next_offset: int | None
    timings: dict[Stage, list[Timing]]
    verified: Literal["sha256_and_file_structure"] = "sha256_and_file_structure"
    perceptual_quality_assessed: Literal[False] = False
    lip_sync_assessed: Literal[False] = False
    warning: str = "Diagnostic tones and test-pattern video are not speech or lip-sync."
