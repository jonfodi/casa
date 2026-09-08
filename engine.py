import json, csv, sys, collections

sys.path.insert(0, "starter_kit")
from mock_connectors import MockPayerGateway

CHANNEL_COLUMN = {"api": "supports_api_276_277", "portal": "supports_portal",
                  "fax": "supports_fax", "ivr": "supports_ivr",
                  "clearinghouse": "supports_clearinghouse_837"}

CHANNEL_RANK = ["api", "clearinghouse", "portal", "fax", "ivr", "doc"]

RESULT_STATE = {"SUCCESS": "completed",
                "PERMANENT_FAILURE": "permanently_failed",
                "NEEDS_HUMAN": "needs_human_review",
                "WARM_HANDOFF": "warm_handoff_ready"}

items = [json.loads(line) for line in open("starter_kit/work_items.jsonl")]

action_channels = {row["action"]: row["valid_channels"].split("|")
                   for row in csv.DictReader(open("starter_kit/action_channel_map.csv"))}

payers = {row["payer_id"]: row
          for row in csv.DictReader(open("starter_kit/payer_capability_matrix.csv"))}

gw = MockPayerGateway(starter_kit_dir="starter_kit")


def choose_channel(item):
    allowed = action_channels[item["action"]]
    payer = payers[item["payer"]]
    usable = [c for c in allowed if c not in CHANNEL_COLUMN or payer[CHANNEL_COLUMN[c]] == "Y"]
    return sorted(usable, key=CHANNEL_RANK.index)


def reconcile(jobs):
    """Mark rows that must not be acted on. Covers the cases seen in this
    batch; there may be others."""
    superseded = {j["item"]["supersedes"] for j in jobs if j["item"].get("supersedes")}
    by_key, by_work = {}, {}

    for job in jobs:
        item = job["item"]
        key = (item["tenant"], item["idempotency_key"])
        work = (item["tenant"], item["encounter"], item["action"])

        if item["id"] in superseded:
            cancel(job, "replaced by a later work item")
        elif key in by_key:
            cancel(job, "same instruction already queued as " + by_key[key])
        elif work in by_work:
            cancel(job, "same encounter and action already queued as " + by_work[work])
        else:
            by_key[key] = by_work[work] = item["id"]


def cancel(job, reason):
    job["state"] = "cancelled_or_superseded"
    job["reason"] = reason


jobs = [{"item": item, "state": "queued", "reason": ""} for item in items]

reconcile(jobs)

for job in jobs:
    if job["state"] != "queued":
        continue
    item = job["item"]
    channels = choose_channel(item)
    if not channels:
        job["state"] = "needs_human_review"
        continue
    channel = channels[0]
    action = item["action"]
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
    print(item["id"].ljust(12), response["result"].ljust(20), "->", job["state"])

print()
for state, n in collections.Counter(j["state"] for j in jobs).most_common():
    print(f"{n:3}  {state}")
