#!/usr/bin/env python3
"""Payer-action automation engine."""

import argparse, json, csv, sys, os, collections

CHANNEL_COLUMN = {"api": "supports_api_276_277", "portal": "supports_portal",
                  "fax": "supports_fax", "ivr": "supports_ivr",
                  "clearinghouse": "supports_clearinghouse_837"}

CHANNEL_RANK = ["api", "clearinghouse", "portal", "fax", "ivr", "doc"]

TERMINAL = {"completed", "needs_human_review", "warm_handoff_ready",
            "permanently_failed", "cancelled_or_superseded"}

RESULT_STATE = {"SUCCESS": "completed",
                "PERMANENT_FAILURE": "permanently_failed",
                "NEEDS_HUMAN": "needs_human_review",
                "WARM_HANDOFF": "warm_handoff_ready"}

MESSAGE_FIELDS = {"id", "idempotency_key", "source", "created_at"}


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

def reconcile(jobs):
    """Mark rows that must not be acted on. Covers the cases seen in this
    batch; there may be others."""
    superseded = {j["item"]["supersedes"] for j in jobs if j["item"].get("supersedes")}
    by_key, by_work, winners = {}, {}, {}

    for job in jobs:
        item = job["item"]
        key = (item["tenant"], item["idempotency_key"])
        work = (item["tenant"], item["encounter"], item["action"])

        if item["id"] in superseded:
            cancel(job, "replaced by a later work item")
        elif key in by_key:
            cancel(job, "same instruction already queued as " + by_key[key], by_key[key])
        elif work in by_work:
            other = by_work[work]
            if same_instruction(item, winners[other]["item"]):
                cancel(job, "same encounter and action already queued as " + other, other)
            else:
                escalate(job, "conflicts with " + other + " on the same encounter and action")
                escalate(winners[other], "conflicts with " + item["id"] + " on the same encounter and action")
        else:
            by_key[key] = by_work[work] = item["id"]
            winners[item["id"]] = job


MESSAGE_FIELDS = {"id", "idempotency_key", "source", "created_at"}


def same_instruction(a, b):
    """Two rows are the same instruction if everything but the message
    envelope matches. Unknown fields are compared, so we over-flag rather
    than silently act on the wrong one."""
    strip = lambda d: {k: v for k, v in d.items() if k not in MESSAGE_FIELDS}
    return strip(a) == strip(b)


def cancel(job, reason, duplicate_of=None):
    job["state"] = "cancelled_or_superseded"
    job["reason"] = reason
    job["duplicate_of"] = duplicate_of


def escalate(job, reason):
    job["state"] = "needs_human_review"
    job["reason"] = reason

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

    reconcile(jobs)

    for job in jobs:
        if job["state"] != "queued":
            continue
        item = job["item"]
        channels = choose_channel(item, kit)
        if not channels:
            job["state"] = "needs_human_review"
            continue
        channel = channels[0]
        action = item["action"]
        job["attempts"] += 1
        # retrieve_document and ivr_call take no channel; there is only ever one.
        if action == "claim_status_inquiry":
            response = gw.claim_status(item, channel=channel, attempt=1, at_minute=0)
        elif action in ("corrected_claim_submission", "appeal_submission"):
            response = gw.submit(item, channel, item["idempotency_key"], action, attempt=1, at_minute=0)
        elif action == "document_retrieval":
            response = gw.retrieve_document(item, attempt=1)
        else:
            response = gw.ivr_call(item, attempt=1)
        job["state"] = RESULT_STATE.get(response["result"], job["state"])
        job["reason"] = response.get("detail", "")
        job["external_ref"] = response.get("external_ref")
        print(item["id"].ljust(12), response["result"].ljust(20), "->", job["state"])

    print()
    for state, n in collections.Counter(j["state"] for j in jobs).most_common():
        print(f"{n:3}  {state}")

    write_run_summary(jobs, kit, args.out)


if __name__ == "__main__":
    main()
