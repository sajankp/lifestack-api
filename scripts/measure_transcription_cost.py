"""spec-079 Q4: measure whether enabling Gemini Live audio transcription adds
billable tokens — the owner-run metered test that gates real-usage transcript
capture and the fuller capture-turn log.

The gate (spec-079 resolved question 4): user/assistant transcript capture is
in scope ONLY if the existing Gemini Live session already emits transcription
"at no additional API call / token cost". This script answers that empirically
instead of guessing from docs.

What it does
------------
For an identical input it opens two Live sessions and compares the token usage
the API self-reports (`usageMetadata`):

  * baseline           — the app's real setup (no transcription config)
  * with_transcription — same setup + `inputAudioTranscription` and
                         `outputAudioTranscription` enabled

It sums every `usageMetadata.totalTokenCount` seen in each session across
`--trials` repetitions, and prints a side-by-side comparison. If the
transcription variant does not raise the token totals, enabling it is free and
spec-079 Q4 is satisfied.

Input
-----
  * Text (default): exercises OUTPUT transcription only — the model answers with
    audio, and output transcription turns that audio into text. Input
    transcription is NOT exercised (there is no user audio).
  * `--audio <file>` (any format ffmpeg can decode): also exercises INPUT
    transcription. Record a short clip (e.g. "add milk to my todo list"); it is
    converted to the 16 kHz mono s16le PCM the app sends.

Safety: read-only w.r.t. the app DB. Tool calls the model emits are answered
with a synthetic success so the turn can complete — `execute_agent_tool` is
never called, nothing is written. It DOES make live, billable Gemini calls
(that is the point — we are measuring their cost).

Usage
-----
    uv run python scripts/measure_transcription_cost.py
    uv run python scripts/measure_transcription_cost.py --text "what's my portfolio worth?"
    uv run python scripts/measure_transcription_cost.py --audio /tmp/add-milk.m4a --trials 5
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass, field

import websockets

from app.capture.gemini_setup import _build_setup_message
from app.config import settings

_TURN_TIMEOUT_SECONDS = 25.0
_PCM_CHUNK_BYTES = 2048  # ~64 ms of 16 kHz 16-bit mono PCM (matches the app)


@dataclass
class TrialResult:
    total_tokens: int = 0
    prompt_tokens: int = 0
    response_tokens: int = 0
    thinking_tokens: int = 0
    first_response_ms: float | None = None  # input sent → first content byte back
    turn_ms: float | None = None  # input sent → turnComplete
    input_transcripts: list[str] = field(default_factory=list)
    output_transcripts: list[str] = field(default_factory=list)
    modality_breakdown: dict[str, int] = field(default_factory=dict)
    modalities: list[str] = field(default_factory=list)
    saw_usage: bool = False
    error: str | None = None


def _decode_to_pcm16k(path: str) -> bytes:
    """ffmpeg-decode any audio file to the 16 kHz mono s16le PCM the app streams."""
    proc = subprocess.run(
        ["ffmpeg", "-i", path, "-f", "s16le", "-acodec", "pcm_s16le",
         "-ar", "16000", "-ac", "1", "pipe:1"],
        capture_output=True, check=True,
    )  # fmt: skip
    return proc.stdout


# Same negotiation order the app uses in `_connect_gemini`: native-audio models
# reject ["TEXT","AUDIO"] with a 1007 close, so we fall through to what they take.
_MODALITY_ATTEMPTS = [["TEXT", "AUDIO"], ["AUDIO"], ["TEXT"]]


def _setup_message(*, with_transcription: bool, modalities: list[str]) -> dict:
    """The app's real setup payload, optionally with transcription enabled.

    `inputAudioTranscription` / `outputAudioTranscription` are top-level keys
    under `setup` (siblings of `generationConfig`); an empty object enables each.
    """
    msg = _build_setup_message(
        response_modalities=modalities,
        user_timezone="UTC",
        workspace_context="",
    )
    if with_transcription:
        msg["setup"]["inputAudioTranscription"] = {}
        msg["setup"]["outputAudioTranscription"] = {}
    return msg


async def _connect_with_setup(url: str, *, with_transcription: bool):
    """Open a Live session, negotiating a modality set the model accepts.

    Returns (ws, modalities). Raises RuntimeError if every option is rejected.
    Caller owns closing the returned ws.
    """
    last_err: Exception | None = None
    for modalities in _MODALITY_ATTEMPTS:
        ws = await websockets.connect(url, max_size=None)
        try:
            await ws.send(
                json.dumps(
                    _setup_message(with_transcription=with_transcription, modalities=modalities)
                )
            )
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
            if first.get("setupComplete") is not None:
                return ws, modalities
            last_err = RuntimeError(f"unexpected first message: {first}")
        except Exception as exc:  # noqa: BLE001 - try the next modality option
            last_err = exc
        await ws.close()
    raise RuntimeError(f"no accepted modality set: {last_err!r}")


def _accumulate_usage(result: TrialResult, usage: dict) -> None:
    result.saw_usage = True
    # usageMetadata is cumulative-per-message on the Live API; keep the max so a
    # single trial isn't double-counted across the turn's messages.
    result.total_tokens = max(result.total_tokens, usage.get("totalTokenCount", 0) or 0)
    result.prompt_tokens = max(result.prompt_tokens, usage.get("promptTokenCount", 0) or 0)
    result.response_tokens = max(result.response_tokens, usage.get("responseTokenCount", 0) or 0)
    # Thinking tokens (thinkingBudget) are billed and add turn latency; the Live
    # API reports them as thoughtsTokenCount when a budget is set.
    result.thinking_tokens = max(result.thinking_tokens, usage.get("thoughtsTokenCount", 0) or 0)
    for key in ("promptTokensDetails", "responseTokensDetails", "tokensDetails"):
        for detail in usage.get(key, []) or []:
            modality = detail.get("modality", "UNSPECIFIED")
            count = detail.get("tokenCount", 0) or 0
            result.modality_breakdown[modality] = max(
                result.modality_breakdown.get(modality, 0), count
            )


async def _run_trial(*, with_transcription: bool, text: str, pcm: bytes | None) -> TrialResult:
    result = TrialResult()
    url = f"{settings.GEMINI_LIVE_URL}?key={settings.GEMINI_API_KEY}"
    ws, modalities = await _connect_with_setup(url, with_transcription=with_transcription)
    result.modalities = modalities
    try:
        if pcm is not None:
            for i in range(0, len(pcm), _PCM_CHUNK_BYTES):
                chunk = pcm[i : i + _PCM_CHUNK_BYTES]
                await ws.send(
                    json.dumps({
                        "realtimeInput": {
                            "mediaChunks": [
                                {
                                    "mimeType": "audio/pcm;rate=16000",
                                    "data": base64.b64encode(chunk).decode("utf-8"),
                                }
                            ]
                        }
                    })
                )
            await ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
        else:
            await ws.send(json.dumps({"realtimeInput": {"text": text}}))

        sent_at = time.monotonic()

        async def _drive() -> None:
            async for raw in ws:
                msg = json.loads(raw)

                if (usage := msg.get("usageMetadata")) is not None:
                    _accumulate_usage(result, usage)

                sc = msg.get("serverContent")
                if sc:
                    model_turn = sc.get("modelTurn")
                    if model_turn and result.first_response_ms is None:
                        result.first_response_ms = (time.monotonic() - sent_at) * 1000
                    if (it := sc.get("inputTranscription")) and it.get("text"):
                        result.input_transcripts.append(it["text"])
                    if (ot := sc.get("outputTranscription")) and ot.get("text"):
                        result.output_transcripts.append(ot["text"])
                    if sc.get("turnComplete"):
                        result.turn_ms = (time.monotonic() - sent_at) * 1000
                        return

                tc = msg.get("toolCall")
                if tc:
                    responses = [
                        {
                            "id": fc.get("id"),
                            "name": fc.get("name"),
                            "response": {"output": {"status": "success"}},
                        }
                        for fc in tc.get("functionCalls") or []
                    ]
                    if responses:
                        await ws.send(
                            json.dumps({"toolResponse": {"functionResponses": responses}})
                        )

        try:
            await asyncio.wait_for(_drive(), timeout=_TURN_TIMEOUT_SECONDS)
        except TimeoutError:
            result.error = "turn_timeout"
    finally:
        with suppress(Exception):
            await ws.close()
    return result


async def _run_variant(
    label: str, *, with_transcription: bool, text: str, pcm: bytes | None, trials: int
) -> list[TrialResult]:
    print(f"\n=== {label} ({trials} trials) ===")
    results: list[TrialResult] = []
    for n in range(1, trials + 1):
        try:
            r = await _run_trial(with_transcription=with_transcription, text=text, pcm=pcm)
        except Exception as exc:  # noqa: BLE001 - a metering harness must not crash mid-run
            r = TrialResult(error=repr(exc))
        results.append(r)
        note = f" [{r.error}]" if r.error else ""
        first = f"{r.first_response_ms:.0f}" if r.first_response_ms is not None else "-"
        turn = f"{r.turn_ms:.0f}" if r.turn_ms is not None else "-"
        print(
            f"  trial {n}: total={r.total_tokens} "
            f"(prompt={r.prompt_tokens}, response={r.response_tokens}, think={r.thinking_tokens}) "
            f"first_resp={first}ms turn={turn}ms "
            f"in_tx={len(r.input_transcripts)} out_tx={len(r.output_transcripts)}{note}"
        )
    return results


def _avg(results: list[TrialResult], attr: str) -> float:
    vals = [getattr(r, attr) for r in results if r.saw_usage]
    return sum(vals) / len(vals) if vals else 0.0


def _avg_opt(results: list[TrialResult], attr: str) -> float | None:
    vals = [v for r in results if (v := getattr(r, attr)) is not None]
    return sum(vals) / len(vals) if vals else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default="What is my portfolio worth right now?")
    parser.add_argument("--audio", default=None, help="Audio file to exercise INPUT transcription")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument(
        "--model",
        default=None,
        help="Override settings.GEMINI_MODEL to try a new model without editing .env",
    )
    parser.add_argument(
        "--thinking-budget",
        type=int,
        default=None,
        help="Override GEMINI_THINKING_BUDGET to A/B latency vs tool-arg quality (try 0)",
    )
    args = parser.parse_args()

    if not settings.GEMINI_API_KEY:
        raise SystemExit("GEMINI_API_KEY is not set — cannot run a live metering test.")
    if args.model:
        settings.GEMINI_MODEL = args.model
    if args.thinking_budget is not None:
        settings.GEMINI_THINKING_BUDGET = args.thinking_budget
    print(f"Model under test: {settings.GEMINI_MODEL}")
    print(f"Thinking budget: {settings.GEMINI_THINKING_BUDGET}")

    pcm = _decode_to_pcm16k(args.audio) if args.audio else None
    if pcm is not None:
        print(f"Loaded {len(pcm)} bytes of 16 kHz PCM from {args.audio}")
    else:
        print("No --audio: exercising OUTPUT transcription only (text input).")

    baseline = asyncio.run(
        _run_variant(
            "baseline (no transcription)",
            with_transcription=False,
            text=args.text,
            pcm=pcm,
            trials=args.trials,
        )
    )
    enabled = asyncio.run(
        _run_variant(
            "with_transcription",
            with_transcription=True,
            text=args.text,
            pcm=pcm,
            trials=args.trials,
        )
    )

    base_total = _avg(baseline, "total_tokens")
    enab_total = _avg(enabled, "total_tokens")
    delta = enab_total - base_total
    # The model's spoken reply length varies per call, so total tokens jitter
    # even between two baseline runs. Measure that spread and treat a delta
    # inside it as noise, not a real transcription surcharge.
    base_vals = [r.total_tokens for r in baseline if r.saw_usage]
    raw_spread = (max(base_vals) - min(base_vals)) if len(base_vals) > 1 else 0
    # Floor the jitter: with few trials the baseline can happen to be identical
    # (spread 0), which would flag any reply-length delta as a false "surcharge".
    # ~1% of the total is well below any real transcription cost.
    base_spread = max(raw_spread, round(0.01 * base_total), 1)

    print("\n================ RESULT ================")
    print(f"avg total tokens  baseline={base_total:.1f}  with_transcription={enab_total:.1f}")
    print(f"delta (with - baseline) = {delta:+.1f} tokens/turn")

    # Latency + thinking-token cost (both variants share the same thinking
    # budget, so average across all trials to characterise the model/budget).
    all_trials = baseline + enabled
    first_ms = _avg_opt(all_trials, "first_response_ms")
    turn_ms = _avg_opt(all_trials, "turn_ms")
    think = _avg(all_trials, "thinking_tokens")
    print(
        f"latency  first_response={first_ms:.0f}ms  turn_complete={turn_ms:.0f}ms"
        if first_ms is not None and turn_ms is not None
        else "latency  (no timed turns)"
    )
    print(f"thinking tokens/turn (budget={settings.GEMINI_THINKING_BUDGET}): {think:.1f}")
    any_in = any(r.input_transcripts for r in enabled)
    any_out = any(r.output_transcripts for r in enabled)
    print(f"input transcription received:  {any_in}")
    print(f"output transcription received: {any_out}")
    if enabled and any(r.modality_breakdown for r in enabled):
        print("modality breakdown (with_transcription, per-trial max seen):")
        merged: dict[str, int] = {}
        for r in enabled:
            for k, v in r.modality_breakdown.items():
                merged[k] = max(merged.get(k, 0), v)
        for k, v in sorted(merged.items()):
            print(f"  {k}: {v}")

    example = next((t for r in enabled for t in r.output_transcripts), None)
    if example:
        print(f'example output transcript: "{example[:120]}"')
    example_in = next((t for r in enabled for t in r.input_transcripts), None)
    if example_in:
        print(f'example input transcript:  "{example_in[:120]}"')

    print(f"\nbaseline token spread across trials: {base_spread} (reply-length jitter)")
    print("Verdict:")
    if not any_in and not any_out:
        print("  Transcription config produced NO transcripts — check API/model support.")
    elif abs(delta) <= max(base_spread, 1.0):
        print(
            f"  delta ({delta:+.1f}) is within baseline jitter ({base_spread}) → transcription "
            "adds no measurable token cost → spec-079 Q4 satisfied (for the exercised direction)."
        )
    else:
        print(
            f"  delta ({delta:+.1f}) EXCEEDS baseline jitter ({base_spread}) → a real surcharge; "
            "re-run with more --trials to confirm, then owner decides if acceptable."
        )
    if not any_in:
        print(
            "  NOTE: input (user-speech) transcription was not exercised — pass --audio to test it."
        )


if __name__ == "__main__":
    main()
