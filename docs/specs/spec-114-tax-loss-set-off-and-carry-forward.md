# Spec-114: Tax estimate — set off losses across the four buckets (highest tax first) and carry forward prior-year losses

**Status:** Draft for owner approval (2026-11-28). The owner has confirmed the ordering rule: **losses go against
the highest-taxed gain first.**
**Scope:** `lifestack-api` only (engine, schema, migration, REST, MCP). The web part is in spec-113 phase 5.
**Depends on:** spec-099 (capital-gains tax estimate). This spec implements spec-099's phase 4 and the
set-off rule from spec-099 §1, which the engine never applied. Where spec-099 §3.4 gives a different
ordering, this spec replaces it.
**Branch:** `fix/spec-114-tax-loss-set-off` (one PR, three commits T1/T2/T3, each with its own RED test).

---

## 1. Problem

The engine sorts each realised lot into one of four buckets: Indian long-term (112A), Indian short-term
(111A), foreign long-term and foreign short-term. It then taxes **each bucket on its own**
(`app/investing/tax/engine.py` ~L366-395). A bucket with a net loss pays ₹0 (`capital_gains_tax` returns 0
for `gain <= 0`, `app/investing/tax/slab.py` ~L242), and that loss never reduces another bucket's gain.

Indian law (spec-099 §1, "Loss set-off" row):
- A short-term capital loss (STCL) can be set off against **any** capital gain.
- A long-term capital loss (LTCL) can be set off against **long-term** gains only.
- Unabsorbed losses carry forward for 8 assessment years, provided the return was filed on time.

Effect today: whenever one bucket is a loss, the estimate is **too high**.

The owner's 28 Nov personal report hints at this: summed short-term tax ₹43,132 on a net short-term gain
of ₹1,03,928, i.e. 41.5%. That's above any single short-term rate (Indian 20%, foreign at slab, each plus
surcharge and cess), and the figure is only possible if a short-term loss in one bucket isn't reducing the
other. This is evidence, not proof; step 0 below confirms it.

Carry-forward (spec-099 phase 4) was never built. The profile has `apply_carry_forward`
(`app/finance/models.py` ~L371), but nothing reads it.

## 2. Rules (what "correct" means)

All amounts in INR after the existing Rule 115 conversion and fee netting. A bucket's `gain_inr` is already
the net of the gains and losses inside that bucket; that stays as it is.

### 2.1 Tranches

Gains are split into tranches, each with a **marginal rate**:

| Tranche | Rate used for ordering |
|---|---|
| Foreign short-term gain | `slab.rate_pct` (the resolved marginal slab) |
| Indian short-term gain | 20% (`INDIAN_REGIME.stcg_flat_rate_pct`) |
| Foreign long-term gain | 12.5% |
| Indian long-term gain **above** the ₹1.25L exemption | 12.5% |
| Indian long-term gain **within** the ₹1.25L exemption | 0% |

Surcharge and cess multiply every tranche by the same factor (surcharge capped at 15%, cess 4%), so
ordering by base rate is enough.

### 2.2 Current-year set-off (T1), the order confirmed by the owner

1. **Long-term losses first**, because they can only go against long-term gains. Apply the total LTCL to
   long-term tranches from highest rate to lowest. Ties: foreign long-term before Indian-above-exemption.
   Indian-within-exemption comes last.
2. **Then short-term losses**: apply the total STCL to **all** remaining tranches from highest rate to
   lowest, using the actual rates from 2.1. For example, if the slab is below 20%, Indian short-term comes
   before foreign short-term. Ties: foreign before Indian.
3. Set-off is mandatory while gains remain, so losses do reach the 0% tranche (eating into the exemption)
   before anything is left over.
4. Whatever is left is the year's **unabsorbed loss**, split short-term / long-term and reported. It is
   never negative tax.
5. **Exemption**: the ₹1.25L 112A exemption applies to the Indian long-term gain **after** set-off. The
   tranche model in 2.1 already encodes this; the reported `exemption_applied` must equal
   `min(125000, indian_lt_after_set_off)`.
6. **Slab**: the total income that sets the slab, surcharge and 87A rebate uses
   `max(0, sum of all buckets after set-off)`. A capital loss never reduces other income. The total doesn't
   depend on the order, so there's no circularity with the foreign short-term rate.

### 2.3 Brought-forward losses (T2)

- **Source of truth is the owner's filed return** (ITR Schedule CFL): carry-forward is only allowed if
  that return was filed on time, which the app can't know.
- The owner (or the `personal` agent) records, **for a given FY**, the losses available to bring into it:
  `(applies_to_fy, arising_fy, term, amount_inr)`.
- Applied **after** current-year set-off, only if `profile.apply_carry_forward` is true:
  - Brought-forward short-term losses go against any remaining tranche, highest rate first.
  - Brought-forward long-term losses go against remaining long-term tranches only.
  - Oldest `arising_fy` first.
  - A row whose `arising_fy` is more than 8 FYs before `applies_to_fy` is ignored, and the summary lists it
    under `expired_carry_forward`.
- The summary reports `carry_forward_used` and `carry_forward_remaining` per row. It **does not** write
  anything back; next year's rows come from next year's ITR.

### 2.4 What does not change
- FIFO replay and the reconciliation guard. If reconciliation fails, still no number.
- Regime classification, Rule 115 FX, the `missing_fx_rate` status.
- Lot-level `gain_inr` values. Set-off is a bucket/tranche-level step and doesn't rewrite lots.

## 3. Engine design

**Refactor into two steps (moves only, then behaviour):**
- `compute_tax_estimate(...)` keeps its signature and result. Internally it does:
  1. replay + bucket (unchanged), then
  2. a new pure function
     `tax_from_buckets(buckets, profile, carry_forward_rows=()) -> TaxComputation`.

  The function lives in a new module `app/investing/tax/set_off.py`. It is pure: no I/O and no DB.
- `TaxComputation` holds, per bucket:
  - `gain_inr` (before set-off)
  - `set_off_inr` (current-year losses applied to this bucket)
  - `carry_forward_set_off_inr`
  - `taxable_inr`
  - `exemption_applied`
  - `tax`

  It also holds `unabsorbed_loss` {short_term, long_term}, `carry_forward_used[]`,
  `carry_forward_remaining[]`, `expired_carry_forward[]` and an ordered `set_off_steps[]` audit trail.
  Each step is `{loss_from, applied_to, amount_inr, rate_pct}`; the Tax tab shows them as plain sentences.
- `capital_gains_tax` is unchanged; it's called on `taxable_inr`.
- **Assumption lines** (`_build_assumptions`):
  - replace "No prior-year losses applied." with the actual set-off summary (e.g. "Short-term loss of
    ₹46,210 (foreign) set off against Indian short-term gains first (20%), then …")
  - add "Brought-forward losses: none recorded" or the applied rows

## 4. API changes

### 4.1 `GET /investing/tax/summary` (`TaxSummaryResponse`, `app/investing/tax_schemas.py`)

Additive only, so existing clients keep working.
- `TaxBucketResponse` gets `set_off_inr`, `carry_forward_set_off_inr` and `taxable_inr` (strings, like the
  other money fields).
- `TaxSummaryResponse` gets:
  - `unabsorbed_loss: {short_term: str, long_term: str}`
  - `set_off_steps: list[{loss_from, applied_to, amount_inr, rate_pct}]`
  - `carry_forward: {used: [...], remaining: [...], expired: [...]}`
- `total_gain_inr` keeps its meaning (net of all buckets). `total_tax_inr` is the corrected figure.
- MCP `get_tax_summary` (`app/mcp/tools/tax.py` L37) returns the same new fields. Update the MCP golden
  catalog (`app/tests/mcp_surface/golden_catalog.json`) if any tool signature changes.

### 4.2 Carry-forward storage (T2)
- Alembic `0077_tax_loss_carry_forward.py`: table `tax_loss_carry_forward`

  | Column | Type |
  |---|---|
  | `id`, `public_id` (uuid) | |
  | `workspace_id` | FK |
  | `applies_to_fy` | str, e.g. "2026-27" |
  | `arising_fy` | str |
  | `term` | enum `short_term` \| `long_term` |
  | `amount_inr` | Numeric(18,2), > 0 |
  | `note` | str nullable, e.g. "ITR AY 2026-27 Schedule CFL" |
  | `created_at`, `updated_at` | |

  Unique `(workspace_id, applies_to_fy, arising_fy, term)`. Check: `arising_fy < applies_to_fy`.
- Layering: `TaxLossCarryForwardRepository` → `TaxService` methods → router.
- REST: `GET/POST /investing/tax/carry-forward?fy=`, `PATCH/DELETE /investing/tax/carry-forward/{public_id}`.
- MCP: `list_tax_loss_carry_forward(workspace_id, fy)` and `set_tax_loss_carry_forward(workspace_id,
  applies_to_fy, arising_fy, term, amount_inr, note=None, dry_run=False)`. The set tool is an upsert on the
  unique key and is idempotent. Follow `references/mcp-write-tool-hardening.md`: dry_run parity, commit
  inside the session, and a test that fails without the commit.

### 4.3 What-if tools that exist only for agents get REST (T3)

These are thin routers over the existing `TaxWhatIfService` (`app/investing/tax_whatif_service.py`):
- `GET  /investing/tax/lots?holding_id=` → `list_lots`
- `POST /investing/tax/simulate-sale` body `{holding_id, quantity, sale_date?, price?}` → `simulate_sale`
- `GET  /investing/tax/harvest-candidates?limit=` → `harvest`

**Harvest valuation changes to the marginal engine result.**
- Today `simulation.harvest_candidates` (~L690) values a loss as `loss × flat rate`. That's wrong once
  set-off exists: a short-term loss with no gains to absorb saves ₹0 this year.
- New value: `tax_saved_inr = tax_from_buckets(current) − tax_from_buckets(current + this lot's loss)`.
  The realised buckets are computed once, then the pure function runs per candidate. **No replay per
  candidate**: the prod VM has 1 GB.
- A candidate that saves ₹0 this year but adds to the carry-forward gets `saves_now_inr = 0` and
  `adds_to_carry_forward_inr = loss`, and ranks after candidates that save now.
- The slab-dependent foreign short-term case no longer needs `stcg_rate_pct`: the engine knows the slab. The
  parameter stays as an override.

## 5. Implementation plan (TDD; each task is RED → GREEN → commit)

**Step 0: evidence (non-blocking, about 10 min).**
- Get the four buckets for FY 2026-27 for workspace 1. The coding profile has no LifeStack MCP, so create a
  kanban card for `personal`: "call `get_tax_summary(workspace_id=1, fy='2026-27')` and comment the four
  buckets' `gain_inr` and `tax` (read-only)".
- Record the result in the PR description. The fix doesn't wait for it.

Setup:
```bash
cd /home/sajan/lifestack/lifestack-api && git fetch origin
git worktree add /home/sajan/lifestack-wt/spec-114 -b fix/spec-114-tax-loss-set-off origin/main
cd /home/sajan/lifestack-wt/spec-114
export PY=/home/sajan/lifestack/lifestack-api/.venv/bin/python   # the worktree has no venv of its own
export PRECOMMIT=/home/sajan/lifestack/lifestack-api/.venv/bin/pre-commit
```

**T1: current-year set-off**
1. New test file `app/tests/investing/test_tax_set_off.py`, pure unit tests on `tax_from_buckets`:
   - `test_stcl_foreign_offsets_indian_stcg_when_slab_below_20` (slab 10%: Indian ST 20% is higher, so it
     absorbs first)
   - `test_stcl_goes_to_foreign_stcg_first_when_slab_30`
   - `test_ltcl_only_against_long_term` (an LTCL with only ST gains → all unabsorbed, ST tax unchanged)
   - `test_ltcl_applied_before_stcl` (both kinds of loss; LTCL uses the LT gains, STCL goes to ST)
   - `test_losses_reach_exempt_tranche_last` (Indian LT ₹1.5L, foreign LT ₹0, STCL ₹40k → taxable
     Indian LT = 1.5L − 40k − 1.25L = 0; tax 0; exemption_applied = 1.10L)
   - `test_unabsorbed_reported_never_negative_tax`
   - `test_slab_income_floors_at_zero` (net capital loss doesn't reduce other income)
   - `test_no_losses_matches_old_engine` (regression: no negative bucket → identical tax to today)
2. Run: `$PY -m pytest app/tests/investing/test_tax_set_off.py -p no:cacheprovider --no-cov -q`
   → fails (module missing).
3. Implement `set_off.py` and wire it into `compute_tax_estimate`.
4. Run the tax suites: `test_tax_engine.py test_tax_simulation.py test_tax_simulation_incremental.py
   test_tax_whatif_service.py integration/test_tax.py integration/test_tax_whatif_tools.py`.
   Existing expectations that relied on the bug must be changed **only where a loss bucket exists**, and
   each changed expectation gets a comment citing this spec.
5. Schema and MCP additions (§4.1) with a router test asserting the new fields.
6. Commit: `fix(tax): set off losses across buckets, highest tax first (spec-114 T1)`.

**T2: carry-forward**
1. Migration and model tests (upgrade/downgrade); repository and service tests; router tests for CRUD; MCP
   tests (idempotent upsert, dry_run, commit guard).
2. Engine tests:
   - `test_bf_stcl_after_current_year`
   - `test_bf_ltcl_long_term_only`
   - `test_bf_oldest_first`
   - `test_bf_expired_after_8_fys_listed_not_used`
   - `test_apply_carry_forward_false_ignores_rows`
3. Commit: `feat(tax): brought-forward capital losses (spec-114 T2)`.

**T3: REST for the what-if tools + marginal harvest valuation**
1. Router tests for the three endpoints (auth, workspace scoping, validation errors mirror the MCP errors).
2. Harvest test: with no realised gains, a short-term loss candidate has `saves_now_inr == 0` and
   `adds_to_carry_forward_inr == loss`. With a foreign short-term gain, the saving equals the engine diff.
3. Commit: `feat(tax): REST for lots, simulate-sale and harvest; marginal harvest value (spec-114 T3)`.

**Finish**
- `git add -A && $PRECOMMIT run --all-files` (run twice if it rewrites files).
- Full suite with coverage: `$PY -m pytest app/tests -q --cov=app --cov-fail-under=80`.
  It takes longer than 10 minutes, so run it in the background.
- Push and open a PR **only when the owner asks**. Merge with `--merge`; squash is disabled.

## 6. Acceptance
- With any loss bucket present, `total_tax_inr` is lower than or equal to today's figure, and equals the
  hand calculation in the T1 test fixtures.
- With no loss bucket, results are byte-identical to today (regression test).
- The summary shows the set-off steps and the unabsorbed loss. The MCP and REST outputs match.
- Carry-forward rows are applied only when `apply_carry_forward` is true, and expired rows are listed, not
  used.
- Harvest candidates are valued by the engine diff, not by a flat rate.
- Coverage ≥ 80%, pre-commit green, reconciliation guard tests untouched and green.

## 7. Not in scope
- Web UI (spec-113 phase 5: "This year so far" and "Ways to pay less").
- Detecting ELSS lock-ins (spec-113 phase 5).
- Indexation, pre-FY2025-26 regimes, Schedule FA (still spec-099 non-goals).
- Automatically deriving carry-forward from earlier FYs' estimates. The ITR is the source of truth.

## 8. Risks
- **The ordering is the owner's choice, confirmed 2026-11-28, and should be confirmed with a CA** before
  filing. The assumption line says which order was used.
- **Prod memory**: harvest must not replay orders per candidate. There's a test asserting
  `replay_fifo_lots` is called once per request (patch and count).
