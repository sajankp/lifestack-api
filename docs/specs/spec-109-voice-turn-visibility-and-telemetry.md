# Spec-109: Voice turn visibility, turn telemetry and VAD controls

**Status:** Draft — implementation started on the owner's go-ahead ("get this sorted"); review before merge
**Scope:** `lifestack-api` capture bridge (`app/capture/*`, `app/config.py`) + `lifestack-web` `VoiceAgentWidget`
**Depends on:** spec-059 (barge-in), spec-079 (capture log, model benchmark), spec-090 (session ids)

## Problem

Report: "I say something and stop, and it keeps listening and doesn't do what I asked."

Investigation (2026-11-15, production model `gemini-3.1-flash-live-preview`):

- Server-side VAD is not at fault. Streaming real-time PCM through the production setup
  message, Gemini reacted 0.5–2.9 s after speech end (clean and noisy). The ffmpeg decode pipe
  adds no accumulating lag. Explicit VAD tuning was no faster than the default.
- The widget shows a decorative "Listening" pulse for as long as the mic is on. It never reflects
  hearing / thinking / speaking, so the 3–16 s the model needs per turn (production assistant
  turns: median 6.5 s, p90 15.9 s) reads as "still listening".
- The bridge forwards neither `turnComplete` nor the user's own transcript to the client.
- Mic stop never sends `audioStreamEnd` (documented requirement after a stream pause).
- The capture log has no timing: end-of-turn complaints cannot be diagnosed after the fact.
- `_connect_gemini` always opens `[TEXT, AUDIO]` first; 3.x Live models reject it, so every
  session on the deployed model opens (and discards) a socket first.

## Goals

1. The user can see what the agent is doing: listening → hearing (live words) → thinking →
   speaking → listening.
2. Every completed turn writes a `turn_timing` row to the capture log so latency/endpointing
   problems are measurable.
3. VAD parameters are env-tunable without a code change; **default behaviour unchanged**.
4. Mic stop flushes the stream (`audioStreamEnd`).
5. Skip the doomed modality probe on 3.x models.
6. `gemini-3.8-live` can be evaluated and adopted by an env change alone (setup builder
   omits `thinkingConfig` and declares tools `BLOCKING` for it). **Production model is not
   changed by this spec** — adoption needs a trustworthy eval first (see "Eval result" below:
   the current fixture/harness mis-scores correct behaviour, so ≥ 90 % is not yet a usable gate).

## Non-goals

AudioWorklet capture, push-to-talk / client-side VAD, custom vocabulary, any change to the tool set.

## Design

### Server → client messages (new)

- `{"type": "input_transcript", "content": "<fragment>"}` — the user's own words, sent only to
  that user's socket and only when `CAPTURE_ENABLE_INPUT_TRANSCRIPTION` is on (the same gate as
  the log). Persistence rules are unchanged.
- `{"type": "turn_complete"}` — on `serverContent.turnComplete`.

### Client → server message (new)

- `{"type": "audio_stream_end"}` — sent after the last mic chunk when the mic is stopped. The
  server waits 400 ms (in-flight audio still in the ffmpeg pipe), then sends
  `realtimeInput.audioStreamEnd: true` and logs `mic_stopped`.

### Capture-log rows (new `kind`s)

| kind | fields |
|---|---|
| `turn_timing` | `input_ms` (first→last input fragment), `input_end_to_first_response_ms`, `input_end_to_first_tool_ms`, `tool_ms` (summed execution time), `tool_count`, `turn_ms` (last input→turnComplete), `interrupted`, `has_input_transcript` |
| `mic_stopped` | `session_id` |
| `go_away` | `time_left` |
| `session_ended` | `reason`, `duration_seconds` |

`session_started` additionally records `model`. Existing `tool_call` rows gain `duration_ms` (tool execution time;
`null` when nothing executed, e.g. a suppressed replay) so slow tools can be identified per tool name. No utterance text is added to any of these rows.
Timing fields that cannot be computed for a turn (e.g. typed text has no input transcript) are `null`.

### VAD settings (all default unset → no `realtimeInputConfig` sent)

`CAPTURE_VAD_SILENCE_MS`, `CAPTURE_VAD_PREFIX_PADDING_MS`, `CAPTURE_VAD_START_SENSITIVITY`
(`LOW|HIGH`), `CAPTURE_VAD_END_SENSITIVITY` (`LOW|HIGH`). Docs recommend 500–800 ms silence;
below 500 ms fragments utterances.

### Model handling

- 3.x models: modality attempts start at `["AUDIO"]` (fallbacks retained).
- `gemini-3.8-*`: no `thinkingConfig`; every function declaration gets `behavior: BLOCKING` so the
  preview-then-confirm flow (spec-095) still waits for the tool result.

### Web

`VoiceAgentWidget` keeps an `agentPhase` (`listening | hearing | thinking | speaking`) derived from
`input_transcript` (hearing; 700 ms without a new fragment → thinking), `tool_call` (thinking),
`transcript`/audio (speaking), `turn_complete`/`interrupted` (listening). The footer indicator
shows the phase label instead of the constant "Listening". Input fragments render live as a user
bubble. On mic stop the widget sends `audio_stream_end` after the final chunk.

## Test plan

API (unit, no DB): setup message with/without VAD env; 3.8 setup shape; modality order; `input_transcript`
forwarded only when flag on; `turn_complete` forwarded; `turn_timing` row values from a scripted
message sequence; `audio_stream_end` handling; `session_started`/`session_ended` rows.
Web (vitest): phase transitions; live user bubble; `audio_stream_end` sent on mic stop.
Live: `scripts/run_capture_eval.py --model models/gemini-3.8-live` (read-only).

### Eval result (2026-11-15) and its limits

Same fixture (24 scored cases), same code: `gemini-3.8-live` 18/24 (75.0 %),
`gemini-3.1-flash-live-preview` 17/24 (70.8 %). All six 3.8 failures (adv-03, 08, 12, 15,
17, 19) also fail on 3.1, so they do not discriminate between models. At least three look
like fixture/scorer issues rather than model misses: adv-08 (179 lb → 81.2 kg is correct
rounding, scorer wants "81.19"), adv-19 (model uses `transaction_type: income` for a refund,
a parameter added by spec-095 after the fixture), adv-03 (the injection is contained —
`category_name` stays "utilities" — but the payload lands in `account_name`). adv-12/15 show
two calls; the harness answers every call with a bare `{"status":"success"}` (no entity id or
summary), which may provoke a retry — unverified. **These scores must not gate a model
decision until the fixture/harness is corrected.** The only trustworthy 3.8 signal so far is
latency (≈0.3 s speech-end → first reaction vs 0.5–2.9 s on 3.1, synthetic audio, 1–3 runs).
Run the eval with `PYTHONPATH=.` from a worktree, otherwise `import app` resolves to the
main checkout.

## Deployment

Both repos auto-safe: no migration, all new settings default to current behaviour. Web deploys on
merge. API needs `deploy-production.sh`. The capture log gains rows; consumers filter on `kind`.
