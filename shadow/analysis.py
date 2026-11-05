"""Application-facing contract for model-assisted analysis."""

from typing import Any, NotRequired, Protocol, TypedDict, runtime_checkable


class AnalysisError(Exception):
    """The configured analysis provider could not produce a usable result."""


class AnalysisTimeout(AnalysisError):
    """The provider exceeded its configured transport timeout."""


class AnalysisUnavailable(AnalysisError):
    """The provider cannot currently accept requests."""


class AnalysisValidationError(AnalysisError):
    """The provider returned a structured but unsupported or inconsistent result."""


class AnalysisFormatError(AnalysisValidationError):
    """The provider result does not satisfy the structural result contract."""


class EvidenceLinkedResult(TypedDict):
    """Fields supplied by the common application boundary, not by adapters."""

    evidence_context_sha256: NotRequired[str]
    evidence_references: NotRequired[list[dict[str, Any]]]
    grounding: NotRequired[dict[str, bool]]


class LogAnalysis(EvidenceLinkedResult):
    severity: str
    summary: str
    evidence: list[str]
    probable_causes: list[str]
    recommended_actions: list[str]
    confidence: float


class EventInterpretation(EvidenceLinkedResult):
    model: str
    executed: bool
    reason: str
    summary: str
    findings: list[dict[str, str]]
    limitations: list[str]
    confidence: float


class DiagnosticAnalysis(EvidenceLinkedResult):
    model: str
    model_severity: str
    summary: str
    findings: list[dict[str, str]]
    limitations: list[str]
    confidence: float
    ai_input_truncated: bool


@runtime_checkable
class AnalysisProvider(Protocol):
    """Provide validated model results without changing objective observations.

    Implementations raise AnalysisError when analysis cannot be completed. The
    caller remains responsible for fallback behavior and baseline persistence.
    """

    def analyze_logs(self, container_name: str, logs: str) -> tuple[LogAnalysis, bool]:
        """Return the analysis and whether the model input was truncated."""
        ...

    def interpret_events(self, log_activity: dict[str, Any]) -> EventInterpretation:
        """Interpret relevant events without assigning objective severity."""
        ...

    def diagnose(self, snapshot: dict[str, Any]) -> DiagnosticAnalysis:
        """Return an advisory model interpretation of an objective snapshot."""
        ...
