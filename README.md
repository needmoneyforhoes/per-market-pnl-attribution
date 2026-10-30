# per-market-pnl-attribution

Offline PnL and fire/snipe attribution audits over Polymarket recap and replay logs for the 5-minute BTC/XRP up/down bot.

Replays the live bot's fire, fill, and settlement logs to attribute realized and hypothetical PnL per market, strategy, side, and price bucket. Read-only; no wallet, key, or network access.

## Scripts

- `snipe_audit.py`: per-market and per-sub-strategy PnL breakdown of the EV-snipe path. Parses `[ledger]` lines from `race_test_*.log` plus `ev_snipe.jsonl` and `btc_snipe_training.jsonl`. Columns: entry/hedge/save-exit/save-flip/settle/net, with correct/save/ghost/bail classification.
- `preseed_audit.py`: aggregates fires by `strategy|side` and flags promotion candidates (>=30 fires, positive hypo PnL, WR > 45%, positive EV).
- `shadow_audit.py`: tier-scored standing of each shadow strategy vs promotion thresholds (mu, CI, WR) against the production `STRATEGY_SIDE_MAP`.
- `audit_first_fires.py`: post-deploy sanity check. Lists recent LIVE `depth_collapse_mid` fires and confirms both UP/DN sides are wired.
- `missed_opportunity_audit.py`: finds markets where the winning side dipped into a cheap bucket but no strategy fired. Reports missed PnL per price bucket.
- `verify_buckets.py`: re-derives per-bucket UP/DN touch/win/PnL directly from `replay_*.json`, with replay data-quality filters.
- `improv_compare.py`: live-gate simulator (`sim_bn_gates_live`). Replays markets through the BN veto / held-release / BN-floor gate stack; reports equity curve, Sharpe, drawdown, walk-forward split.
- `hold_up_decide.py`: sweep of the hold-UP-then-decide gate (checkpoint cd, DN-trajectory feature, threshold) on $1-min PnL with train/OOS split.
- `two_stage_hold.py`: two-stage early-hold plus late-decision UP gate sweep over checkpoints and features.
- `deep_quant_twostage.py`: statistical comparison of the two-stage hold at cd=190 vs cd=170 (CIs, Sharpe, drawdown, streaks).

## Requirements

Python 3.8+, standard library only (`json`, `argparse`, `glob`, `re`, `collections`, `pathlib`).

## Usage

Run from a directory containing the data files; most default to the cwd.

```bash
python3 snipe_audit.py
python3 preseed_audit.py market_recap_history.jsonl
python3 shadow_audit.py market_recap_history.jsonl
python3 missed_opportunity_audit.py --recap market_recap_history.jsonl --replays $DATA_DIR/replays
python3 verify_buckets.py $DATA_DIR/replays
python3 improv_compare.py --data-dir $DATA_DIR --bn-floor 0.02
python3 hold_up_decide.py market_recap_history.jsonl market_history.jsonl
python3 two_stage_hold.py market_recap_history.jsonl market_history.jsonl
python3 deep_quant_twostage.py market_recap_history.jsonl market_history.jsonl
```

## Data

Reads runtime logs and recaps from the private `polymarket-data` repo. None are committed; see `.gitignore`. Place them in the cwd or pass via `--data-dir` or positional paths.

- `market_recap_history.jsonl`, `market_history.jsonl`, `market_history_lite.jsonl`: per-market fire records and outcomes
- `ev_snipe.jsonl`, `btc_snipe_training.jsonl`: snipe fills and training rows
- `replay_*.json`: per-market tick replays
- `race_test_*.log`: per-market execution logs (snipe ledger lines)

## Feedback

Suggestions, improvements, or something not working? Please
[open an issue](https://github.com/needmoneyforhoes/per-market-pnl-attribution/issues/new). Pull requests are welcome too.
