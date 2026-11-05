"""References come from exact inputs, preserving ambiguity and occurrences."""
from copy import deepcopy
import hashlib
import unittest

from shadow.validation import ValidatedProvider
from test_shared_analysis import IndependentAdapter, SNAPSHOT, ACTIVITY


class EvidenceReferenceTests(unittest.TestCase):
    def test_log_occurrences_and_unicode_character_offsets(self):
        provider = IndependentAdapter()
        logs = "café warning\nwarning\n"
        result, _ = ValidatedProvider(provider).analyze_logs("web", logs)
        refs = result["evidence_references"][0]
        self.assertEqual(refs["match_status"], "ambiguous")
        self.assertEqual(len(refs["matches"]), 2)
        self.assertNotEqual(refs["matches"][0]["observation_id"], refs["matches"][1]["observation_id"])
        for ref in refs["matches"]:
            self.assertEqual(logs[ref["start_character"]:ref["end_character"]], "warning")
            self.assertEqual(ref["source_sha256"], hashlib.sha256(logs.encode()).hexdigest())

    def test_forged_references_are_overwritten_and_no_causal_guarantee_is_given(self):
        provider = IndependentAdapter()
        provider.result["evidence_references"] = ["model-invented-id"]
        provider.result["grounding"] = {"causal_accuracy_verified": True}
        result, _ = ValidatedProvider(provider).analyze_logs("web", "warning")
        self.assertEqual(result["evidence_references"][0]["match_status"], "unique")
        self.assertFalse(result["grounding"]["causal_accuracy_verified"])
        self.assertFalse(result["grounding"]["recommendation_safety_verified"])

    def test_event_references_name_one_real_occurrence_not_two_identical_fields(self):
        result = ValidatedProvider(IndependentAdapter()).interpret_events(ACTIVITY)
        ref = result["evidence_references"][0]
        self.assertEqual(ref["match_status"], "unique")
        self.assertEqual(ref["matches"][0]["input_pointer"], "/containers/0/relevant_event_details/0/content")
        repeated = deepcopy(ACTIVITY)
        repeated["containers"][0]["relevant_event_details"] *= 2
        result = ValidatedProvider(IndependentAdapter()).interpret_events(repeated)
        self.assertEqual(len(result["evidence_references"][0]["matches"]), 2)

    def test_fact_pointer_and_enclosing_document_for_cross_fact_quote(self):
        provider = IndependentAdapter()
        finding = provider.diagnostic["findings"][0]
        finding.update(source="state", evidence='"running": false')
        result = ValidatedProvider(provider).diagnose(SNAPSHOT)
        self.assertEqual(result["evidence_references"][0]["matches"][0]["input_pointer"], "/objective_data/running")
        snapshot = {**SNAPSHOT, "objective_data": {"running": False, "health": "unhealthy"}}
        finding["evidence"] = '"running": false,\n  "health": "unhealthy"'
        result = ValidatedProvider(provider).diagnose(snapshot)
        self.assertEqual(result["evidence_references"][0]["matches"][0]["input_pointer"], "/objective_data")

    def test_reference_count_is_bounded_and_truncation_is_explicit(self):
        result, _ = ValidatedProvider(IndependentAdapter()).analyze_logs("web", "warning\n" * 1000)
        refs = result["evidence_references"][0]
        self.assertEqual(len(refs["matches"]), 16)
        self.assertTrue(refs["matches_truncated"])
