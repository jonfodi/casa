# Connector and output contract (read before you build)


## The clock
All deadline decisions must use a single fixed clock, not the wall clock:


    case_as_of = 2026-06-19T09:00:00Z


So a work item whose `deadline` is before that instant is already past due.


## Ingestion is a batch, not a stream
`work_items.jsonl` is one ingestion batch. Load and reconcile the whole batch
before you dispatch any work. In particular, a later work item may `supersede`
an earlier one, and `available_at` may be out of file order. A purely sequential
engine that acts on line 1 before reading line 2 will mishandle supersession.


## Channels
A work item does not name a channel. You choose one that is valid for the action
(`action_channel_map.csv`) and supported by the payer (`payer_capability_matrix.csv`).


| Action | Valid channels |
| --- | --- |
| claim_status_inquiry | api (276/277), portal, ivr |
| corrected_claim_submission | clearinghouse (837), portal |
| appeal_submission | fax, portal |
| document_retrieval | doc |
| payer_followup_call | ivr |


## Connector contract
Every connector returns a dict with a `result` in: `SUCCESS, AMBIGUOUS, RETRYABLE,
NEEDS_HUMAN, PERMANENT_FAILURE, WARM_HANDOFF, BLOCKED_MISSING_ARTIFACT, CONFLICT,
CHANNEL_UNAVAILABLE, INVALID_CHANNEL_FOR_ACTION`, plus `status, detail,
external_ref, received_by_payer, disposition, channel`, and `context` (warm
handoff) or `artifact` (document retrieval).


Signatures (Python reference; reimplement equivalently in your language):


    claim_status(work_item, channel, attempt=1, at_minute=0)
    submit(work_item, channel, idempotency_key, action, attempt=1, at_minute=0)
    submission_status(tenant, idempotency_key)    # is a prior submit on file?
    retrieve_document(work_item, attempt=1)        # SUCCESS returns `artifact`
    ivr_call(work_item, attempt=1)                 # may return WARM_HANDOFF


Your engine owns attempt state and passes `attempt`. The mock keeps no per-item
attempt counter, so a worker restart is safe. Idempotency is **tenant-scoped**:
the same key under two tenants is two different actions. The API status channel
is **rate limited**: calls are counted per `(payer, at_minute)` against the
matrix limit, so pace your calls and pass `at_minute`, or you will eat 429s.


On an **ambiguous submit** (for example a timeout after the payer received the
claim) the immediate response will not tell you whether it landed. Call
`submission_status(tenant, idempotency_key)` to find out before resubmitting, or
rely on the idempotency key. Never blindly resubmit an ambiguous action.


## Two ledgers (the mock writes these; the grader reads them)
- `external_side_effect_ledger.jsonl` records real side effects (claims, appeals,
  faxes submitted). There must be no duplicate side effect.
- `connector_interaction_ledger.jsonl` records every connector call (reads and
  `submission_status` checks included), so unnecessary duplicate calls are visible.


## Submission entrypoint
Your engine must be runnable from the command line using the following interface:


    python engine.py --kit <kit_directory> --out <output_directory>


- `--kit` points to a directory containing the provided work-item data, payer
  configuration, scenarios, connector implementation, and schemas.
- `--out` is the directory where your engine must write `audit_log.jsonl` and
  `run_summary.json`.
- Your engine must not assume that the kit directory is located beside your
  source code.
- We will run the same engine against a second kit that follows the same
  contract but contains different work-item ids, scenario labels, payers, and
  timestamps.
- You may use a different executable filename or language, but your README must
  provide one equivalent command accepting explicit kit and output directories.


## What you must output
- `audit_log.jsonl` matching `audit_event.schema.json` (see `audit_event.example.json`).
- `run_summary.json` matching `run_summary.schema.json` (see `run_summary.example.json`).
  Report each work item's `current_state` and, when terminal, its
  `terminal_disposition`. A bounded run may legitimately end with items still
  `awaiting_external_response` or `retry_scheduled`; that is fine.


Optional self-check: `python validate_submission.py --kit . --out <your_output_dir>`
confirms exact work-item coverage, unique ids, and schema/semantic consistency.


## States
Workflow `current_state` is one of: `queued, in_progress, awaiting_external_response,
retry_scheduled, needs_human_review, warm_handoff_ready, completed,
permanently_failed, cancelled_or_superseded`. The terminal dispositions are the
last five.


## Please do not hardcode
Handle these conditions generally. Do not branch on specific `work_item` ids or
on the opaque `scenario` tag. We review for that, and we run your engine against a
second, hidden set of work items.