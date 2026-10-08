from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import unicodedata
from collections.abc import Mapping
from pathlib import Path

import anyio
from mcp.server.stdio import stdio_server

from talkvideo_mcp import __version__
from talkvideo_mcp.coefont import CoefontProvider
from talkvideo_mcp.config import Credentials, load_config
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


def configured_engine(
    root: Path,
    enable_diagnostics: bool,
    input_root: Path | None = None,
    config_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Engine:
    provider = None
    missing: tuple[str, ...] = ("explicit_operator_configuration",)
    if config_path is not None:
        config = load_config(config_path).coefont
        missing = tuple(config.activation_missing())
        if not missing:
            try:
                credentials = Credentials.from_environment(
                    os.environ if environment is None else environment
                )
            except TalkVideoError as exc:
                if exc.problem.code != "coefont_credentials_missing":
                    raise
                missing = ("credential_environment",)
            else:
                provider = CoefontProvider(config, credentials)
    return Engine(
        root,
        enable_diagnostics=enable_diagnostics,
        input_root=input_root,
        official_provider=provider,
        production_missing=missing,
    )


async def serve(
    root: Path,
    enable_diagnostics: bool,
    input_root: Path | None = None,
    config_path: Path | None = None,
) -> None:
    engine = configured_engine(root, enable_diagnostics, input_root, config_path)
    await serve_engine(engine)


async def serve_engine(engine: Engine) -> None:
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
        subparser.add_argument("--input-root", type=Path)
        subparser.add_argument(
            "--config", type=Path, help="Explicit private operator TOML; no credentials."
        )
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
            asyncio.run(serve(args.root, args.enable_diagnostics, args.input_root, args.config))
        elif args.command == "capabilities":
            engine = configured_engine(
                args.root, args.enable_diagnostics, args.input_root, args.config
            )
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
