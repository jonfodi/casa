#!/usr/bin/env python3
"""
Ruby Automation Case - runner STUB (starting point, not a solution).

Shows how to load the batch and call a connector under the v2 contract (you pass
`attempt`; the mock writes two ledgers). The orchestration ENGINE is what you
build: batch reconciliation (supersede/dedupe), state machine, idempotency
(tenant-scoped), retries with backoff and pacing (`at_minute`), escalation, the
warm handoff, `audit_log.jsonl` and `run_summary.json`. See CONTRACT.md.

Run: python runner_stub.py
"""
import json, os
from mock_connectors import MockPayerGateway, CASE_AS_OF

HERE = os.path.dirname(os.path.abspath(__file__))


def load_work_items():
    with open(os.path.join(HERE, "work_items.jsonl")) as f:
        return [json.loads(line) for line in f]


def main():
    gw = MockPayerGateway()        # starts fresh: truncates both ledgers
    items = load_work_items()
    print(f"loaded {len(items)} work items; case_as_of={CASE_AS_OF}")

    # ------------------------------------------------------------------
    # TODO (you): reconcile the batch first (supersede/dedupe), then build
    # the engine. The single demo call below is only to show the contract;
    # it is NOT the assignment. Do not just expand this loop.
    # ------------------------------------------------------------------
    demo = items[0]
    r = gw.claim_status(demo, channel="api", attempt=1, at_minute=0)
    print("demo:", demo["id"], "->", r["result"], "-", r["detail"])
    print("ledgers written to ./external_side_effect_ledger.jsonl and ./connector_interaction_ledger.jsonl")


if __name__ == "__main__":
    main()
