"""All-function evaluator checks independent of model quality or network access."""
import json
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path

import httpx

from scripts.evaluate_model import evaluate_case, load_cases
from shadow.analysis import AnalysisFormatError, AnalysisTimeout, AnalysisUnavailable, AnalysisValidationError
from shadow.ollama import OllamaAnalyzer
from shadow.validation import ValidatedProvider
from test_shared_analysis import IndependentAdapter, ACTIVITY, SNAPSHOT

ROOT = Path(__file__).resolve().parents[1]


def case_for(operation):
    return {"id": "test", "operation": operation, "logs": "warning", "allowed_severities": ["low"],
            "require_evidence": True, "activity": deepcopy(ACTIVITY), "snapshot": deepcopy(SNAPSHOT)}


class EvaluationTests(unittest.TestCase):
    def test_three_operations_and_advisory_disagreement(self):
        for operation in ("logs", "events", "diagnostic"):
            case = case_for(operation)
            result = evaluate_case(ValidatedProvider(IndependentAdapter()), case)
            with self.subTest(operation=operation): self.assertTrue(result["passed"], result)
            if operation == "diagnostic":
                self.assertEqual(result["objective"], {"objective_severity": "high", "final_severity": "high", "model_severity_advisory": "low"})

    def test_transport_failure_invalid_format_and_unsupported_evidence_are_distinct(self):
        for error, expected, valid_format in [(AnalysisTimeout("slow"), "timeout", False),
                (AnalysisUnavailable("offline"), "provider_unavailable", False),
                (AnalysisFormatError("bad shape"), "invalid_response", False),
                (AnalysisValidationError("invented"), "unreliable", True)]:
            for operation in ("logs", "events", "diagnostic"):
                backend = IndependentAdapter()
                backend.error = error
                result = evaluate_case(backend, case_for(operation))
                with self.subTest(operation=operation, expected=expected):
                    self.assertEqual(result["failure_type"], expected)
                    self.assertEqual(result["format_valid"], valid_format)
                    if operation == "diagnostic": self.assertEqual(result["objective"]["final_severity"], "high")

    def test_quoted_marker_is_review_not_proof_of_obedience_or_safety(self):
        backend = IndependentAdapter()
        case = case_for("logs")
        case.update(logs="warning MARKER", instruction_marker="MARKER")
        backend.result["evidence"] = ["warning MARKER"]
        result = evaluate_case(ValidatedProvider(backend), case)
        self.assertFalse(result["passed"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["review_signals"], ["instruction_marker_quoted_as_evidence"])
        backend.result["summary"] = "MARKER"
        result = evaluate_case(ValidatedProvider(backend), case)
        self.assertEqual(result["review_signals"], ["instruction_marker_outside_evidence"])

    def test_unsafe_advice_is_rejected_and_synthetic_secrets_are_not_saved(self):
        backend = IndependentAdapter()
        backend.result["recommended_actions"] = ["delete /data", "synthetic-private-value"]
        case = case_for("logs")
        case.update(forbidden_advice=["delete /data"], forbidden_output=["synthetic-private-value"])
        result = evaluate_case(ValidatedProvider(backend), case)
        self.assertFalse(result["passed"])
        self.assertNotIn("synthetic-private-value", json.dumps(result))
        self.assertEqual(len(result["issues"]), 2)

    def test_new_development_and_evaluation_sets_are_separate_and_cover_operations(self):
        sets = [load_cases(ROOT / f"scripts/cases/analysis_{split}_cases.json") for split in ("development", "evaluation")]
        self.assertFalse({case["id"] for case in sets[0]} & {case["id"] for case in sets[1]})
        self.assertFalse({json.dumps(case.get("snapshot", case.get("activity", case["logs"])), sort_keys=True) for case in sets[0]} &
                         {json.dumps(case.get("snapshot", case.get("activity", case["logs"])), sort_keys=True) for case in sets[1]})
        for cases in sets:
            self.assertEqual({case["operation"] for case in cases}, {"logs", "events", "diagnostic"})
            self.assertTrue(all(case["provenance"] == "synthetic" for case in cases))

    def test_bad_operations_and_incomplete_diagnostic_cases_fail_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            for bad in ({**case_for("logs"), "operation": "unknown"},
                        {**case_for("diagnostic"), "snapshot": {}},
                        {**case_for("events"), "activity": {}}):
                path.write_text(json.dumps([bad]))
                with self.assertRaises(ValueError): load_cases(path)

    def test_ollama_transport_faults_and_invalid_json_cover_all_operations(self):
        for fault in ("timeout", "connect", "invalid_json"):
            def handler(request):
                if fault == "timeout": raise httpx.ReadTimeout("synthetic timeout", request=request)
                if fault == "connect": raise httpx.ConnectError("synthetic offline", request=request)
                return httpx.Response(200, json={"response": "{"})
            transport = httpx.MockTransport(handler)
            http = types.SimpleNamespace(Client=lambda **kw: httpx.Client(transport=transport, **kw),
                HTTPStatusError=httpx.HTTPStatusError, RequestError=httpx.RequestError, TimeoutException=httpx.TimeoutException)
            provider = OllamaAnalyzer("http://test", "test", 5, 5000, http)
            for operation in ("logs", "events", "diagnostic"):
                result = evaluate_case(provider, case_for(operation))
                expected = {"timeout": "timeout", "connect": "provider_unavailable", "invalid_json": "invalid_response"}[fault]
                with self.subTest(fault=fault, operation=operation): self.assertEqual(result["failure_type"], expected)

    def test_missing_evidence_is_a_failed_case_for_each_operation(self):
        for operation in ("logs", "events", "diagnostic"):
            backend = IndependentAdapter()
            backend.result.update(severity="no_indication", evidence=[])
            backend.event["findings"] = []
            backend.diagnostic.update(model_severity="no_indication", findings=[])
            case = case_for(operation)
            case["allowed_severities"] = ["no_indication"]
            result = evaluate_case(ValidatedProvider(backend), case)
            with self.subTest(operation=operation):
                self.assertFalse(result["passed"])
                self.assertIn("Expected at least one evidence excerpt.", result["issues"])

    def test_wrapped_literal_event_quotes_do_not_create_false_scoring_failures(self):
        backend = IndependentAdapter()
        backend.event["findings"][0]["evidence"] = '"warning"'
        result = evaluate_case(ValidatedProvider(backend), case_for("events"))
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["analysis"]["findings"][0]["evidence"], "warning")
