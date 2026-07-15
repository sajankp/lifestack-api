"""Build/refresh `app/investing/reference_data/securities.json.gz` (spec-083 §7.3).

Offline/build-time only — never run in the request path. Merges five sources
into the single hand-editable master JSON that ships with the app:

- India mutual funds: the AMFI scheme master CSV (`--mf-csv`, defaults to the
  workspace-root `seed_data/mf_mapping.csv` dev staging file — not committed
  to this repo; re-derivable from AMFI's published scheme master).
- India equities: NSE's published equity list
  (https://archives.nseindia.com/content/equities/EQUITY_L.csv), fetched live
  unless `--nse-csv` points at a local snapshot.
- India ETFs: NSE's published ETF list
  (https://archives.nseindia.com/content/equities/eq_etfseclist.csv), fetched
  live unless `--nse-etf-csv` points at a local snapshot.
- US stocks + ETFs: Nasdaq Trader's full symbol directories — NASDAQ-listed
  (https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt) and
  NYSE/AMEX/other-listed
  (https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt) — fetched
  live unless `--nasdaq-listed-txt`/`--other-listed-txt` point at local
  snapshots. Each file's own `ETF` flag column classifies stock vs. ETF; test
  issues are excluded. This supersedes an S&P-500-only list (a strict
  subset) with the full US-listed universe.
- A small hand-curated set of London-listed UCITS ETFs (no free bulk source
  with ISINs the way NSE/AMFI/Nasdaq Trader data has one).

Merges rather than clobbers: an existing `securities.json.gz`'s hand-edited
entries (any `source` not starting with `bundled:amfi`/`bundled:nse`/
`bundled:nasdaq`/`bundled:otherlisted`) are preserved as-is; entries for the
same key are refreshed from the source data.

Usage:
    python seed_data/scripts/build_securities_json.py
    python seed_data/scripts/build_securities_json.py --mf-csv /path/to/mf_mapping.csv --offline
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# Gzipped, not plain .json: the full AMFI+NSE+S&P500 dataset (~19k entries)
# is ~4.3MB as compact JSON, over the repo's 1000KB pre-commit large-file
# limit; gzip brings it to ~280KB with zero data loss (highly repetitive
# tabular data compresses well). Hand-edits go through this build script
# (`--output` can point anywhere for a decompress/edit/recompress cycle);
# it round-trips an existing gzipped file same as it would a plain one.
DEFAULT_OUTPUT = REPO_ROOT / "app" / "investing" / "reference_data" / "securities.json.gz"
DEFAULT_MF_CSV = REPO_ROOT.parent / "seed_data" / "mf_mapping.csv"

NSE_EQUITY_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_ETF_URL = "https://archives.nseindia.com/content/equities/eq_etfseclist.csv"
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

# NYSE/AMEX/Arca single-letter exchange codes used by otherlisted.txt's
# "Exchange" column, mapped to a MIC-standard label (spec-083 §6.1). Left as
# None (unknown/not worth guessing) for anything not in this table — ticker
# alone still satisfies the US mandate (§6: exchange is optional there).
_OTHER_LISTED_EXCHANGE_MAP = {
    "N": "XNYS",  # NYSE
    "A": "XASE",  # NYSE American
    "P": "ARCX",  # NYSE Arca
}

# Small hand-curated London-listed UCITS ETF set (spec-083 §7.1/§7.3): no
# free bulk source with ISINs the way NSE/AMFI/Nasdaq Trader data has one.
# Extend by hand-editing the generated JSON, or via the API fallback + cache
# path (spec-083 §7.2).
CURATED_LONDON_ETFS = [
    {
        "ticker": "HIEU.L",
        "isin": "IE00B5BD5K76",
        "name": "HSBC MSCI Europe UCITS ETF",
    },
    {
        "ticker": "VWRL.L",
        "isin": "IE00BK5BQT80",
        "name": "Vanguard FTSE All-World UCITS ETF",
    },
    {
        "ticker": "CSPX.L",
        "isin": "IE00B5BMR087",
        "name": "iShares Core S&P 500 UCITS ETF USD (Acc)",
    },
]

# NSE-listed REITs/InvITs (spec-083 §7.1/§7.3): these trade on a separate
# NSE segment not covered by EQUITY_L.csv/eq_etfseclist.csv, and no public
# bulk-download URL for that segment was found — hand-curated (ticker/name
# only; isin omitted rather than guessed) until one turns up. Ticker+exchange
# already satisfies the India-stock mandate (§6) without an isin.
CURATED_INDIA_REITS = [
    {"ticker": "EMBASSY", "name": "Embassy Office Parks REIT"},
    {"ticker": "MINDSPACE", "name": "Mindspace Business Parks REIT"},
    {"ticker": "BIRET", "name": "Brookfield India Real Estate Trust"},
]


def _read_maybe_gzipped(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8")


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed, documented URLs
        return resp.read().decode("utf-8", errors="replace")


def _extract_isin(raw: str) -> str | None:
    """AMFI's ISIN column sometimes concatenates two 12-char ISINs with no
    separator (growth + dividend-reinvestment share classes). Take the first
    well-formed 12-char ISIN found.
    """
    raw = (raw or "").strip()
    if len(raw) >= 12 and _ISIN_RE.match(raw[:12]):
        return raw[:12]
    return None


def build_mutual_fund_entries(mf_csv_path: Path) -> list[dict]:
    if not mf_csv_path.exists():
        print(
            f"warning: mutual-fund CSV not found at {mf_csv_path}, skipping India MF entries",
            file=sys.stderr,
        )
        return []

    entries: list[dict] = []
    seen_codes: set[str] = set()
    with mf_csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        isin_col = next(
            (c for c in (reader.fieldnames or []) if c and c.strip().startswith("ISIN")), None
        )
        for row in reader:
            code = (row.get("Code") or "").strip()
            name = (row.get("Scheme NAV Name") or row.get("Scheme Name") or "").strip()
            if not code or not name or code in seen_codes:
                continue
            seen_codes.add(code)
            isin = _extract_isin(row.get(isin_col, "")) if isin_col else None
            entries.append({
                "isin": isin,
                "ticker": None,
                "exchange": None,
                "amfi_code": code,
                "security_type": "mutual_fund",
                "name": name,
                "aliases": [],
                "country_code": "IN",
                "source": "bundled:amfi",
            })
    return entries


def build_nse_equity_entries(nse_csv_text: str) -> list[dict]:
    entries: list[dict] = []
    reader = csv.DictReader(nse_csv_text.splitlines())
    for row in reader:
        symbol = (row.get("SYMBOL") or "").strip()
        name = (row.get("NAME OF COMPANY") or "").strip()
        isin = (row.get(" ISIN NUMBER") or row.get("ISIN NUMBER") or "").strip()
        if not symbol or not name:
            continue
        entries.append({
            "isin": isin if _ISIN_RE.match(isin) else None,
            "ticker": symbol,
            "exchange": "XNSE",
            "amfi_code": None,
            "security_type": "stock",
            "name": name,
            "aliases": [],
            "country_code": "IN",
            "source": "bundled:nse",
        })
    return entries


def build_nse_etf_entries(nse_etf_csv_text: str) -> list[dict]:
    """`eq_etfseclist.csv`'s "Underlying" column is the tracked benchmark
    index (e.g. "Nifty MNC ETF Total Return Index"), not the fund's own
    name — "SecurityName" (e.g. "MOTILALAMC - MOMNC") is the actual security,
    so it must take priority. A cleaner name for the same ISIN often exists
    in the AMFI mutual-fund data (Indian ETFs are also registered as MF
    schemes) and wins during the isin-collision reconciliation pass.
    """
    entries: list[dict] = []
    reader = csv.DictReader(nse_etf_csv_text.splitlines())
    for row in reader:
        symbol = (row.get("Symbol") or "").strip()
        name = (row.get("SecurityName") or row.get("Underlying") or "").strip()
        isin = (row.get("ISINNumber") or "").strip()
        if not symbol or not name:
            continue
        entries.append({
            "isin": isin if _ISIN_RE.match(isin) else None,
            "ticker": symbol,
            "exchange": "XNSE",
            "amfi_code": None,
            "security_type": "etf",
            "name": name,
            "aliases": [],
            "country_code": "IN",
            "source": "bundled:nse",
        })
    return entries


def build_nasdaq_listed_entries(text: str) -> list[dict]:
    """`nasdaqlisted.txt`: NASDAQ-listed stocks + ETFs (own `ETF` flag
    column). Pipe-delimited; last line is a "File Creation Time" footer, not
    a data row — `csv.DictReader` naturally yields a row with mismatched
    field count there, so it's dropped on the `symbol` sanity check below.
    """
    entries: list[dict] = []
    reader = csv.DictReader(text.splitlines(), delimiter="|")
    for row in reader:
        symbol = (row.get("Symbol") or "").strip()
        name = (row.get("Security Name") or "").strip()
        if not symbol or not name or (row.get("Test Issue") or "").strip() == "Y":
            continue
        entries.append({
            "isin": None,
            "ticker": symbol,
            "exchange": "XNAS",
            "amfi_code": None,
            "security_type": "etf" if (row.get("ETF") or "").strip() == "Y" else "stock",
            "name": name,
            "aliases": [],
            "country_code": "US",
            "source": "bundled:nasdaq",
        })
    return entries


def build_other_listed_entries(text: str) -> list[dict]:
    """`otherlisted.txt`: NYSE/AMEX/Arca-listed stocks + ETFs (own `ETF`
    flag column). Same pipe-delimited/footer-row shape as nasdaqlisted.txt.
    """
    entries: list[dict] = []
    reader = csv.DictReader(text.splitlines(), delimiter="|")
    for row in reader:
        symbol = (row.get("ACT Symbol") or "").strip()
        name = (row.get("Security Name") or "").strip()
        if not symbol or not name or (row.get("Test Issue") or "").strip() == "Y":
            continue
        entries.append({
            "isin": None,
            "ticker": symbol,
            "exchange": _OTHER_LISTED_EXCHANGE_MAP.get((row.get("Exchange") or "").strip()),
            "amfi_code": None,
            "security_type": "etf" if (row.get("ETF") or "").strip() == "Y" else "stock",
            "name": name,
            "aliases": [],
            "country_code": "US",
            "source": "bundled:otherlisted",
        })
    return entries


def build_curated_etf_entries() -> list[dict]:
    entries: list[dict] = []
    for etf in CURATED_LONDON_ETFS:
        entries.append({
            "isin": etf.get("isin"),
            "ticker": etf["ticker"],
            "exchange": "XLON",
            "amfi_code": None,
            "security_type": "etf",
            "name": etf["name"],
            "aliases": [],
            "country_code": "GB",
            "source": "bundled:manual",
        })
    for reit in CURATED_INDIA_REITS:
        entries.append({
            "isin": reit.get("isin"),
            "ticker": reit["ticker"],
            "exchange": "XNSE",
            "amfi_code": None,
            "security_type": "stock",
            "name": reit["name"],
            "aliases": [],
            "country_code": "IN",
            "source": "bundled:manual",
        })
    return entries


def _reconcile_isin_collisions(entries: list[dict]) -> list[dict]:
    """Resolve every isin shared by more than one entry — two different
    things can cause this, and they need different handling:

    1. **Genuine dual registration**: an Indian ETF is simultaneously an
       AMFI-registered MF scheme (amfi_code, no ticker) and an NSE-listed
       instrument (ticker, no amfi_code) — same real security, same isin, by
       design. These merge into ONE entry: the ticker-bearing (tradeable)
       side survives with the amfi_code folded in, preferring the AMFI name
       when the ticker-side name is a raw broker code. The redundant
       amfi-only duplicate is dropped from the output.
    2. **True source-data collision**: two entries in the *same* identity
       namespace (two different amfi_codes, or two different
       ticker+exchange pairs) recorded against the same isin — a handful of
       distinct AMFI fixed-maturity-plan series do this. `isin` is
       partial-uniquely indexed in `reference_securities`; letting these
       collide there would silently merge two distinct securities on
       upsert (the identity-fragmentation bug spec-083 exists to fix, in
       reverse). amfi_code/ticker+exchange are the reliable keys for these
       rows — validation degrades to "no isin", not "wrong isin".
    """
    by_isin: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("isin"):
            by_isin.setdefault(entry["isin"], []).append(entry)

    dropped_ids: set[int] = set()
    merged_count = 0
    collided_count = 0

    for isin, group in by_isin.items():
        if len(group) < 2:
            continue

        amfi_entries = [e for e in group if e.get("amfi_code")]
        ticker_entries = [e for e in group if e.get("ticker")]
        distinct_amfi = {e["amfi_code"] for e in amfi_entries}
        distinct_ticker_exchange = {(e["ticker"], e.get("exchange")) for e in ticker_entries}

        if len(distinct_amfi) > 1 or len(distinct_ticker_exchange) > 1:
            for entry in group:
                entry["isin"] = None
            collided_count += 1
            print(
                f"warning: dropping isin {isin} shared by {len(group)} distinct securities "
                f"(source data collision): {[e['name'] for e in group]}",
                file=sys.stderr,
            )
            continue

        if (
            amfi_entries
            and ticker_entries
            and len(amfi_entries) + len(ticker_entries) == len(group)
        ):
            primary = ticker_entries[0]
            primary["amfi_code"] = amfi_entries[0]["amfi_code"]
            amfi_name = amfi_entries[0]["name"]
            if amfi_name and (
                " - " not in primary["name"] or len(amfi_name) > len(primary["name"])
            ):
                primary["name"] = amfi_name
            dropped_ids.update(id(e) for e in amfi_entries)
            merged_count += 1

    if collided_count:
        print(f"Dropped isin from {collided_count} true source-data collision group(s).")
    if merged_count:
        print(f"Merged {merged_count} dual AMFI/ticker registration(s) sharing one isin.")

    return [e for e in entries if id(e) not in dropped_ids]


def _entry_key(entry: dict) -> tuple:
    return (
        entry.get("isin"),
        entry.get("ticker"),
        entry.get("exchange"),
        entry.get("amfi_code"),
    )


def _drop_duplicate_amfi_and_ticker(entries: list[dict]) -> list[dict]:
    """Defensive final pass: `amfi_code` and `(ticker, exchange)` are also
    partial-uniquely indexed in the DB. Nothing in the current sources
    collides here (checked at build time), but a future source/merge could
    introduce one — keep the first occurrence, drop and warn on the rest
    rather than let the loader's upsert silently overwrite one security's
    identity with another's.
    """
    seen_amfi: set[str] = set()
    seen_ticker_exchange: set[tuple] = set()
    kept: list[dict] = []
    for entry in entries:
        amfi = entry.get("amfi_code")
        if amfi and amfi in seen_amfi:
            print(
                f"warning: dropping duplicate amfi_code {amfi} ({entry['name']})", file=sys.stderr
            )
            continue
        ticker_key = (entry.get("ticker"), entry.get("exchange"))
        if entry.get("ticker") and ticker_key in seen_ticker_exchange:
            print(
                f"warning: dropping duplicate ticker+exchange {ticker_key} ({entry['name']})",
                file=sys.stderr,
            )
            continue
        if amfi:
            seen_amfi.add(amfi)
        if entry.get("ticker"):
            seen_ticker_exchange.add(ticker_key)
        kept.append(entry)
    return kept


def merge_entries(existing: list[dict], generated: list[dict]) -> list[dict]:
    generated_sources = {"bundled:amfi", "bundled:nse", "bundled:nasdaq", "bundled:otherlisted"}
    hand_edited = [e for e in existing if e.get("source") not in generated_sources]
    hand_edited_keys = {_entry_key(e) for e in hand_edited}
    merged = list(hand_edited)
    for entry in generated:
        if _entry_key(entry) not in hand_edited_keys:
            merged.append(entry)
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mf-csv", type=Path, default=DEFAULT_MF_CSV)
    parser.add_argument("--nse-csv", type=Path, default=None, help="Local NSE equity CSV snapshot")
    parser.add_argument("--nse-etf-csv", type=Path, default=None, help="Local NSE ETF CSV snapshot")
    parser.add_argument(
        "--nasdaq-listed-txt", type=Path, default=None, help="Local nasdaqlisted.txt snapshot"
    )
    parser.add_argument(
        "--other-listed-txt", type=Path, default=None, help="Local otherlisted.txt snapshot"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip network fetches; only rebuild from local CSV snapshots + curated ETF list.",
    )
    args = parser.parse_args()

    entries: list[dict] = []
    entries.extend(build_mutual_fund_entries(args.mf_csv))

    if args.nse_csv:
        nse_text = args.nse_csv.read_text(encoding="utf-8", errors="replace")
        entries.extend(build_nse_equity_entries(nse_text))
    elif not args.offline:
        print(f"Fetching NSE equity list from {NSE_EQUITY_URL} ...")
        entries.extend(build_nse_equity_entries(_fetch_text(NSE_EQUITY_URL)))

    if args.nse_etf_csv:
        nse_etf_text = args.nse_etf_csv.read_text(encoding="utf-8", errors="replace")
        entries.extend(build_nse_etf_entries(nse_etf_text))
    elif not args.offline:
        print(f"Fetching NSE ETF list from {NSE_ETF_URL} ...")
        entries.extend(build_nse_etf_entries(_fetch_text(NSE_ETF_URL)))

    if args.nasdaq_listed_txt:
        nasdaq_text = args.nasdaq_listed_txt.read_text(encoding="utf-8", errors="replace")
        entries.extend(build_nasdaq_listed_entries(nasdaq_text))
    elif not args.offline:
        print(f"Fetching NASDAQ-listed symbols from {NASDAQ_LISTED_URL} ...")
        entries.extend(build_nasdaq_listed_entries(_fetch_text(NASDAQ_LISTED_URL)))

    if args.other_listed_txt:
        other_text = args.other_listed_txt.read_text(encoding="utf-8", errors="replace")
        entries.extend(build_other_listed_entries(other_text))
    elif not args.offline:
        print(f"Fetching NYSE/other-listed symbols from {OTHER_LISTED_URL} ...")
        entries.extend(build_other_listed_entries(_fetch_text(OTHER_LISTED_URL)))

    entries.extend(build_curated_etf_entries())
    entries = _reconcile_isin_collisions(entries)

    existing: list[dict] = []
    if args.output.exists():
        existing = json.loads(_read_maybe_gzipped(args.output)).get("securities", [])

    merged = merge_entries(existing, entries)
    merged = _drop_duplicate_amfi_and_ticker(merged)
    merged.sort(
        key=lambda e: (e["security_type"], e.get("country_code") or "", e.get("ticker") or "")
    )

    payload = {"version": date.today().isoformat(), "securities": merged}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, indent=2) + "\n").encode()
    if args.output.suffix == ".gz":
        args.output.write_bytes(gzip.compress(body, compresslevel=9))
    else:
        args.output.write_bytes(body)
    print(
        f"Wrote {len(merged)} securities to {args.output} "
        f"(generated at {datetime.now(UTC).isoformat()})"
    )


if __name__ == "__main__":
    main()
