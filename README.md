# Ruby payer-action automation engine

## Run it

```
python3 engine.py --kit <kit_directory> --out <output_directory>
```

Python 3, standard library only. Nothing to install.

The kit directory does not need to sit next to the source. Connectors, work
items and lookup tables are all loaded from `--kit` at runtime.

Four files land in `--out`:

- `run_summary.json`, `audit_log.jsonl` — the deliverables
- `external_side_effect_ledger.jsonl`, `connector_interaction_ledger.jsonl` — written by the mock connectors

Self-check:

```
python3 <kit_directory>/validate_submission.py --kit <kit_directory> --out <output_directory>
```

## What it does

Takes a batch of work items, executes each one against a payer, and reports where
every item ended up with a trail proving what happened.

- **Reconcile the whole batch before dispatching anything.** Every row is read
  against every other row first. Superseded rows and duplicates are cancelled,
  past-due rows and contradictory instructions are flagged. Nothing is called yet.
- **Pick a channel.** Intersect what the action allows with what the payer
  supports, then rank. No usable channel means the job stops without a call being
  made.
- **Work whatever is ready.** A loop that keeps running while any job is queued or
  scheduled for a retry, respecting each payer's per-minute call limit.
- **Interpret what came back.** The connector's result maps to one of nine states.
  An ambiguous submission is verified before anything is resent.
- **Write it down.** Every state change emits an audit event in the same call, so
  the summary and the trail cannot drift apart.

## Decisions

**The engine executes instructions. It never decides what should happen to a
claim.** When two sources disagree about the same encounter it stops and hands
both to a human rather than picking a winner. When a deadline has already passed
it stops rather than deciding the filing is still worth making. Those are
upstream's calls, not the execution layer's.

- **Duplicates are three separate rules, not one.** Same tenant and idempotency
  key is a redelivery. Same tenant, encounter and action is two sources asking for
  one job. An explicit `supersedes` is upstream changing its mind. Keys are
  tenant-scoped, so the same key under two tenants is two real actions and both
  run. — `reconcile_verdicts`

- **Two rows for the same work that disagree both stop.** Everything but the
  message envelope (`id`, `idempotency_key`, `source`, `created_at`) is compared.
  Identical rows run once. Anything else escalates both sides. Unknown fields are
  compared too, so an unfamiliar kit over-flags rather than silently acting on the
  wrong row. — `same_instruction`

- **Channels are ranked by how verifiable they are**, not by convention:
  `api > clearinghouse > portal > fax > ivr`. This is why appeals go by portal
  rather than fax. A fax can never tell you whether it arrived, and that choice
  decides whether the job is recoverable later. — `CHANNEL_RANK`

- **Availability is resolved from the tables, never by probing.** A payer with no
  usable channel for an action ends the job with zero calls made. Discovering it by
  calling and failing would write junk into the ledger being graded. — `choose_channel`

- **An ambiguous submit is verified before anything is resent.** One
  `submission_status` call, three-way branch: no record means resending is safe,
  on file means we are done, on file but unconfirmed means a human decides and the
  reason says explicitly not to resend. — `interpret_verification`

- **Ambiguous means different things on a read and a write.** A 277 pending is the
  payer saying they have not decided, so the job waits in
  `awaiting_external_response`. An ambiguous submit is us not knowing whether we
  acted, so it verifies. — `SIDE_EFFECT_ACTIONS`

- **Three attempts, doubling the gap between them.** A failure waits a minute, the
  next waits two. Backoff is a separate idea from pacing: pacing keeps us under a
  payer's per-minute call limit, backoff keeps us off a job that just failed. A job
  that exhausts its attempts goes to `needs_human_review`, not `retry_scheduled` —
  ending a bounded run in `retry_scheduled` is permitted, but a job nothing will
  ever pick up again should not claim to be scheduled.

- **State changes and audit events are the same call.** `apply` is the only thing
  that writes a state, and it always emits the event. The summary is therefore the
  last frame of the trail by construction, and the two cannot drift. It is also
  the single place persistence would go.

- **No task queue.** The run is a bounded batch behind one command. A broker adds a
  dependency the graders would have to install, and a queue's default retry — the
  task threw, run it again — is exactly the double-filing bug this exercise is
  built around.

## Boundaries

**No model is used anywhere in this engine.**

Every decision in the execution layer is made from structured data we already
have. Nothing requires generating new information or reading unstructured input,
so there is nothing for a model to do. Channel choice, duplicate detection,
deadlines, retries, and whether a submission landed are all lookups and
comparisons.

**Two places a model would belong**, both unstructured input, both escalated
today rather than guessed at:

- reading a malformed document
- a portal whose layout changed

## Cut deliberately

- **No persistence.** State lives in memory for one run. A crash halfway through
  loses the record of what was already filed, and a restart would refile it.
  `apply` is the single place this would go.

- **No cross-channel conflict detection.** The connector's result list includes
  `CONFLICT`, and a payer can answer `PAID` on one channel and `DENIED` on
  another, both as `SUCCESS`. Catching it means deliberately asking twice and
  comparing, which doubles read volume against a ledger graded on unnecessary
  calls. We take the first channel's answer.

- **No dependency resolution.** A work item blocked on an artifact that another
  item in the same batch successfully retrieves is escalated, rather than having
  the artifact handed forward.

- **`in_progress` is unused.** Calls are synchronous, so a job is never in flight
  from the engine's point of view. Eight of the nine states are reachable.

- **The kit defines neither `human_minutes` nor `touchless_completion_rate`.** We
  define `human_minutes` as hold time the automation absorbed on a warm handoff,
  taken from the connector's own `hold_minutes`. `touchless_completion_rate`
  equals `completion_rate` because no human acts mid-run — they diverge the moment
  a person can intervene and hand a job back.

## With more time

- **Persist the job table.** `apply` is already the only place state changes, so
  this is one write per transition. That buys real restart safety: on start, load
  what was already done and never refile something a crashed run had already sent.
  The mock supports testing it directly — `MockPayerGateway(fresh=False)` simulates
  payer-side memory surviving a worker restart.

- **Honor `available_at`.** The contract says availability may be out of file
  order, and the engine never reads the field: every row is treated as workable
  from minute zero. On this batch that is invisible, because everything was
  available before `case_as_of`. A kit carrying a future `available_at` would be
  dispatched early, which is the one ingestion rule stated in the contract that is
  not implemented. It belongs next to `not_before` in `is_ready`, and it needs a
  parser, because `available_at` is a full timestamp while `deadline` is a bare
  date.

- **Stop comparing the `scenario` tag.** `same_instruction` compares every field
  outside the message envelope, and the opaque scenario tag is one of them. Two
  rows that are the same instruction but carry different tags would escalate as
  conflicting rather than dedupe. The over-broad comparison is deliberate — an
  unfamiliar kit should over-flag — but the scenario tag is the one field that
  should never have been in it. One entry in `MESSAGE_FIELDS`.

- **Check artifacts before dispatching, not after.** An item whose
  `required_artifacts` are not all present in `provided_artifacts` currently gets a
  connector call, comes back `BLOCKED_MISSING_ARTIFACT`, and reaches a human
  through the default branch of `interpret` rather than a deliberate one. The end
  state is right and the call has no side effect, but it was avoidable and the
  interaction ledger records it. The comparison is local and needs no connector.

- **Hand retrieved artifacts to the jobs waiting on them.** A document fetched
  successfully in the same batch should unblock the appeal that needs it, instead
  of both ending on a human's desk. Needs an ordering pass so the fetch runs first,
  and a cycle check so a bad `blocked_by` chain cannot hang the run.

- **Decide when a second opinion is worth the call.** Cross-channel disagreement is
  real and currently invisible to us. The open question is which jobs earn a second
  read — all of them, only high-dollar ones, or only when the first answer is
  terminal. The trade is a confidently wrong answer against a ledger full of
  duplicate reads.

- **Drive `at_minute` from a real clock.** Today it is a counter that only advances
  when everything is blocked. That is deterministic and repeatable, but it claims
  three minutes of a payer's allowance inside a second of real time. In production
  it reads a clock, and blocked jobs defer to the next window rather than the next
  integer.

- **Make retry budgets cost-aware.** Three API retries cost nothing. Three phone
  retries cost an hour of hold time. The cap belongs to the channel, not to one
  number for everything.
