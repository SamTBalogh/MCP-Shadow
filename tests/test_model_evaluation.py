import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModelEvaluationTests(unittest.TestCase):
    def test_generated_failure_cases_come_from_failing_processes(self):
        output = subprocess.check_output([sys.executable, "-m", "scripts.generate_failure_cases"],
                                         cwd=ROOT, text=True)
        cases = json.loads(output)
        failures = [case for case in cases if case["id"].startswith("real_")]
        self.assertEqual(len(failures), 3)
        self.assertTrue(all("Error" in case["logs"] for case in failures))
        self.assertTrue(all(str(ROOT) not in case["logs"] for case in failures))

    def test_cases_are_synthetic_and_have_distinct_names(self):
        cases = json.loads((ROOT / "scripts/cases/model_cases.json").read_text())
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        self.assertTrue(all(isinstance(case["logs"], str) and case["allowed_severities"] for case in cases))

    def test_holdout_is_valid_and_disjoint_from_development_cases(self):
        from scripts.evaluate_model import load_cases
        development = load_cases(ROOT / "scripts/cases/model_cases.json")
        holdout = load_cases(ROOT / "scripts/cases/model_holdout_cases.json")
        self.assertFalse({case["id"] for case in development} & {case["id"] for case in holdout})
        self.assertFalse({case["logs"] for case in development} & {case["logs"] for case in holdout})
        self.assertTrue(any(case.get("forbidden_output") for case in holdout))

    def test_evaluator_accepts_grounded_and_rejects_invented_evidence(self):
        code = '''
from scripts.evaluate_model import evaluate_case
class Provider:
    def __init__(self, evidence): self.evidence = evidence
    def analyze_logs(self, name, logs):
        return {"severity": "high", "summary": "Error", "evidence": [self.evidence]}, False
case = {"id": "test", "logs": "connection refused", "allowed_severities": ["high"], "require_evidence": True}
assert evaluate_case(Provider("connection refused"), case)["passed"]
assert not evaluate_case(Provider("invented"), case)["passed"]
'''
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)

    def test_diagnostic_schema_rejects_missing_or_invalid_severity(self):
        code = '''
from shadow.analysis import AnalysisError
from shadow.ollama import _validate_interpretation
valid = {"summary": "ok", "findings": [], "limitations": [], "confidence": 0.5}
for payload in (valid, {**valid, "model_severity": "unknown"},
                {**valid, "model_severity": "high", "confidence": True}):
    try:
        _validate_interpretation(payload, "model_severity")
    except AnalysisError:
        continue
    raise AssertionError("invalid diagnostic was accepted")
_validate_interpretation({**valid, "model_severity": "high"}, "model_severity")
'''
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)

    def test_unreliable_output_is_not_counted_as_a_format_error(self):
        from scripts.evaluate_model import evaluate_case
        from shadow.analysis import AnalysisValidationError
        class Provider:
            def analyze_logs(self, *_):
                raise AnalysisValidationError("Unsupported evidence")
        result = evaluate_case(Provider(), {"id": "case", "logs": "log"})
        self.assertFalse(result["passed"])
        self.assertTrue(result["format_valid"])
        self.assertEqual(result["failure_type"], "unreliable")

    def test_custom_corpus_validation_and_forbidden_output(self):
        code = '''
import json, tempfile
from pathlib import Path
from scripts.evaluate_model import evaluate_case, load_cases
case = {"id": "real-anonymized", "logs": "failed", "allowed_severities": ["high"],
        "require_evidence": True, "forbidden_output": ["secret"]}
class Provider:
    def analyze_logs(self, name, logs):
        return {"severity": "high", "summary": "ok", "evidence": ["failed"],
                "recommended_actions": ["secret"], "probable_causes": [], "confidence": 0.5}, False
assert not evaluate_case(Provider(), case)["passed"]
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "cases.json"
    path.write_text(json.dumps([case]))
    assert load_cases(path) == [case]
    path.write_text(json.dumps([case, case]))
    try: load_cases(path)
    except ValueError: pass
    else: raise AssertionError("duplicate case accepted")
'''
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)

    def test_cli_compares_models_on_identical_cases(self):
        code = '''
import contextlib, io, json, sys
from scripts import evaluate_model
class Provider:
    def __init__(self, base_url, model, timeout, max_chars, http, **options): self.model = model
    def analyze_logs(self, name, logs):
        return {"severity": "no_indication", "summary": "ok", "evidence": [],
                "recommended_actions": [], "probable_causes": [], "confidence": 0.5}, False
evaluate_model.OllamaAnalyzer = Provider
sys.argv = ["evaluate_model", "--model", "one", "--compare", "two", "--case", "normal_startup", "--repeat", "2", "--think", "--temperature", "0.1"]
output = io.StringIO()
with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
    assert evaluate_model.main() == 0
models = json.loads(output.getvalue())["models"]
assert [model["model"] for model in models] == ["one", "two"]
assert all(model["cases"][0]["id"] == "normal_startup" for model in models)
assert all(len(model["cases"]) == 2 and model["settings"]["think"] for model in models)
'''
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


if __name__ == "__main__":
    unittest.main()
