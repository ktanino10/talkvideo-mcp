from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TypeVar

from mcp import Client, StdioServerParameters
from pydantic import BaseModel

from talkvideo_mcp.audio import DiagnosticTone, SafeToRetry
from talkvideo_mcp.engine import Engine
from talkvideo_mcp.models import (
    Capabilities,
    CueEdit,
    CueInput,
    Inspection,
    Job,
    PreparedScript,
    ReviseInput,
    Revision,
    SaveRevisionInput,
    ScriptInput,
)
from talkvideo_mcp.revisions import record_review, review_subject
from talkvideo_mcp.storage import LocalStore
from talkvideo_mcp.text import prepare_script

REPO = Path(__file__).resolve().parents[1]
T = TypeVar("T", bound=BaseModel)


class RetryFixture(DiagnosticTone):
    async def synthesize(self, text, settings):
        raise SafeToRetry()


async def finish(engine: Engine, job: Job) -> Job:
    async with asyncio.timeout(15):
        while True:
            result = engine.get_job(job.job_id)
            if result.status not in {"queued", "running"}:
                return result
            await asyncio.sleep(0.01)


async def complete_fixture_audio(engine: Engine, revision: Revision) -> None:
    for stage, review_stage in (("preview", "script"), ("full", "audio_preview")):
        record_review(
            engine.store,
            revision,
            review_stage,
            review_subject(engine.store, revision, review_stage),
        )
        job = engine.start_job(revision.ref, "audio", stage)
        completed = await finish(engine, job)
        if completed.status != "succeeded":
            raise AssertionError(f"Fixture setup failed: {completed.problem_code}")


async def seed_fixture(root: Path) -> tuple[Revision, Revision, Job]:
    if await asyncio.to_thread(root.exists):
        raise ValueError(
            "Use a new fixture directory; evaluation never overwrites existing output."
        )
    engine = Engine(root, enable_diagnostics=True)
    script = ScriptInput(
        cues=[
            CueInput(cue_id="intro", display_text="診断の始まり。", spoken_text="あ" * 10),
            CueInput(cue_id="detail", display_text="2025年度は12.5%。", spoken_text="か" * 20),
            CueInput(cue_id="outro", display_text="必ず減るわけではない。", spoken_text="な" * 10),
        ]
    )
    try:
        base = engine.save_revision(
            SaveRevisionInput(
                video_name="readonly-fixture",
                script=script,
                expected_plan_digest=prepare_script(script).plan_digest,
                backend="diagnostic",
            )
        )
        await complete_fixture_audio(engine, base)
        edited = engine.revise_cues(
            ReviseInput(
                ref=base.ref,
                expected_revision_digest=base.revision_digest,
                edits=[CueEdit(cue_id="detail", spoken_text="か" * 40)],
            )
        )
        await complete_fixture_audio(engine, edited)
        engine.store.write_bytes(
            "fixture-provenance.json",
            json.dumps(
                {
                    "fixture_only": True,
                    "reviews": "Programmatic fixture acknowledgments, NOT user approvals.",
                    "media": "Locally generated 440 Hz diagnostic tones, NOT speech.",
                    "rights": "No person, voice service, footage or model used.",
                    "purpose": "Read-only native MCP contract evaluation, not perceptual quality.",
                }
            ).encode(),
        )
    finally:
        await engine.close()
    failing = Engine(root, enable_diagnostics=True, provider=RetryFixture())
    try:
        failed_revision = failing.save_revision(
            SaveRevisionInput(
                video_name="retry-fixture",
                script=script,
                expected_plan_digest=prepare_script(script).plan_digest,
                backend="diagnostic",
            )
        )
        record_review(
            failing.store,
            failed_revision,
            "script",
            review_subject(failing.store, failed_revision, "script"),
        )
        failed = await finish(failing, failing.start_job(failed_revision.ref, "audio", "preview"))
        if failed.problem_code != "retry_exhausted":
            raise AssertionError("Retry fixture did not exhaust its bounded attempts.")
    finally:
        await failing.close()
    return base, edited, failed


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


class ReadOnlyClient:
    def __init__(self, client: Client, names: set[str]) -> None:
        self.client = client
        self.names = names
        self.calls: list[str] = []

    async def raw(self, name: str, arguments: dict[str, object]):
        if name not in self.names:
            raise AssertionError("Evaluation attempted a non-read-only tool.")
        self.calls.append(name)
        return await self.client.call_tool(name, arguments)

    async def model(self, name: str, arguments: dict[str, object], model: type[T]) -> T:
        response = await self.raw(name, arguments)
        if response.is_error or response.structured_content is None:
            raise AssertionError(f"Read-only tool failed: {name}")
        return model.model_validate(response.structured_content["data"])

    async def caps(self) -> Capabilities:
        return await self.model("talkvideo_get_capabilities", {}, Capabilities)

    async def prepare(self, text: str, **options: object) -> PreparedScript:
        return await self.model(
            "talkvideo_prepare_script",
            {"cues": [{"display_text": text}], **options},
            PreparedScript,
        )

    async def inspect(self, revision: Revision, offset: int = 0, limit: int = 20) -> Inspection:
        return await self.model(
            "talkvideo_inspect_output",
            {"ref": revision.ref.model_dump(), "offset": offset, "limit": limit},
            Inspection,
        )


async def answer(
    identifier: str, client: ReadOnlyClient, base: Revision, edited: Revision, failed: Job
) -> str:
    if identifier == "production-readiness":
        prepared = await client.prepare("これは準備のみ。")
        caps = await client.caps()
        return str(
            prepared.total_chunks > 0
            and caps.production_audio.available
            and caps.real_person_lip_sync.available
        )
    if identifier == "default-unicode":
        caps = await client.caps()
        if caps.normalization_default != "none" or not caps.script_file_input.available:
            raise AssertionError("Unexpected default normalization.")
        prepared = await client.model(
            "talkvideo_prepare_script", {"script_file": "unicode.txt"}, PreparedScript
        )
        if prepared.source_file is None:
            raise AssertionError("File preparation lost its source hash.")
        return "".join(chunk.text for cue in prepared.cues for chunk in cue.chunks)
    if identifier == "explicit-nfc":
        plain = await client.prepare("e\u0301e\u0301", normalization="none")
        nfc = await client.prepare("e\u0301e\u0301", normalization="NFC")
        if plain.display_text != "e\u0301e\u0301" or not nfc.normalization_changed:
            raise AssertionError("Unexpected normalization behavior.")
        return nfc.display_text
    if identifier == "decimal-boundary":
        await client.caps()
        prepared = await client.prepare("価格は 3.14 円です。続き", limits={"graphemes": 10})
        return str(prepared.cues[0].chunks[0].codepoints)
    if identifier == "oversized-grapheme":
        caps = await client.caps()
        if caps.provider_limits_verified:
            raise AssertionError("Provider authorization must not be inferred from host limits.")
        result = await client.raw(
            "talkvideo_prepare_script",
            {"cues": [{"display_text": "👨‍👩‍👧‍👦"}], "limits": {"utf8_bytes": 24}},
        )
        if not result.is_error or result.structured_content is None:
            raise AssertionError("Oversized grapheme was not rejected.")
        return str(result.structured_content["error"]["code"])
    if identifier == "generic-1000-limit":
        caps = await client.caps()
        if caps.provider_limits_verified:
            raise AssertionError("Unverified provider limits were advertised as verified.")
        prepared = await client.prepare("a" * 1001, limits={"graphemes": 1000, "codepoints": 1000})
        return str(prepared.total_chunks)
    if identifier == "targeted-revision":
        old = await client.model("talkvideo_get_revision", {"ref": base.ref.model_dump()}, Revision)
        new = await client.model(
            "talkvideo_get_revision", {"ref": edited.ref.model_dump()}, Revision
        )
        changed = [
            right.cue_id
            for left, right in zip(old.prepared.cues, new.prepared.cues, strict=True)
            if left.spoken_text != right.spoken_text
        ]
        return ",".join(changed)
    if identifier == "reused-chunks":
        revision = await client.model(
            "talkvideo_get_revision", {"ref": edited.ref.model_dump()}, Revision
        )
        count = 0
        offset = 0
        while True:
            page = await client.inspect(revision, offset, 2)
            count += sum(
                artifact.reused_from == base.ref and artifact.path.startswith("chunks/")
                for artifact in page.artifacts
            )
            if page.next_offset is None:
                return str(count)
            offset = page.next_offset
    if identifier == "retimed-offset":
        old = await client.inspect(base)
        new = await client.inspect(edited)
        before = next(item.start_frame for item in old.timings["full"] if item.cue_id == "outro")
        after = next(item.start_frame for item in new.timings["full"] if item.cue_id == "outro")
        return str(after - before)
    if identifier == "durable-retry-budget":
        caps = await client.caps()
        job = await client.model("talkvideo_get_job", {"job_id": failed.job_id}, Job)
        attempts = max(job.attempts.values())
        if (
            job.problem_code != "retry_exhausted"
            or attempts != caps.host_limits["attempts_per_chunk_across_resumes"]
        ):
            raise AssertionError("Retry budget evidence differs.")
        return str(attempts)
    raise AssertionError(f"Unknown evaluation case: {identifier}")


async def run_evaluations(fixture_root: Path) -> dict[str, object]:
    base, edited, failed = await seed_fixture(fixture_root)
    input_root = fixture_root.parent / (fixture_root.name + "-inputs")
    LocalStore(input_root).write_bytes("unicode.txt", "e\u0301☕".encode())
    before = (snapshot(fixture_root), snapshot(input_root))
    results = []
    cases = ET.parse(REPO / "evals/readonly.xml").getroot()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "talkvideo_mcp",
            "serve",
            "--root",
            str(fixture_root),
            "--input-root",
            str(input_root),
        ],
        cwd=str(REPO),
    )
    async with Client(parameters) as native:
        listed = await native.list_tools()
        read_only = {
            tool.name
            for tool in listed.tools
            if tool.annotations and tool.annotations.read_only_hint
        }
        client = ReadOnlyClient(native, read_only)
        for case in cases:
            identifier = case.attrib["id"]
            expected = case.findtext("answer")
            client.calls = []
            actual = await answer(identifier, client, base, edited, failed)
            if len(client.calls) < 2:
                raise AssertionError("Each read-only evaluation must exercise multiple tool calls.")
            results.append(
                {
                    "id": identifier,
                    "expected": expected,
                    "actual": actual,
                    "passed": actual == expected,
                    "tool_calls": list(client.calls),
                }
            )
    unchanged = before == (snapshot(fixture_root), snapshot(input_root))
    if not unchanged:
        raise AssertionError("Read-only evaluation changed fixture files.")
    return {
        "scope": "Native SDK stdio read-only golden evaluation; not perceptual media quality",
        "fixture_reviews": "Mocked setup only; not actual user approvals",
        "fixture_root": fixture_root.name,
        "base": base.ref.model_dump(),
        "edited": edited.ref.model_dump(),
        "failed_job_id": failed.job_id,
        "fixture_unchanged": unchanged,
        "passed": sum(bool(item["passed"]) for item in results),
        "total": len(results),
        "cases": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ten fixed read-only native MCP evaluations")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("The report already exists. Choose a new output file; nothing is overwritten.")
    fixture_root = args.output.parent / f"readonly-fixtures-{uuid.uuid4().hex[:12]}"
    report = asyncio.run(run_evaluations(fixture_root))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"{report['passed']}/{report['total']} read-only cases passed; fixture unchanged.")
    if report["passed"] != report["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
