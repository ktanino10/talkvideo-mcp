from evals.run_readonly import run_evaluations


async def test_ten_fixed_read_only_native_mcp_cases(tmp_path):
    report = await run_evaluations(tmp_path / "readonly-fixture")
    assert report["total"] == 10
    assert report["passed"] == 10
    assert report["fixture_unchanged"]
