import json
from pathlib import Path

import yaml

from evals.export_contract import contract

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".github/skills/talkvideo-creator"


async def test_skill_fixture_schema_matches_registered_tools():
    assert json.loads((SKILL / "evals/fixtures/tools.json").read_text()) == await contract()


def test_project_discovery_and_safe_skill_metadata():
    skill = (SKILL / "SKILL.md").read_text()
    assert skill.startswith("---\nname: talkvideo-creator\ndescription:")
    frontmatter = yaml.safe_load(skill.split("---", 2)[1])
    assert set(frontmatter) == {"name", "description"}
    assert isinstance(frontmatter["description"], str)
    assert len(frontmatter["description"]) <= 1024
    assert len(skill.splitlines()) < 500
    assert "allowed-tools:" not in skill
    assert "zundamon-video" in skill
    assert "approved=true" in skill
    assert "ひろゆき風の解説動画" in skill
    assert "この文章をひろゆき風に読み上げ" in skill
    assert "個人的・非公開利用" in skill
    assert "https://coefont.cloud/maker/terms" in skill
    assert "https://coefont.cloud/selectPlan" in skill
    config = json.loads((REPO / ".github/mcp.json").read_text())
    assert set(config) == {"mcpServers"}
    server = config["mcpServers"]["talkvideo"]
    assert server["type"] == "stdio"
    assert "--enable-diagnostics" not in server["args"]
    assert not (REPO / ".vscode/mcp.json").exists()


def test_official_example_remains_disabled_without_identity_or_secrets():
    from talkvideo_mcp.config import load_config

    config = load_config(REPO / "examples/official-api.toml.example").coefont
    assert not config.enabled
    assert config.voice_id is None
    assert config.authorization is None
    assert config.trusted_download_hosts == ()


def test_support_is_owned_here_and_original_code_is_mit():
    for name in ["README.md", "SUPPORT.md", "CONTRIBUTING.md"]:
        text = (REPO / name).read_text()
        assert "https://github.com/ktanino10/talkvideo-mcp/issues" in text
    assert "Copyright (c) 2026 ktanino10" in (REPO / "LICENSE").read_text()


def test_issue_templates_are_parseable_without_upstream_assignment():
    for path in (REPO / ".github/ISSUE_TEMPLATE").glob("*.yml"):
        form = yaml.safe_load(path.read_text())
        assert isinstance(form, dict)
        assert not form.get("assignees")
        assert "contact_links" not in form
