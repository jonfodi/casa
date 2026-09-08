import json, csv

CHANNEL_COLUMN = {"api": "supports_api_276_277", "portal": "supports_portal",
                  "fax": "supports_fax", "ivr": "supports_ivr",
                  "clearinghouse": "supports_clearinghouse_837"}

CHANNEL_RANK = ["api", "clearinghouse", "portal", "fax", "ivr", "doc"]

jobs = [json.loads(line) for line in open("starter_kit/work_items.jsonl")]

action_channels = {row["action"]: row["valid_channels"].split("|")
                   for row in csv.DictReader(open("starter_kit/action_channel_map.csv"))}

payers = {row["payer_id"]: row
          for row in csv.DictReader(open("starter_kit/payer_capability_matrix.csv"))}


def choose_channel(job):
    allowed = action_channels[job["action"]]
    payer = payers[job["payer"]]
    usable = [c for c in allowed if c not in CHANNEL_COLUMN or payer[CHANNEL_COLUMN[c]] == "Y"]
    return sorted(usable, key=CHANNEL_RANK.index)


for job in jobs:
    job["state"] = "queued"

for job in jobs:
    channels = choose_channel(job)
    if not channels:
        job["state"] = "needs_human_review"
    print(job["id"].ljust(12), job["action"].ljust(28), job["state"].ljust(20),
          str(channels).ljust(30), "pick:", channels[0] if channels else None)
