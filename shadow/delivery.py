"""One durable, explicitly acknowledged report per baseline stream."""
from __future__ import annotations

import uuid
import re
from copy import deepcopy

from mcp.server.fastmcp.exceptions import ToolError


class ReportDelivery:
    """Baseline and outbox share one atomic file and one cross-process lock.

    Build must stage its baseline instead of saving it. Acknowledgement commits
    the proposed checkpoint and removes the outbox in the same replacement.
    Lost responses are replayed until acknowledgement, allowing duplicates.
    """

    def __init__(self, store):
        self.store = store

    def generate(self, build, acknowledged=False):
        with self.store.cycle_lock():
            baseline, _, _ = self.store.load()
            delivery = baseline.get("delivery")
            if delivery is not None:
                self._validate(delivery)
                if not acknowledged:
                    raise ToolError("A report awaits acknowledgement; use acknowledged delivery to replay it, then acknowledge_server_report.")
                return deepcopy(delivery["report"])
            if not acknowledged:
                return build(None)
            staged = []
            report = build(lambda next_baseline: staged.append(deepcopy(next_baseline)))
            if len(staged) > 1:
                raise ToolError("The report attempted multiple baseline commits.")
            identifier = uuid.uuid4().hex
            report["delivery"] = {"id": identifier, "acknowledgement_required": True,
                                  "semantics": "replay_until_acknowledged"}
            report["baseline"]["checkpoint_staged"] = bool(staged)
            report["baseline"]["committed"] = False
            report["baseline"]["awaiting_acknowledgement"] = True
            report["baseline"]["saved"] = False
            report["baseline"]["progress_saved"] = False
            if "markdown" in report:
                report["markdown"] += "\n\nDelivery:\n• The checkpoint awaits explicit acknowledgement."
            # A failed collection may still deliver objective data, but cannot
            # commit a checkpoint that the collector did not stage.
            proposed = staged[0] if staged else deepcopy(baseline)
            proposed["last_acknowledged_delivery"] = baseline.get("last_acknowledged_delivery")
            baseline["delivery"] = {"id": identifier, "report": report, "next_baseline": proposed}
            self.store.save(baseline)
            return report

    def acknowledge(self, identifier):
        if not isinstance(identifier, str) or not identifier:
            raise ToolError("A non-empty delivery_id is required.")
        with self.store.cycle_lock():
            baseline, _, _ = self.store.load()
            delivery = baseline.get("delivery")
            if baseline.get("last_acknowledged_delivery") == identifier:
                return {"delivery_id": identifier, "acknowledged": True, "already_acknowledged": True}
            if delivery is None:
                raise ToolError("No pending report matches this delivery_id.")
            self._validate(delivery)
            if identifier != delivery["id"]:
                raise ToolError("The delivery_id does not match the pending report.")
            proposed = deepcopy(delivery["next_baseline"])
            proposed["delivery"] = None
            proposed["last_acknowledged_delivery"] = identifier
            self.store.save(proposed)
            return {"delivery_id": identifier, "acknowledged": True, "already_acknowledged": False}

    def _validate(self, delivery):
        if (not isinstance(delivery, dict) or not isinstance(delivery.get("id"), str)
                or not isinstance(delivery.get("report"), dict)
                or not isinstance(delivery.get("next_baseline"), dict)):
            raise ToolError("The pending report is invalid; the baseline was not advanced.")
        proposed = delivery["next_baseline"]
        envelope = delivery["report"].get("delivery", {})
        if (re.fullmatch(r"[0-9a-f]{32}", delivery["id"]) is None
                or not isinstance(envelope, dict) or envelope.get("id") != delivery["id"]
                or envelope.get("acknowledgement_required") is not True
                or proposed.get("version") != self.store.version
                or not isinstance(proposed.get("containers"), dict)
                or not isinstance(proposed.get("metrics"), dict)
                or proposed.get("delivery") is not None
                or (proposed.get("last_run") is not None and not isinstance(proposed["last_run"], str))):
            raise ToolError("The pending report or checkpoint is inconsistent; the baseline was not advanced.")
