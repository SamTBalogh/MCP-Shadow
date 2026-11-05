"""Human judgments are artifact-bound, blinded, complete and never fabricated."""
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.evaluate_model import load_cases
from scripts.review_evaluation import prepare, summarize, RATINGS
from test_shared_analysis import DIAGNOSTIC_RESULT, SNAPSHOT

ROOT = Path(__file__).resolve().parents[1]
GOOD = {"factual_accuracy": "supported", "causal_calibration": "appropriate",
        "recommendation_utility": "useful", "recommendation_safety": "safe", "omissions": "none_material"}


class HumanReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cases = Path(self.directory.name) / "cases.json"
        self.results = Path(self.directory.name) / "results.json"
        case = {"id": "diagnostic-test", "operation": "diagnostic", "logs": "warning",
                "snapshot": SNAPSHOT, "allowed_severities": ["low"], "require_evidence": True}
        self.cases.write_text(json.dumps([case]))
        result = {"models": [{"model": model, "corpus": {"sha256": hashlib.sha256(self.cases.read_bytes()).hexdigest()},
            "cases": [{"id": case["id"], "run": 1, "passed": True, "elapsed_seconds": 123,
                       "analysis": {**deepcopy(DIAGNOSTIC_RESULT), "model": model}}]} for model in ("model-A", "model-B")]}
        self.results.write_text(json.dumps(result))

    def completed(self):
        packet = prepare(self.results, self.cases)
        packet["reviewer"] = "Independent reviewer"
        for sample in packet["samples"]: sample["ratings"] = dict(GOOD)
        return packet

    def test_packet_hides_models_scores_and_timings_but_keeps_inputs_and_advice(self):
        packet = prepare(self.results, self.cases)
        encoded = json.dumps(packet)
        for hidden in ("model-A", "model-B", "elapsed_seconds", '"passed"'):
            self.assertNotIn(hidden, encoded)
        self.assertEqual(len({sample["sample_id"] for sample in packet["samples"]}), 2)
        self.assertTrue(all(sample["response"]["findings"] for sample in packet["samples"]))
        self.assertTrue(all(all(value is None for value in sample["ratings"].values()) for sample in packet["samples"]))
        with self.assertRaises(ValueError): summarize(packet, self.results, self.cases)

    def test_unsafe_unfounded_and_uncertain_advice_cannot_receive_a_quality_pass(self):
        for field, rating, verdict in (("recommendation_safety", "unsafe", "fail"),
                                      ("causal_calibration", "overstated", "fail"),
                                      ("factual_accuracy", "uncertain", "uncertain"),
                                      ("omissions", "material", "fail")):
            packet = self.completed()
            for sample in packet["samples"]: sample["ratings"][field] = rating
            with self.subTest(field=field):
                self.assertEqual(summarize(packet, self.results, self.cases)["human_review"][verdict], 2)

    def test_tampered_input_response_duplicate_or_changed_artifact_is_rejected(self):
        packet = self.completed()
        for mutation in ("response", "input", "duplicate", "hash"):
            changed = deepcopy(packet)
            if mutation == "response": changed["samples"][0]["response"]["summary"] = "Edited response"
            elif mutation == "input": changed["samples"][0]["input"]["logs"] = "Changed evidence"
            elif mutation == "duplicate": changed["samples"][1] = deepcopy(changed["samples"][0])
            else: changed["evaluation_sha256"] = "wrong"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): summarize(changed, self.results, self.cases)

    def test_every_dimension_is_required_and_missing_responses_remain_unreviewable(self):
        packet = self.completed()
        packet["samples"][0]["ratings"]["causal_calibration"] = None
        with self.assertRaises(ValueError): summarize(packet, self.results, self.cases)
        raw = json.loads(self.results.read_text())
        raw["models"][0]["cases"][0].pop("analysis")
        self.results.write_text(json.dumps(raw))
        packet = self.completed()
        for sample in packet["samples"]:
            if not sample["reviewable"]: sample["ratings"] = {key: None for key in RATINGS}
        counts = summarize(packet, self.results, self.cases)["human_review"]
        self.assertEqual(counts, {"pass": 1, "fail": 0, "uncertain": 0, "not_reviewable": 1})

    def test_new_reserved_corpus_is_frozen_disjoint_and_unexecuted(self):
        reserved = ROOT / "scripts/cases/quality_evaluation_cases.json"
        manifest = json.loads((ROOT / "scripts/cases/quality_evaluation_manifest.json").read_text())
        self.assertEqual(manifest["corpus_sha256"], hashlib.sha256(reserved.read_bytes()).hexdigest())
        self.assertEqual(manifest["status"], "reserved_unexecuted")
        development = load_cases(ROOT / "scripts/cases/quality_development_cases.json")
        evaluation = load_cases(reserved)
        self.assertFalse({case["logs"] for case in development} & {case["logs"] for case in evaluation})
        self.assertEqual({case["operation"] for case in evaluation}, {"logs", "events", "diagnostic"})
        self.assertTrue(all(case["review_rubric"] for case in development + evaluation))

    def test_secret_masking_is_case_insensitive_and_notes_are_not_printed_in_summary(self):
        from scripts.evaluate_model import _safe_output
        case = {"forbidden_output": ["synthetic-private-value"]}
        self.assertEqual(_safe_output("SYNTHETIC-PRIVATE-VALUE", case), "[REDACTED EVALUATION TERM]")
        packet = self.completed()
        packet["samples"][0]["notes"] = "Private reviewer note"
        self.assertNotIn("Private reviewer note", json.dumps(summarize(packet, self.results, self.cases)))
