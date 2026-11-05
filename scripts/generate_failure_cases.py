"""Generate evaluation cases from real, intentionally induced Python failures."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.evaluate_model import CASES_PATH, load_cases


FAILURES = (
    ("real_json_decode_error", "import json; json.loads('{broken')"),
    ("real_missing_file", "open('/nonexistent-mcp-shadow-evaluation-file')"),
    ("real_type_error", "int('invalid-mcp-shadow-evaluation-value')"),
)


def main() -> int:
    cases = load_cases(CASES_PATH)
    for name, code in FAILURES:
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=10, check=False)
        if result.returncode == 0 or not result.stderr:
            raise RuntimeError(f"Expected an actual failure for {name}.")
        # Omit traceback paths from the synthetic cases.
        exception = result.stderr.strip().splitlines()[-1]
        cases.append({"id": name, "logs": f"2026-01-01T00:00:00Z ERROR {exception}",
                      "allowed_severities": ["low", "medium", "high", "critical"],
                      "require_evidence": True})
    print(json.dumps(cases, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
