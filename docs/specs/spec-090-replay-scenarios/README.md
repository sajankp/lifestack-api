# Spec-090 replay scenarios (scrubbed golden fixtures)

Reconstructions of the resume-replay incidents found in the 2026-07-22
production capture-log analysis (spec-090 addendum). **All amounts,
descriptions, dates, and session ids are synthetic** — only the structural
shape of each incident is preserved (session boundaries, relative timing,
burst-at-t=0 signature, argument drift). Entries use the exact JSONL schema
of the production capture log (`CAPTURE_TURN_LOG_PATH`), so the dedup guard's
tests can replay these files verbatim against the guard once spec-090 is
approved and implemented.

| File | Recreates | Guard must |
|---|---|---|
| `scenario-01-exact-replay-burst.jsonl` | 5-item batch re-executed at +47 s by a zero-duration, tool-calls-only resumed session (the `0851bd87` → `c8f9b049` incident; that batch was executed 3× in production) | suppress all 5 |
| `scenario-02-drifted-replay.jsonl` | Replay at +37 min with argument drift — description reworded, `account_name` added, category recategorized (the `25bfb31f` → `b1a3ad51` incident) | suppress via fuzzy key (tool, amount, occurred_at) |
| `scenario-03-legit-identical-repeat.jsonl` | Two genuinely identical items in one utterance, same session, after user input (the two same-price transit fares the assistant confirmed) | execute BOTH — never suppress |
| `scenario-04-post-user-input-repeat.jsonl` | Resumed session where the user actively re-requests an identical item after speaking | execute — suppression only applies before the first user input on a connection |

Scenario 3 and 4 are the overcorrection guards: they encode the cases where
identical calls are intentional. See spec-090 "Part B revisions" for the rule
each file exercises.
