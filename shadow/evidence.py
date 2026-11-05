"""Application-derived references to input text, without claims of causal truth."""
from __future__ import annotations

import hashlib
import json

MAX_MATCHES = 16


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _pointer(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def _matches(excerpt, text, pointer, container, context):
    offset = 0
    while excerpt and (start := text.find(excerpt, offset)) >= 0:
        end = start + len(excerpt)
        identity = json.dumps([context, container, pointer, _digest(text), start, end], separators=(",", ":"))
        yield {"observation_id": _digest(identity), "input_pointer": pointer,
               "container": container, "source_sha256": _digest(text),
               "start_character": start, "end_character": end,
               "start_line": text.count("\n", 0, start) + 1,
               "end_line": text.count("\n", 0, end - 1) + 1}
        offset = start + 1


def _reference(index, excerpt, sources, context):
    matches = []
    capped = False
    for pointer, container, text in sources:
        for match in _matches(excerpt, text, pointer, container, context):
            if len(matches) == MAX_MATCHES:
                capped = True
                break
            matches.append(match)
        if capped:
            break
    return {"item_index": index, "matches": matches,
            "match_status": "unique" if len(matches) == 1 and not capped else "ambiguous" if matches else "unresolved",
            "matches_truncated": capped}


def attach_references(result, operation, data, container_name=""):
    """Overwrite any adapter-supplied references with references to actual inputs.

    Duplicate occurrences remain separate candidates; short excerpts may be
    ambiguous. Offsets count Unicode characters in the named field's text.
    State/metric fields use indented JSON for the single named key and value.
    No raw source text, secrets, or model-chosen IDs are added to the response.
    """
    context = _digest(json.dumps([operation, container_name, data], ensure_ascii=False, sort_keys=True))
    references = []
    if operation == "logs":
        sources = [("/logs", container_name, data)]
        references = [_reference(i, text, sources, context) for i, text in enumerate(result["evidence"])]
    elif operation == "events":
        for index, finding in enumerate(result["findings"]):
            sources = []
            for ci, container in enumerate(data.get("containers", [])):
                name = container["name"].strip().lstrip("/")
                if name != finding["container"].strip().lstrip("/"):
                    continue
                for ei, event in enumerate(container.get("relevant_event_details", [])):
                    if not event.get("relevance", {}).get("relevant"):
                        continue
                    # Prefer the full event content; identical message/content
                    # within one event must not manufacture two occurrences.
                    keys = ("content",) if event.get("content") == event.get("message") else ("content", "message")
                    for key in keys:
                        sources.append((f"/containers/{ci}/relevant_event_details/{ei}/{key}", name, str(event.get(key, ""))))
            references.append(_reference(index, finding["evidence"], sources, context))
    else:
        from shadow.validation import diagnostic_sources
        grouped = diagnostic_sources(data)
        for index, finding in enumerate(result["findings"]):
            kind = finding["source"]
            if kind == "logs":
                sources = [("/logs", data["name"], data["logs"])]
            else:
                sources = []
                for key, value in data["objective_data"].items():
                    is_metric = key in {"cpu_percent", "memory", "network", "processes"}
                    if is_metric == (kind == "metrics"):
                        sources.append((f"/objective_data/{_pointer(key)}", data["name"],
                                        json.dumps({key: value}, ensure_ascii=False, indent=2)))
                # A quote spanning several facts gets an enclosing-document
                # reference, rather than an invented single-field attribution.
                if not any(finding["evidence"] in text for _, _, text in sources):
                    sources = [("/objective_data", data["name"], grouped[kind])]
            references.append(_reference(index, finding["evidence"], sources, context))
    result["evidence_context_sha256"] = context
    result["evidence_references"] = references
    result["grounding"] = {"evidence_presence_checked": True,
                           "causal_accuracy_verified": False, "recommendation_safety_verified": False}
    return result
