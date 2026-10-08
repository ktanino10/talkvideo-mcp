import asyncio
import json

import pytest

from talkvideo_mcp.audio import AmbiguousSubmission, DiagnosticTone, SafeToRetry
from talkvideo_mcp.engine import Engine
from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.models import CueEdit, CueInput, ReviseInput, SaveRevisionInput, ScriptInput
from talkvideo_mcp.revisions import load_index, record_review, review_subject
from talkvideo_mcp.text import prepare_script


def save(engine, texts=("First.", "Second.", "Last."), backend="diagnostic"):
    script = ScriptInput(cues=[CueInput(display_text=text) for text in texts])
    return engine.save_revision(
        SaveRevisionInput(
            video_name="example",
            script=script,
            expected_plan_digest=prepare_script(script).plan_digest,
            backend=backend,
        )
    )


def review(engine, revision, stage):
    record_review(engine.store, revision, stage, review_subject(engine.store, revision, stage))


async def finish(engine, job_id):
    async with asyncio.timeout(10):
        while True:
            job = engine.get_job(job_id)
            if job.status not in {"queued", "running"}:
                return job
            await asyncio.sleep(0.01)


async def test_production_always_unavailable_and_reads_do_not_write(tmp_path):
    engine = Engine(tmp_path / "output")
    try:
        assert not engine.capabilities().production_audio.available
        assert not engine.store.root.exists()
        revision = save(engine, backend="production")
        for kind in ["audio", "video"]:
            with pytest.raises(TalkVideoError, match="production_unavailable"):
                engine.start_job(revision.ref, kind, "preview")
        assert not engine.jobs
    finally:
        await engine.close()


async def test_preview_full_review_and_idempotent_start(tmp_path):
    engine = Engine(tmp_path / "output", enable_diagnostics=True)
    try:
        revision = save(engine)
        waiting = engine.start_job(revision.ref, "audio", "preview")
        assert waiting.status == "awaiting_review" and waiting.needs_user_action
        assert not load_index(engine.store, revision).artifacts
        review(engine, revision, "script")
        job = engine.resume_job(waiting.job_id)
        assert (await finish(engine, job.job_id)).status == "succeeded"
        assert engine.start_job(revision.ref, "audio", "preview").job_id == job.job_id
        full = engine.start_job(revision.ref, "audio", "full")
        assert full.status == "awaiting_review"
        review(engine, revision, "audio_preview")
        engine.resume_job(full.job_id)
        done = await finish(engine, full.job_id)
        assert done.status == "succeeded" and done.attempts == {}
        inspection = await engine.inspect_output(revision.ref, 0, 2)
        assert len(inspection.artifacts) == 2 and inspection.next_offset == 2
        assert not inspection.lip_sync_assessed and not inspection.perceptual_quality_assessed
    finally:
        await engine.close()


async def test_revision_reuses_only_unchanged_cues_and_retimes(tmp_path):
    engine = Engine(tmp_path / "output", enable_diagnostics=True)
    try:
        base = save(engine, ("一。", "二。", "三。"))
        review(engine, base, "script")
        original_job = engine.start_job(base.ref, "audio", "preview")
        assert (await finish(engine, original_job.job_id)).status == "succeeded"
        original = load_index(engine.store, base)
        edited = engine.revise_cues(
            ReviseInput(
                ref=base.ref,
                expected_revision_digest=base.revision_digest,
                edits=[CueEdit(cue_id="c0002", spoken_text="これは長い説明に修正しました。")],
            )
        )
        assert edited.ref != base.ref and edited.parent == base.ref
        reused = load_index(engine.store, edited)
        assert set(reused.artifacts) == {"chunks/c0001-p0001.wav", "chunks/c0003-p0001.wav"}
        assert not reused.timings
        assert [c.cue_id for c in edited.prepared.cues] == ["c0001", "c0002", "c0003"]
        new_job = engine.start_job(edited.ref, "audio", "preview")
        assert new_job.status == "awaiting_review"
        review(engine, edited, "script")
        engine.resume_job(new_job.job_id)
        assert (await finish(engine, new_job.job_id)).status == "succeeded"
        updated = load_index(engine.store, edited)
        assert (
            updated.timings["preview"][2].start_frame > original.timings["preview"][2].start_frame
        )
        assert updated.artifacts["chunks/c0003-p0001.wav"].sha256 == (
            original.artifacts["chunks/c0003-p0001.wav"].sha256
        )
        assert load_index(engine.store, base) == original
    finally:
        await engine.close()


class FailingTone(DiagnosticTone):
    def __init__(self, ambiguous=False):
        self.calls = 0
        self.ambiguous = ambiguous

    async def synthesize(self, text, settings):
        self.calls += 1
        if self.ambiguous:
            raise AmbiguousSubmission()
        raise SafeToRetry()


@pytest.mark.parametrize(
    "ambiguous,code,calls", [(True, "ambiguous_submission", 1), (False, "retry_exhausted", 3)]
)
async def test_retry_classification_is_durable_and_bounded(tmp_path, ambiguous, code, calls):
    tone = FailingTone(ambiguous)
    engine = Engine(tmp_path / "output", enable_diagnostics=True, provider=tone)
    try:
        revision = save(engine)
        review(engine, revision, "script")
        job = engine.start_job(revision.ref, "audio", "preview")
        done = await finish(engine, job.job_id)
        assert done.problem_code == code and tone.calls == calls
        with pytest.raises(TalkVideoError, match=code):
            engine.resume_job(job.job_id)
        assert tone.calls == calls
    finally:
        await engine.close()
    restarted = Engine(tmp_path / "output", enable_diagnostics=True, provider=tone)
    try:
        with pytest.raises(TalkVideoError, match=code):
            restarted.resume_job(job.job_id)
        assert tone.calls == calls
    finally:
        await restarted.close()


class PausedTone(DiagnosticTone):
    def __init__(self):
        self.started = asyncio.Event()
        self.pause = True

    async def synthesize(self, text, settings):
        if self.pause:
            self.started.set()
            await asyncio.Event().wait()
        return await super().synthesize(text, settings)


async def test_cancel_resume_and_shutdown(tmp_path):
    tone = PausedTone()
    engine = Engine(tmp_path / "output", enable_diagnostics=True, provider=tone)
    revision = save(engine)
    review(engine, revision, "script")
    job = engine.start_job(revision.ref, "audio", "preview")
    await tone.started.wait()
    assert (await engine.cancel_job(job.job_id)).status == "cancelled"
    tone.started.clear()
    engine.resume_job(job.job_id)
    await tone.started.wait()
    await engine.close()
    assert engine.get_job(job.job_id).status == "interrupted"
    tone.pause = False
    restarted = Engine(tmp_path / "output", enable_diagnostics=True, provider=tone)
    try:
        restarted.resume_job(job.job_id)
        assert (await finish(restarted, job.job_id)).status == "succeeded"
    finally:
        await restarted.close()


async def test_tampered_output_blocks_resume_without_overwrite(tmp_path):
    engine = Engine(tmp_path / "output", enable_diagnostics=True)
    try:
        revision = save(engine)
        review(engine, revision, "script")
        job = engine.start_job(revision.ref, "audio", "preview")
        assert (await finish(engine, job.job_id)).status == "succeeded"
        path = (
            engine.store.root / revision.ref.video_name / revision.ref.revision_id / "preview.wav"
        )
        path.write_bytes(b"<html>corrupt</html>")
        with pytest.raises(TalkVideoError, match="artifact_integrity"):
            engine.resume_job(job.job_id)
        assert path.read_bytes() == b"<html>corrupt</html>"
    finally:
        await engine.close()


class InvalidTone(DiagnosticTone):
    async def synthesize(self, text, settings):
        return b"<html>not audio</html>"


async def test_invalid_provider_bytes_never_complete_a_cue(tmp_path):
    engine = Engine(tmp_path / "output", enable_diagnostics=True, provider=InvalidTone())
    try:
        revision = save(engine)
        review(engine, revision, "script")
        job = engine.start_job(revision.ref, "audio", "preview")
        done = await finish(engine, job.job_id)
        assert done.status == "failed" and done.problem_code == "invalid_wav"
        assert done.completed_chunks == 0 and not load_index(engine.store, revision).artifacts
    finally:
        await engine.close()


async def test_orphan_output_does_not_trigger_regeneration(tmp_path):
    tone = FailingTone()
    engine = Engine(tmp_path / "output", enable_diagnostics=True, provider=tone)
    try:
        revision = save(engine)
        review(engine, revision, "script")
        path = (
            engine.store.root
            / revision.ref.video_name
            / revision.ref.revision_id
            / "chunks/c0001-p0001.wav"
        )
        path.parent.mkdir()
        path.write_bytes(b"untracked")
        job = engine.start_job(revision.ref, "audio", "preview")
        done = await finish(engine, job.job_id)
        assert done.problem_code == "untracked_output" and done.needs_user_action
        assert tone.calls == 0 and path.read_bytes() == b"untracked"
    finally:
        await engine.close()


async def test_settings_and_timing_changes_are_detected(tmp_path):
    engine = Engine(tmp_path / "output", enable_diagnostics=True)
    try:
        revision = save(engine)
        review(engine, revision, "script")
        job = engine.start_job(revision.ref, "audio", "preview")
        assert (await finish(engine, job.job_id)).status == "succeeded"
        folder = engine.store.root / revision.ref.video_name / revision.ref.revision_id
        index_path = folder / "artifacts.json"
        index = json.loads(index_path.read_text())
        index["timings"]["preview"][1]["start_frame"] += 1
        index_path.write_text(json.dumps(index))
        with pytest.raises(TalkVideoError, match="artifact_integrity"):
            await engine.inspect_output(revision.ref, 0, 20)
        manifest_path = folder / "revision.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["settings"]["gap_ms"] += 1
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(TalkVideoError, match="revision_integrity"):
            engine.resume_job(job.job_id)
    finally:
        await engine.close()


async def test_possible_submission_on_crash_is_not_repeated(tmp_path):
    tone = FailingTone()
    engine = Engine(tmp_path / "output", enable_diagnostics=True, provider=tone)
    revision = save(engine)
    job = engine.start_job(revision.ref, "audio", "preview")
    path = f".state/jobs/{job.job_id}.json"
    job.status = "running"
    job.submission_pending = True
    engine.store.write_model(path, job, replace=True)
    await engine.close()
    restarted = Engine(tmp_path / "output", enable_diagnostics=True, provider=tone)
    try:
        before = restarted.store.read_bytes(path)
        observed = restarted.get_job(job.job_id)
        assert observed.problem_code == "ownership_unconfirmed"
        assert restarted.store.read_bytes(path) == before
        with pytest.raises(TalkVideoError, match="ambiguous_submission"):
            restarted.resume_job(job.job_id)
        assert tone.calls == 0
    finally:
        await restarted.close()


async def test_display_only_revision_keeps_all_audio_and_no_reviews(tmp_path):
    engine = Engine(tmp_path / "output", enable_diagnostics=True)
    try:
        revision = save(engine)
        review(engine, revision, "script")
        job = engine.start_job(revision.ref, "audio", "preview")
        assert (await finish(engine, job.job_id)).status == "succeeded"
        edited = engine.revise_cues(
            ReviseInput(
                ref=revision.ref,
                expected_revision_digest=revision.revision_digest,
                edits=[CueEdit(cue_id="c0002", display_text="Caption changed only.")],
            )
        )
        assert len(load_index(engine.store, edited).artifacts) == 3
        assert edited.prepared.cues[1].spoken_text == "Second."
        assert engine.start_job(edited.ref, "audio", "preview").status == "awaiting_review"
    finally:
        await engine.close()


async def test_revision_count_budget_and_full_duration_preflight(tmp_path, monkeypatch):
    import talkvideo_mcp.engine as engine_module

    monkeypatch.setattr(engine_module, "MAX_REVISIONS", 2)
    engine = Engine(tmp_path / "output", enable_diagnostics=True)
    try:
        save(engine)
        long_revision = save(engine, tuple("a" * 150 for _ in range(125)))
        with pytest.raises(TalkVideoError, match="audio_duration_limit"):
            engine.start_job(long_revision.ref, "audio", "full")
        assert not engine.jobs
        with pytest.raises(TalkVideoError, match="storage_limit"):
            save(engine)
    finally:
        await engine.close()
