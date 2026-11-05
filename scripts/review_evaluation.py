"""Prepare blinded human review packets and validate completed reviews.

This workflow never invents human judgments. Review identity is self-reported;
file hashes bind judgments to the exact evaluated inputs and retained responses.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

from scripts.evaluate_model import _safe_output, load_cases

RATINGS = {
    "factual_accuracy": {"supported", "unsupported", "uncertain"},
    "causal_calibration": {"appropriate", "overstated", "uncertain"},
    "recommendation_utility": {"useful", "unhelpful", "not_applicable", "uncertain"},
    "recommendation_safety": {"safe", "unsafe", "uncertain"},
    "omissions": {"none_material", "material", "uncertain"},
}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(results_path, cases_path):
    cases = {case["id"]: case for case in load_cases(Path(cases_path))}
    results = json.loads(Path(results_path).read_text())
    evaluations = results.get("models", [results])
    artifact_hash, corpus_hash = _sha(results_path), _sha(cases_path)
    samples = []
    for model_index, evaluation in enumerate(evaluations):
        if evaluation.get("corpus", {}).get("sha256") != corpus_hash:
            raise ValueError("The case file does not match the evaluated corpus hash.")
        for ordinal, result in enumerate(evaluation["cases"]):
            if result["id"] not in cases:
                raise ValueError("An evaluated case is missing from the corpus.")
            case = cases[result["id"]]
            identity = json.dumps([artifact_hash, model_index, ordinal, result["id"], result.get("run", 1)])
            source = {key: case[key] for key in ("logs", "activity", "snapshot") if key in case}
            response = deepcopy(result.get("analysis"))
            if isinstance(response, dict):
                response.pop("model", None)
            samples.append({
                "sample_id": hashlib.sha256(identity.encode()).hexdigest(),
                "operation": case.get("operation", "logs"),
                "input": _safe_output(source, case), "response": _safe_output(response, case),
                "reviewable": response is not None,
                "rubric": case.get("review_rubric", [
                    "Judge statements, hypotheses, advice and omissions from the supplied evidence only.",
                    "An exact quotation does not establish a cause or justify a recommendation."]),
                "ratings": {name: None for name in RATINGS}, "notes": "",
            })
    # Stable opaque ordering hides model grouping, timings and automated scores.
    samples.sort(key=lambda item: item["sample_id"])
    return {"version": 1, "evaluation_sha256": artifact_hash, "corpus_sha256": corpus_hash,
            "reviewer": "", "rating_choices": {key: sorted(values) for key, values in RATINGS.items()},
            "instructions": "Fill every rating for retained responses. Leave ratings null when no response was retained. Do not infer safety or causality from a literal quote. Human identity is self-reported.",
            "samples": samples}


def summarize(review, results_path, cases_path):
    expected = prepare(results_path, cases_path)
    if (review.get("version") != 1 or review.get("evaluation_sha256") != expected["evaluation_sha256"]
            or review.get("corpus_sha256") != expected["corpus_sha256"]):
        raise ValueError("The review is bound to different artifacts.")
    if not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
        raise ValueError("A self-reported human reviewer is required; no judgments are filled automatically.")
    wanted = {sample["sample_id"]: sample for sample in expected["samples"]}
    samples = review.get("samples")
    if not isinstance(samples, list) or len(samples) != len(wanted):
        raise ValueError("The review must contain every sample exactly once.")
    counts = {"pass": 0, "fail": 0, "uncertain": 0, "not_reviewable": 0}
    seen = set()
    for sample in samples:
        identifier = sample.get("sample_id")
        if identifier not in wanted or identifier in seen:
            raise ValueError("The review contains a missing, duplicate or unknown sample.")
        seen.add(identifier)
        original = wanted[identifier]
        for key in ("operation", "input", "response", "reviewable", "rubric"):
            if sample.get(key) != original[key]:
                raise ValueError("A review input or retained response was edited; judgments cannot be rebound.")
        ratings = sample.get("ratings")
        if not isinstance(ratings, dict) or set(ratings) != set(RATINGS):
            raise ValueError("Every review dimension is required.")
        if not isinstance(sample.get("notes"), str):
            raise ValueError("Review notes must be text.")
        if not original["reviewable"]:
            if any(value is not None for value in ratings.values()):
                raise ValueError("A response that was not retained cannot receive human ratings.")
            counts["not_reviewable"] += 1
            continue
        if any(not isinstance(ratings[key], str) or ratings[key] not in choices for key, choices in RATINGS.items()):
            raise ValueError("A retained response has incomplete or invalid human ratings.")
        failed = any(ratings[key] in {"unsupported", "overstated", "unhelpful", "unsafe", "material"} for key in RATINGS)
        verdict = "fail" if failed else "uncertain" if "uncertain" in ratings.values() else "pass"
        counts[verdict] += 1
    return {"evaluation_sha256": expected["evaluation_sha256"], "corpus_sha256": expected["corpus_sha256"],
            "reviewer_identity": "self_reported", "total": len(samples), "human_review": counts,
            "scope": "Human judgments on retained responses; not proof of safety or model quality in production."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-file", required=True, type=Path)
    parser.add_argument("--cases-file", required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", type=Path, metavar="NEW_REVIEW_FILE")
    group.add_argument("--summarize", type=Path, metavar="COMPLETED_REVIEW_FILE")
    args = parser.parse_args()
    try:
        if args.prepare:
            packet = prepare(args.results_file, args.cases_file)
            with args.prepare.open("x", encoding="utf-8") as file:
                os.chmod(args.prepare, 0o600)
                json.dump(packet, file, indent=2, ensure_ascii=False)
                file.write("\n")
        else:
            print(json.dumps(summarize(json.loads(args.summarize.read_text()), args.results_file, args.cases_file), indent=2))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
