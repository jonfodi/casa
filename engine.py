import json, csv

jobs = [json.loads(line) for line in open("starter_kit/work_items.jsonl")]

action_channels = {row["action"]: row["valid_channels"].split("|")
                   for row in csv.DictReader(open("starter_kit/action_channel_map.csv"))}

for job in jobs:
    print(job["id"], job["payer"], job["action"], action_channels[job["action"]])
