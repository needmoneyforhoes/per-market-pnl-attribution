#!/usr/bin/env python3
"""
snipe_audit.py — Per-market and per-sub-strategy PnL breakdown.

Reads from the current working directory:
  ev_snipe.jsonl              slug + pnl per market
  btc_snipe_training.jsonl    slug + winner + fired_side + fill_price + delta + secs_left
  race_test_{slug}.log        per-market log file (preferred)
  race_test_*.log             fallback: scanned and partitioned by slug

Primary parse: [ledger] lines containing "snipe/"
  format: BUY/SELL SIDE | shares @ price | $usdc_delta | snipe/sub_name | reason
Fallback:  FOK BUY/SELL filled lines + nearby snipe-hedge / save_exit context

Output:
  Per-market table with columns:
    entry_cost, hedge_pnl, save_exit_pnl, save_flip_pnl, settle_pnl, net_pnl
  Sub-strategy totals across all 30 markets
  Unmatched fill errors
  Classification flags (correct, save, ghost, bail)
"""

import glob
import json
import os
import re
import sys
from collections import defaultdict

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def c(text, colour):
    return f"{colour}{text}{RESET}"

def fmt_pnl(v, width=9):
    if v is None:
        return c(f"{'?':>{width}}", DIM)
    s = f"{v:+.4f}"
    col = GREEN if v > 0.0001 else (RED if v < -0.0001 else DIM)
    return c(f"{s:>{width}}", col)

def fmt_zero(v, width=9):
    if v is None or v == 0.0:
        return c(f"{'—':>{width}}", DIM)
    return fmt_pnl(v, width)

# ── Constants ─────────────────────────────────────────────────────────────────
WORKDIR        = os.getcwd()
EV_SNIPE_FILE  = os.path.join(WORKDIR, "ev_snipe.jsonl")
TRAINING_FILE  = os.path.join(WORKDIR, "btc_snipe_training.jsonl")
LOG_PATTERN    = os.path.join(WORKDIR, "race_test_*.log")
N_MARKETS      = 30

KNOWN_SUBS = {
    "entry",
    "settle_sell", "settle_expire", "settle_redeem",
    "save_exit", "save_flip", "save_snipe",
    "hedge_t1", "hedge_t2", "hedge_t2_oh", "hedge_gtc", "hedge_exit",
}

COLUMN_GROUPS = {
    "entry_cost":    {"entry"},
    "hedge_pnl":     {"hedge_t1", "hedge_t2", "hedge_t2_oh", "hedge_gtc", "hedge_exit"},
    "save_exit_pnl": {"save_exit"},
    "save_flip_pnl": {"save_flip", "save_snipe"},
    "settle_pnl":    {"settle_sell", "settle_expire", "settle_redeem"},
}

# ── File helpers ──────────────────────────────────────────────────────────────

def load_jsonl(path):
    records = []
    if not os.path.exists(path):
        return records
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    return records


def read_log(path):
    if not os.path.exists(path):
        return None
    with open(path, errors="replace") as f:
        return f.read()


def find_log_for_slug(slug):
    """Try race_test_{slug}.log first, then scan all logs for slug's lines."""
    direct = os.path.join(WORKDIR, f"race_test_{slug}.log")
    if os.path.exists(direct):
        return direct, read_log(direct)

    for log_path in sorted(glob.glob(LOG_PATTERN)):
        text = read_log(log_path)
        if not text or slug not in text:
            continue
        lines = text.splitlines(keepends=True)
        in_slug = False
        slug_lines = []
        for line in lines:
            start = re.search(
                r"Waiting for flat entry signal.*? on ([a-z0-9\-]+)\b", line
            )
            if start:
                in_slug = (start.group(1).strip() == slug)
                if in_slug:
                    slug_lines = []
            if in_slug:
                slug_lines.append(line)
        if slug_lines:
            return log_path, "".join(slug_lines)
    return None, None


# ── Ledger parsing ────────────────────────────────────────────────────────────

# Primary format:
#   [ledger] BUY UP | 12.3456 @ 0.4800 | $-5.9260 | snipe/entry | signal fire
_LEDGER_RE = re.compile(
    r"\[ledger\]\s+"
    r"(BUY|SELL)\s+(\w+)\s*\|\s*"
    r"([\d.]+)\s*@\s*([\d.]+)\s*\|\s*"
    r"\$([+-]?[\d.]+)\s*\|\s*"
    r"([\w]+)/([\w]+)",
    re.IGNORECASE
)

# Fallback format — two variants:
#   FOK BUY filled: 12.3456 shares @ 0.4800 | $5.93
#   FOK BUY filled matched=12.3456 avg=$0.4800
_FOK_RE = re.compile(
    r"FOK\s+(BUY|SELL)\s+filled"
    r"(?:"
        r"[:\s]+(?P<shares1>[\d.]+)\s+shares?\s*@\s*(?P<price1>[\d.]+)"
        r"(?:\s*\|\s*\$(?P<usdc1>[\d.]+))?"
    r"|"
        r"[^|]*?matched=(?P<shares2>[\d.]+)[^|]*?avg=\$(?P<price2>[\d.]+)"
    r")",
    re.IGNORECASE
)

_CONTEXT_SUB_MAP = [
    (r"COLLAPSE[.\s_-]EXIT",  "save_exit"),
    (r"save[_\s-]exit",       "save_exit"),
    (r"save[_\s-]flip",       "save_flip"),
    (r"snipe[_\s-]hedge",     "hedge_t1"),
    (r"hedge[._]t2\b",        "hedge_t2"),
    (r"settle[._]sell",       "settle_sell"),
    (r"settle[._]expire",     "settle_expire"),
    (r"settle[._]redeem",     "settle_redeem"),
    (r"\bsettle\b",           "settle_sell"),   # generic settle catch-all
]


def parse_ledger_lines(log_text):
    """
    Return (fills, unmatched_list).
    fills: list of {direction, side, shares, price, usdc_delta, sub, source}
    """
    fills = []

    # Primary: [ledger] lines
    for m in _LEDGER_RE.finditer(log_text):
        strategy = m.group(6).lower()
        sub      = m.group(7).lower()
        if strategy == "snipe":
            fills.append({
                "direction":  m.group(1).upper(),
                "side":       m.group(2).upper(),
                "shares":     float(m.group(3)),
                "price":      float(m.group(4)),
                "usdc_delta": float(m.group(5)),
                "sub":        sub,
                "source":     "ledger",
            })

    if fills:
        return fills, []

    # Fallback: FOK filled lines
    lines = log_text.splitlines()
    unmatched = []
    fired_fok_entry_pending = False   # set by FIRING FOK, consumed by first BUY

    for i, line in enumerate(lines):
        # Arm the entry flag whenever the firing signal appears
        if re.search(r"FIRING\s+FOK", line, re.IGNORECASE):
            fired_fok_entry_pending = True

        m = _FOK_RE.search(line)
        if not m:
            continue

        direction = m.group(1).upper()

        # Extract shares/price from whichever regex branch matched
        shares = float(m.group("shares1") or m.group("shares2"))
        price  = float(m.group("price1")  or m.group("price2"))
        usdc_raw = float(m.group("usdc1")) if m.group("usdc1") \
                   else round(shares * price, 4)
        delta = -usdc_raw if direction == "BUY" else usdc_raw

        # Rule 1: first FOK BUY after FIRING FOK is always the entry
        if direction == "BUY" and fired_fok_entry_pending:
            sub = "entry"
            fired_fok_entry_pending = False
        else:
            # Rule 2: classify by surrounding context
            context = "\n".join(lines[max(0, i - 5): i + 6])
            sub = None
            for pattern, sub_name in _CONTEXT_SUB_MAP:
                if re.search(pattern, context, re.IGNORECASE):
                    sub = sub_name
                    break

        if sub:
            fills.append({
                "direction":  direction,
                "side":       "?",
                "shares":     shares,
                "price":      price,
                "usdc_delta": delta,
                "sub":        sub,
                "source":     "fok_fallback",
            })
        else:
            unmatched.append(
                f"  line {i+1}: {direction} {shares:.4f}@{price:.4f} | "
                f"{line.strip()[:80]}"
            )

    return fills, unmatched


def grep(text, *patterns):
    for pat in patterns:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


# ── Load source data ──────────────────────────────────────────────────────────

print(f"\n{c('snipe_audit.py', BOLD+CYAN)}  —  {WORKDIR}\n")

ev_records = load_jsonl(EV_SNIPE_FILE)
if not ev_records:
    print(c(f"ERROR: {EV_SNIPE_FILE} not found or empty.", RED))
    sys.exit(1)
print(f"  ev_snipe.jsonl        : {len(ev_records)} records")

training_records = load_jsonl(TRAINING_FILE)
print(f"  btc_snipe_training    : {len(training_records)} records")

training_by_slug = {}
for rec in training_records:
    slug = rec.get("slug", "")
    if slug:
        training_by_slug[slug] = rec

log_files = sorted(glob.glob(LOG_PATTERN))
print(f"  race_test_*.log files : {len(log_files)}\n")

# ── Process each market ───────────────────────────────────────────────────────

recent_ev = ev_records[-N_MARKETS:]
sub_totals = defaultdict(lambda: {"n": 0, "sum": 0.0})
error_markets = []
rows = []

for ev in recent_ev:
    slug      = ev.get("slug") or ev.get("market_slug") or ev.get("id", "?")
    ev_pnl    = ev.get("pnl") or ev.get("pnl_net") or ev.get("net_pnl") or 0.0

    tr         = training_by_slug.get(slug, {})
    fired_side = tr.get("fired_side") or tr.get("side")          or ev.get("fired_side")
    winner     = tr.get("winner")     or tr.get("actual_winner") or ev.get("winner")
    fill_price = tr.get("fill_price") or tr.get("price")         or ev.get("fill_price")
    delta_val  = tr.get("delta")      or tr.get("bn_delta")      or ev.get("delta")
    secs_left  = tr.get("secs_left")  or tr.get("fill_cd")       or ev.get("secs_left")

    _log_path, log_text = find_log_for_slug(slug)
    log_text = log_text or ""

    fills, unmatched_fills = parse_ledger_lines(log_text)
    bad_subs = [f for f in fills if f["sub"] not in KNOWN_SUBS]

    # Settle fills from logs are unreliable / absent for older markets.
    # We derive settle as a residual from ev_pnl instead.
    _SETTLE_SUBS = COLUMN_GROUPS["settle_pnl"]  # {"settle_sell", "settle_expire", ...}

    sub_pnl = defaultdict(float)
    sub_n   = defaultdict(int)
    for f in fills:
        if f["sub"] in _SETTLE_SUBS:
            continue   # excluded; settle is implied from ev_pnl below
        sub_pnl[f["sub"]] += f["usdc_delta"]
        sub_n[f["sub"]]   += 1
        sub_totals[f["sub"]]["n"]   += 1
        sub_totals[f["sub"]]["sum"] += f["usdc_delta"]

    # Column values for entry / hedge / save  (fills-based)
    col = {}
    for col_name, sub_set in COLUMN_GROUPS.items():
        if col_name == "settle_pnl":
            continue   # filled in below
        val = sum(sub_pnl[s] for s in sub_set if s in sub_pnl)
        col[col_name] = val if val != 0.0 else None

    # Net PnL: ev_snipe.jsonl is the ground truth, never the sum of parsed fills
    net_pnl = ev_pnl

    # Settle = residual so that entry + hedge + save + settle == ev_pnl
    # e.g. ev_pnl=+0.22, entry=-1.08, others=0  →  settle_implied = +1.30
    non_settle = sum(col.get(k) or 0.0
                     for k in ("entry_cost", "hedge_pnl",
                               "save_exit_pnl", "save_flip_pnl"))
    settle_implied = (ev_pnl - non_settle) if ev_pnl is not None else None
    col["settle_pnl"] = settle_implied if settle_implied else None

    # Add implied settle to sub_totals so the grand total reconciles with ev_snipe
    if settle_implied:
        sub_totals["settle_sell"]["n"]   += 1
        sub_totals["settle_sell"]["sum"] += settle_implied

    entry_correct = bool(fired_side and winner and
                         fired_side.upper() == winner.upper())
    entry_wrong   = bool(fired_side and winner and
                         fired_side.upper() != winner.upper())
    save_fired    = grep(log_text,
                         r"COLLAPSE EXIT", r"save_exit", r"save_flip",
                         r"collapse flip", r"save.snipe.*fired")
    ghost_fill    = grep(log_text,
                         r"GHOST FILL", r"ghost.fill",
                         r"Request exception.*not enough balance",
                         r"not enough balance.*request exception")
    settle_bail   = grep(log_text,
                         r"SETTLE_BAIL", r"settle.*bail",
                         r"never resolved", r"could not redeem after")

    rows.append({
        "slug":          slug,
        "ev_pnl":        ev_pnl,
        "net_pnl":       net_pnl,
        "col":           col,
        "sub_pnl":       dict(sub_pnl),
        "sub_n":         dict(sub_n),
        "fills":         fills,
        "unmatched":     unmatched_fills,
        "bad_subs":      bad_subs,
        "fired_side":    fired_side or "?",
        "winner":        winner or "?",
        "fill_price":    fill_price,
        "delta":         delta_val,
        "secs_left":     secs_left,
        "entry_correct": entry_correct,
        "entry_wrong":   entry_wrong,
        "save_fired":    save_fired,
        "ghost_fill":    ghost_fill,
        "settle_bail":   settle_bail,
        "log_found":     bool(log_text),
    })

    if unmatched_fills or bad_subs:
        error_markets.append((slug, unmatched_fills, bad_subs))

# ── Per-market table ──────────────────────────────────────────────────────────

SEP  = "─" * 140
SEP2 = "═" * 140
SLUG_W = 32

print(c(SEP2, DIM))
print(
    c(f"  {'SLUG':<{SLUG_W}}", BOLD) +
    c(f"  {'ENTRY':>9}", BOLD) +
    c(f"  {'HEDGE':>9}", BOLD) +
    c(f"  {'SAVE_EXIT':>9}", BOLD) +
    c(f"  {'SAVE_FLIP':>9}", BOLD) +
    c(f"  {'SETTLE':>9}", BOLD) +
    c(f"  {'NET_PNL':>9}", BOLD) +
    c(f"  {'DIR':>3}", BOLD) +
    c(f"  {'WIN':>3}", BOLD) +
    c(f"  {'OK':>3}", BOLD) +
    c(f"  {'SV':>3}", BOLD) +
    c(f"  {'GH':>3}", BOLD) +
    c(f"  {'BL':>3}", BOLD)
)
print(c(SEP, DIM))

for r in rows:
    col     = r["col"]
    ok_str  = (c("YES", GREEN) if r["entry_correct"]
               else (c("NO ", RED) if r["entry_wrong"] else c(" ? ", DIM)))
    sv_str  = c("YES", YELLOW) if r["save_fired"]  else c(" no", DIM)
    gh_str  = c("YES", RED)    if r["ghost_fill"]  else c(" no", DIM)
    bl_str  = c("YES", RED)    if r["settle_bail"] else c(" no", DIM)
    flags   = (c("!", RED) if (r["unmatched"] or r["bad_subs"]) else " ") + \
              (c("~", DIM) if not r["log_found"] else " ")

    print(
        f"{flags} {r['slug']:<{SLUG_W}}"
        f"  {fmt_zero(col.get('entry_cost'))}"
        f"  {fmt_zero(col.get('hedge_pnl'))}"
        f"  {fmt_zero(col.get('save_exit_pnl'))}"
        f"  {fmt_zero(col.get('save_flip_pnl'))}"
        f"  {fmt_zero(col.get('settle_pnl'))}"
        f"  {fmt_pnl(r['net_pnl'])}"
        f"  {r['fired_side']:>3}"
        f"  {r['winner']:>3}"
        f"  {ok_str}"
        f"  {sv_str}"
        f"  {gh_str}"
        f"  {bl_str}"
    )

print(c(SEP, DIM))

col_totals = defaultdict(float)
for r in rows:
    for cname in COLUMN_GROUPS:
        v = r["col"].get(cname)
        if v is not None:
            col_totals[cname] += v
net_total = sum(r["net_pnl"] for r in rows if r["net_pnl"] is not None)

print(
    f"   {c('TOTALS', BOLD):<{SLUG_W+2}}"
    f"  {fmt_zero(col_totals['entry_cost'])}"
    f"  {fmt_zero(col_totals['hedge_pnl'])}"
    f"  {fmt_zero(col_totals['save_exit_pnl'])}"
    f"  {fmt_zero(col_totals['save_flip_pnl'])}"
    f"  {fmt_zero(col_totals['settle_pnl'])}"
    f"  {fmt_pnl(net_total)}"
)
print(c(f"\n  Flags:  ! = unmatched fills   ~ = no log file found", DIM))

# ── Sub-strategy totals ───────────────────────────────────────────────────────

print(f"\n{c('SUB-STRATEGY TOTALS', BOLD)}  (last {len(rows)} markets)")
print(c("─" * 64, DIM))

ordered_subs = [
    "entry",
    "settle_sell", "settle_expire", "settle_redeem",
    "save_exit", "save_flip", "save_snipe",
    "hedge_t1", "hedge_t2", "hedge_t2_oh", "hedge_gtc", "hedge_exit",
]
extra_subs = sorted(s for s in sub_totals if s not in ordered_subs)

for sub in ordered_subs + extra_subs:
    if sub not in sub_totals:
        if sub in ordered_subs:
            print(f"  {sub:<22}  n={0:<4}  "
                  f"Σ={c(f'  +0.0000', DIM):>9}  "
                  f"avg={c(f'  +0.0000', DIM):>9}")
        continue
    n     = sub_totals[sub]["n"]
    total = sub_totals[sub]["sum"]
    avg   = total / n if n else 0.0
    marker = c("  ← unknown sub!", RED) if sub not in KNOWN_SUBS else ""
    print(
        f"  {sub:<22}  n={n:<4}  "
        f"Σ={fmt_pnl(total, 9)}  "
        f"avg={fmt_pnl(avg, 9)}"
        f"{marker}"
    )

# ── PnL attribution ───────────────────────────────────────────────────────────

print(f"\n{c('PnL ATTRIBUTION', BOLD)}")
print(c("─" * 64, DIM))

clean      = [r for r in rows if r["entry_correct"]
              and not r["save_fired"] and not r["ghost_fill"] and not r["settle_bail"]]
wrong_only = [r for r in rows if r["entry_wrong"]
              and not r["save_fired"] and not r["ghost_fill"] and not r["settle_bail"]]
save_mkts  = [r for r in rows if r["save_fired"]]
bug_mkts   = [r for r in rows if r["ghost_fill"] or r["settle_bail"]]

def pnl_sum(subset):
    return sum(r["net_pnl"] for r in subset if r["net_pnl"] is not None)

rows_n = len(rows)
print(f"  {c('Clean entries', GREEN):<40}  "
      f"n={len(clean):>3}/{rows_n}  {fmt_pnl(pnl_sum(clean))}")
print(f"  {c('Wrong direction (no interference)', RED):<40}  "
      f"n={len(wrong_only):>3}/{rows_n}  {fmt_pnl(pnl_sum(wrong_only))}")
print(f"  {c('Save-snipe fired', YELLOW):<40}  "
      f"n={len(save_mkts):>3}/{rows_n}  {fmt_pnl(pnl_sum(save_mkts))}")
print(f"  {c('Bug-affected (ghost fill / bail)', RED):<40}  "
      f"n={len(bug_mkts):>3}/{rows_n}  {fmt_pnl(pnl_sum(bug_mkts))}")
print(c("─" * 64, DIM))
print(f"  {'Total (all 30)':<40}  {' ' * 7}  {fmt_pnl(net_total)}")

# ── Signal quality ────────────────────────────────────────────────────────────

known_fp = [r for r in rows if r["fill_price"] is not None]
if known_fp:
    print(f"\n{c('SIGNAL QUALITY', BOLD)}")
    print(c("─" * 64, DIM))
    avg_all  = sum(r["fill_price"] for r in known_fp) / len(known_fp)
    ok_fp    = [r for r in known_fp if r["entry_correct"]]
    bad_fp   = [r for r in known_fp if r["entry_wrong"]]
    deltas   = [r["delta"] for r in rows if r["delta"] is not None]
    secs_all = [r["secs_left"] for r in rows if r["secs_left"] is not None]
    print(f"  Avg fill price  (all)     : {avg_all:.4f}  n={len(known_fp)}")
    if ok_fp:
        _avg_ok = sum(r["fill_price"] for r in ok_fp) / len(ok_fp)
        print(f"  Avg fill price  (correct) : {c(f'{_avg_ok:.4f}', GREEN)}  n={len(ok_fp)}")
    if bad_fp:
        _avg_bad = sum(r["fill_price"] for r in bad_fp) / len(bad_fp)
        print(f"  Avg fill price  (wrong)   : {c(f'{_avg_bad:.4f}', RED)}  n={len(bad_fp)}")
    if deltas:
        print(f"  Avg BN delta at entry     : "
              f"{sum(deltas)/len(deltas):+.5f}  n={len(deltas)}")
    if secs_all:
        print(f"  Avg secs left at entry    : "
              f"{sum(secs_all)/len(secs_all):.1f}s  n={len(secs_all)}")

# ── Unmatched fill errors ─────────────────────────────────────────────────────

if error_markets:
    print(f"\n{c('UNMATCHED FILL ERRORS', BOLD+RED)}")
    print(c("─" * 64, DIM))
    for slug, unmatched, bad_subs in error_markets:
        print(f"  {c('ERROR', RED+BOLD)}  {slug}")
        if unmatched:
            print(f"    {c('No sub-strategy matched for these fills:', YELLOW)}")
            for u in unmatched:
                print(f"      {u}")
        if bad_subs:
            print(f"    {c('Unknown sub-strategy name:', RED)}")
            for f in bad_subs:
                print(f"      sub={f['sub']}  {f['direction']} "
                      f"{f['shares']:.4f}@{f['price']:.4f}  Δ{f['usdc_delta']:+.4f}")
else:
    print(f"\n{c('✓ No unmatched fills.', GREEN)}")

print(f"\n{c('Done.', DIM)}\n")
