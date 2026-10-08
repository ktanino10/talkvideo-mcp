from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from pydantic import ValidationError

from talkvideo_mcp import __version__
from talkvideo_mcp.audio import (
    AmbiguousSubmission,
    AudioProvider,
    DiagnosticTone,
    SafeToRetry,
    SubmissionRejected,
    assemble_wav,
    parse_wav,
    validate_settings,
)
from talkvideo_mcp.coefont import (
    DOWNLOAD_SECONDS,
    MAX_DOWNLOAD_ATTEMPTS,
    MAX_LOCAL_RETRY_WAIT,
    MAX_REDIRECTS,
    MAX_REQUEST_BYTES,
    MAX_REQUESTS_PER_ROOT,
    POST_SECONDS,
    CoefontProvider,
    RetrievalDeferred,
)
from talkvideo_mcp.errors import SUPPORT_URL, TalkVideoError, unavailable
from talkvideo_mcp.media import inspect_video, media_tools, render_diagnostic_video
from talkvideo_mcp.models import (
    MAX_ATTEMPTS,
    MAX_AUDIO_SECONDS,
    MAX_CHUNKS,
    MAX_CUES,
    MAX_FILE_BYTES,
    MAX_JOBS,
    MAX_JSON_BYTES,
    MAX_PREVIEW_SECONDS,
    MAX_QUEUED_JOBS,
    MAX_REVISIONS,
    MAX_TEXT_BYTES,
    MAX_TEXT_CODEPOINTS,
    OFFICIAL_PREVIEW_CODEPOINTS,
    PREVIEW_CHUNKS,
    Artifact,
    ArtifactIndex,
    AudioNormalization,
    BackendCapability,
    Capabilities,
    Chunk,
    CueInput,
    FileInputCapability,
    Inspection,
    Job,
    Kind,
    PreparedCue,
    PreparedScript,
    ReviewStage,
    ReviseInput,
    Revision,
    RevisionRef,
    SaveRevisionInput,
    ScriptFileSource,
    ScriptInput,
    Stage,
    Timing,
    VideoDecodeEvidence,
    WavInfo,
    digest_bytes,
)
from talkvideo_mcp.revisions import (
    has_review,
    load_index,
    load_revision,
    now,
    revision_digest,
    revision_path,
    settings_digest,
    verified_artifact,
)
from talkvideo_mcp.storage import LocalStore
from talkvideo_mcp.text import prepare_script

logger = logging.getLogger(__name__)
MAX_ATTEMPT_SECONDS = 10
MAX_JOB_SECONDS = 180


def chunk_source(revision: Revision, chunk: Chunk) -> str:
    return digest_bytes((revision.settings_digest + chunk.sha256).encode())


def final_source(revision: Revision, kind: Kind, stage: Stage) -> str:
    return digest_bytes((revision.revision_digest + kind + stage + "-v1").encode())


class Engine:
    def __init__(
        self,
        root: Path,
        *,
        enable_diagnostics: bool = False,
        provider: AudioProvider | None = None,
        input_root: Path | None = None,
        official_provider: CoefontProvider | None = None,
        production_missing: tuple[str, ...] = ("explicit_operator_configuration",),
    ) -> None:
        self.store = LocalStore(root)
        self.enable_diagnostics = enable_diagnostics
        self.provider: AudioProvider = provider if provider is not None else DiagnosticTone()
        self.input_store = LocalStore(input_root) if input_root is not None else None
        self.official_provider = official_provider
        self.production_missing = production_missing
        if self.official_provider is not None:
            self.official_provider.bind_store(self.store)
        if self.input_store is not None and (
            self.input_store.root.is_relative_to(self.store.root)
            or self.store.root.is_relative_to(self.input_store.root)
        ):
            raise TalkVideoError(
                "overlapping_roots",
                "Script inputs and private output/provider state must use disjoint roots.",
                "Use separate input/output folders; do not expose private journals as scripts.",
                needs_user_action=True,
            )
        self.owner = uuid.uuid4().hex
        self.jobs: dict[str, Job] = {}
        self._ready = False
        self._worker: asyncio.Task[None] | None = None
        self._closing = False
        self._worker_failed = False

    def capabilities(self) -> Capabilities:
        blocked = BackendCapability(
            available=False,
            diagnostic_only=False,
            reason="No implemented, authorized production backend in this release.",
            next_action="Prepare scripts only; establish rights and validate a backend separately.",
        )
        official = self.official_provider
        production_ready = official is not None and official.blocked_reason is None
        production = BackendCapability(
            available=production_ready,
            implemented=True,
            configured=official is not None,
            diagnostic_only=official.fixture_only if official is not None else False,
            authorization_status=(
                "operator_confirmed_not_independently_verified"
                if official is not None
                else "not_established"
            ),
            live_verified=official.live_verified if official is not None else False,
            execution_mode=(
                ("mock" if official.fixture_only else "live")
                if official is not None
                else "disabled"
            ),
            reason=(
                f"Official API blocked: {official.blocked_reason}."
                if official is not None and official.blocked_reason is not None
                else "Official API adapter implemented; configuration/contract/voice/paid-use "
                "confirmation and credentials are separate from local review receipts. "
                + ("Missing: " + ", ".join(self.production_missing) if official is None else "")
            ),
            next_action="Only the operator may configure an eligible official API contract, "
            "private-use voice permission, paid requests, credentials and exact download hosts. "
            "Hiroyuki Maker automation is prohibited. No voice or plan is selected automatically.",
        )
        diagnostics = BackendCapability(
            available=self.enable_diagnostics,
            diagnostic_only=True,
            reason="440 Hz diagnostic tones, not speech or any person's voice.",
            next_action="Start the server with --enable-diagnostics only for synthetic testing.",
            implemented=True,
            configured=self.enable_diagnostics,
            execution_mode="diagnostic" if self.enable_diagnostics else "disabled",
        )
        video = BackendCapability(
            available=self.enable_diagnostics and media_tools() is not None,
            diagnostic_only=True,
            reason="Local FFmpeg test-pattern mux only; not a face or lip-sync model.",
            next_action="Enable diagnostics and provide local FFmpeg/ffprobe; no cloud fallback.",
            implemented=True,
            configured=self.enable_diagnostics and media_tools() is not None,
            execution_mode="diagnostic" if self.enable_diagnostics else "disabled",
        )
        return Capabilities(
            version=__version__,
            sdk_version="2.3.0",
            production_audio=production,
            real_person_lip_sync=blocked,
            diagnostic_audio=diagnostics,
            diagnostic_video=video,
            script_file_input=FileInputCapability(
                available=self.input_store is not None,
                next_action=(
                    "Use a user-designated relative UTF-8 filename, exclusive with inline cues."
                    if self.input_store is not None
                    else "The operator must set --input-root to an approved local input folder."
                ),
            ),
            host_limits={
                "codepoints_per_track": MAX_TEXT_CODEPOINTS,
                "utf8_bytes_per_track": MAX_TEXT_BYTES,
                "cues": MAX_CUES,
                "chunks": MAX_CHUNKS,
                "preview_chunks": PREVIEW_CHUNKS,
                "official_preview_chunks": 1,
                "official_preview_codepoints": OFFICIAL_PREVIEW_CODEPOINTS,
                "preview_audio_seconds": MAX_PREVIEW_SECONDS,
                "audio_seconds_including_gaps": MAX_AUDIO_SECONDS,
                "file_bytes": MAX_FILE_BYTES,
                "manifest_bytes": MAX_JSON_BYTES,
                "jobs_per_root": MAX_JOBS,
                "revisions_per_root": MAX_REVISIONS,
                "queued_jobs": MAX_QUEUED_JOBS,
                "concurrent_jobs": 1,
                "attempts_per_chunk_across_resumes": MAX_ATTEMPTS,
                "attempt_seconds": MAX_ATTEMPT_SECONDS,
                "diagnostic_attempt_seconds": MAX_ATTEMPT_SECONDS,
                "official_post_seconds": POST_SECONDS,
                "official_get_chain_seconds": DOWNLOAD_SECONDS,
                "official_request_bytes": MAX_REQUEST_BYTES,
                "official_requests_per_root": MAX_REQUESTS_PER_ROOT,
                "official_get_attempts": MAX_DOWNLOAD_ATTEMPTS,
                "official_redirects": MAX_REDIRECTS,
                "official_automatic_wait_seconds": int(MAX_LOCAL_RETRY_WAIT),
                "job_seconds": MAX_JOB_SECONDS,
            },
            review_stages=["script", "audio_preview", "video_preview"],
            review_authority="Interactive local CLI only; acknowledgments do not establish rights.",
            model_notes=[
                "No model weights or real-person footage are installed or downloaded.",
                "Apple Silicon/MPS support and perceptual quality are unverified.",
                "Wav2Lip has separate restrictive terms and legacy dependencies.",
                "MuseTalk's documented CUDA path is not a verified Apple Silicon backend.",
            ],
            support_url=SUPPORT_URL,
            external_text_transmission_enabled=production_ready and not production.diagnostic_only,
        )

    def _save_job(self, job: Job) -> None:
        job.updated_at = now()
        self.store.write_model(f".state/jobs/{job.job_id}.json", job, replace=True)
        self.jobs[job.job_id] = job

    def _writable(self) -> None:
        if self._closing or self._worker_failed:
            raise TalkVideoError(
                "engine_unavailable",
                "The job engine is shutting down or requires recovery.",
                "Restart this local server and inspect the durable job state.",
                needs_user_action=True,
            )
        if self._ready:
            return
        self.store.acquire_writer()
        for name in self.store.list_names(".state/jobs", limit=MAX_JOBS):
            job = self.store.read_model(f".state/jobs/{name}", Job)
            if name != f"{job.job_id}.json":
                raise TalkVideoError(
                    "invalid_manifest",
                    "A job manifest has an unexpected filename.",
                    "Preserve local state and inspect it before resuming.",
                    needs_user_action=True,
                )
            self.jobs[job.job_id] = job
            if job.status in {"queued", "running"}:
                job.status = "needs_user_action" if job.submission_pending else "interrupted"
                job.problem_code = (
                    "ambiguous_submission" if job.submission_pending else "interrupted"
                )
                job.needs_user_action = job.submission_pending
                job.next_action = (
                    "Reconcile the possible submission outside MCP; never retry it automatically."
                    if job.submission_pending
                    else "Resume the same job after verifying its immutable artifacts."
                )
                self._save_job(job)
        self._ready = True

    def get_revision(self, ref: RevisionRef) -> Revision:
        return load_revision(self.store, ref)

    def prepare_script(self, request: ScriptInput) -> PreparedScript:
        if request.script_file is None:
            return prepare_script(request)
        if self.input_store is None:
            raise TalkVideoError(
                "input_root_required",
                "No script input root is configured; no file was read.",
                "The operator must set --input-root to an approved local folder.",
                needs_user_action=True,
            )
        try:
            raw = self.input_store.read_bytes(request.script_file, limit=MAX_TEXT_BYTES)
        except TalkVideoError as exc:
            raise TalkVideoError(
                exc.problem.code,
                "The script file could not be read safely within the configured input root.",
                "Use a bounded regular UTF-8 file and a safe relative path.",
                needs_user_action=True,
            ) from exc
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise TalkVideoError(
                "invalid_utf8",
                "The designated script file is not valid UTF-8.",
                "Save a UTF-8 copy explicitly; the original was not changed or partially decoded.",
                needs_user_action=True,
            ) from exc
        if len(text) > MAX_TEXT_CODEPOINTS:
            raise TalkVideoError(
                "script_too_large",
                "The decoded script exceeds the 20000-codepoint host limit.",
                "Designate a shorter file; its contents were not echoed or truncated.",
            )
        if not text.lstrip("\ufeff").strip():
            raise TalkVideoError(
                "empty_script_file",
                "The script file has no readable text.",
                "Provide a nonempty script.",
            )
        try:
            prepared = prepare_script(
                ScriptInput(
                    cues=[CueInput(display_text=text)],
                    normalization=request.normalization,
                    limits=request.limits,
                )
            )
        except ValidationError as exc:
            raise TalkVideoError(
                "invalid_script_text",
                "The decoded file contains unsupported script text or control characters.",
                "Correct a copy explicitly; no file content is included in this error.",
            ) from exc
        prepared.source_file = ScriptFileSource(
            relative_path=request.script_file, sha256=digest_bytes(raw), size_bytes=len(raw)
        )
        return prepared

    def get_job(self, job_id: str) -> Job:
        job = self.store.read_model(f".state/jobs/{job_id}.json", Job)
        if job.job_id != job_id:
            raise TalkVideoError(
                "invalid_manifest", "Job identity mismatch.", "Inspect the local job state."
            )
        self._refresh_provider_phase(job)
        if job.status in {"queued", "running"} and (job.owner != self.owner or self._worker_failed):
            job.status = "needs_user_action"
            job.problem_code = "ownership_unconfirmed"
            job.needs_user_action = True
            job.next_action = (
                "This server cannot confirm an active worker. Resume this job to acquire the "
                "root lock and reconcile durable state; another server may still own it."
            )
        return job

    def _production(self) -> CoefontProvider:
        if self.official_provider is None:
            raise TalkVideoError(
                "production_unavailable",
                "The official API adapter is implemented but not configured/authorized here.",
                "Keep using read-only preparation. The operator must separately establish the "
                "eligible API contract, voice permission, paid use and private configuration.",
                needs_user_action=True,
            )
        return self.official_provider

    def _refresh_provider_phase(self, job: Job) -> None:
        if self.official_provider is None or job.active_chunk is None:
            return
        revision = self.get_revision(job.ref)
        if (
            revision.backend != "production"
            or revision.backend_fingerprint != self.official_provider.fingerprint
        ):
            return
        chunk = next(
            (
                chunk
                for cue in revision.prepared.cues
                for chunk in cue.chunks
                if chunk.chunk_id == job.active_chunk
            ),
            None,
        )
        if chunk is None:
            raise TalkVideoError(
                "invalid_manifest",
                "The active cue is absent from the immutable revision.",
                "Preserve the job; do not submit another generation.",
            )
        status = self.official_provider.operation_status(chunk.text)
        if status in {"retrieval", "rejected"}:
            job.submission_pending = False
            job.retrieval_pending = status == "retrieval"
            job.retry_not_before = self.official_provider.retry_not_before(chunk.text)
            if status == "retrieval" and job.problem_code == "ambiguous_submission":
                job.problem_code = "coefont_retrieval_pending"
                job.next_action = (
                    "Resume download/local normalization only; the POST is not repeated."
                )

    def save_revision(self, request: SaveRevisionInput) -> Revision:
        prepared = self.prepare_script(request.script)
        if prepared.plan_digest != request.expected_plan_digest:
            raise TalkVideoError(
                "plan_changed",
                "The supplied script does not match the prepared digest.",
                "Prepare the exact current script and review its normalization/diff first.",
                needs_user_action=True,
            )
        if request.backend == "diagnostic" and not self.enable_diagnostics:
            raise TalkVideoError(
                "diagnostics_disabled",
                "Diagnostic media is disabled for this server.",
                "Explicitly start a separate local test server with --enable-diagnostics.",
                needs_user_action=True,
            )
        self._writable()
        fingerprint = (
            self.provider.fingerprint
            if request.backend == "diagnostic"
            else self.official_provider.fingerprint
            if self.official_provider is not None
            else "unconfigured-official-coefont-v2"
        )
        revision = Revision(
            ref=RevisionRef(video_name=request.video_name, revision_id=f"r-{uuid.uuid4().hex}"),
            prepared=prepared,
            backend=request.backend,
            backend_fingerprint=fingerprint,
            settings=request.settings,
            settings_digest=settings_digest(request.settings, request.backend, fingerprint),
            revision_digest="0" * 64,
            created_at=now(),
            diagnostic_only=(
                request.backend == "diagnostic"
                or (self.official_provider is not None and self.official_provider.fixture_only)
            ),
        )
        revision.revision_digest = revision_digest(revision)
        self._persist_revision(revision, ArtifactIndex(revision_digest=revision.revision_digest))
        return revision

    def _persist_revision(self, revision: Revision, index: ArtifactIndex) -> None:
        self._reserve_revision(revision.ref)
        self.store.write_model(revision_path(revision.ref, "artifacts.json"), index)
        self.store.write_model(revision_path(revision.ref, "revision.json"), revision)

    def _reserve_revision(self, ref: RevisionRef) -> None:
        if len(self.store.list_names(".state/revisions", limit=MAX_REVISIONS)) >= MAX_REVISIONS:
            raise TalkVideoError(
                "storage_limit",
                "The output root has reached its 200-revision limit.",
                "Use a separate root or manually archive completed projects.",
                needs_user_action=True,
            )
        self.store.write_model(f".state/revisions/{ref.revision_id}.json", ref)

    def revise_cues(self, request: ReviseInput) -> Revision:
        base = self.get_revision(request.ref)
        if base.revision_digest != request.expected_revision_digest:
            raise TalkVideoError(
                "revision_changed",
                "The expected base digest does not match this revision.",
                "Read the current revision and target its stable cue IDs.",
            )
        edits = {edit.cue_id: edit for edit in request.edits}
        if len(edits) != len(request.edits) or not edits.keys() <= {
            cue.cue_id for cue in base.prepared.cues
        }:
            raise TalkVideoError(
                "invalid_cue_edit",
                "Edited cue IDs must be unique and present in the base revision.",
                "Use get_revision to find stable cue IDs. Do not edit a job's stored script.",
            )
        cues: list[CueInput] = []
        changed: list[str] = []
        audio_changed: set[str] = set()
        for cue in base.prepared.cues:
            edit = edits.get(cue.cue_id)
            display = (
                edit.display_text
                if edit is not None and edit.display_text is not None
                else cue.display_text
            )
            spoken = (
                edit.spoken_text
                if edit is not None and edit.spoken_text is not None
                else cue.spoken_text
            )
            if display != cue.display_text or spoken != cue.spoken_text:
                changed.append(cue.cue_id)
            if spoken != cue.spoken_text:
                audio_changed.add(cue.cue_id)
            cues.append(CueInput(cue_id=cue.cue_id, display_text=display, spoken_text=spoken))
        if not changed:
            raise TalkVideoError(
                "no_changes", "No cue content changed.", "Keep using the existing revision."
            )
        prepared = prepare_script(
            ScriptInput(
                cues=cues, normalization=base.prepared.normalization, limits=base.prepared.limits
            )
        )
        self._writable()
        revision = Revision(
            ref=RevisionRef(video_name=base.ref.video_name, revision_id=f"r-{uuid.uuid4().hex}"),
            prepared=prepared,
            backend=base.backend,
            backend_fingerprint=base.backend_fingerprint,
            settings=base.settings,
            settings_digest=base.settings_digest,
            revision_digest="0" * 64,
            created_at=now(),
            parent=base.ref,
            changed_cues=changed,
            diagnostic_only=base.diagnostic_only,
        )
        revision.revision_digest = revision_digest(revision)
        self._reserve_revision(revision.ref)
        original = load_index(self.store, base)
        updated = ArtifactIndex(revision_digest=revision.revision_digest)
        for cue in prepared.cues:
            if cue.cue_id in audio_changed:
                continue
            for chunk in cue.chunks:
                name = f"chunks/{chunk.chunk_id}.wav"
                artifact = original.artifacts.get(name)
                if artifact is None:
                    continue
                if artifact.source_digest != chunk_source(base, chunk):
                    raise TalkVideoError(
                        "artifact_integrity",
                        "A reusable chunk does not match its source/settings.",
                        "Inspect the base revision; do not regenerate over mismatched artifacts.",
                        needs_user_action=True,
                    )
                data = verified_artifact(self.store, base, artifact)
                self.store.write_bytes(revision_path(revision.ref, name), data)
                if artifact.normalization is not None:
                    raw_name = artifact.normalization.raw_path
                    self.store.write_bytes(
                        revision_path(revision.ref, raw_name),
                        self.store.read_bytes(revision_path(base.ref, raw_name)),
                    )
                copied = artifact.model_copy(deep=True)
                copied.reused_from = base.ref
                updated.artifacts[name] = copied
        self.store.write_model(revision_path(revision.ref, "artifacts.json"), updated)
        self.store.write_model(revision_path(revision.ref, "revision.json"), revision)
        return revision

    def _check_backend(self, revision: Revision, kind: Kind) -> None:
        if revision.backend == "production":
            if kind == "video":
                raise unavailable("real-person lip-sync")
            official = self._production()
            if official.blocked_reason is not None:
                raise TalkVideoError(
                    official.blocked_reason,
                    "The official API rejected this request; further generation is paused.",
                    "Resolve the operator's contract/voice/request/quota condition outside MCP.",
                    needs_user_action=True,
                )
            if revision.backend_fingerprint != official.fingerprint:
                raise TalkVideoError(
                    "backend_changed",
                    "This revision does not match the configured official voice/options/contract.",
                    "Review a new revision. Existing reviews cannot unlock provider permissions.",
                    needs_user_action=True,
                )
            return
        if not self.enable_diagnostics:
            raise TalkVideoError(
                "diagnostics_disabled",
                "Diagnostic media is disabled in this server.",
                "Restart with --enable-diagnostics only for synthetic testing.",
                needs_user_action=True,
            )
        if revision.backend_fingerprint != self.provider.fingerprint:
            raise TalkVideoError(
                "backend_changed",
                "The backend fingerprint differs from the revision's generation settings.",
                "Use the original implementation or create and review a new revision.",
                needs_user_action=True,
            )
        if kind == "video" and media_tools() is None:
            raise TalkVideoError(
                "media_tools_unavailable",
                "Diagnostic video needs local FFmpeg and ffprobe.",
                "Use audio/prepare only until local tools are installed; no cloud fallback exists.",
                needs_user_action=True,
            )

    @staticmethod
    def _selected(revision: Revision, stage: Stage) -> list[tuple[PreparedCue, Chunk]]:
        chunks = [(cue, chunk) for cue in revision.prepared.cues for chunk in cue.chunks]
        if stage == "preview":
            return chunks[:1] if revision.backend == "production" else chunks[:PREVIEW_CHUNKS]
        return chunks

    def _missing_reviews(self, revision: Revision, kind: Kind, stage: Stage) -> list[ReviewStage]:
        required: list[ReviewStage] = ["script"]
        if stage == "full":
            required.append("audio_preview")
            if kind == "video":
                required.append("video_preview")
        return [review for review in required if not has_review(self.store, revision, review)]

    def _verify_index(self, revision: Revision) -> ArtifactIndex:
        index = load_index(self.store, revision)
        sources = {
            f"chunks/{chunk.chunk_id}.wav": chunk_source(revision, chunk)
            for cue in revision.prepared.cues
            for chunk in cue.chunks
        }
        sources.update(
            {
                "preview.wav": final_source(revision, "audio", "preview"),
                "narration.wav": final_source(revision, "audio", "full"),
                "preview.mp4": final_source(revision, "video", "preview"),
                "result.mp4": final_source(revision, "video", "full"),
            }
        )
        for name, artifact in index.artifacts.items():
            if name != artifact.path or sources.get(name) != artifact.source_digest:
                raise TalkVideoError(
                    "artifact_integrity",
                    "An artifact does not match its immutable source/settings.",
                    "Preserve and inspect the revision; create a new revision for changes.",
                    needs_user_action=True,
                )
            verified_artifact(self.store, revision, artifact)
        for stage in ("preview", "full"):
            self._verify_assembly(revision, index, stage)
        return index

    def _verify_assembly(self, revision: Revision, index: ArtifactIndex, stage: Stage) -> None:
        filename = "preview.wav" if stage == "preview" else "narration.wav"
        output = index.artifacts.get(filename)
        if output is None:
            if stage in index.timings:
                raise TalkVideoError(
                    "artifact_integrity",
                    "Timing metadata exists without its assembled audio.",
                    "Inspect the interrupted output; do not fabricate a completed artifact.",
                    needs_user_action=True,
                )
            return
        selected = self._selected(revision, stage)
        names = [f"chunks/{chunk.chunk_id}.wav" for _, chunk in selected]
        if any(name not in index.artifacts for name in names):
            raise TalkVideoError(
                "artifact_integrity",
                "Assembled audio is missing one or more source chunks.",
                "Preserve the revision for inspection.",
                needs_user_action=True,
            )
        parts = [verified_artifact(self.store, revision, index.artifacts[name]) for name in names]
        data, info, offsets = assemble_wav(parts, revision.settings)
        if stage == "preview" and info.duration_seconds > MAX_PREVIEW_SECONDS:
            raise TalkVideoError(
                "artifact_integrity",
                "The recorded preview exceeds the actual preview-duration limit.",
                "Create a deliberately shorter preview revision; do not overwrite this artifact.",
                needs_user_action=True,
            )
        timings = self._timings(selected, offsets)
        if (
            digest_bytes(data) != output.sha256
            or info != output.wav
            or index.timings.get(stage) != timings
            or output.input_artifacts != {name: index.artifacts[name].sha256 for name in names}
        ):
            raise TalkVideoError(
                "artifact_integrity",
                "Assembly, source hashes, or frame offsets differ from the completed audio.",
                "Create a new revision for edits; do not modify completed timing metadata.",
                needs_user_action=True,
            )
        video = index.artifacts.get("preview.mp4" if stage == "preview" else "result.mp4")
        if video is not None and video.input_artifacts != {filename: output.sha256}:
            raise TalkVideoError(
                "artifact_integrity",
                "Video provenance does not match the source audio.",
                "Inspect the revision; do not report a successful matching render.",
                needs_user_action=True,
            )

    @staticmethod
    def _timings(
        selected: list[tuple[PreparedCue, Chunk]], offsets: list[tuple[int, int, int]]
    ) -> list[Timing]:
        return [
            Timing(
                cue_id=cue.cue_id,
                chunk_id=chunk.chunk_id,
                start_frame=start,
                end_frame=end,
                gap_after_frames=gap,
            )
            for (cue, chunk), (start, end, gap) in zip(selected, offsets, strict=True)
        ]

    def start_job(self, ref: RevisionRef, kind: Kind, stage: Stage) -> Job:
        revision = self.get_revision(ref)
        self._check_backend(revision, kind)
        self._writable()
        for job in self.jobs.values():
            if job.ref == ref and job.kind == kind and job.stage == stage:
                self._verify_index(revision)
                return self.get_job(job.job_id)
        if kind == "audio" and revision.backend == "production":
            for _, chunk in self._selected(revision, stage):
                if stage == "preview" and chunk.codepoints > OFFICIAL_PREVIEW_CODEPOINTS:
                    raise TalkVideoError(
                        "preview_text_limit",
                        "Official speech previews require a first chunk of at most 80 codepoints.",
                        "Re-prepare with limits.codepoints=80 or an explicitly short first cue. "
                        "No generation was submitted or text dropped.",
                        needs_user_action=True,
                    )
                self._production().request_for(chunk.text).request_bytes()
        if kind == "audio" and revision.backend == "diagnostic":
            selected = self._selected(revision, stage)
            frames = sum(
                DiagnosticTone.frames(chunk.text, revision.settings) for _, chunk in selected
            )
            frames += (
                (len(selected) - 1)
                * revision.settings.sample_rate
                * revision.settings.gap_ms
                // 1000
            )
            if frames > MAX_AUDIO_SECONDS * revision.settings.sample_rate:
                raise TalkVideoError(
                    "audio_duration_limit",
                    "This diagnostic job would exceed 180 seconds including gaps.",
                    "Prepare a shorter script; the preview is only a subset, not a full render.",
                )
        if len(self.jobs) >= MAX_JOBS:
            raise TalkVideoError(
                "storage_limit",
                "The output root has reached its 1000-job limit.",
                "Use a separate output root or manually archive completed projects.",
                needs_user_action=True,
            )
        job = Job(
            job_id=f"j-{uuid.uuid4().hex}",
            ref=ref,
            revision_digest=revision.revision_digest,
            kind=kind,
            stage=stage,
            status="awaiting_review",
            created_at=now(),
            updated_at=now(),
            owner=self.owner,
            total_chunks=len(self._selected(revision, stage)) if kind == "audio" else 0,
            next_action="Check the required local review.",
            diagnostic_only=revision.diagnostic_only,
        )
        self._save_job(job)
        return self.resume_job(job.job_id)

    def resume_job(self, job_id: str) -> Job:
        self._writable()
        job = self.get_job(job_id)
        revision = self.get_revision(job.ref)
        self._check_backend(revision, job.kind)
        if job.revision_digest != revision.revision_digest:
            raise TalkVideoError(
                "revision_changed",
                "Resume cannot change a job's revision.",
                "Use revise_cues to create a new revision.",
            )
        index = self._verify_index(revision)
        if job.status in {"succeeded", "queued", "running"}:
            return job
        if job.submission_pending or job.problem_code == "ambiguous_submission":
            raise TalkVideoError(
                "ambiguous_submission",
                "An earlier submission may have succeeded; it will not be repeated.",
                "Reconcile it outside MCP. A caller approval flag is not authorization to retry.",
                needs_user_action=True,
            )
        if job.problem_code == "retry_exhausted":
            raise TalkVideoError(
                "retry_exhausted",
                "This job's cumulative retry budget is exhausted.",
                "Investigate the cause before explicitly creating and reviewing a new revision.",
                needs_user_action=True,
            )
        missing = self._missing_reviews(revision, job.kind, job.stage)
        if missing:
            job.status = "awaiting_review"
            job.problem_code = "review_required"
            job.needs_user_action = True
            job.next_action = (
                "The user must inspect and acknowledge these stages with the interactive local "
                f"'talkvideo-mcp review' command, then resume this job: {', '.join(missing)}."
            )
            self._save_job(job)
            return job
        if job.retry_not_before is not None:
            job.status = "deferred"
            job.problem_code = "coefont_retrieval_deferred"
            job.needs_user_action = True
            job.next_action = (
                f"Resume no earlier than UNIX time {job.retry_not_before:.3f}; "
                "only retrieval/local normalization may continue, never a repeated POST."
            )
            self._save_job(job)
            return job
        if job.kind == "video":
            filename = "preview.wav" if job.stage == "preview" else "narration.wav"
            if filename not in index.artifacts:
                raise TalkVideoError(
                    "audio_missing",
                    "The corresponding validated audio output is not ready.",
                    "Complete the same revision's audio job first.",
                )
        pending = sum(item.status in {"queued", "running"} for item in self.jobs.values())
        if pending >= MAX_QUEUED_JOBS:
            raise TalkVideoError(
                "queue_full", "The sequential queue is full.", "Wait for a job or cancel one."
            )
        job.status = "queued"
        job.owner = self.owner
        job.problem_code = None
        job.needs_user_action = False
        job.next_action = (
            "Poll get_job; output is diagnostic only and not yet complete."
            if revision.diagnostic_only
            else "Poll get_job. The configured API may receive text; no output is complete yet."
        )
        self._save_job(job)
        self._ensure_worker()
        return job

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(), name="talkvideo-sequential-worker")
            self._worker.add_done_callback(self._worker_done)

    def _worker_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._worker_failed = True
            logger.error("worker_failed: durable state requires recovery")

    async def _work(self) -> None:
        while not self._closing and not self._worker_failed:
            job = next((item for item in self.jobs.values() if item.status == "queued"), None)
            if job is None:
                return
            await self._execute(job)

    async def _execute(self, job: Job) -> None:
        job.status = "running"
        self._save_job(job)
        try:
            async with asyncio.timeout(MAX_JOB_SECONDS):
                revision = self.get_revision(job.ref)
                self._check_backend(revision, job.kind)
                if self._missing_reviews(revision, job.kind, job.stage):
                    raise TalkVideoError(
                        "review_required",
                        "Review acknowledgments are no longer valid.",
                        "Reinspect the exact revision and preview.",
                        needs_user_action=True,
                    )
                self._verify_index(revision)
                if job.kind == "audio":
                    try:
                        await self._audio(job, revision)
                    finally:
                        try:
                            self._refresh_provider_phase(job)
                        except TalkVideoError as error:
                            logger.error("provider_phase_unavailable code=%s", error.problem.code)
                else:
                    await self._video(job, revision)
            job.status = "succeeded"
            job.active_chunk = None
            job.submission_pending = False
            job.retrieval_pending = False
            job.retry_not_before = None
            job.next_action = "Inspect hashes/frames and obtain the user's preview review."
            if revision.diagnostic_only:
                job.next_action += " These are diagnostic/mock fixtures, not production speech."
            else:
                job.next_action += " Keep media private; live speech quality has not been assessed."
            self._save_job(job)
        except asyncio.CancelledError:
            if job.submission_pending:
                job.status = "needs_user_action"
                job.problem_code = "ambiguous_submission"
                job.needs_user_action = True
                job.next_action = (
                    "Reconcile the possible submission outside MCP before any new job."
                )
            else:
                job.status = "interrupted" if self._closing else "cancelled"
                job.next_action = "Resume this job to reuse verified completed chunks."
            self._save_job(job)
            raise
        except AmbiguousSubmission:
            job.status = "needs_user_action"
            job.problem_code = "ambiguous_submission"
            job.submission_pending = True
            job.needs_user_action = True
            job.next_action = "Reconcile the possible submission outside MCP; do not retry it."
            self._save_job(job)
        except TimeoutError:
            job.status = "needs_user_action" if job.submission_pending else "failed"
            job.problem_code = "ambiguous_submission" if job.submission_pending else "job_timeout"
            job.needs_user_action = job.submission_pending
            job.next_action = (
                "Reconcile the possible submission outside MCP."
                if job.submission_pending
                else "Inspect completed chunks and resume within the remaining attempt budget."
            )
            self._save_job(job)
        except RetrievalDeferred as exc:
            job.status = "deferred"
            job.problem_code = exc.problem.code
            job.needs_user_action = True
            job.submission_pending = False
            job.retrieval_pending = True
            job.retry_not_before = exc.retry_not_before
            job.next_action = exc.problem.next_action
            self._save_job(job)
        except TalkVideoError as exc:
            if exc.problem.code == "process_cleanup_timeout":
                self._worker_failed = True
            job.status = "needs_user_action" if job.submission_pending else "failed"
            job.problem_code = (
                "ambiguous_submission" if job.submission_pending else exc.problem.code
            )
            job.needs_user_action = job.submission_pending or exc.problem.needs_user_action
            job.next_action = (
                "Reconcile the possible submission outside MCP; do not repeat it."
                if job.submission_pending
                else exc.problem.next_action
            )
            logger.warning("job_failed code=%s", job.problem_code)
            self._save_job(job)
        except Exception as exc:
            job.status = "needs_user_action" if job.submission_pending else "failed"
            job.problem_code = (
                "ambiguous_submission" if job.submission_pending else "internal_error"
            )
            job.needs_user_action = True
            job.next_action = "Stop and inspect durable state; report only the error code locally."
            logger.error("job_internal_error type=%s", type(exc).__name__)
            self._save_job(job)

    def _put_artifact(
        self,
        revision: Revision,
        index: ArtifactIndex,
        name: str,
        data: bytes,
        source: str,
        *,
        wav: WavInfo | None = None,
        inputs: dict[str, str] | None = None,
        normalization: AudioNormalization | None = None,
    ) -> None:
        self.store.write_bytes(revision_path(revision.ref, name), data)
        index.artifacts[name] = Artifact(
            path=name,
            sha256=digest_bytes(data),
            size_bytes=len(data),
            media_type="audio/wav" if wav is not None else "video/mp4",
            wav=wav,
            source_digest=source,
            input_artifacts=inputs or {},
            diagnostic_only=revision.diagnostic_only,
            normalization=normalization,
        )
        self.store.write_model(revision_path(revision.ref, "artifacts.json"), index, replace=True)

    async def _audio(self, job: Job, revision: Revision) -> None:
        selected = self._selected(revision, job.stage)
        index = load_index(self.store, revision)
        parts: list[bytes] = []
        total_frames = 0
        for _cue, chunk in selected:
            name = f"chunks/{chunk.chunk_id}.wav"
            existing = index.artifacts.get(name)
            if existing is not None:
                data = verified_artifact(self.store, revision, existing)
            else:
                if self.store.exists(revision_path(revision.ref, name)):
                    raise TalkVideoError(
                        "untracked_output",
                        "A chunk was published without its completion record.",
                        "Inspect the orphan artifact; do not regenerate or overwrite it.",
                        needs_user_action=True,
                    )
                normalization = None
                if revision.backend == "production":
                    official = self._production()
                    status = official.operation_status(chunk.text)
                    if status == "new" and job.attempts.get(chunk.chunk_id, 0):
                        raise AmbiguousSubmission()
                    job.active_chunk = chunk.chunk_id
                    if status == "new":
                        job.attempts[chunk.chunk_id] = 1
                    job.submission_pending = status in {"new", "ambiguous"}
                    job.retrieval_pending = status == "retrieval"
                    self._save_job(job)
                    try:
                        data = await official.synthesize(chunk.text, revision.settings)
                    except SubmissionRejected:
                        job.submission_pending = False
                        job.retrieval_pending = False
                        self._save_job(job)
                        raise
                    normalization = official.materialize_source(
                        chunk.text, revision.ref, chunk.chunk_id, revision.settings
                    )
                else:
                    data = await self._diagnostic_chunk(job, chunk, revision)
                info, _ = parse_wav(data)
                validate_settings(info, revision.settings)
                if job.stage == "preview" and info.frames > MAX_PREVIEW_SECONDS * info.sample_rate:
                    raise TalkVideoError(
                        "preview_duration_limit",
                        "The actual speech preview exceeds 30 seconds; it is not a short preview.",
                        "Keep the cached source and explicitly shorten the first cue or adjust "
                        "approved speech settings. Do not claim a full/short preview succeeded.",
                        needs_user_action=True,
                    )
                if total_frames + info.frames > MAX_AUDIO_SECONDS * revision.settings.sample_rate:
                    raise TalkVideoError(
                        "audio_duration_limit",
                        "The chunk would exceed the total audio frame budget.",
                        "Use a shorter approved script; no partial assembly is accepted.",
                    )
                self._put_artifact(
                    revision,
                    index,
                    name,
                    data,
                    chunk_source(revision, chunk),
                    wav=info,
                    normalization=normalization,
                )
                job.submission_pending = False
                job.retrieval_pending = False
            info, _ = parse_wav(data)
            total_frames += info.frames
            if parts:
                total_frames += revision.settings.sample_rate * revision.settings.gap_ms // 1000
            if total_frames > MAX_AUDIO_SECONDS * revision.settings.sample_rate:
                raise TalkVideoError(
                    "audio_duration_limit",
                    "The audio including gaps exceeds the total frame budget.",
                    "Use a shorter approved script; no partial assembly is accepted.",
                )
            parts.append(data)
            job.completed_chunks = len(parts)
            job.active_chunk = None
            self._save_job(job)
            await asyncio.sleep(0)
        filename = "preview.wav" if job.stage == "preview" else "narration.wav"
        if filename not in index.artifacts:
            if self.store.exists(revision_path(revision.ref, filename)):
                raise TalkVideoError(
                    "untracked_output",
                    "Assembled audio exists without its completion record.",
                    "Inspect the orphan assembly; do not overwrite it or regenerate its cues.",
                    needs_user_action=True,
                )
            data, info, offsets = assemble_wav(parts, revision.settings)
            index.timings[job.stage] = self._timings(selected, offsets)
            self._put_artifact(
                revision,
                index,
                filename,
                data,
                final_source(revision, "audio", job.stage),
                wav=info,
                inputs={
                    f"chunks/{chunk.chunk_id}.wav": index.artifacts[
                        f"chunks/{chunk.chunk_id}.wav"
                    ].sha256
                    for _, chunk in selected
                },
            )

    async def _diagnostic_chunk(self, job: Job, chunk: Chunk, revision: Revision) -> bytes:
        while True:
            attempts = job.attempts.get(chunk.chunk_id, 0)
            if attempts >= MAX_ATTEMPTS:
                raise TalkVideoError(
                    "retry_exhausted",
                    "The cumulative per-chunk retry budget is exhausted.",
                    "Investigate before explicitly creating a new revision.",
                    needs_user_action=True,
                )
            job.active_chunk = chunk.chunk_id
            job.attempts[chunk.chunk_id] = attempts + 1
            job.submission_pending = self.provider.side_effects_possible
            self._save_job(job)
            try:
                async with asyncio.timeout(MAX_ATTEMPT_SECONDS):
                    data = await self.provider.synthesize(chunk.text, revision.settings)
                return data
            except SafeToRetry:
                job.submission_pending = False
                self._save_job(job)
                await asyncio.sleep(0.05 * (2**attempts))

    async def _video(self, job: Job, revision: Revision) -> None:
        index = load_index(self.store, revision)
        source_name = "preview.wav" if job.stage == "preview" else "narration.wav"
        filename = "preview.mp4" if job.stage == "preview" else "result.mp4"
        if filename in index.artifacts:
            return
        if self.store.exists(revision_path(revision.ref, filename)):
            raise TalkVideoError(
                "untracked_output",
                "A video exists without its completion record.",
                "Inspect it; do not automatically rerender or overwrite it.",
                needs_user_action=True,
            )
        source = index.artifacts[source_name]
        wav = verified_artifact(self.store, revision, source)
        if source.wav is None:
            raise TalkVideoError(
                "invalid_manifest", "Source WAV metadata is missing.", "Inspect the audio job."
            )
        data, _ = await render_diagnostic_video(wav, source.wav.duration_seconds)
        self._put_artifact(
            revision,
            index,
            filename,
            data,
            final_source(revision, "video", job.stage),
            inputs={source_name: source.sha256},
        )

    async def cancel_job(self, job_id: str) -> Job:
        self._writable()
        job = self.get_job(job_id)
        if job.status == "running" and self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
            self._ensure_worker()
            return self.get_job(job_id)
        if job.status in {"awaiting_review", "queued", "interrupted", "deferred"}:
            job.status = "cancelled"
            job.next_action = "Resume this job to reuse verified completed chunks."
            self._save_job(job)
        return job

    async def inspect_output(self, ref: RevisionRef, offset: int, limit: int) -> Inspection:
        revision = self.get_revision(ref)
        index = self._verify_index(revision)
        names = sorted(index.artifacts)
        page = names[offset : offset + limit]
        video_checks: dict[str, VideoDecodeEvidence] = {}
        for name in page:
            artifact = index.artifacts[name]
            if artifact.media_type == "video/mp4":
                audio_name = "preview.wav" if name == "preview.mp4" else "narration.wav"
                wav_info = index.artifacts[audio_name].wav
                if wav_info is None:
                    raise TalkVideoError(
                        "invalid_manifest",
                        "Source WAV metadata is missing.",
                        "Inspect the audio job.",
                    )
                video = await inspect_video(
                    verified_artifact(self.store, revision, artifact), wav_info.duration_seconds
                )
                if video.decode is None:
                    raise RuntimeError("Video inspection did not include full decode evidence.")
                video_checks[name] = video.decode
        return Inspection(
            ref=ref,
            revision_digest=revision.revision_digest,
            backend=revision.backend,
            artifacts=[index.artifacts[name] for name in page],
            total=len(names),
            next_offset=offset + limit if offset + limit < len(names) else None,
            timings={
                stage: index.timings[stage]
                for stage in ("preview", "full")
                if stage in index.timings
            },
            video_decode=video_checks,
            warning=(
                "Diagnostic/mock tones and test patterns are not production speech or lip-sync."
                if revision.diagnostic_only
                else "Structural checks are not voice-quality or rights assessments. "
                "Keep media private; real-person lip-sync is not provided."
            ),
        )

    async def close(self) -> None:
        self._closing = True
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        for job in self.jobs.values():
            if job.status == "queued":
                job.status = "interrupted"
                job.problem_code = "interrupted"
                job.next_action = "Resume after restarting the local server."
                self._save_job(job)
        try:
            if self.official_provider is not None:
                await self.official_provider.aclose()
        finally:
            self.store.close()
