import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from talkvideo_mcp.media import media_tools
from talkvideo_mcp.models import RevisionRef
from talkvideo_mcp.revisions import load_revision, record_review, review_subject
from talkvideo_mcp.storage import LocalStore

REPO = Path(__file__).resolve().parents[1]
READ_ONLY = {
    "talkvideo_get_capabilities",
    "talkvideo_prepare_script",
    "talkvideo_get_revision",
    "talkvideo_get_job",
    "talkvideo_inspect_output",
}
TOOLS = READ_ONLY | {
    "talkvideo_save_revision",
    "talkvideo_start_audio_job",
    "talkvideo_start_video_job",
    "talkvideo_cancel_job",
    "talkvideo_resume_job",
    "talkvideo_revise_cues",
}


def connection(root, diagnostic=True):
    args = ["-m", "talkvideo_mcp", "serve", "--root", str(root)]
    if diagnostic:
        args.append("--enable-diagnostics")
    return Client(
        StdioServerParameters(command=sys.executable, args=args, cwd=str(REPO)),
        read_timeout_seconds=30,
    )


async def call(client, name, arguments=None):
    result = await client.call_tool(name, arguments or {})
    assert not result.is_error, result.content
    assert result.structured_content["ok"]
    assert json.loads(result.content[0].text) == result.structured_content
    return result.structured_content["data"]


async def wait_job(client, job_id):
    async with asyncio.timeout(30):
        while True:
            job = await call(client, "talkvideo_get_job", {"job_id": job_id})
            if job["status"] not in {"queued", "running"}:
                return job
            await asyncio.sleep(0.01)


def fixture_review(root, ref, stage):
    # Synthetic test acknowledgment, never a real user's consent or media-rights evidence.
    store = LocalStore(root)
    revision = load_revision(store, RevisionRef.model_validate(ref))
    record_review(store, revision, stage, review_subject(store, revision, stage))


async def prepared_revision(client, text="説明です。", long=False):
    script = {"cues": [{"display_text": text}]}
    if long:
        script["limits"] = {"graphemes": 100}
    prepared = await call(client, "talkvideo_prepare_script", script)
    revision = await call(
        client,
        "talkvideo_save_revision",
        {
            "video_name": "stdio-fixture",
            "script": script,
            "expected_plan_digest": prepared["plan_digest"],
            "backend": "diagnostic",
        },
    )
    return revision


async def full_ready(client, root):
    revision = await prepared_revision(client, "あ" * 16_000, long=True)
    ref = revision["ref"]
    fixture_review(root, ref, "script")
    preview = await call(client, "talkvideo_start_audio_job", {"ref": ref, "stage": "preview"})
    assert (await wait_job(client, preview["job_id"]))["status"] == "succeeded"
    fixture_review(root, ref, "audio_preview")
    return revision


async def test_native_cli_discovery_strict_errors_and_no_read_side_effects(tmp_path):
    root = tmp_path / "output"
    async with connection(root, diagnostic=False) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == TOOLS
        for tool in listed.tools:
            assert tool.input_schema["additionalProperties"] is False
            assert tool.output_schema["type"] == "object"
            assert tool.annotations.read_only_hint == (tool.name in READ_ONLY)
            assert not tool.annotations.open_world_hint
        caps = await call(client, "talkvideo_get_capabilities")
        assert not caps["production_audio"]["available"]
        assert not caps["real_person_lip_sync"]["available"]
        assert caps["sdk_version"] == "2.3.0"
        prepared = await call(
            client,
            "talkvideo_prepare_script",
            {"cues": [{"display_text": "記号 👨‍👩‍👧‍👦 と e\u0301。"}]},
        )
        assert (
            "".join(chunk["text"] for cue in prepared["cues"] for chunk in cue["chunks"])
            == prepared["spoken_text"]
        )
        for name, arguments in [
            ("talkvideo_get_capabilities", {"approved": True}),
            ("talkvideo_get_job", {"job_id": "../escape"}),
            ("talkvideo_prepare_script", {"cues": [{"display_text": "SECRET\x00SCRIPT"}]}),
        ]:
            result = await client.call_tool(name, arguments)
            assert result.is_error
            assert result.structured_content["error"]["code"] == "invalid_input"
            assert "SECRET" not in str(result.content)
            assert not result.structured_content["ok"]
    assert not root.exists()


async def test_native_job_outlives_call_and_inspection_verifies_frames(tmp_path):
    root = tmp_path / "output"
    async with connection(root) as client:
        revision = await full_ready(client, root)
        full = await call(
            client, "talkvideo_start_audio_job", {"ref": revision["ref"], "stage": "full"}
        )
        assert full["status"] == "queued"
        active = await call(client, "talkvideo_get_job", {"job_id": full["job_id"]})
        assert active["status"] in {"queued", "running"}
        assert active["completed_chunks"] < active["total_chunks"]
        done = await wait_job(client, full["job_id"])
        assert done["status"] == "succeeded" and done["completed_chunks"] == 160
        inspected = await call(
            client,
            "talkvideo_inspect_output",
            {"ref": revision["ref"], "limit": 50, "offset": 150},
        )
        assert inspected["total"] == 162 and inspected["next_offset"] is None
        narration = next(item for item in inspected["artifacts"] if item["path"] == "narration.wav")
        assert narration["wav"]["frames"] == 160 * 16000 + 159 * 1600
        assert inspected["timings"]["full"][-1]["end_frame"] == narration["wav"]["frames"]
        assert inspected["lip_sync_assessed"] is False


@pytest.mark.parametrize("cancel", [True, False])
async def test_native_unfinished_cancel_or_shutdown_and_restart(tmp_path, cancel):
    root = tmp_path / "output"
    async with connection(root) as client:
        revision = await full_ready(client, root)
        full = await call(
            client, "talkvideo_start_audio_job", {"ref": revision["ref"], "stage": "full"}
        )
        active = await call(client, "talkvideo_get_job", {"job_id": full["job_id"]})
        assert active["status"] in {"running", "queued"}
        assert active["completed_chunks"] < active["total_chunks"]
        if cancel:
            stopped = await call(client, "talkvideo_cancel_job", {"job_id": full["job_id"]})
            assert stopped["status"] == "cancelled"
    async with connection(root) as restarted:
        stopped = await call(restarted, "talkvideo_get_job", {"job_id": full["job_id"]})
        assert stopped["status"] == ("cancelled" if cancel else "interrupted")
        resumed = await call(restarted, "talkvideo_resume_job", {"job_id": full["job_id"]})
        assert resumed["job_id"] == full["job_id"]
        done = await wait_job(restarted, full["job_id"])
        assert done["status"] == "succeeded"
        assert all(attempts <= 3 for attempts in done["attempts"].values())


async def test_repository_mcp_config_runs_via_real_sdk(tmp_path):
    config = json.loads((REPO / ".github/mcp.json").read_text())["mcpServers"]["talkvideo"]
    assert config["type"] == "stdio"
    assert "env" not in config and "allowed-tools" not in config
    async with Client(
        StdioServerParameters(command=config["command"], args=config["args"], cwd=str(REPO))
    ) as client:
        capabilities = await call(client, "talkvideo_get_capabilities")
        assert not capabilities["diagnostic_audio"]["available"]
        assert not capabilities["uploads"]


@pytest.mark.skipif(media_tools() is None, reason="Local FFmpeg/ffprobe not installed")
async def test_native_video_jobs_require_both_matching_preview_reviews(tmp_path):
    root = tmp_path / "output"
    async with connection(root) as client:
        revision = await prepared_revision(client, "synthetic diagnostic pattern " * 3)
        ref = revision["ref"]
        fixture_review(root, ref, "script")
        audio = await call(client, "talkvideo_start_audio_job", {"ref": ref})
        assert (await wait_job(client, audio["job_id"]))["status"] == "succeeded"
        preview = await call(client, "talkvideo_start_video_job", {"ref": ref})
        assert (await wait_job(client, preview["job_id"]))["status"] == "succeeded"
        full_video = await call(client, "talkvideo_start_video_job", {"ref": ref, "stage": "full"})
        assert full_video["status"] == "awaiting_review"
        fixture_review(root, ref, "audio_preview")
        still_waiting = await call(client, "talkvideo_resume_job", {"job_id": full_video["job_id"]})
        assert still_waiting["status"] == "awaiting_review"
        fixture_review(root, ref, "video_preview")
        full_audio = await call(client, "talkvideo_start_audio_job", {"ref": ref, "stage": "full"})
        assert (await wait_job(client, full_audio["job_id"]))["status"] == "succeeded"
        await call(client, "talkvideo_resume_job", {"job_id": full_video["job_id"]})
        assert (await wait_job(client, full_video["job_id"]))["status"] == "succeeded"
        inspected = await call(client, "talkvideo_inspect_output", {"ref": ref})
        assert {item["path"] for item in inspected["artifacts"]} >= {
            "preview.mp4",
            "result.mp4",
            "preview.wav",
            "narration.wav",
        }
        assert not inspected["lip_sync_assessed"]
