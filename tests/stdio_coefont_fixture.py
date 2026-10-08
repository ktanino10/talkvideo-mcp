"""Native stdio fixture: both HTTP transports are in-memory, with runtime-only fake keys."""

import argparse
import asyncio
import json
import secrets
from pathlib import Path
from uuid import UUID

import httpx2
from pydantic import SecretStr

from talkvideo_mcp.audio import encode_wav
from talkvideo_mcp.cli import configure_logging, serve_engine
from talkvideo_mcp.coefont import CoefontProvider
from talkvideo_mcp.config import CoefontConfig, Credentials, OperatorAuthorization
from talkvideo_mcp.engine import Engine
from talkvideo_mcp.storage import LocalStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--voice", type=UUID, required=True)
    parser.add_argument("--pause-download", action="store_true")
    args = parser.parse_args()
    metrics = LocalStore(args.root)

    def record(method):
        counts = (
            json.loads(metrics.read_bytes("fixture-calls.json"))
            if metrics.exists("fixture-calls.json")
            else {}
        )
        counts[method] = counts.get(method, 0) + 1
        metrics.write_bytes("fixture-calls.json", json.dumps(counts).encode(), replace=True)

    async def api(request):
        record("POST")
        return httpx2.Response(302, headers={"Location": "https://files.example.test/audio"})

    async def download(request):
        record("GET")
        if args.pause_download:
            await asyncio.Event().wait()
        return httpx2.Response(200, content=encode_wav(bytes(800), rate=16000, channels=1, width=2))

    config = CoefontConfig(
        enabled=True,
        voice_id=args.voice,
        authorization=OperatorAuthorization(
            api_contract_reference="native-stdio-offline-fixture-only",
            voice_permission_reference="no-person-no-live-voice-fixture",
            paid_api_use_confirmed=True,
        ),
        trusted_download_hosts=("files.example.test",),
    )
    provider = CoefontProvider(
        config,
        Credentials(
            access_key=SecretStr(secrets.token_hex(16)),
            access_secret=SecretStr(secrets.token_hex(24)),
        ),
        api_transport=httpx2.MockTransport(api),
        download_transport=httpx2.MockTransport(download),
    )
    configure_logging()
    try:
        asyncio.run(serve_engine(Engine(args.root, official_provider=provider)))
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    main()
