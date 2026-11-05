"""Run the same analysis contract against Ollama and a reference adapter."""
import json
import types
import unittest

import httpx

from provider_contract import ProviderContract
from shadow.analysis import AnalysisError
from shadow.validation import ValidatedProvider
from shadow.ollama import OllamaAnalyzer


class ReferenceProvider:
    def __init__(self, fail=False): self.fail = fail
    def analyze_logs(self, container_name, logs):
        if self.fail: raise AnalysisError("unavailable")
        return {"severity": "low", "summary": "warning", "evidence": ["warning"],
                "probable_causes": [], "recommended_actions": [], "confidence": 0.5}, False
    def interpret_events(self, log_activity):
        if self.fail: raise AnalysisError("unavailable")
        return {"model": "reference", "executed": True, "reason": "relevant_events",
                "summary": "warning", "findings": [], "limitations": [], "confidence": 0.5}
    def diagnose(self, snapshot):
        if self.fail: raise AnalysisError("unavailable")
        return {"model": "reference", "model_severity": "no_indication", "summary": "ok",
                "findings": [], "limitations": [], "confidence": 0.5, "ai_input_truncated": False}


class ReferenceProviderContractTests(ProviderContract, unittest.TestCase):
    def make_provider(self, fail=False): return ValidatedProvider(ReferenceProvider(fail))


class OllamaProviderContractTests(ProviderContract, unittest.TestCase):
    def make_provider(self, fail=False):
        def handler(request):
            if fail:
                raise httpx.ConnectError("unavailable", request=request)
            payload = json.loads(request.content)
            properties = payload["format"]["properties"]
            if "model_severity" in properties:
                response = {"model_severity": "no_indication", "summary": "ok", "findings": [], "limitations": [], "confidence": 0.5}
            elif "severity" in properties:
                response = {"severity": "low", "summary": "warning", "evidence": ["warning"], "probable_causes": [], "recommended_actions": [], "confidence": 0.5}
            else:
                response = {"summary": "warning", "findings": [], "limitations": [], "confidence": 0.5}
            return httpx.Response(200, json={"response": json.dumps(response)})
        transport = httpx.MockTransport(handler)
        http = types.SimpleNamespace(
            Client=lambda **kwargs: httpx.Client(transport=transport, **kwargs),
            HTTPStatusError=httpx.HTTPStatusError, RequestError=httpx.RequestError,
        )
        return ValidatedProvider(OllamaAnalyzer("http://ollama", "mock", 5, 5000, http))

    def test_incomplete_model_responses_fail_for_every_operation(self):
        activity = {"containers": [{"name": "web", "relevant_event_details": [{
            "message": "warning", "content": "warning", "relevance": {"relevant": True},
        }]}]}
        snapshot = {"name": "web", "logs": "warning", "objective_data": {},
                    "log_indicators": {}, "objective_findings": [], "objective_severity": "high"}
        for body in ({"response": "{"}, {"response": "{}"}, {}, []):
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
            http = types.SimpleNamespace(
                Client=lambda **kwargs: httpx.Client(transport=transport, **kwargs),
                HTTPStatusError=httpx.HTTPStatusError, RequestError=httpx.RequestError,
            )
            provider = OllamaAnalyzer("http://ollama", "mock", 5, 5000, http)
            for operation in (lambda: provider.analyze_logs("web", "warning"),
                              lambda: provider.interpret_events(activity),
                              lambda: provider.diagnose(snapshot)):
                with self.subTest(body=body, operation=operation), self.assertRaises(AnalysisError):
                    operation()

    def test_credentials_are_removed_from_model_input(self):
        prompts = []
        def handler(request):
            prompts.append(json.loads(request.content)["prompt"])
            answer = {"severity": "low", "summary": "failure", "evidence": ["ERROR"],
                      "probable_causes": [], "recommended_actions": [], "confidence": 0.5}
            return httpx.Response(200, json={"response": json.dumps(answer)})
        transport = httpx.MockTransport(handler)
        http = types.SimpleNamespace(
            Client=lambda **kwargs: httpx.Client(transport=transport, **kwargs),
            HTTPStatusError=httpx.HTTPStatusError, RequestError=httpx.RequestError,
        )
        provider = OllamaAnalyzer("http://ollama", "mock", 5, 5000, http)
        provider.analyze_logs("web", "ERROR token=synthetic-secret-value")
        self.assertNotIn("synthetic-secret-value", prompts[0])
        self.assertIn("[REDACTED]", prompts[0])
