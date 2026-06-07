# per-market-pnl-attribution

Per-market and per-strategy PnL and fire/snipe attribution audits over Polymarket recap/history replays.

Part of a private Polymarket quant-trading suite, split out of a monorepo. These are the **offline analysis tools** that answer "what actually happened, and what would have happened" for the 5-minute BTC/XRP up/down bot — without touching the trading hot path or any wallet.

## Why it exists

The live bot fires hundreds of shadow/LIVE strategies and logs every fire, fill, and settlement. This repo replays those logs to attribute realized and hypothetical PnL down to the individual market, strategy, side, and price bucket — so you can decide what to promote, what to mute, and where the bot left money on the table. It is read-only and handles no funds.

## What's inside

| Script | Purpose |
| --- | --- |
| `snipe_audit.py` | Per-market and per-sub-strategy PnL breakdown of the EV-snipe path. Parses `[ledger]` lines from `race_test_*.log` plus `ev_snipe.jsonl` / `btc_snipe_training.jsonl`; columns: entry/hedge/save-exit/save-flip/settle/net, with correct/save/ghost/bail classification. |
| `preseed_audit.py` | Aggregates every fire by `strategy\|side` and flags which meet promotion criteria (≥30 fires, positive hypo PnL, WR > 45%, positive EV). |
| `shadow_audit.py` | Tier-scored standing of each shadow strategy vs promotion thresholds (μ, CI, WR), against the production `STRATEGY_SIDE_MAP`. |
| `audit_first_fires.py` | Quick post-deploy sanity check: lists recent LIVE `depth_collapse_mid` fires and confirms both UP/DN sides are wired. |
| `missed_opportunity_audit.py` | Finds markets where the *winning* side dipped into a cheap bucket but no strategy fired there — quantifies missed PnL per price bucket. |
| `verify_buckets.py` | Independently re-derives per-bucket UP/DN touch/win/PnL straight from `replay_*.json`, with replay data-quality filters. |
| `improv_compare.py` | Live-gate simulator (`sim_bn_gates_live`): replays all markets through the BN veto / held-release / BN-floor gate stack and reports equity curve, Sharpe, drawdown, walk-forward split. |
| `hold_up_decide.py` | Sweep of the hold-UP-then-decide gate (checkpoint cd, DN-trajectory feature, threshold) on realistic $1-min PnL with train/OOS split. |
| `two_stage_hold.py` | Two-stage early-hold + late-decision UP gate sweep over hold/decision checkpoints and features. |
| `deep_quant_twostage.py` | Deep statistical comparison of the two-stage hold at cd=190 vs cd=170 (CIs, Sharpe, drawdown, streaks). |

## Requirements

- **Python 3.8+**, standard library only — no third-party packages (`json`, `argparse`, `glob`, `re`, `collections`, `pathlib`).
- **No wallet, key, or network access.** These scripts are pure offline replays and never touch a ClobClient or private key.
- Input data (see below) — the audits read the bot's logs/recaps, which are not committed.

## Usage

Run any audit from a directory containing the relevant data files (most default to the cwd):

```bash
# EV-snipe per-market / per-sub-strategy attribution
python3 snipe_audit.py

# Strategy promotion screens
python3 preseed_audit.py market_recap_history.jsonl
python3 shadow_audit.py  market_recap_history.jsonl

# Missed cheap-bucket opportunities (optionally point at recap + replays)
python3 missed_opportunity_audit.py --recap market_recap_history.jsonl --replays /path/to/replays

# Per-bucket verification straight from replays
python3 verify_buckets.py /path/to/replays

# Gate-stack live simulator (BN veto / held-release / floor)
python3 improv_compare.py --data-dir /path/to/data --bn-floor 0.02

# Hold/two-stage gate sweeps
python3 hold_up_decide.py     market_recap_history.jsonl market_history.jsonl
python3 two_stage_hold.py     market_recap_history.jsonl market_history.jsonl
python3 deep_quant_twostage.py market_recap_history.jsonl market_history.jsonl
```

## Data

These scripts consume the bot's runtime logs and recaps — none are committed (see `.gitignore`). Fetch them from the private **`polymarket-data`** repo (or copy from the live VPS) into the working directory or pass via `--data-dir` / positional paths:

- `market_recap_history.jsonl`, `market_history.jsonl`, `market_history_lite.jsonl` — per-market fire records and outcomes
- `ev_snipe.jsonl`, `btc_snipe_training.jsonl` — snipe fills/training rows
- `replay_*.json` (e.g. `replay_btc-updown-5m-<slug>.json`) — per-market tick replays
- `race_test_*.log` — per-market execution logs (snipe ledger lines)

> Private research software. No warranty; trades/handles real funds at your own risk.
