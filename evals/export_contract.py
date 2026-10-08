from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from mcp import Client

from talkvideo_mcp.engine import Engine
from talkvideo_mcp.server import build_server


async def contract() -> list[dict[str, object]]:
    engine = Engine(Path("output"))
    try:
        async with Client(build_server(engine)) as client:
            result = await client.list_tools()
            return [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in result.tools
            ]
    finally:
        await engine.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Export registered schemas for mocked skill evals")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(asyncio.run(contract()), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
