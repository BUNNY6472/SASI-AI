from app.verification import artifact_hash, check_citations, verify_output, verify_python


def by_kind(checks, kind):
    return next(c for c in checks if c["kind"] == kind)


def test_valid_python_is_parsed_without_executing(tmp_path):
    marker = tmp_path / "must-not-exist"
    code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nprint('done')\n"
    checks = verify_python(code)
    assert by_kind(checks, "python_syntax")["status"] == "pass"
    assert by_kind(checks, "execution")["status"] == "unknown"
    assert not marker.exists()
    assert all(c["artifact_hash"] == artifact_hash(code) for c in checks)


def test_known_syntax_error_reports_location():
    checks = verify_python("def broken()\n    return 1\n")
    syntax = by_kind(checks, "python_syntax")
    assert syntax["status"] == "fail"
    assert "line 1" in syntax["summary"]


def test_hash_mismatch_detected_and_large_code_not_parsed():
    checks = verify_python("#" + "a" * 100_000, expected_hash="incorrect")
    assert by_kind(checks, "artifact_hash")["status"] == "fail"
    assert by_kind(checks, "python_syntax")["status"] == "unknown"


def test_citation_coverage_is_not_a_truth_score():
    evidence = {"items": [{"url": "https://example.org/report", "requested_url": "https://example.org/old", "source_kind": "web", "excerpt": "Source"}]}
    check = check_citations("Claim [source](https://example.org/report#section).", evidence)
    assert check["status"] == "pass" and "Claim support" in check["summary"]
    assert check_citations("[old](https://example.org/old)", evidence)["status"] == "pass"
    assert check_citations("[invented](https://example.org/invented)", evidence)["status"] == "fail"
    assert check_citations("No citations.", evidence)["status"] == "unknown"
    assert check_citations("[source](https://example.org/report)", {"items": []})["status"] == "fail"


def test_code_urls_are_not_treated_as_research_citations():
    assert check_citations('```python\nurl = "https://example.org/api"\n```')["status"] == "unknown"


def test_output_checks_primary_artifact_exactly():
    code = "import math\nprint(math.pi)\nprint('ok')\n"
    checks = verify_output(f"## solution.py\n```python\n{code}```")
    assert by_kind(checks, "python_syntax")["artifact_hash"] == artifact_hash(code)
    assert by_kind(checks, "execution")["status"] == "unknown"
