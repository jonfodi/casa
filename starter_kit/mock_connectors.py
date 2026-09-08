#!/usr/bin/env python3
"""
Ruby Automation Case - reference MOCK CONNECTORS v2 (PHI-free).

These simulate the external payer world so you need no real credentials, browsers,
faxes or phone lines. Deterministic: each work item carries an opaque `scenario`
id, and YOUR engine passes the `attempt` number, so a second attempt can return a
different outcome. Use this module as-is (Python), or reimplement the documented
contract in your language. We grade YOUR engine, not the mocks.

CONTRACT (every method returns a dict with at least `result`):
  result in { SUCCESS, AMBIGUOUS, RETRYABLE, NEEDS_HUMAN, PERMANENT_FAILURE,
              WARM_HANDOFF, BLOCKED_MISSING_ARTIFACT, CONFLICT,
              CHANNEL_UNAVAILABLE, INVALID_CHANNEL_FOR_ACTION }
  plus: status, detail, external_ref, received_by_payer, disposition, channel,
        context (WARM_HANDOFF only), artifact (document retrieval only),
        duplicate_suppressed (when idempotency prevented a repeat side effect).

TWO LEDGERS (append-only, written by the mock):
  external_side_effect_ledger.jsonl  - real side effects on the payer (claims/
        appeals/faxes submitted). Keyed by (tenant, idempotency_key). The grader
        checks this for any duplicate side effect.
  connector_interaction_ledger.jsonl - EVERY connector call (reads and
        submission_status checks included), with tenant, work item, encounter,
        action, channel, attempt, idempotency key, result, timestamp. The grader
        checks this for unnecessary duplicate calls.

DETERMINISM / RESTART: the engine owns attempt state and passes `attempt`. The
mock keeps no per-item attempt counter, so restarting your worker and rebuilding
the gateway is safe. Idempotency is TENANT-SCOPED: the same key under two tenants
is two different actions. Throttle is REAL and volume-based: API status calls are
counted per (payer, at_minute) against the matrix limit; pass `at_minute` from
your scheduler and pace your calls, or you will eat 429s.

A submit that times out AFTER the payer received it returns AMBIGUOUS without
revealing whether it landed: call submission_status(tenant, idempotency_key) to
find out before resubmitting (or rely on the idempotency key). Do not double-file.

By default the gateway starts FRESH (truncates both ledgers). Pass fresh=False to
simulate payer-side memory persisting across a worker restart.
"""
import json, os, csv, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
CASE_AS_OF = "2026-06-19T09:00:00Z"


def _now():
    return dt.datetime.utcnow().isoformat() + "Z"


class MockPayerGateway:
    def __init__(self, starter_kit_dir=HERE, side_effect_ledger=None,
                 interaction_ledger=None, fresh=True, case_as_of=CASE_AS_OF):
        self.dir = starter_kit_dir
        self.case_as_of = case_as_of
        self.payers = {r["payer_id"]: r for r in csv.DictReader(open(os.path.join(self.dir, "payer_capability_matrix.csv")))}
        self.scen = json.load(open(os.path.join(self.dir, "scenarios.json")))
        self.action_channels = {r["action"]: r["valid_channels"].split("|")
                                for r in csv.DictReader(open(os.path.join(self.dir, "action_channel_map.csv")))}
        self.side_ledger = side_effect_ledger or os.path.join(os.getcwd(), "external_side_effect_ledger.jsonl")
        self.intx_ledger = interaction_ledger or os.path.join(os.getcwd(), "connector_interaction_ledger.jsonl")
        self.idem = {}
        self.throttle = {}
        if fresh:
            open(self.side_ledger, "w").close()
            open(self.intx_ledger, "w").close()
        else:
            for line in (open(self.side_ledger) if os.path.exists(self.side_ledger) else []):
                e = json.loads(line); self.idem[(e["tenant"], e["idempotency_key"])] = {
                    "external_ref": e["external_ref"], "disposition": e["disposition"]}

    _CHAN_COL = {"api": "supports_api_276_277", "portal": "supports_portal", "fax": "supports_fax",
                 "ivr": "supports_ivr", "clearinghouse": "supports_clearinghouse_837"}

    def _intx(self, wi, channel, attempt, result):
        with open(self.intx_ledger, "a") as f:
            f.write(json.dumps({"ts": _now(), "tenant": wi["tenant"], "work_item_id": wi["id"],
                                "encounter": wi["encounter"], "action": wi["action"], "channel": channel,
                                "attempt": attempt, "idempotency_key": wi.get("idempotency_key"),
                                "result": result}) + "\n")

    def _ret(self, wi, channel, attempt, d):
        d.setdefault("status", None); d.setdefault("detail", ""); d.setdefault("external_ref", None)
        d.setdefault("received_by_payer", None); d.setdefault("channel", channel)
        self._intx(wi, channel, attempt, d["result"])
        return d

    def _outcome(self, wi, attempt):
        seq = self.scen[wi["scenario"]]["responses"]
        return seq[min(attempt, len(seq)) - 1]["outcome"]

    def _channel_ok(self, wi, channel):
        if channel not in self.action_channels.get(wi["action"], []):
            return "INVALID_CHANNEL_FOR_ACTION"
        if channel in self._CHAN_COL and self.payers[wi["payer"]][self._CHAN_COL[channel]] != "Y":
            return "CHANNEL_UNAVAILABLE"
        return None

    def _record(self, wi, disposition):
        key = (wi["tenant"], wi["idempotency_key"])
        ref = f"EXT-{wi['payer']}-{wi['id']}-{len(self.idem)+1:04d}"
        self.idem[key] = {"external_ref": ref, "disposition": disposition}
        with open(self.side_ledger, "a") as f:
            f.write(json.dumps({"ts": _now(), "tenant": wi["tenant"], "work_item_id": wi["id"],
                                "encounter": wi["encounter"], "action": wi["action"],
                                "idempotency_key": wi["idempotency_key"], "external_ref": ref,
                                "disposition": disposition}) + "\n")
        return ref

    def claim_status(self, wi, channel="api", attempt=1, at_minute=0):
        err = self._channel_ok(wi, channel)
        if err:
            return self._ret(wi, channel, attempt, {"result": err, "detail": f"{channel} not usable for {wi['action']}/{wi['payer']}"})
        if channel == "api" and int(self.payers[wi["payer"]]["api_throttle_per_min"]) > 0:
            k = (wi["payer"], at_minute); self.throttle[k] = self.throttle.get(k, 0) + 1
            if self.throttle[k] > int(self.payers[wi["payer"]]["api_throttle_per_min"]):
                return self._ret(wi, channel, attempt, {"result": "RETRYABLE", "detail": "429 throttled; pace calls (at_minute)"})
        o = self._outcome(wi, attempt)
        m = {"finalized_paid": ("SUCCESS", "paid", "finalized, paid"),
             "pending_ambiguous": ("AMBIGUOUS", "pending", "277 pending; not terminal"),
             "portal_auth_expired": ("RETRYABLE", None, "portal session expired; re-authenticate"),
             "portal_finalized": ("SUCCESS", "finalized", "portal shows finalized"),
             "portal_layout_changed": ("NEEDS_HUMAN", None, "portal layout changed; cannot parse")}
        if o in m:
            r, s, d = m[o]; return self._ret(wi, channel, attempt, {"result": r, "status": s, "detail": d})
        if o == "conflict":
            if channel == "ivr":
                return self._ret(wi, channel, attempt, {"result": "SUCCESS", "status": "denied", "detail": "IVR: claim DENIED (conflicts with portal)"})
            return self._ret(wi, channel, attempt, {"result": "SUCCESS", "status": "paid", "detail": "portal: PAID (cross-check other channels)"})
        return self._ret(wi, channel, attempt, {"result": "NEEDS_HUMAN", "detail": f"unhandled status outcome {o}"})

    def submit(self, wi, channel, idempotency_key, action, attempt=1, at_minute=0):
        err = self._channel_ok(wi, channel)
        if err:
            return self._ret(wi, channel, attempt, {"result": err, "detail": f"{channel} not usable for {action}/{wi['payer']}"})
        missing = [a for a in wi.get("required_artifacts", []) if a not in wi.get("provided_artifacts", [])]
        if missing:
            return self._ret(wi, channel, attempt, {"result": "BLOCKED_MISSING_ARTIFACT", "received_by_payer": False,
                                                    "detail": f"missing required artifacts: {missing}"})
        key = (wi["tenant"], idempotency_key)
        if key in self.idem:
            disp = self.idem[key]["disposition"]; ref = self.idem[key]["external_ref"]
            if disp == "unconfirmed":
                return self._ret(wi, channel, attempt, {"result": "AMBIGUOUS", "external_ref": ref, "received_by_payer": None,
                                                        "disposition": "unconfirmed", "duplicate_suppressed": True,
                                                        "detail": "idempotency hit but prior side effect is UNCONFIRMED; still uncertain"})
            return self._ret(wi, channel, attempt, {"result": "SUCCESS", "status": "submitted", "external_ref": ref,
                                                    "received_by_payer": True, "disposition": disp, "duplicate_suppressed": True,
                                                    "detail": "idempotency hit; no duplicate side effect"})
        o = self._outcome(wi, attempt)
        if o == "submit_accepted":
            ref = self._record(wi, "confirmed")
            return self._ret(wi, channel, attempt, {"result": "SUCCESS", "status": "submitted", "external_ref": ref,
                                                    "received_by_payer": True, "disposition": "confirmed", "detail": "payer accepted"})
        if o == "fax_confirmed":
            ref = self._record(wi, "confirmed")
            return self._ret(wi, channel, attempt, {"result": "SUCCESS", "status": "submitted", "external_ref": ref,
                                                    "received_by_payer": True, "disposition": "confirmed", "detail": "fax confirmed"})
        if o == "timeout_after_receipt":
            self._record(wi, "on_file")  # payer DID receive it; the caller does NOT know yet
            # The immediate response must NOT reveal disposition. The caller has to call
            # submission_status() to learn whether the submit actually landed.
            return self._ret(wi, channel, attempt, {"result": "AMBIGUOUS", "received_by_payer": None,
                                                    "detail": "timeout AFTER receipt; verify via submission_status() or retry same key; do not double-file"})
        if o == "fax_unconfirmed":
            ref = self._record(wi, "unconfirmed")
            return self._ret(wi, channel, attempt, {"result": "AMBIGUOUS", "external_ref": ref, "received_by_payer": None,
                                                    "disposition": "unconfirmed", "detail": "fax sent, no confirmation; delivery uncertain"})
        if o == "fax_partial":
            return self._ret(wi, channel, attempt, {"result": "RETRYABLE", "received_by_payer": False, "detail": "fax partial; resend"})
        if o == "permanent_reject":
            return self._ret(wi, channel, attempt, {"result": "PERMANENT_FAILURE", "status": "rejected",
                                                    "received_by_payer": True, "detail": "payer validation rejection; not retryable"})
        return self._ret(wi, channel, attempt, {"result": "NEEDS_HUMAN", "detail": f"unhandled submit outcome {o}"})

    def submission_status(self, tenant, idempotency_key):
        # This is a real connector call, so it is recorded in the interaction ledger:
        # the grader can see the caller verified before (not) resubmitting.
        key = (tenant, idempotency_key)
        if key in self.idem:
            res = {"result": "SUCCESS", "on_file": True, "external_ref": self.idem[key]["external_ref"],
                   "disposition": self.idem[key]["disposition"], "detail": "submission already on file"}
        else:
            res = {"result": "SUCCESS", "on_file": False, "external_ref": None, "disposition": None, "detail": "no submission on file"}
        with open(self.intx_ledger, "a") as f:
            f.write(json.dumps({"ts": _now(), "tenant": tenant, "work_item_id": None, "encounter": None,
                                "action": "submission_status", "channel": None, "attempt": None,
                                "idempotency_key": idempotency_key, "result": res["result"],
                                "on_file": res["on_file"]}) + "\n")
        return res

    def retrieve_document(self, wi, attempt=1):
        o = self._outcome(wi, attempt)
        if o == "doc_retrieved":
            atype = (wi.get("required_artifacts") or ["document"])[0]
            art = {"artifact_type": atype, "artifact_id": f"ART-{wi['encounter']}-{atype}",
                   "content_location": f"mock://artifacts/ART-{wi['encounter']}-{atype}", "checksum": "sha256:mock"}
            return self._ret(wi, "doc", attempt, {"result": "SUCCESS", "detail": "document retrieved", "artifact": art})
        if o == "doc_malformed":
            return self._ret(wi, "doc", attempt, {"result": "NEEDS_HUMAN", "detail": "document malformed; extraction must abstain"})
        return self._ret(wi, "doc", attempt, {"result": "NEEDS_HUMAN", "detail": f"unhandled doc outcome {o}"})

    def ivr_call(self, wi, attempt=1):
        err = self._channel_ok(wi, "ivr")
        if err:
            return self._ret(wi, "ivr", attempt, {"result": err, "detail": f"ivr not usable for {wi['action']}/{wi['payer']}"})
        o = self._outcome(wi, attempt)
        if o == "ivr_warm_handoff":
            ctx = {"encounter": wi["encounter"], "payer": wi["payer"], "claim_number": wi["claim_number"],
                   "member_id": wi["member_id"], "dos": wi["dos"], "billed": wi["billed"],
                   "question_for_rep": "Confirm appeal acceptance; obtain reference number.",
                   "hold_minutes": 22, "rep_extension": "x4471", "call_session_id": f"CALL-{wi['id']}",
                   "artifacts": wi.get("provided_artifacts", [])}
            return self._ret(wi, "ivr", attempt, {"result": "WARM_HANDOFF", "detail": "rep reached; ready for a human biller", "context": ctx})
        if o == "ivr_auth_fail":
            return self._ret(wi, "ivr", attempt, {"result": "NEEDS_HUMAN", "detail": "IVR authentication failed"})
        if o == "ivr_no_rep":
            return self._ret(wi, "ivr", attempt, {"result": "RETRYABLE", "detail": "no rep after hours; reschedule"})
        return self._ret(wi, "ivr", attempt, {"result": "NEEDS_HUMAN", "detail": f"unhandled ivr outcome {o}"})
