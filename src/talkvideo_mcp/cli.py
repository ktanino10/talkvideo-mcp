from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import unicodedata
from pathlib import Path

import anyio
from mcp.server.stdio import stdio_server

from talkvideo_mcp import __version__
from talkvideo_mcp.engine import Engine
from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.models import ReviewStage, RevisionRef
from talkvideo_mcp.revisions import load_revision, record_review, review_subject
from talkvideo_mcp.server import build_server
from talkvideo_mcp.storage import LocalStore


class SafeLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = (
            record.getMessage()
            if record.name.startswith("talkvideo_mcp")
            else "sdk_event details_suppressed"
        )
        return f"{record.levelname} {record.name}: {message}"


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SafeLogFormatter())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)


async def serve(root: Path, enable_diagnostics: bool) -> None:
    engine = Engine(root, enable_diagnostics=enable_diagnostics)
    server = build_server(engine)
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    if current is None:
        raise RuntimeError("Missing server task.")
    loop.add_signal_handler(signal.SIGTERM, current.cancel)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        with anyio.CancelScope(shield=True):
            await engine.close()
        loop.remove_signal_handler(signal.SIGTERM)


def visible_text(text: str) -> str:
    return "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char) == "Cf" and char != "\u200d" else char
        for char in text
    )


def review(root: Path, ref: RevisionRef, stage: ReviewStage) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise TalkVideoError(
            "interactive_review_required",
            "Reviews must be acknowledged by the user in an interactive local terminal.",
            "Run this yourself after inspecting the script/preview. There is no --yes flag.",
            needs_user_action=True,
        )
    store = LocalStore(root)
    revision = load_revision(store, ref)
    subject = review_subject(store, revision, stage)
    print(f"Revision: {ref.video_name}/{ref.revision_id}")
    print(f"Stage: {stage}; subject SHA-256: {subject}")
    print("This records a local review, NOT voice/media/model rights.")
    if revision.diagnostic_only:
        print("DIAGNOSTIC ONLY: tones and test patterns, not speech or lip-sync.")
    if stage == "script":
        for cue in revision.prepared.cues:
            print(f"\n[{cue.cue_id}] Display:\n{visible_text(cue.display_text)}")
            print(f"Spoken:\n{visible_text(cue.spoken_text)}")
    else:
        filename = "preview.wav" if stage == "audio_preview" else "preview.mp4"
        path = root / ref.video_name / ref.revision_id / filename
        print("Open and inspect this local preview yourself before proceeding:")
        print(json.dumps(str(path), ensure_ascii=False))
    expected = f"REVIEW {subject[:12]}"
    answer = input(f"\nTo acknowledge this exact content, type {expected}: ")
    if answer != expected:
        raise TalkVideoError(
            "review_not_recorded",
            "No matching review acknowledgment was provided.",
            "No approval was saved. Re-run only after inspecting the exact content.",
            needs_user_action=True,
        )
    receipt = record_review(store, revision, stage, subject)
    print(receipt.model_dump_json())


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Independent local TalkVideo MCP workflow")
    result.add_argument("--version", action="version", version=__version__)
    subcommands = result.add_subparsers(dest="command", required=True)
    for command in ("serve", "capabilities"):
        subparser = subcommands.add_parser(command)
        subparser.add_argument("--root", type=Path, default=Path("output"))
        subparser.add_argument("--enable-diagnostics", action="store_true")
    review_parser = subcommands.add_parser("review")
    review_parser.add_argument("video_name")
    review_parser.add_argument("revision_id")
    review_parser.add_argument(
        "--stage", choices=["script", "audio_preview", "video_preview"], required=True
    )
    review_parser.add_argument("--root", type=Path, default=Path("output"))
    return result


def main() -> None:
    configure_logging()
    args = parser().parse_args()
    try:
        if args.command == "serve":
            asyncio.run(serve(args.root, args.enable_diagnostics))
        elif args.command == "capabilities":
            engine = Engine(args.root, enable_diagnostics=args.enable_diagnostics)
            print(engine.capabilities().model_dump_json(indent=2))
        else:
            ref = RevisionRef(video_name=args.video_name, revision_id=args.revision_id)
            review(args.root, ref, args.stage)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except TalkVideoError as exc:
        print(exc.problem.model_dump_json(), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        logging.getLogger(__name__).error("cli_failed type=%s", type(exc).__name__)
        raise SystemExit(2) from None
