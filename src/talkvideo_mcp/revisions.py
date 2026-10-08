from __future__ import annotations

import json
from datetime import UTC, datetime

from talkvideo_mcp.audio import parse_wav, validate_settings
from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.models import (
    Artifact,
    ArtifactIndex,
    AudioSettings,
    Backend,
    ReviewReceipt,
    ReviewStage,
    Revision,
    RevisionRef,
    canonical_bytes,
    digest_bytes,
)
from talkvideo_mcp.storage import LocalStore


def now() -> str:
    return datetime.now(UTC).isoformat()


def revision_path(ref: RevisionRef, filename: str) -> str:
    return f"{ref.video_name}/{ref.revision_id}/{filename}"


def settings_digest(settings: AudioSettings, backend: Backend, fingerprint: str) -> str:
    return digest_bytes(canonical_bytes(settings) + backend.encode() + fingerprint.encode())


def revision_digest(revision: Revision) -> str:
    value = revision.model_dump(mode="json", exclude={"revision_digest"})
    return digest_bytes(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def load_revision(store: LocalStore, ref: RevisionRef) -> Revision:
    revision = store.read_model(revision_path(ref, "revision.json"), Revision)
    if (
        revision.ref != ref
        or revision.revision_digest != revision_digest(revision)
        or revision.settings_digest
        != settings_digest(revision.settings, revision.backend, revision.backend_fingerprint)
    ):
        raise TalkVideoError(
            "revision_integrity",
            "The immutable revision or its settings no longer match their digest.",
            "Do not resume modified state. Preserve the original and create a new revision.",
            needs_user_action=True,
        )
    return revision


def load_index(store: LocalStore, revision: Revision) -> ArtifactIndex:
    index = store.read_model(revision_path(revision.ref, "artifacts.json"), ArtifactIndex)
    if index.revision_digest != revision.revision_digest:
        raise TalkVideoError(
            "artifact_integrity",
            "Artifact metadata belongs to a different revision.",
            "Do not resume; inspect local state and create a new revision.",
            needs_user_action=True,
        )
    return index


def verified_artifact(store: LocalStore, revision: Revision, artifact: Artifact) -> bytes:
    data = store.read_bytes(revision_path(revision.ref, artifact.path))
    if len(data) != artifact.size_bytes or digest_bytes(data) != artifact.sha256:
        raise TalkVideoError(
            "artifact_integrity",
            "A completed artifact is missing bytes or does not match its SHA-256.",
            "Do not regenerate over it. Preserve evidence and create a new revision.",
            needs_user_action=True,
        )
    if artifact.media_type == "audio/wav":
        info, _ = parse_wav(data)
        validate_settings(info, revision.settings)
        if info != artifact.wav:
            raise TalkVideoError(
                "artifact_integrity",
                "The WAV frame metadata does not match its contents.",
                "Inspect the revision; do not resume changed artifacts.",
                needs_user_action=True,
            )
    return data


def review_subject(store: LocalStore, revision: Revision, stage: ReviewStage) -> str:
    if stage == "script":
        return revision.revision_digest
    filename = "preview.wav" if stage == "audio_preview" else "preview.mp4"
    index = load_index(store, revision)
    artifact = index.artifacts.get(filename)
    if artifact is None:
        raise TalkVideoError(
            "preview_missing",
            "The requested preview does not exist yet.",
            "Complete and inspect the short preview before recording a review.",
            needs_user_action=True,
        )
    verified_artifact(store, revision, artifact)
    return artifact.sha256


def has_review(store: LocalStore, revision: Revision, stage: ReviewStage) -> bool:
    path = revision_path(revision.ref, f"reviews/{stage}.json")
    if not store.exists(path):
        return False
    receipt = store.read_model(path, ReviewReceipt)
    return (
        receipt.revision_digest == revision.revision_digest
        and receipt.stage == stage
        and receipt.subject_digest == review_subject(store, revision, stage)
    )


def record_review(
    store: LocalStore, revision: Revision, stage: ReviewStage, expected_subject: str
) -> ReviewReceipt:
    """Called only by the interactive local CLI; never registered as an MCP tool."""
    current = load_revision(store, revision.ref)
    subject = review_subject(store, current, stage)
    if current.revision_digest != revision.revision_digest or subject != expected_subject:
        raise TalkVideoError(
            "review_changed",
            "The revision or preview changed during review.",
            "Inspect the current content again before acknowledging it.",
            needs_user_action=True,
        )
    path = revision_path(revision.ref, f"reviews/{stage}.json")
    if has_review(store, current, stage):
        return store.read_model(path, ReviewReceipt)
    receipt = ReviewReceipt(
        revision_digest=current.revision_digest,
        stage=stage,
        subject_digest=subject,
        recorded_at=now(),
    )
    store.write_model(path, receipt)
    return receipt
