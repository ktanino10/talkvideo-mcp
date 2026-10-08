import hashlib
import json
import sys

import pytest
from jsonschema import Draft202012Validator
from mcp import Client, StdioServerParameters
from pydantic import ValidationError

from talkvideo_mcp.engine import Engine
from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.models import CueInput, ScriptInput


def test_file_preparation_is_lossless_bounded_and_read_only(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    text = "\ufeff価格は 3.14 円です。\r\n👨‍👩‍👧‍👦 e\u0301\t終わり。 \n"
    raw = text.encode("utf-8")
    (inputs / "script.txt").write_bytes(raw)
    engine = Engine(tmp_path / "output", input_root=inputs)
    prepared = engine.prepare_script(ScriptInput(script_file="script.txt"))
    assert prepared.display_text == text == prepared.spoken_text
    assert "".join(part.text for cue in prepared.cues for part in cue.chunks) == text
    assert prepared.source_file.sha256 == hashlib.sha256(raw).hexdigest()
    assert prepared.source_file.size_bytes == len(raw)
    assert engine.prepare_script(ScriptInput(script_file="script.txt")) == prepared
    assert not engine.store.root.exists()
    assert (inputs / "script.txt").read_bytes() == raw


def test_inline_serialization_and_mutual_exclusion_are_compatible():
    inline = ScriptInput(cues=[CueInput(display_text="unchanged")])
    assert set(inline.model_dump()) == {"cues", "normalization", "limits"}
    schema = ScriptInput.model_json_schema()
    validator = Draft202012Validator(schema)
    for request in ({}, {"cues": None}, {"cues": [{"display_text": "x"}], "script_file": "a.txt"}):
        with pytest.raises(ValidationError):
            ScriptInput.model_validate(request)
        assert list(validator.iter_errors(request))
    assert not list(validator.iter_errors({"script_file": "a.txt"}))
    assert not list(validator.iter_errors({"cues": [{"display_text": "x"}]}))


@pytest.mark.parametrize(
    "payload,code",
    [
        (b"PRIVATE\xffDATA", "invalid_utf8"),
        (b"x" * 80001, "file_too_large"),
        (b"x" * 20001, "script_too_large"),
        (b"", "empty_script_file"),
        (b"\xef\xbb\xbf", "empty_script_file"),
        (b"PRIVATE\x00DATA", "invalid_script_text"),
    ],
)
def test_bad_files_are_rejected_without_echo(tmp_path, payload, code):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "script.txt").write_bytes(payload)
    engine = Engine(tmp_path / "output", input_root=inputs)
    with pytest.raises(TalkVideoError, match=code) as error:
        engine.prepare_script(ScriptInput(script_file="script.txt"))
    assert "PRIVATE" not in error.value.problem.model_dump_json()
    assert not engine.store.root.exists()


@pytest.mark.parametrize(
    "relative", ["../outside.txt", "/outside.txt", "link.txt", "nested/link.txt"]
)
def test_input_roots_do_not_allow_traversal_or_symlinks(tmp_path, relative):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("not a designated input")
    (inputs / "link.txt").symlink_to(outside)
    (inputs / "nested").symlink_to(tmp_path)
    engine = Engine(tmp_path / "output", input_root=inputs)
    with pytest.raises(TalkVideoError, match="unsafe_path"):
        engine.prepare_script(ScriptInput(script_file=relative))
    assert outside.read_text() == "not a designated input"


def test_input_root_is_explicit_and_disjoint_from_private_state(tmp_path):
    with pytest.raises(TalkVideoError, match="input_root_required"):
        Engine(tmp_path / "output").prepare_script(ScriptInput(script_file="script.txt"))
    for inputs in [tmp_path, tmp_path / "output", tmp_path / "output/inputs"]:
        with pytest.raises(TalkVideoError, match="overlapping_roots"):
            Engine(tmp_path / "output", input_root=inputs)


async def test_native_stdio_file_surface_does_not_write(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    raw = "読込だけ。改行\r\nも保ちます。".encode()
    (inputs / "scenario.txt").write_bytes(raw)
    root = tmp_path / "output"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "talkvideo_mcp", "serve", "--root", str(root), "--input-root", str(inputs)],
    )
    async with Client(parameters) as client:
        caps = await client.call_tool("talkvideo_get_capabilities", {})
        assert caps.structured_content["data"]["script_file_input"]["available"]
        prepared = await client.call_tool(
            "talkvideo_prepare_script", {"script_file": "scenario.txt"}
        )
        assert not prepared.is_error
        data = prepared.structured_content["data"]
        assert data["spoken_text"].encode() == raw
        assert data["source_file"]["sha256"] == hashlib.sha256(raw).hexdigest()
        invalid = await client.call_tool(
            "talkvideo_prepare_script",
            {"cues": [{"display_text": "x"}], "script_file": "scenario.txt"},
        )
        assert invalid.is_error
        assert json.loads(invalid.content[0].text)["error"]["code"] == "invalid_input"
    assert not root.exists()
