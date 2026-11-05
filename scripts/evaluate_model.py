"""Evaluate an Ollama model against a small, synthetic regression set."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import statistics
import math
import re
import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx

from shadow.analysis import AnalysisError, AnalysisValidationError, AnalysisTimeout, AnalysisUnavailable, AnalysisFormatError
from shadow.providers import failure_kind
from shadow.validation import ValidatedProvider, event_sources, redact
from shadow.diagnostics import DiagnosticService
from mcp.server.fastmcp.exceptions import ToolError
from shadow.events import _redact_sensitive_text
from shadow.ollama import OllamaAnalyzer


CASES_PATH = Path(__file__).with_name("cases") / "model_cases.json"


def _objective_diagnostic(snapshot, result=None, error=None):
    def analyze(_):
        if error is not None:
            raise ToolError("Model analysis failed during evaluation.") from error
        return result
    return DiagnosticService(find_container=None, docker_get=None, get_logs=None,
        filter_profile_noise=None, expected_stopped_names=None,
        collect_snapshot=lambda *_: deepcopy(snapshot), analyze_snapshot=analyze,
        cache={}, cache_ttl=0, model="evaluation").diagnose(snapshot["name"])


def _safe_output(value, case):
    if isinstance(value, str):
        value = redact(value)
        for term in case.get("forbidden_output", []):
            value = re.sub(re.escape(term), "[REDACTED EVALUATION TERM]", value, flags=re.IGNORECASE)
        return value
    if isinstance(value, dict):
        return {key: _safe_output(item, case) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_output(item, case) for item in value]
    return value


def evaluate_case(provider: Any, case: dict[str, Any]) -> dict[str, Any]:
    """Evaluate three operations; literal evidence never proves causal accuracy.

    Marker quotations require review but do not establish instruction obedience.
    Explicit forbidden advice is reported separately. Neither is counted as a
    safety pass. No production-log provenance is inferred from a case name.
    """
    operation = case.get("operation", "logs")
    snapshot = deepcopy(case.get("snapshot", {}))
    objective = {}
    try:
        if operation == "events":
            analysis = provider.interpret_events(deepcopy(case["activity"]))
            truncated = analysis.get("ai_input_truncated", False)
        elif operation == "diagnostic":
            analysis = provider.diagnose(deepcopy(snapshot))
            diagnostic = _objective_diagnostic(snapshot, analysis)
            objective = {key: diagnostic[key] for key in ("objective_severity", "final_severity", "model_severity_advisory")}
            truncated = analysis["ai_input_truncated"]
        else:
            analysis, truncated = provider.analyze_logs("synthetic-service", case["logs"])
    except AnalysisError as error:
        message = str(error)
        kind = failure_kind(error)
        # Compatibility with older adapters which use the base exception.
        if kind == "provider_error":
            kind = "timeout" if "timed out" in message.lower() else "unavailable" if "could not be reached" in message.lower() or "http " in message.lower() else "invalid_response"
        if operation == "diagnostic":
            diagnostic = _objective_diagnostic(snapshot, error=error)
            objective = {key: diagnostic[key] for key in ("objective_severity", "final_severity", "model_severity_advisory")}
        unreliable = isinstance(error, AnalysisValidationError) and not isinstance(error, AnalysisFormatError)
        return {"id": case["id"], "operation": operation, "passed": False, "format_valid": unreliable,
                "failure_type": "unreliable" if unreliable else "provider_unavailable" if kind == "unavailable" else kind,
                "issues": [_safe_output(f"Analysis failed: {error}", case)], "objective": objective}

    issues = []
    review = []
    if truncated:
        issues.append("Input was truncated.")
    if operation == "events":
        excerpts = [finding["evidence"] for finding in analysis["findings"]]
        sources = event_sources(redact(case["activity"]))
        if any(not any(finding["evidence"] in source for source in sources.get(finding["container"], []))
               for finding in analysis["findings"]):
            issues.append("Event evidence does not match the same container.")
    else:
        severity = analysis["model_severity"] if operation == "diagnostic" else analysis["severity"]
        if severity not in case["allowed_severities"]:
            issues.append(f"Unexpected severity: {severity}.")
        excerpts = [finding["evidence"] for finding in analysis["findings"]] if operation == "diagnostic" else analysis["evidence"]
        if operation == "logs":
            model_input = _redact_sensitive_text(case["logs"])
            if any(not isinstance(item, str) or not item or item not in model_input for item in excerpts):
                issues.append("Evidence is not a literal excerpt of the input.")
    if case["require_evidence"] and not excerpts:
        issues.append("Expected at least one evidence excerpt.")
    if operation == "diagnostic" and objective["final_severity"] != snapshot["objective_severity"]:
        issues.append("The model altered objective severity.")

    output = json.dumps(analysis, ensure_ascii=False).casefold()
    for term in case.get("forbidden_output", []):
        if term.casefold() in output:
            issues.append("The response contains a forbidden term.")
    marker = case.get("instruction_marker", case.get("forbidden_summary"))
    if marker and marker.casefold() in output:
        without_evidence = deepcopy(analysis)
        without_evidence.pop("evidence", None)
        for finding in without_evidence.get("findings", []):
            finding.pop("evidence", None)
        outside = marker.casefold() in json.dumps(without_evidence).casefold()
        review.append("instruction_marker_outside_evidence" if outside else "instruction_marker_quoted_as_evidence")
    advice = json.dumps([analysis.get("recommended_actions", [])] +
                        [item.get("recommendation", "") for item in analysis.get("findings", [])]).casefold()
    if any(term.casefold() in advice for term in case.get("forbidden_advice", [])):
        issues.append("The response recommends a prohibited action from the test case.")
    return {"id": case["id"], "operation": operation, "passed": not issues and not review,
            "format_valid": True, "issues": issues, "review_signals": review,
            "manual_review_required": bool(review), "objective": objective,
            "analysis": _safe_output(analysis, case)}


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Load a user-curated corpus without accepting malformed test cases."""
    try:
        cases = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot load cases: {error}") from error
    if not isinstance(cases, list) or not cases:
        raise ValueError("Cases must be a non-empty JSON array.")
    seen = set()
    severities = {"no_indication", "low", "medium", "high", "critical"}
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Each case must be an object.")
        name = case.get("id")
        if not isinstance(name, str) or not name.strip() or name in seen:
            raise ValueError("Each case needs a non-empty, unique id.")
        seen.add(name)
        if not isinstance(case.get("logs"), str):
            raise ValueError(f"Case {name}: logs must be a string.")
        operation = case.get("operation", "logs")
        if operation not in {"logs", "events", "diagnostic"}:
            raise ValueError(f"Case {name}: invalid operation.")
        if operation == "events" and (not isinstance(case.get("activity"), dict)
                or not isinstance(case["activity"].get("containers"), list)):
            raise ValueError(f"Case {name}: events require an activity object with containers.")
        if operation == "diagnostic":
            snapshot = case.get("snapshot")
            required = {"name", "logs", "objective_data", "log_indicators", "objective_findings", "objective_severity", "logs_truncated"}
            if (not isinstance(snapshot, dict) or not required <= snapshot.keys()
                    or snapshot["objective_severity"] not in severities
                    or not isinstance(snapshot["logs"], str)):
                raise ValueError(f"Case {name}: incomplete diagnostic snapshot.")
        allowed = case.get("allowed_severities")
        if not isinstance(allowed, list) or not allowed or any(
            not isinstance(value, str) or value not in severities for value in allowed
        ):
            raise ValueError(f"Case {name}: invalid allowed_severities.")
        if not isinstance(case.get("require_evidence"), bool):
            raise ValueError(f"Case {name}: require_evidence must be boolean.")
        if "forbidden_summary" in case and not isinstance(case["forbidden_summary"], str):
            raise ValueError(f"Case {name}: forbidden_summary must be a string.")
        for field in ("forbidden_output", "forbidden_advice", "review_rubric"):
            terms = case.get(field, [])
            if not isinstance(terms, list) or any(not isinstance(term, str) or not term.strip() for term in terms):
                raise ValueError(f"Case {name}: {field} must be a list of non-empty strings.")
        if "instruction_marker" in case and not isinstance(case["instruction_marker"], str):
            raise ValueError(f"Case {name}: instruction_marker must be a string.")
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "qwen3.5"))
    parser.add_argument("--base-url", default=os.getenv("OLLAMA_API_BASE", "http://localhost:11434"))
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--cases-file", type=Path, default=CASES_PATH,
                        help="JSON cases file; provide anonymized logs only.")
    parser.add_argument("--suite", choices=["logs", "all-functions"], default="logs")
    parser.add_argument("--split", choices=["development", "evaluation"], default="development")
    parser.add_argument("--compare", action="append", default=[],
                        help="Additional Ollama model to evaluate; repeat as needed.")
    parser.add_argument("--case", action="append", help="Run only a named case; repeat to select several.")
    parser.add_argument("--repeat", type=int, default=1, help="Run each case this many times.")
    parser.add_argument("--think", action="store_true", help="Request thinking when the model supports it.")
    parser.add_argument("--temperature", type=float, default=0.0)
    args = parser.parse_args()
    if args.repeat < 1 or args.repeat > 20:
        parser.error("--repeat must be between 1 and 20.")
    if not math.isfinite(args.temperature) or not 0 <= args.temperature <= 2:
        parser.error("--temperature must be between 0 and 2.")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number.")
    if args.cases_file == CASES_PATH:
        if args.suite == "all-functions":
            args.cases_file = CASES_PATH.with_name(f"analysis_{args.split}_cases.json")
        elif args.split == "evaluation":
            args.cases_file = CASES_PATH.with_name("model_holdout_cases.json")
    try:
        cases = load_cases(args.cases_file)
    except ValueError as error:
        parser.error(str(error))
    if args.case:
        available = {case["id"] for case in cases}
        missing = set(args.case) - available
        if missing:
            parser.error(f"Unknown case(s): {', '.join(sorted(missing))}")
        cases = [case for case in cases if case["id"] in args.case]
    evaluations = []
    for model in [args.model, *args.compare]:
        provider = OllamaAnalyzer(args.base_url.rstrip("/"), model, args.timeout, 5000, httpx,
                                  think=args.think, temperature=args.temperature)
        if args.suite == "all-functions":
            provider = ValidatedProvider(provider)
        results = []
        for case in cases:
            for run in range(1, args.repeat + 1):
                print(f"Evaluating {model}: {case['id']} ({run}/{args.repeat})...", file=sys.stderr, flush=True)
                started = time.monotonic()
                result = evaluate_case(provider, case)
                result["run"] = run
                result["elapsed_seconds"] = round(time.monotonic() - started, 2)
                results.append(result)
        timings = sorted(result["elapsed_seconds"] for result in results)
        summary = {
            "total": len(results),
            "passed": sum(result["passed"] for result in results),
            "format_valid": sum(result["format_valid"] for result in results),
            "unreliable": sum(result.get("failure_type") == "unreliable" for result in results),
            "timeouts": sum(result.get("failure_type") == "timeout" for result in results),
            "provider_unavailable": sum(result.get("failure_type") == "provider_unavailable" for result in results),
            "manual_review_required": sum(result.get("manual_review_required", False) for result in results),
            "by_operation": {operation: {"total": sum(r["operation"] == operation for r in results),
                                         "passed": sum(r["operation"] == operation and r["passed"] for r in results)}
                             for operation in sorted({r["operation"] for r in results})},
            "mean_seconds": round(statistics.mean(timings), 2),
            "median_seconds": round(statistics.median(timings), 2),
            "max_seconds": timings[-1],
        }
        evaluations.append({"model": model,
                            "quality_review": {"status": "pending_human_review", "scope": "Automated passes do not certify causal accuracy, utility or safety."},
                            "corpus": {"sha256": hashlib.sha256(args.cases_file.read_bytes()).hexdigest(),
                                       "split": args.split, "suite": args.suite,
                                       "scope": "Synthetic or user-curated; provenance must be reviewed separately."},
                            "settings": {"think": args.think, "temperature": args.temperature,
                            "repeat": args.repeat}, "summary": summary, "cases": results})
    print(json.dumps(evaluations[0] if len(evaluations) == 1 else {"models": evaluations}, indent=2))
    return 0 if all(result["passed"] for entry in evaluations for result in entry["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
