# Domain primer (read once, ~5 minutes)

You do not need to be a billing expert. This is everything the case assumes.

**The setting.** Ruby works denied, underpaid, and unadjudicated insurance claims
for outpatient infusion clinics. An upstream intelligence layer decides *what
should happen* to a claim and emits a **work item** (for example "submit a
corrected claim", "check claim status", "file an appeal", "retrieve the missing
medical records", "call the payer"). Your automation engine's job is to **execute
that work item reliably** against the messy external world and report the outcome
back.

**Channels.** Payers expose different ways to interact, and not every payer
supports every channel (see `payer_capability_matrix.csv`):

- **API / 276-277**: the electronic claim-status transaction. Fast when available;
  can throttle, time out, or return an ambiguous "pending".
- **Portal**: a web portal a human (or automation) logs into. Sessions expire;
  layouts change without notice.
- **Fax**: still used for appeals and records. Confirmations are unreliable;
  transmissions can be partial.
- **IVR / phone**: an automated phone tree, then a hold, then a human
  representative. Used when payer judgement is needed.
- **Clearinghouse**: the intermediary for submitting claims electronically (treat
  as another submit channel).
- **Document retrieval**: fetching an artifact (medical records, EOB) needed
  before an action can proceed.

**Action types in this case.** `claim_status_inquiry`, `corrected_claim_submission`,
`appeal_submission`, `document_retrieval`, `payer_followup_call`.

**Key terms.**
- **Idempotency key**: a stable id on a work item so the same action is not
  performed twice even if delivered or retried twice.
- **Hard deadline**: a payer filing or appeal deadline. Missing it can forfeit the
  money. Distinct from an internal follow-up SLA.
- **Warm handoff**: automation does the slow setup (authenticate, wait on hold,
  gather context, reach a representative), then transfers the live interaction to a
  human biller who picks up *with all context already in front of them* and does
  not restart the call.
- **277 pending / ambiguous**: the payer has not given a final answer. Do not mark
  the work complete.
- **Supersede**: the upstream layer can change its mind; a newer work item can
  replace an older one for the same encounter.

**What "done" means.** A work item has a `current_state` (one of nine, see
CONTRACT.md) and, when it ends, a `terminal_disposition` (one of `completed`,
`needs_human_review`, `warm_handoff_ready`, `permanently_failed`,
`cancelled_or_superseded`). A bounded run may legitimately end with an item still
`awaiting_external_response` or `retry_scheduled`. Every step must be auditable and
resumable.

**Read `CONTRACT.md` next.** It is the precise connector and output contract: the
fixed clock (`case_as_of`), the batch-reconcile rule, the action-to-channel map,
the connector signatures (you pass `attempt`), the two ledgers, and the required
output schemas. It also asks you not to hardcode against specific work items,
because we run your engine against a second hidden set.
