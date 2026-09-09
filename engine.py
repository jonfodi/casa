#!/usr/bin/env python3
"""Payer-action automation engine.

Split into pure decisions and impure effects. Everything above `load_kit` takes
data and returns data. Everything below it touches the network, the disk, or
mutates a job.
"""

import argparse, json, csv, sys, os, collections
from datetime import date

WORKFLOW_VERSION = "engine-0.1.0"

MAX_ATTEMPTS = 3

WORKABLE = {"queued", "retry_scheduled"}

TERMINAL = {"completed", "needs_human_review", "warm_handoff_ready",
            "permanently_failed", "cancelled_or_superseded"}

CHANNEL_RANK = ["api", "clearinghouse", "portal", "fax", "ivr", "doc"]

RESULT_STATE = {"SUCCESS": "completed",
                "RETRYABLE": "retry_scheduled",
                "AMBIGUOUS": "awaiting_external_response",
                "PERMANENT_FAILURE": "permanently_failed",
                "NEEDS_HUMAN": "needs_human_review",
                "WARM_HANDOFF": "warm_handoff_ready"}

SIDE_EFFECT_ACTIONS = {"corrected_claim_submission", "appeal_submission"}

MESSAGE_FIELDS = {"id", "idempotency_key", "source", "created_at"}


# ---------------------------------------------------------------- pure


def choose_channel(item, kit):
    """Channels the action allows, kept to what the payer has, best first."""
    allowed = kit["action_channels"][item["action"]]
    payer = kit["payers"][item["payer"]]
    return sorted([c for c in allowed if supports(payer, c)], key=CHANNEL_RANK.index)


def supports(payer, channel):
    """A channel is available unless the payer has a supports_ column saying no."""
    column = next((c for c in payer if c.startswith("supports_") and channel in c), None)
    return column is None or payer[column] == "Y"


def same_instruction(a, b):
    """Same job if everything but the message envelope matches. Unknown fields
    are compared, so an unfamiliar kit over-flags rather than acting wrongly."""
    strip = lambda d: {k: v for k, v in d.items() if k not in MESSAGE_FIELDS}
    return strip(a) == strip(b)


def reconcile_verdicts(items, as_of):
    """Rows that must not be dispatched, decided from the batch alone."""
    verdicts = {}
    superseded = {i["supersedes"] for i in items if i.get("supersedes")}
    by_key, by_work, winners = {}, {}, {}

    for item in items:
        key = (item["tenant"], item["idempotency_key"])
        work = (item["tenant"], item["encounter"], item["action"])

        if item["id"] in superseded:
            verdicts[item["id"]] = ("cancelled_or_superseded",
                                    "replaced by a later work item", None)
        elif key in by_key:
            verdicts[item["id"]] = ("cancelled_or_superseded",
                                    "same instruction already queued as " + by_key[key],
                                    by_key[key])
        elif work in by_work:
            other = by_work[work]
            if same_instruction(item, winners[other]):
                verdicts[item["id"]] = ("cancelled_or_superseded",
                                        "same encounter and action already queued as " + other,
                                        other)
            else:
                note = " on the same encounter and action"
                verdicts[item["id"]] = ("needs_human_review", "conflicts with " + other + note, None)
                verdicts[other] = ("needs_human_review", "conflicts with " + item["id"] + note, None)
        else:
            by_key[key] = by_work[work] = item["id"]
            winners[item["id"]] = item
            if item.get("deadline") and date.fromisoformat(item["deadline"]) < as_of:
                verdicts[item["id"]] = ("needs_human_review",
                                        "deadline %s already passed as of %s" % (item["deadline"], as_of),
                                        None)
    return verdicts


def is_ready(job, kit, used, minute):
    """Workable, done resting, and this payer still has calls left this minute."""
    if job["state"] not in WORKABLE or job["not_before"] > minute:
        return False
    channels = choose_channel(job["item"], kit)
    if not channels or channels[0] != "api":
        return True
    limit = int(kit["payers"][job["item"]["payer"]]["api_throttle_per_min"])
    return limit <= 0 or used.get((job["item"]["payer"], minute), 0) < limit


def interpret(response):
    """What the connector's answer means for the job."""
    return (RESULT_STATE.get(response["result"], "needs_human_review"),
            handoff_detail(response), response.get("external_ref"))


def interpret_verification(check):
    """What a submission_status answer means for the job."""
    if not check["on_file"]:
        return "retry_scheduled", "no submission on file; safe to resend", None
    if check["disposition"] == "unconfirmed":
        return ("needs_human_review", "on file but delivery unconfirmed; do not resend",
                check["external_ref"])
    return "completed", "verified on file; no resubmit", check["external_ref"]


def handoff_detail(response):
    """Carry any handoff context forward so a human does not redo the work."""
    context = response.get("context") or {}
    if not context:
        return response.get("detail", "")
    bundle = "; ".join("%s=%s" % (k, v) for k, v in sorted(context.items()))
    return "%s | handoff context: %s" % (response.get("detail", ""), bundle)


def build_summary(jobs, kit):
    total = len(jobs) or 1
    count = lambda s: sum(1 for j in jobs if j["state"] == s)
    return {
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
            # No human acts mid-run, so every completion is touchless.
            "touchless_completion_rate": count("completed") / total,
            "human_minutes": sum(j["human_minutes"] for j in jobs),
            "retry_count": sum(max(0, j["attempts"] - 1) for j in jobs),
            "failure_count": count("permanently_failed"),
            "duplicates_prevented": sum(1 for j in jobs if j["duplicate_of"]),
            "warm_handoffs_ready": count("warm_handoff_ready"),
        },
    }


# -------------------------------------------------------------- impure


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


def apply(job, state, reason, kit, events, actor="automation",
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


def call_connector(item, channel, attempt, minute, gw):
    action = item["action"]
    # retrieve_document and ivr_call take no channel; there is only ever one.
    if action == "claim_status_inquiry":
        return gw.claim_status(item, channel=channel, attempt=attempt, at_minute=minute)
    if action in SIDE_EFFECT_ACTIONS:
        return gw.submit(item, channel, item["idempotency_key"], action,
                         attempt=attempt, at_minute=minute)
    if action == "document_retrieval":
        return gw.retrieve_document(item, attempt=attempt)
    return gw.ivr_call(item, attempt=attempt)


def work(job, kit, gw, events, used, minute):
    item = job["item"]
    channels = choose_channel(item, kit)
    if not channels:
        apply(job, "needs_human_review", "no channel supports this action for this payer",
              kit, events, actor="system")
        return

    channel = channels[0]
    job["attempts"] += 1
    if channel == "api":
        used[(item["payer"], minute)] = used.get((item["payer"], minute), 0) + 1

    response = call_connector(item, channel, job["attempts"], minute, gw)
    job["human_minutes"] += (response.get("context") or {}).get("hold_minutes", 0)

    if response["result"] == "AMBIGUOUS" and item["action"] in SIDE_EFFECT_ACTIONS:
        state, detail, ref = interpret_verification(
            gw.submission_status(item["tenant"], item["idempotency_key"]))
    else:
        state, detail, ref = interpret(response)
    job["external_ref"] = ref or job["external_ref"]

    apply(job, state, detail, kit, events,
          result=response["result"], channel=channel, attempt=job["attempts"])
    if state == "retry_scheduled":
        job["not_before"] = minute + 2 ** (job["attempts"] - 1)


def run(jobs, kit, gw, events):
    minute, used = 0, {}
    while True:
        # Scans the whole list each pass. Fine at this size, revisit at scale.
        job = next((j for j in jobs
                    if j["state"] in WORKABLE and j["attempts"] >= MAX_ATTEMPTS), None)
        if job:
            apply(job, "needs_human_review",
                  "retry limit reached after %d attempts" % job["attempts"],
                  kit, events, actor="system")
            continue

        job = next((j for j in jobs if is_ready(j, kit, used, minute)), None)
        if job is None:
            if any(j["state"] in WORKABLE for j in jobs):
                minute += 1
                continue
            break

        work(job, kit, gw, events, used, minute)


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
             "external_ref": None, "duplicate_of": None, "human_minutes": 0,
             "not_before": 0} for item in kit["items"]]
    by_id = {j["item"]["id"]: j for j in jobs}
    events = []

    for job in jobs:
        apply(job, "queued", "loaded from batch", kit, events, actor="system")

    verdicts = reconcile_verdicts(kit["items"], date.fromisoformat(kit["case_as_of"][:10]))
    for job_id, (state, reason, duplicate_of) in verdicts.items():
        by_id[job_id]["duplicate_of"] = duplicate_of
        apply(by_id[job_id], state, reason, kit, events, actor="system")

    run(jobs, kit, gw, events)

    with open(os.path.join(args.out, "audit_log.jsonl"), "w") as f:
        f.writelines(json.dumps(e) + "\n" for e in events)
    with open(os.path.join(args.out, "run_summary.json"), "w") as f:
        json.dump(build_summary(jobs, kit), f, indent=2)

    print()
    for state, n in collections.Counter(j["state"] for j in jobs).most_common():
        print(f"{n:3}  {state}")


if __name__ == "__main__":
    main()
