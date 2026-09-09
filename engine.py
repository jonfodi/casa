#!/usr/bin/env python3
"""Payer-action automation engine."""

import argparse, json, csv, sys, os, collections
from datetime import date

CHANNEL_COLUMN = {"api": "supports_api_276_277", "portal": "supports_portal",
                  "fax": "supports_fax", "ivr": "supports_ivr",
                  "clearinghouse": "supports_clearinghouse_837"}

CHANNEL_RANK = ["api", "clearinghouse", "portal", "fax", "ivr", "doc"]

TERMINAL = {"completed", "needs_human_review", "warm_handoff_ready",
            "permanently_failed", "cancelled_or_superseded"}

RESULT_STATE = {"SUCCESS": "completed",
                "RETRYABLE": "retry_scheduled",
                "PERMANENT_FAILURE": "permanently_failed",
                "NEEDS_HUMAN": "needs_human_review",
                "WARM_HANDOFF": "warm_handoff_ready"}

MESSAGE_FIELDS = {"id", "idempotency_key", "source", "created_at"}

WORKFLOW_VERSION = "engine-0.1.0"

WORKABLE = {"queued", "retry_scheduled"}

MAX_ATTEMPTS = 3

SIDE_EFFECT_ACTIONS = {"corrected_claim_submission", "appeal_submission"}


def record(job, state, reason, kit, events, actor="automation",
           result=None, channel=None, attempt=0):
    """The only way a job changes state. Always writes one audit event.
    Single door, so persisting jobs here would survive a mid-run crash."""
    item = job["item"]
    job["state"] = state
    job["reason"] = reason
    events.append({
        "event_id": "evt-%06d" % (len(events) + 1),
        "correlation_id": "corr-" + item["id"],
        "work_item_id": item["id"],
        "tenant": item["tenant"],
        "ts": kit["case_as_of"],
        "actor": actor,
        "schema_version": item.get("schema_version", "1.0"),
        "workflow_version": WORKFLOW_VERSION,
        "action": item["action"],
        "channel": channel,
        "attempt": attempt,
        "result": result,
        "external_ref": job["external_ref"],
        "artifacts": list(item.get("provided_artifacts", [])),
        "human_override": None,
        "disposition": state,
        "detail": reason,
    })

def load_kit(kit_dir):
    sys.path.insert(0, kit_dir)
    import mock_connectors
    return {
        "case_as_of": mock_connectors.CASE_AS_OF,
        "gateway_class": mock_connectors.MockPayerGateway,
        "items": [json.loads(l) for l in open(os.path.join(kit_dir, "work_items.jsonl")) if l.strip()],
        "action_channels": {r["action"]: r["valid_channels"].split("|") for r in
                            csv.DictReader(open(os.path.join(kit_dir, "action_channel_map.csv")))},
        "payers": {r["payer_id"]: r for r in
                   csv.DictReader(open(os.path.join(kit_dir, "payer_capability_matrix.csv")))},
    }

def choose_channel(item, kit):
    allowed = kit["action_channels"][item["action"]]
    payer = kit["payers"][item["payer"]]
    usable = [c for c in allowed if c not in CHANNEL_COLUMN or payer[CHANNEL_COLUMN[c]] == "Y"]
    return sorted(usable, key=CHANNEL_RANK.index)

def has_room(job, kit, used, minute):
    """Only api calls are rate limited, per payer per minute."""
    # Recomputes the channel the work body will pick; fine at this size.
    channels = choose_channel(job["item"], kit)
    if not channels or channels[0] != "api":
        return True
    limit = int(kit["payers"][job["item"]["payer"]]["api_throttle_per_min"])
    return limit <= 0 or used.get((job["item"]["payer"], minute), 0) < limit

def handoff_detail(response):
    """Carry any handoff context forward so a human does not redo the work."""
    context = response.get("context") or {}
    if not context:
        return response.get("detail", "")
    bundle = "; ".join("%s=%s" % (k, v) for k, v in sorted(context.items()))
    return "%s | handoff context: %s" % (response.get("detail", ""), bundle)


def verify_submission(item, gw):
    """An ambiguous submit may already have landed. Ask before ever resending."""
    check = gw.submission_status(item["tenant"], item["idempotency_key"])
    if not check["on_file"]:
        return "retry_scheduled", "no submission on file; safe to resend", None
    if check["disposition"] == "unconfirmed":
        return ("needs_human_review", "on file but delivery unconfirmed; do not resend",
                check["external_ref"])
    return "completed", "verified on file; no resubmit", check["external_ref"]


def reconcile(jobs, kit, events):
    """Mark rows that must not be acted on. Covers the cases seen in this
    batch; there may be others."""
    for job in jobs:
        record(job, "queued", "loaded from batch", kit, events, actor="system")

    as_of = date.fromisoformat(kit["case_as_of"][:10])
    superseded = {j["item"]["supersedes"] for j in jobs if j["item"].get("supersedes")}
    by_key, by_work, winners = {}, {}, {}

    for job in jobs:
        item = job["item"]
        key = (item["tenant"], item["idempotency_key"])
        work = (item["tenant"], item["encounter"], item["action"])

        if item["id"] in superseded:
            cancel(job, "replaced by a later work item", kit, events)
        elif key in by_key:
            cancel(job, "same instruction already queued as " + by_key[key], kit, events, by_key[key])
        elif work in by_work:
            other = by_work[work]
            if same_instruction(item, winners[other]["item"]):
                cancel(job, "same encounter and action already queued as " + other, kit, events, other)
            else:
                escalate(job, "conflicts with " + other + " on the same encounter and action", kit, events)
                escalate(winners[other], "conflicts with " + item["id"] + " on the same encounter and action", kit, events)
        else:
            by_key[key] = by_work[work] = item["id"]
            winners[item["id"]] = job
            if item.get("deadline") and date.fromisoformat(item["deadline"]) < as_of:
                escalate(job, "deadline %s already passed as of %s" % (item["deadline"], as_of),
                         kit, events)

def same_instruction(a, b):
    """Two rows are the same instruction if everything but the message
    envelope matches. Unknown fields are compared, so we over-flag rather
    than silently act on the wrong one."""
    strip = lambda d: {k: v for k, v in d.items() if k not in MESSAGE_FIELDS}
    return strip(a) == strip(b)

def cancel(job, reason, kit, events, duplicate_of=None):
    job["duplicate_of"] = duplicate_of
    record(job, "cancelled_or_superseded", reason, kit, events, actor="system")

def escalate(job, reason, kit, events):
    record(job, "needs_human_review", reason, kit, events, actor="system")

def write_audit_log(events, out_dir):
    with open(os.path.join(out_dir, "audit_log.jsonl"), "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")

def write_run_summary(jobs, kit, out_dir):
    total = len(jobs) or 1
    count = lambda s: sum(1 for j in jobs if j["state"] == s)
    summary = {
        "case_as_of": kit["case_as_of"],
        "items": [{"work_item_id": j["item"]["id"],
                   "tenant": j["item"]["tenant"],
                   "current_state": j["state"],
                   "terminal_disposition": j["state"] if j["state"] in TERMINAL else None,
                   "attempts": j["attempts"],
                   "external_ref": j["external_ref"],
                   "reason": j["reason"]} for j in jobs],
        "metrics": {
            "completion_rate": count("completed") / total,
            "touchless_completion_rate": 0.0,
            "human_minutes": 0,
            "retry_count": 0,
            "failure_count": count("permanently_failed"),
            "duplicates_prevented": sum(1 for j in jobs if j["duplicate_of"]),
            "warm_handoffs_ready": count("warm_handoff_ready"),
        },
    }
    with open(os.path.join(out_dir, "run_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kit", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    kit = load_kit(args.kit)
    gw = kit["gateway_class"](
        starter_kit_dir=args.kit,
        side_effect_ledger=os.path.join(args.out, "external_side_effect_ledger.jsonl"),
        interaction_ledger=os.path.join(args.out, "connector_interaction_ledger.jsonl"))

    jobs = [{"item": item, "state": "queued", "reason": "", "attempts": 0,
             "external_ref": None, "duplicate_of": None} for item in kit["items"]]

    events = []
    reconcile(jobs, kit, events)

    minute, used = 0, {}

    while True:
        # Three scans of the whole list per pass. Fine at this size, revisit at scale.
        job = next((j for j in jobs
                    if j["state"] in WORKABLE and j["attempts"] >= MAX_ATTEMPTS), None)
        if job:
            escalate(job, "retry limit reached after %d attempts" % job["attempts"], kit, events)
            continue

        job = next((j for j in jobs
                    if j["state"] in WORKABLE and has_room(j, kit, used, minute)), None)
        if job is None:
            if any(j["state"] in WORKABLE for j in jobs):
                minute += 1
                continue
            break

        item = job["item"]
        channels = choose_channel(item, kit)
        if not channels:
            escalate(job, "no channel supports this action for this payer", kit, events)
            continue
        channel = channels[0]
        action = item["action"]
        job["attempts"] += 1
        if channel == "api":
            used[(item["payer"], minute)] = used.get((item["payer"], minute), 0) + 1
        # retrieve_document and ivr_call take no channel; there is only ever one.
        if action == "claim_status_inquiry":
            response = gw.claim_status(item, channel=channel, attempt=job["attempts"], at_minute=minute)
        elif action in ("corrected_claim_submission", "appeal_submission"):
            response = gw.submit(item, channel, item["idempotency_key"], action, attempt=job["attempts"], at_minute=minute)
        elif action == "document_retrieval":
            response = gw.retrieve_document(item, attempt=job["attempts"])
        else:
            response = gw.ivr_call(item, attempt=job["attempts"])
        job["external_ref"] = response.get("external_ref") or job["external_ref"]
        detail = handoff_detail(response)
        state = RESULT_STATE.get(response["result"], "needs_human_review")
        if response["result"] == "AMBIGUOUS":
            if action in SIDE_EFFECT_ACTIONS:
                state, detail, ref = verify_submission(item, gw)
                job["external_ref"] = ref or job["external_ref"]
            else:
                state = "awaiting_external_response"
        record(job, state, detail, kit, events,
               result=response["result"], channel=channel, attempt=job["attempts"])
        print(item["id"].ljust(12), response["result"].ljust(20), "->", job["state"])

    print()
    for state, n in collections.Counter(j["state"] for j in jobs).most_common():
        print(f"{n:3}  {state}")

    write_audit_log(events, args.out)
    write_run_summary(jobs, kit, args.out)


if __name__ == "__main__":
    main()
