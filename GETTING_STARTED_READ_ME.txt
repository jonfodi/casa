RUBY AUTOMATION ENGINEER CASE — START HERE


Welcome, and thanks for taking this on.


1. Read the brief first:
     Ruby-Automation-Case-CANDIDATE-BRIEF.docx
   It explains what to build, what to hand back, the ground rules, and how we
   evaluate. It also includes a 4-hour core variant if you are time-constrained.


2. Then read the precise contract and a short primer:
     starter_kit/CONTRACT.md      (read before you build)
     starter_kit/domain_primer.md (about 5 minutes; no billing expertise needed)


3. See the mocked payer world in action, then replace the stub with your engine:
     cd starter_kit
     python runner_stub.py


Your final submission should expose a command such as:


     python engine.py --kit . --out ./output


The evaluator will replace `.` with another conforming kit directory when
running the hidden evaluation set. Your implementation must not assume the kit
is colocated with your source code.


Requirements: Python 3, standard library only. The optional self-check
(starter_kit/validate_submission.py) uses the "jsonschema" package if you have it
installed; it degrades gracefully if you do not.


What to hand back: your engine, the audit_log.jsonl and run_summary.json it
produces, and a short README. Full details are in the brief.