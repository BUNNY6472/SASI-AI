"""Evaluate a running SASI app only with explicit --execute authorization.

Default invocation validates local fixtures and previews the experiment. It does
not connect to SASI or providers. External oracles never execute generated code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

MODES = ("quick", "adaptive", "deep")
TERMINAL = {"completed", "failed", "cancelled", "budget_stopped", "interrupted"}
DEFAULT_CASES = Path(__file__).with_name("cases.json")


class EvaluationError(ValueError):
    pass


def _strict_json(value: str):
    def unique(pairs):
        result = {}
        for key, val in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = val
        return result
    def invalid(number):
        raise ValueError("Non-finite JSON number")
    return json.loads(value, object_pairs_hook=unique, parse_constant=invalid)


def load_cases(path: Path) -> list[dict]:
    if path.stat().st_size > 1_000_000:
        raise EvaluationError("Fixture file exceeds 1 MB")
    cases = _strict_json(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise EvaluationError("Fixtures must be a list of 1–100 cases")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or case["id"] in ids:
            raise EvaluationError("Each case needs a unique string id")
        ids.add(case["id"])
        if not isinstance(case.get("request"), str) or not 3 <= len(case["request"]) <= 20_000:
            raise EvaluationError(f"Invalid request in {case['id']}")
        if not isinstance(case.get("materials", ""), str) or len(case.get("materials", "")) > 60_000:
            raise EvaluationError(f"Invalid materials in {case['id']}")
        if case.get("kind") == "objective":
            oracle = case.get("oracle", {})
            if not isinstance(oracle, dict) or oracle.get("kind") not in {"exact_text", "json_equals"} or "expected" not in oracle:
                raise EvaluationError(f"Unsupported oracle in {case['id']}")
            if oracle["kind"] == "exact_text" and not isinstance(oracle["expected"], str):
                raise EvaluationError("exact_text requires a string expectation")
        elif case.get("kind") == "human_review":
            if not isinstance(case.get("rubric"), list) or not case["rubric"] or not all(isinstance(item, str) for item in case["rubric"]):
                raise EvaluationError(f"Research case {case['id']} needs a human rubric")
            if "oracle" in case:
                raise EvaluationError("Human-reviewed cases must not have an automatic oracle")
        else:
            raise EvaluationError(f"Unknown case kind in {case['id']}")
    return cases


def grade(case: dict, output: str) -> dict:
    """Strict accepted oracles only; no eval(), shell execution or model judging."""
    if case["kind"] == "human_review":
        return {"verdict": "unscored", "reason": "Requires blinded human review against the supplied rubric."}
    if len(output) > 1_000_000:
        return {"verdict": "fail", "reason": "Output exceeds the bounded 1 MB objective grader limit."}
    oracle = case["oracle"]
    if oracle["kind"] == "exact_text":
        passed = output.strip() == oracle["expected"]
        reason = "Exact text match after stripping surrounding whitespace."
    else:
        try:
            actual = _strict_json(output.strip())
            passed = json.dumps(actual, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
                oracle["expected"], sort_keys=True, separators=(",", ":"), allow_nan=False)
            reason = "Strict JSON value/type equality; object key order ignored."
        except (ValueError, TypeError, RecursionError):
            passed, reason = False, "Response is not exactly one valid JSON value (no Markdown or duplicate keys)."
    return {"verdict": "pass" if passed else "fail", "reason": reason}


def make_payload(case: dict, mode: str, minutes: float) -> dict:
    return {"request": case["request"], "materials": case.get("materials", ""),
            "profile": "research" if case["kind"] == "human_review" else "general",
            "mode": mode, "time_limit_minutes": minutes, "require_plan_approval": False}


def model_ids(run: dict) -> list[str]:
    found = set()
    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("model"), str) and value["model"]:
                found.add(value["model"])
            for item in value.values():
                if isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(run)
    return sorted(found)


def run_case(client: httpx.Client, case: dict, mode: str, minutes: float, *, poll_s: float = 2,
             extra_wait_s: float = 60) -> dict:
    start = time.monotonic()
    payload = make_payload(case, mode, minutes)
    # Never retry creation automatically: after a network failure it may already exist.
    try:
        created = client.post("/api/runs", json=payload)
        created.raise_for_status()
        run = created.json()
        run_id = run["id"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise EvaluationError("Run creation failed or its outcome is unknown. Inspect the app before retrying; no automatic retry was sent.") from exc
    problem, cancel_status = None, None
    try:
        while run.get("status") not in TERMINAL:
            if time.monotonic() - start > minutes * 60 + extra_wait_s:
                problem = "harness_timeout"
                break
            time.sleep(poll_s)
            response = client.get(f"/api/runs/{run_id}")
            response.raise_for_status()
            run = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        problem = type(exc).__name__
    finally:
        # Also runs on Ctrl-C: do not knowingly leave the harness's paid run running.
        if run.get("status") not in TERMINAL:
            try:
                cancel = client.post(f"/api/runs/{run_id}/cancel", timeout=10)
                cancel_status = "requested" if cancel.is_success else f"HTTP {cancel.status_code}"
            except httpx.HTTPError:
                cancel_status = "unknown; inspect the app"
    final = run.get("final") or {}
    output = final.get("output", "")
    outcome = grade(case, output) if run.get("status") == "completed" and not run.get("simulated") and not problem else {
        "verdict": "unscored", "reason": "Run was incomplete, simulated, failed or interrupted; do not count it as a successful answer."}
    return {"record_type": "result", "case_id": case["id"], "case_kind": case["kind"], "mode": mode,
            "run_id": run_id, "run_status": run.get("status"), "simulated": run.get("simulated", False),
            "request": payload, "output": output, "elapsed_wall_s": round(time.monotonic() - start, 3),
            "run_used_s": run.get("used_s"), "configured_time_limit_s": run.get("time_limit_s"),
            "usage": run.get("usage", {}), "model_ids": model_ids(run),
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(), "reported_artifact_hash": final.get("artifact_hash"),
            "checks": final.get("checks", []), "verification_status": final.get("verification_status"),
            "external_outcome": outcome, "human_rubric": case.get("rubric", []),
            "evidence": run.get("evidence", []), "error_type": problem, "cancellation": cancel_status}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    gate = parser.add_mutually_exclusive_group()
    gate.add_argument("--execute", action="store_true", help="Create real runs; may spend provider tokens and money")
    gate.add_argument("--dry-run", "--validate", action="store_true", help="Validate and preview locally (the default)")
    parser.add_argument("--max-cases", type=int, default=3)
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--minutes", type=float, default=5, help="Same maximum run duration for every mode")
    parser.add_argument("--poll-seconds", type=float, default=2)
    parser.add_argument("--output", type=Path, help="New JSONL result file (existing files are never overwritten)")
    args = parser.parse_args(argv)
    modes = args.modes.split(",")
    if not modes or any(mode not in MODES for mode in modes) or len(set(modes)) != len(modes):
        parser.error("--modes must be unique choices from quick,adaptive,deep")
    if not 1 <= args.max_cases <= 20 or not 5 <= args.minutes <= 60 or not 0.1 <= args.poll_seconds <= 30:
        parser.error("Use 1–20 cases, 5–60 minutes, and 0.1–30 seconds between polls")
    cases = load_cases(args.cases)[:args.max_cases]
    print(f"Validated {len(cases)} selected cases; {len(cases) * len(modes)} planned runs; common cap {args.minutes:g} minutes per run.")
    if not args.execute:
        for case in cases:
            print(f"  {case['id']}: {case['kind']} ({', '.join(modes)})")
        print("Dry run only. No API requests made. --execute is required to create potentially billable runs.")
        return 0
    parsed = urlsplit(args.base_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        parser.error("--base-url must be an origin without credentials, path, query or fragment")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        parser.error("Remote SASI endpoints require HTTPS; HTTP is allowed only on loopback")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = args.output or Path(__file__).with_name("results") / f"{stamp}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    totals = {mode: Counter() for mode in modes}
    with output.open("x", encoding="utf-8") as report, httpx.Client(base_url=args.base_url.rstrip("/"), timeout=30, follow_redirects=False) as client:
        code = os.environ.get("SASI_ACCESS_CODE", "")
        if code:
            login = client.post("/api/login", json={"code": code})
            if not login.is_success:
                raise EvaluationError(f"Login failed (HTTP {login.status_code}); credentials are not logged")
        limits = client.get("/api/limits")
        limits.raise_for_status()
        providers = client.get("/api/providers")
        providers.raise_for_status()
        metadata = {"record_type": "experiment", "started_at": stamp, "app_origin": args.base_url, "modes": modes,
                    "minutes_per_run": args.minutes, "case_ids": [c["id"] for c in cases],
                    "fixture_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(), "server_limits": limits.json(),
                    "providers": [{k: p.get(k) for k in ("provider", "model", "via", "ok", "simulated", "context")}
                                  for p in providers.json().get("providers", [])],
                    "note": "Small regression suite, not a frontier benchmark. Peer review scores are not used as outcome labels."}
        report.write(json.dumps(metadata) + "\n")
        report.flush()
        for index, case in enumerate(cases):
            # Rotate mode order deterministically to reduce a fixed first-mode advantage.
            order = modes[index % len(modes):] + modes[:index % len(modes)]
            for mode in order:
                row = run_case(client, case, mode, args.minutes, poll_s=args.poll_seconds)
                report.write(json.dumps(row, ensure_ascii=False) + "\n")
                report.flush()
                verdict = row["external_outcome"]["verdict"]
                totals[mode][verdict] += 1
                print(f"{case['id']} / {mode}: {row['run_status']}, external outcome {verdict}, {row['elapsed_wall_s']:.1f}s")
                if row["cancellation"] and row["cancellation"] != "requested":
                    raise EvaluationError("Cancellation could not be confirmed; stopping additional runs. Inspect the app.")
        report.write(json.dumps({"record_type": "summary", "counts_by_mode": totals}) + "\n")
    print(f"Results saved to {output.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvaluationError, httpx.HTTPError, OSError, ValueError) as exc:
        # HTTP response bodies and authentication payloads can contain sensitive data.
        print(f"Evaluation stopped ({type(exc).__name__}). Check local configuration and the app; no credentials are printed.")
        raise SystemExit(1)
