"""Provider-independent structural and evidence checks, not a causal verifier."""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy

from shadow.analysis import AnalysisValidationError, AnalysisFormatError
from shadow.events import _redact_sensitive_text
from shadow.evidence import attach_references

SEVERITIES = {"no_indication", "low", "medium", "high", "critical"}


def _require(condition, message):
    if not condition:
        raise AnalysisValidationError(message)


def _format(condition, message):
    if not condition:
        raise AnalysisFormatError(message)


def redact(value):
    """Redact strings recursively before handing data to an adapter."""
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return deepcopy(value)


def _validate_summary_consistency(severity, summary):
    """Reject a few obvious contradictions; this heuristic cannot infer health."""
    if severity != "no_indication":
        return
    text = summary.lower()
    if re.search(r"\b(no|without|not|never)\b.{0,35}\b(error|failure|failed|crash)\b", text):
        return
    if re.search(r"\b(?:has|encountered|experienced) an? (?:error|failure)\b|"
                 r"\b(?:connection|request|authentication|process|service) "
                 r"(?:has )?(?:failed|crashed|was refused|timed out)\b|"
                 r"\b(?:an error|a failure) (?:occurred|was detected)\b", text):
        raise AnalysisValidationError("The summary describes a failure but severity says no indication.")


def _validate_interpretation(payload, severity_key=None):
    required = {"summary", "findings", "limitations", "confidence"}
    if severity_key:
        required.add(severity_key)
    _format(isinstance(payload, dict) and required <= payload.keys(), "The generated analysis is incomplete.")
    _format(isinstance(payload["summary"], str), "The model returned an invalid summary.")
    _require(bool(payload["summary"].strip()), "The model returned an empty summary.")
    _format(isinstance(payload["findings"], list), "The model returned invalid findings.")
    _strings(payload["limitations"], "limitations")
    _confidence(payload["confidence"])
    if severity_key:
        _format(isinstance(payload[severity_key], str) and payload[severity_key] in SEVERITIES,
                 "The model returned an invalid severity.")
        _validate_summary_consistency(payload[severity_key], payload["summary"])


def _strings(value, field):
    _format(isinstance(value, list) and all(isinstance(item, str) for item in value),
             f"The model returned an invalid field: {field}.")


def _confidence(value):
    _format(not isinstance(value, bool) and isinstance(value, (int, float))
             and math.isfinite(value) and 0 <= value <= 1,
             "The model returned an invalid confidence value.")


def _literal_evidence(items, source):
    result = []
    for excerpt in items:
        if excerpt not in source and len(excerpt) >= 2 and excerpt[0] == excerpt[-1] and excerpt[0] in {'"', "'"}:
            excerpt = excerpt[1:-1]
        _require(bool(excerpt.strip()) and excerpt in source,
                 "The model returned evidence that is not a literal excerpt of the supplied logs.")
        result.append(excerpt)
    return result


def validate_logs(payload, logs):
    required = {"severity", "summary", "evidence", "probable_causes", "recommended_actions", "confidence"}
    _format(isinstance(payload, dict) and required <= payload.keys(), "The generated analysis is incomplete.")
    result = deepcopy(payload)
    _validate_interpretation({**result, "findings": [], "limitations": []}, "severity")
    for field in ("evidence", "probable_causes", "recommended_actions"):
        _strings(result[field], field)
    result["evidence"] = _literal_evidence(result["evidence"], logs)
    _require(result["severity"] == "no_indication" or bool(result["evidence"]),
             "The model assigned severity without verifiable evidence.")
    return result


def event_sources(activity):
    sources = {}
    for container in activity.get("containers", []):
        for event in container.get("relevant_event_details", []):
            if event.get("relevance", {}).get("relevant"):
                sources.setdefault(container["name"].strip().lstrip("/"), []).extend(
                    str(event.get(key, "")) for key in ("message", "content"))
    return sources


def validate_events(payload, sources):
    _validate_interpretation(payload)
    result = deepcopy(payload)
    for finding in result["findings"]:
        _finding(finding, {"title", "container", "evidence", "interpretation", "recommendation"})
        name = finding["container"].strip().lstrip("/")
        candidates = sources.get(name, [])
        normalized = _matched_excerpt(finding["evidence"], candidates)
        _require(normalized is not None,
                 "The event finding has an unknown container or unverifiable evidence.")
        finding["evidence"] = normalized
    return result


def _matched_excerpt(excerpt, sources):
    for source in sources:
        try:
            return _literal_evidence([excerpt], source)[0]
        except AnalysisValidationError:
            continue
    return None


def _matches(excerpt, text):
    try:
        return bool(_literal_evidence([excerpt], text))
    except AnalysisValidationError:
        return False


def _finding(finding, fields):
    _format(isinstance(finding, dict) and fields <= finding.keys()
             and all(isinstance(value, str) for value in finding.values())
             and all(finding[key].strip() for key in fields),
             "The model returned an incomplete or invalid finding.")


def diagnostic_sources(snapshot):
    facts = snapshot["objective_data"]
    metric_fields = {"cpu_percent", "memory", "network", "processes", "metrics_available", "metrics_error", "metric_status", "metrics_observed_at", "metrics_sampled_at"}
    state = {key: value for key, value in facts.items() if key not in metric_fields}
    metrics = {key: value for key, value in facts.items() if key in metric_fields}
    return {"state": json.dumps(state, ensure_ascii=False, indent=2),
            "metrics": json.dumps(metrics, ensure_ascii=False, indent=2), "logs": snapshot["logs"]}


def validate_diagnostic(payload, snapshot):
    _validate_interpretation(payload, "model_severity")
    result = deepcopy(payload)
    _require(result["model_severity"] == "no_indication" or bool(result["findings"]),
             "The model assigned severity without verifiable evidence.")
    sources = diagnostic_sources(snapshot)
    for finding in result["findings"]:
        # Evidence presence is checked first to preserve useful failure messages.
        _require(isinstance(finding, dict) and isinstance(finding.get("evidence"), str)
                 and any(_matches(finding["evidence"], text) for text in sources.values()),
                 "The model assigned severity without verifiable evidence.")
        _finding(finding, {"title", "source", "evidence", "probable_cause", "recommendation"})
        _require(finding["source"] in sources and _matches(finding["evidence"], sources[finding["source"]]),
                 "The diagnostic evidence does not match its declared source.")
        finding["evidence"] = _literal_evidence([finding["evidence"]], sources[finding["source"]])[0]
    return result


class ValidatedProvider:
    """Enforce the common contract even for an independently implemented adapter.

    Adapters must also validate against their actual selected/truncated context.
    Copies prevent an adapter from mutating the application's objective facts.
    """

    def __init__(self, provider):
        self.provider = provider

    def analyze_logs(self, container_name, logs):
        logs = redact(logs)
        container_name = redact(container_name)
        response = self.provider.analyze_logs(container_name, logs)
        _format(isinstance(response, tuple) and len(response) == 2 and isinstance(response[1], bool),
                 "The provider returned an invalid log result envelope.")
        result = validate_logs(response[0], logs)
        return attach_references(result, "logs", logs, container_name), response[1]

    def interpret_events(self, log_activity):
        activity = redact(log_activity)
        result = self.provider.interpret_events(deepcopy(activity))
        result = validate_events(result, event_sources(activity))
        _format(isinstance(result.get("executed"), bool) and isinstance(result.get("model"), str)
                 and isinstance(result.get("reason"), str), "The provider returned an invalid event envelope.")
        _require(result["executed"] or (not result["findings"] and result["reason"] == "no_relevant_events"
                 and not event_sources(activity)), "The provider skipped available events.")
        return attach_references(result, "events", activity)

    def diagnose(self, snapshot):
        clean = redact(snapshot)
        result = validate_diagnostic(self.provider.diagnose(deepcopy(clean)), clean)
        _format(isinstance(result.get("model"), str) and isinstance(result.get("ai_input_truncated"), bool),
                 "The provider returned an invalid diagnostic envelope.")
        return attach_references(result, "diagnostic", clean)
