#!/usr/bin/env python3
"""
Semantic submission validator (review pt 5; hardened).

JSON Schema checks the SHAPE of each record; it cannot enforce cross-record rules.
This validator schema-validates audit_log.jsonl and run_summary.json against the two
schemas, then runs the semantic checks the grader cares about:

  - run_summary covers every input work item exactly once; ids unique
  - AUDIT COVERAGE (hard): every input work item has at least one audit event,
    a stable correlation_id, and no audit event references an unknown work item
  - audit event_ids are globally unique
  - TERMINAL-STATE CONSISTENCY (hard): terminal items carry a terminal_disposition
    equal to their current_state; non-terminal items carry none
  - the run_summary current_state for each item equals the disposition of that
    item's FINAL audit event (the summary must agree with the trail)

Usage:
  python validate_submission.py --kit <kit_dir> --out <candidate_output_dir>

Expects in --out: run_summary.json, audit_log.jsonl
Exit code 0 = pass, 1 = fail.
"""
import argparse, json, os, sys, collections

STATES = {"queued","in_progress","awaiting_external_response","retry_scheduled","needs_human_review",
          "warm_handoff_ready","completed","permanently_failed","cancelled_or_superseded"}
TERMINAL = {"needs_human_review","warm_handoff_ready","completed","permanently_failed","cancelled_or_superseded"}


def _schema_validate(summary, audit, errs, warns):
    try:
        import jsonschema
    except ImportError:
        warns.append("jsonschema not installed; skipped shape validation (pip install jsonschema)")
        return
    sdir = os.path.dirname(os.path.abspath(__file__))  # schemas ship next to this script
    aud_s = json.load(open(os.path.join(sdir, "audit_event.schema.json")))
    run_s = json.load(open(os.path.join(sdir, "run_summary.schema.json")))
    try:
        jsonschema.validate(summary, run_s)
    except jsonschema.ValidationError as e:
        errs.append(f"run_summary.json fails schema: {e.message} (at {'/'.join(map(str, e.path))})")
    for i, ev in enumerate(audit):
        try:
            jsonschema.validate(ev, aud_s)
        except jsonschema.ValidationError as e:
            errs.append(f"audit_log.jsonl line {i+1} fails schema: {e.message}")
            break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    work_items = [json.loads(l) for l in open(os.path.join(a.kit, "work_items.jsonl"))]
    summary = json.load(open(os.path.join(a.out, "run_summary.json")))
    audit = [json.loads(l) for l in open(os.path.join(a.out, "audit_log.jsonl"))]

    errs, warns = [], []
    _schema_validate(summary, audit, errs, warns)

    input_ids = [w["id"] for w in work_items]
    input_set = set(input_ids)
    items = summary.get("items", [])
    reported = [it.get("work_item_id") for it in items]
    by_wid = {it.get("work_item_id"): it for it in items}

    # 1) exact coverage in run_summary: every input item reported exactly once
    rc = collections.Counter(reported)
    missing = [i for i in input_ids if i not in rc]
    extra = [i for i in reported if i not in input_set]
    dup_reported = [i for i, c in rc.items() if c > 1]
    if missing: errs.append(f"run_summary missing {len(missing)} work items: {missing[:6]}{'...' if len(missing)>6 else ''}")
    if extra: errs.append(f"run_summary reports unknown work items: {sorted(set(extra))[:6]}")
    if dup_reported: errs.append(f"run_summary reports duplicate work_item_id: {dup_reported}")

    # 2) terminal-state consistency (hardened: terminal_disposition must EQUAL current_state)
    for it in items:
        wid, st, td = it.get("work_item_id"), it.get("current_state"), it.get("terminal_disposition")
        if st not in STATES:
            errs.append(f"{wid}: invalid current_state {st!r}"); continue
        if st in TERMINAL:
            if not td:
                errs.append(f"{wid}: terminal state {st} but terminal_disposition is empty")
            elif td != st:
                errs.append(f"{wid}: terminal_disposition {td!r} != current_state {st!r}")
        else:
            if td:
                errs.append(f"{wid}: non-terminal state {st} must not carry terminal_disposition (got {td!r})")

    # 3) audit log structure
    ev_ids = [e.get("event_id") for e in audit]
    dup_ev = [i for i, c in collections.Counter(ev_ids).items() if c > 1]
    if dup_ev: errs.append(f"duplicate audit event_id(s): {dup_ev[:6]}")

    corr = collections.defaultdict(set)
    last_disp = {}                              # work_item_id -> disposition of its FINAL event (file order)
    orphan = set()
    for e in audit:
        wid = e.get("work_item_id")
        if wid not in input_set:
            orphan.add(wid); continue
        corr[wid].add(e.get("correlation_id"))
        last_disp[wid] = e.get("disposition")   # later events overwrite -> ends on the final one
    if orphan:
        errs.append(f"audit events reference unknown work items: {sorted(orphan)[:6]}")
    for wid, cset in corr.items():
        if len(cset) > 1:
            errs.append(f"{wid}: correlation_id not stable across events: {sorted(cset)}")

    # 4) AUDIT COVERAGE (hard): every input work item must have at least one audit event
    no_audit = [i for i in input_ids if i not in corr]
    if no_audit:
        errs.append(f"AUDIT COVERAGE: {len(no_audit)} work item(s) have no audit events: {no_audit[:8]}")

    # 5) summary must agree with the trail: current_state == final audit disposition
    for wid, it in by_wid.items():
        if wid in last_disp and last_disp[wid] != it.get("current_state"):
            errs.append(f"{wid}: run_summary current_state {it.get('current_state')!r} "
                        f"!= final audit disposition {last_disp[wid]!r}")

    # 6) soft: attempts in summary should match the max attempt seen in the audit trail
    max_att = collections.defaultdict(int)
    for e in audit:
        wid, at = e.get("work_item_id"), e.get("attempt")
        if wid in input_set and isinstance(at, int):
            max_att[wid] = max(max_att[wid], at)
    for wid, it in by_wid.items():
        a_sum = it.get("attempts")
        if isinstance(a_sum, int) and wid in max_att and a_sum != max_att[wid]:
            warns.append(f"{wid}: summary attempts={a_sum} but max audit attempt={max_att[wid]}")

    print(f"input work items : {len(input_ids)}")
    print(f"reported items   : {len(reported)}")
    print(f"audit events     : {len(audit)}")
    for w in warns: print("  WARN:", w)
    if errs:
        print("\nFAIL:")
        for e in errs: print("  -", e)
        sys.exit(1)
    print("\nPASS: coverage exact, ids unique, audit covers every item, "
          "states/dispositions consistent, and summary agrees with the trail.")


if __name__ == "__main__":
    main()
