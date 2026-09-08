import json, csv, sys

sys.path.insert(0, "starter_kit")
from mock_connectors import MockPayerGateway

CHANNEL_COLUMN = {"api": "supports_api_276_277", "portal": "supports_portal",
                  "fax": "supports_fax", "ivr": "supports_ivr",
                  "clearinghouse": "supports_clearinghouse_837"}

CHANNEL_RANK = ["api", "clearinghouse", "portal", "fax", "ivr", "doc"]

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


jobs = [{"item": item, "state": "queued"} for item in items]

for job in jobs:
    channels = choose_channel(job["item"])
    if not channels:
        job["state"] = "needs_human_review"
    print(job["item"]["id"].ljust(12), job["state"].ljust(20),
          str(channels).ljust(30), "pick:", channels[0] if channels else None)
