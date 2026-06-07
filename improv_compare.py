#!/usr/bin/env python3
"""
sim_bn_gates_live.py — Live-gate simulator for Polymarket BTC 5-min bot.

Replays ALL markets in market_recap_history.jsonl through the current gate
stack (BN veto, BN-aligned held release, optional BN floor) and computes
what real-money PnL would have been.

RULES (matches production _v5_execute_entry + _recap_record_fire):
  1. HELD fires (cd>210, gate undecided) are BLOCKED unless BN-aligned
     at |bn| >= BN_SIDE_VETO_THRESH (held release).
  2. WBLOCK fires (opp-gate dominance block) stay blocked.
  3. FREE fires pass UNLESS:
     a) BN veto: fire opposes BN side AND |bn| >= BN_SIDE_VETO_THRESH  → blocked
     b) BN floor: |bn| < BN_FLOOR_MIN  → blocked (optional)
  4. Each passing fire is sized at FIXED_SHARES (default 5) and tracked.
  5. Only the FIRST fire per side per market executes (dedup collar).
     This matches real execution: once you buy UP, subsequent UP fires
     are redundant.

OUTPUT:
  - Per-market PnL with cumulative equity curve
  - Strategy-level breakdown
  - Walk-forward train/test split
  - Sharpe ratio, max drawdown, win rate

USAGE:
  python3 sim_bn_gates_live.py [--bn-floor 0.02] [--no-held-release] [--max-fires 99]

Place in same directory as market_recap_history.jsonl and market_history_lite.jsonl.
"""

import json
import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════
BN_SIDE_VETO_THRESH = 0.05   # |bn_delta_pct| to activate side veto
BN_FLOOR_MIN        = 0.00   # |bn_delta_pct| below which ALL fires blocked
HELD_RELEASE        = True   # release BN-aligned held fires at |bn|>=thresh
FIXED_SHARES        = 5      # shares per fire
MAX_FIRES_PER_MKT   = 99     # max fires per market (dedup collar)
DEDUP_PER_SIDE      = True   # only 1 fire per side per market


def load_data(data_dir):
    """Load market history and recap data."""
    lite_path = Path(data_dir) / "market_history_lite.jsonl"
    recap_path = Path(data_dir) / "market_recap_history.jsonl"

    if not lite_path.exists():
        print(f"ERROR: {lite_path} not found")
        sys.exit(1)
    if not recap_path.exists():
        print(f"ERROR: {recap_path} not found")
        sys.exit(1)

    bn_by_slug = {}
    winner_by_slug = {}
    with open(lite_path) as f:
        for line in f:
            if line.strip():
                m = json.loads(line.strip())
                bn_by_slug[m['slug']] = m.get('bn_delta_final', 0) or 0
                winner_by_slug[m['slug']] = m.get('winner')

    recaps = []
    with open(recap_path) as f:
        for line in f:
            if line.strip():
                recaps.append(json.loads(line.strip()))

    return bn_by_slug, winner_by_slug, recaps


def classify_fire(fire, bn_delta, bn_side, abs_bn, args):
    """
    Classify a fire through ALL live gates.
    Returns: ('PASS', reason) or ('BLOCK', reason)
    """
    side = fire.get('side')
    is_held = fire.get('pre_gate_held', False)
    is_wblock = (fire.get('opp_gate_would_block', False) or
                 fire.get('opp_gate_blocked', False))
    hypo = fire.get('hypo_pnl', 0) or 0

    # Gate 1: WBLOCK — always blocked
    if is_wblock:
        return 'BLOCK', 'wblock'

    # Gate 2: HELD fires
    if is_held:
        if args.held_release and abs_bn >= BN_SIDE_VETO_THRESH and side == bn_side:
            return 'PASS', 'held-released'
        return 'BLOCK', 'held'

    # Gate 3: BN floor — block ALL fires when |bn| too low
    if abs_bn < args.bn_floor:
        return 'BLOCK', 'bn-floor'

    # Gate 4: BN side veto — block anti-BN fires at |bn|>=thresh
    if abs_bn >= BN_SIDE_VETO_THRESH and side != bn_side:
        return 'BLOCK', 'bn-veto'

    return 'PASS', 'clean'


def simulate(bn_by_slug, winner_by_slug, recaps, args):
    """Run full simulation."""
    # Filter to recaps with gate data
    gated = [r for r in recaps
             if any(f.get('pre_gate_held') is not None for f in r.get('fires', []))]

    results = []
    strat_stats = defaultdict(lambda: {
        'fires': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0, 'pnls': []
    })
    block_reasons = defaultdict(int)

    for r in gated:
        slug = r['slug']
        winner = r.get('winner') or winner_by_slug.get(slug)
        bn_delta = bn_by_slug.get(slug, 0)
        if not winner:
            continue

        bn_side = "UP" if bn_delta > 0 else "DN"
        abs_bn = abs(bn_delta)

        mkt_pnl = 0.0
        mkt_fires = 0
        sides_filled = set()
        mkt_detail = []

        # Sort fires by CD descending (earliest fire first)
        fires = sorted(r.get('fires', []),
                        key=lambda f: -(f.get('cd', 0) or 0))

        for fire in fires:
            side = fire.get('side')
            hypo = fire.get('hypo_pnl', 0) or 0
            strat = fire.get('strategy', '?')
            cd = fire.get('cd', 0) or 0
            price = fire.get('entry_price', 0) or 0

            decision, reason = classify_fire(fire, bn_delta, bn_side, abs_bn, args)

            if decision == 'BLOCK':
                block_reasons[reason] += 1
                mkt_detail.append((strat, side, price, cd, hypo, f'BLOCKED:{reason}'))
                continue

            # Dedup: one fire per side per market
            if DEDUP_PER_SIDE and side in sides_filled:
                block_reasons['dedup'] += 1
                mkt_detail.append((strat, side, price, cd, hypo, 'BLOCKED:dedup'))
                continue

            # Max fires cap
            if mkt_fires >= args.max_fires:
                block_reasons['max-fires'] += 1
                mkt_detail.append((strat, side, price, cd, hypo, 'BLOCKED:max-fires'))
                continue

            # FIRE PASSES — this is a real trade
            mkt_pnl += hypo
            mkt_fires += 1
            sides_filled.add(side)

            s = strat_stats[strat]
            s['fires'] += 1
            s['pnl'] += hypo
            s['pnls'].append(hypo)
            if hypo > 0:
                s['wins'] += 1
            elif hypo < 0:
                s['losses'] += 1

            marker = '✅' if hypo > 0 else ('❌' if hypo < 0 else '➖')
            mkt_detail.append((strat, side, price, cd, hypo, f'FIRE {marker} ({reason})'))

        results.append({
            'slug': slug, 'winner': winner, 'bn_delta': bn_delta,
            'pnl': mkt_pnl, 'fires': mkt_fires, 'detail': mkt_detail
        })

    return results, strat_stats, block_reasons, gated


def report(results, strat_stats, block_reasons, gated, args):
    """Print full report."""
    import math

    total_pnl = sum(r['pnl'] for r in results)
    traded = [r for r in results if r['fires'] > 0]
    wins = sum(1 for r in traded if r['pnl'] > 0)
    losses = sum(1 for r in traded if r['pnl'] < 0)
    pnls = [r['pnl'] for r in results]

    print("=" * 90)
    print(f"  SIM RESULTS: BN veto={BN_SIDE_VETO_THRESH}  floor={args.bn_floor}  "
          f"held_release={args.held_release}  max_fires={args.max_fires}  dedup={DEDUP_PER_SIDE}")
    print("=" * 90)

    print(f"\n  Markets simulated: {len(results)}")
    print(f"  Markets with trades: {len(traded)}")
    print(f"  Total PnL: ${total_pnl:+.2f}")
    if traded:
        print(f"  Win rate (markets): {wins}/{len(traded)} = {wins/len(traded)*100:.1f}%")

    # Sharpe
    if len(pnls) > 1:
        import statistics
        mean = statistics.mean(pnls)
        std = statistics.stdev(pnls)
        sharpe = mean / std if std > 0 else 0
        print(f"  Per-market Sharpe: {sharpe:.3f}")
    
    # Max drawdown
    cumsum = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cumsum += p
        if cumsum > peak:
            peak = cumsum
        dd = peak - cumsum
        if dd > max_dd:
            max_dd = dd
    print(f"  Max drawdown: ${max_dd:.2f}")
    print(f"  Final equity: ${cumsum:.2f}")

    # Block reasons
    print(f"\n  Block reasons:")
    for reason, count in sorted(block_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason:<15} {count:>5}")

    # Walk-forward
    split = int(len(results) * 0.67)
    train_r = results[:split]
    test_r = results[split:]
    train_pnl = sum(r['pnl'] for r in train_r)
    test_pnl = sum(r['pnl'] for r in test_r)

    train_pnls = [r['pnl'] for r in train_r]
    test_pnls = [r['pnl'] for r in test_r]

    if len(train_pnls) > 1 and len(test_pnls) > 1:
        import statistics
        tr_sharpe = statistics.mean(train_pnls) / statistics.stdev(train_pnls) if statistics.stdev(train_pnls) > 0 else 0
        ts_sharpe = statistics.mean(test_pnls) / statistics.stdev(test_pnls) if statistics.stdev(test_pnls) > 0 else 0
    else:
        tr_sharpe = ts_sharpe = 0

    print(f"\n  Walk-forward ({len(train_r)} train / {len(test_r)} test):")
    print(f"    Train PnL: ${train_pnl:+.2f}  Sharpe: {tr_sharpe:.3f}")
    print(f"    Test PnL:  ${test_pnl:+.2f}  Sharpe: {ts_sharpe:.3f}")

    # Top strategies
    print(f"\n  {'Strategy':<28} {'N':>4} {'W':>3} {'L':>3} {'WR%':>6} {'PnL':>9} {'Avg':>7}")
    sorted_strats = sorted(strat_stats.items(), key=lambda x: -x[1]['pnl'])
    for strat, s in sorted_strats[:15]:
        n = s['fires']
        wr = s['wins'] / (s['wins'] + s['losses']) * 100 if (s['wins'] + s['losses']) > 0 else 0
        avg = s['pnl'] / n if n > 0 else 0
        print(f"  {strat:<28} {n:>4} {s['wins']:>3} {s['losses']:>3} {wr:>5.1f}% {s['pnl']:>+9.2f} {avg:>+7.2f}")
    print(f"  ...")
    for strat, s in sorted_strats[-5:]:
        n = s['fires']
        wr = s['wins'] / (s['wins'] + s['losses']) * 100 if (s['wins'] + s['losses']) > 0 else 0
        avg = s['pnl'] / n if n > 0 else 0
        print(f"  {strat:<28} {n:>4} {s['wins']:>3} {s['losses']:>3} {wr:>5.1f}% {s['pnl']:>+9.2f} {avg:>+7.2f}")

    # Equity curve (every 10 markets)
    print(f"\n  Equity curve (cumulative):")
    cumsum = 0
    for i, r in enumerate(results):
        cumsum += r['pnl']
        if (i + 1) % 10 == 0 or i == len(results) - 1:
            print(f"    Mkt {i+1:>3}: ${cumsum:>+8.2f}  ({r['slug'][-10:]})")

    # Per-market detail for last 10
    print(f"\n  Last 10 markets detail:")
    for r in results[-10:]:
        bn_side = "UP" if r['bn_delta'] > 0 else "DN"
        bn_ok = "✅" if bn_side == r['winner'] else "❌"
        print(f"    {r['slug'][-15:]} w={r['winner']} bn={r['bn_delta']:+.4f} {bn_ok} "
              f"fires={r['fires']} pnl=${r['pnl']:+.2f}")
        for strat, side, price, cd, hypo, action in r['detail']:
            if 'FIRE' in action:
                print(f"      {strat:<26} {side} @${price:.3f} cd={cd:>3} ${hypo:+.2f} {action}")


def compare_scenarios(bn_by_slug, winner_by_slug, recaps):
    """Run multiple scenarios side by side with deep breakdowns."""
    import statistics
    from collections import defaultdict

    def run_scenario(cfg):
        a = argparse.Namespace(**cfg)
        results, strat_stats, block_reasons, _ = simulate(bn_by_slug, winner_by_slug, recaps, a)
        return results, strat_stats, block_reasons

    def calc_metrics(results):
        pnls = [r['pnl'] for r in results]
        total = sum(pnls)
        n_traded = sum(1 for r in results if r['fires'] > 0)
        wins = sum(1 for r in results if r['fires'] > 0 and r['pnl'] > 0)
        losses = sum(1 for r in results if r['fires'] > 0 and r['pnl'] < 0)
        wr = wins / n_traded * 100 if n_traded > 0 else 0
        sharpe = 0
        if len(pnls) > 1:
            m = statistics.mean(pnls)
            s = statistics.stdev(pnls)
            sharpe = m / s if s > 0 else 0
        # Walk-forward
        split = int(len(results) * 0.67)
        train_pnl = sum(r['pnl'] for r in results[:split])
        test_pnl = sum(r['pnl'] for r in results[split:])
        test_pnls = [r['pnl'] for r in results[split:]]
        ts_sharpe = 0
        if len(test_pnls) > 1:
            m = statistics.mean(test_pnls)
            s = statistics.stdev(test_pnls)
            ts_sharpe = m / s if s > 0 else 0
        # Max drawdown
        cumsum = 0
        peak = 0
        max_dd = 0
        for p in pnls:
            cumsum += p
            if cumsum > peak: peak = cumsum
            dd = peak - cumsum
            if dd > max_dd: max_dd = dd
        return {'pnl': total, 'sharpe': sharpe, 'trades': n_traded, 'wins': wins,
                'losses': losses, 'wr': wr, 'train': train_pnl, 'test': test_pnl,
                'ts_sharpe': ts_sharpe, 'max_dd': max_dd}

    # ═══════════════════════════════════════════════════════════════════
    # 1. GATE STACK COMPARISON
    # ═══════════════════════════════════════════════════════════════════
    scenarios = [
        ("No gates (raw GC)",          {'bn_floor': 0.0,  'held_release': False, 'max_fires': 99}),
        ("BN veto only",               {'bn_floor': 0.0,  'held_release': False, 'max_fires': 99}),
        ("Veto + held release",        {'bn_floor': 0.0,  'held_release': True,  'max_fires': 99}),
        ("Veto + HR + floor 0.02",     {'bn_floor': 0.02, 'held_release': True,  'max_fires': 99}),
        ("Veto + HR + floor 0.03",     {'bn_floor': 0.03, 'held_release': True,  'max_fires': 99}),
        ("Full stack (0.02+dedup)",    {'bn_floor': 0.02, 'held_release': True,  'max_fires': 99}),
    ]

    print("\n" + "=" * 110)
    print("  1. GATE STACK COMPARISON")
    print("=" * 110)
    print(f"  {'Scenario':<30} {'PnL':>8} {'Sharpe':>7} {'Trades':>7} {'WR%':>6} "
          f"{'MaxDD':>7} {'Train':>9} {'Test':>9} {'TsSharpe':>9}")

    for name, cfg in scenarios:
        results, _, _ = run_scenario(cfg)
        m = calc_metrics(results)
        print(f"  {name:<30} {m['pnl']:>+8.2f} {m['sharpe']:>+7.3f} {m['trades']:>7} {m['wr']:>5.1f}% "
              f"{m['max_dd']:>7.2f} {m['train']:>+9.2f} {m['test']:>+9.2f} {m['ts_sharpe']:>+9.3f}")

    # ═══════════════════════════════════════════════════════════════════
    # 2. INDIVIDUAL GATE CONTRIBUTION (add one gate at a time)
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 110)
    print("  2. INDIVIDUAL GATE CONTRIBUTION (each gate's marginal impact)")
    print("=" * 110)

    base_cfg = {'bn_floor': 0.0, 'held_release': False, 'max_fires': 99}
    base_results, _, _ = run_scenario(base_cfg)
    base_m = calc_metrics(base_results)

    gate_tests = [
        ("+ BN veto alone",     {'bn_floor': 0.0,  'held_release': False, 'max_fires': 99}),
        ("+ Held release alone", {'bn_floor': 0.0,  'held_release': True,  'max_fires': 99}),
        ("+ Floor 0.02 alone",  {'bn_floor': 0.02, 'held_release': False, 'max_fires': 99}),
        ("+ Dedup alone",       {'bn_floor': 0.0,  'held_release': False, 'max_fires': 99}),
        ("= All combined",      {'bn_floor': 0.02, 'held_release': True,  'max_fires': 99}),
    ]

    print(f"  {'Gate':<25} {'PnL':>8} {'ΔPNL':>8} {'Sharpe':>7} {'ΔSharpe':>8} {'Trades':>7} {'WR%':>6}")
    print(f"  {'Baseline (no gates)':<25} {base_m['pnl']:>+8.2f} {'':>8} {base_m['sharpe']:>+7.3f} {'':>8} {base_m['trades']:>7} {base_m['wr']:>5.1f}%")
    for name, cfg in gate_tests:
        results, _, _ = run_scenario(cfg)
        m = calc_metrics(results)
        dpnl = m['pnl'] - base_m['pnl']
        dsharpe = m['sharpe'] - base_m['sharpe']
        print(f"  {name:<25} {m['pnl']:>+8.2f} {dpnl:>+8.2f} {m['sharpe']:>+7.3f} {dsharpe:>+8.3f} {m['trades']:>7} {m['wr']:>5.1f}%")

    # ═══════════════════════════════════════════════════════════════════
    # 3. SIDE ANALYSIS (UP vs DN performance)
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 110)
    print("  3. SIDE ANALYSIS (full stack)")
    print("=" * 110)

    full_cfg = {'bn_floor': 0.02, 'held_release': True, 'max_fires': 99}
    full_results, full_strats, _ = run_scenario(full_cfg)

    # Collect per-fire data from the detail
    up_pnl = 0
    dn_pnl = 0
    up_fires = 0
    dn_fires = 0
    up_wins = 0
    dn_wins = 0
    up_losses = 0
    dn_losses = 0
    for r in full_results:
        for strat, side, price, cd, hypo, action in r['detail']:
            if 'FIRE' not in action:
                continue
            if side == 'UP':
                up_pnl += hypo
                up_fires += 1
                if hypo > 0: up_wins += 1
                elif hypo < 0: up_losses += 1
            else:
                dn_pnl += hypo
                dn_fires += 1
                if hypo > 0: dn_wins += 1
                elif hypo < 0: dn_losses += 1

    print(f"  {'Side':<8} {'Fires':>6} {'W':>4} {'L':>4} {'WR%':>6} {'PnL':>9} {'Avg':>7}")
    up_wr = up_wins / (up_wins + up_losses) * 100 if (up_wins + up_losses) > 0 else 0
    dn_wr = dn_wins / (dn_wins + dn_losses) * 100 if (dn_wins + dn_losses) > 0 else 0
    print(f"  {'UP':<8} {up_fires:>6} {up_wins:>4} {up_losses:>4} {up_wr:>5.1f}% {up_pnl:>+9.2f} {up_pnl/max(up_fires,1):>+7.2f}")
    print(f"  {'DN':<8} {dn_fires:>6} {dn_wins:>4} {dn_losses:>4} {dn_wr:>5.1f}% {dn_pnl:>+9.2f} {dn_pnl/max(dn_fires,1):>+7.2f}")
    print(f"  {'TOTAL':<8} {up_fires+dn_fires:>6} {up_wins+dn_wins:>4} {up_losses+dn_losses:>4} "
          f"{(up_wins+dn_wins)/(up_wins+dn_wins+up_losses+dn_losses)*100 if (up_wins+dn_wins+up_losses+dn_losses) > 0 else 0:>5.1f}% "
          f"{up_pnl+dn_pnl:>+9.2f} {(up_pnl+dn_pnl)/max(up_fires+dn_fires,1):>+7.2f}")

    # ═══════════════════════════════════════════════════════════════════
    # 4. TIMING ANALYSIS (entry CD buckets)
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 110)
    print("  4. TIMING ANALYSIS — entry CD buckets (full stack)")
    print("=" * 110)

    cd_buckets = [(250, 300, "250-300 (early)"), (200, 250, "200-250"),
                  (150, 200, "150-200"), (100, 150, "100-150"),
                  (50, 100, "50-100"), (0, 50, "0-50 (late)")]

    print(f"  {'CD Range':<20} {'Fires':>6} {'W':>4} {'L':>4} {'WR%':>6} {'PnL':>9} {'Avg':>7} {'Source':>12}")
    for lo, hi, label in cd_buckets:
        fires = []
        for r in full_results:
            for strat, side, price, cd, hypo, action in r['detail']:
                if 'FIRE' not in action: continue
                if lo <= cd < hi:
                    fires.append({'hypo': hypo, 'action': action})
        if not fires:
            continue
        n = len(fires)
        w = sum(1 for f in fires if f['hypo'] > 0)
        l = sum(1 for f in fires if f['hypo'] < 0)
        pnl = sum(f['hypo'] for f in fires)
        wr = w / (w + l) * 100 if (w + l) > 0 else 0
        hr = sum(1 for f in fires if 'held-released' in f['action'])
        cl = sum(1 for f in fires if 'clean' in f['action'])
        src = f"{hr}hr/{cl}gc"
        print(f"  {label:<20} {n:>6} {w:>4} {l:>4} {wr:>5.1f}% {pnl:>+9.2f} {pnl/n:>+7.2f} {src:>12}")

    # ═══════════════════════════════════════════════════════════════════
    # 5. |BN| BUCKET ANALYSIS
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 110)
    print("  5. |BN| BUCKET ANALYSIS (full stack)")
    print("=" * 110)

    bn_buckets = [(0.02, 0.03, "0.02-0.03"), (0.03, 0.05, "0.03-0.05"),
                  (0.05, 0.10, "0.05-0.10"), (0.10, 0.20, "0.10-0.20"),
                  (0.20, 2.0, "≥0.20")]

    print(f"  {'|BN| Range':<14} {'Mkts':>5} {'Traded':>7} {'Fires':>6} {'W':>4} {'L':>4} {'WR%':>6} {'PnL':>9} {'Avg/mkt':>8}")
    for lo, hi, label in bn_buckets:
        mkt_results = [r for r in full_results if lo <= abs(r['bn_delta']) < hi]
        if not mkt_results: continue
        n_mkts = len(mkt_results)
        n_traded = sum(1 for r in mkt_results if r['fires'] > 0)
        total_fires = sum(r['fires'] for r in mkt_results)
        total_pnl = sum(r['pnl'] for r in mkt_results)
        w = sum(1 for r in mkt_results if r['fires'] > 0 and r['pnl'] > 0)
        l = sum(1 for r in mkt_results if r['fires'] > 0 and r['pnl'] < 0)
        wr = w / (w + l) * 100 if (w + l) > 0 else 0
        avg_mkt = total_pnl / n_mkts
        print(f"  {label:<14} {n_mkts:>5} {n_traded:>7} {total_fires:>6} {w:>4} {l:>4} {wr:>5.1f}% {total_pnl:>+9.2f} {avg_mkt:>+8.2f}")

    # ═══════════════════════════════════════════════════════════════════
    # 6. STRATEGY LEADERBOARD (full stack)
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 110)
    print("  6. STRATEGY LEADERBOARD (full stack, sorted by PnL)")
    print("=" * 110)

    strat_data = defaultdict(lambda: {'fires': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0,
                                       'up_fires': 0, 'dn_fires': 0, 'avg_cd': [],
                                       'hr_fires': 0, 'gc_fires': 0})
    for r in full_results:
        for strat, side, price, cd, hypo, action in r['detail']:
            if 'FIRE' not in action: continue
            s = strat_data[strat]
            s['fires'] += 1
            s['pnl'] += hypo
            s['avg_cd'].append(cd)
            if hypo > 0: s['wins'] += 1
            elif hypo < 0: s['losses'] += 1
            if side == 'UP': s['up_fires'] += 1
            else: s['dn_fires'] += 1
            if 'held-released' in action: s['hr_fires'] += 1
            else: s['gc_fires'] += 1

    print(f"  {'Strategy':<26} {'N':>3} {'W':>3} {'L':>3} {'WR%':>6} {'PnL':>8} {'Avg':>7} "
          f"{'UP':>3} {'DN':>3} {'AvgCD':>6} {'HR':>3} {'GC':>3}")
    for strat, s in sorted(strat_data.items(), key=lambda x: -x[1]['pnl']):
        wr = s['wins'] / (s['wins'] + s['losses']) * 100 if (s['wins'] + s['losses']) > 0 else 0
        avg_cd = sum(s['avg_cd']) / len(s['avg_cd']) if s['avg_cd'] else 0
        avg_pnl = s['pnl'] / s['fires'] if s['fires'] > 0 else 0
        print(f"  {strat:<26} {s['fires']:>3} {s['wins']:>3} {s['losses']:>3} {wr:>5.1f}% "
              f"{s['pnl']:>+8.2f} {avg_pnl:>+7.2f} {s['up_fires']:>3} {s['dn_fires']:>3} "
              f"{avg_cd:>6.0f} {s['hr_fires']:>3} {s['gc_fires']:>3}")

    # ═══════════════════════════════════════════════════════════════════
    # 7. BLOCK REASON BREAKDOWN
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 110)
    print("  7. BLOCK REASON BREAKDOWN (full stack)")
    print("=" * 110)

    _, _, block_reasons = run_scenario(full_cfg)
    total_blocked = sum(block_reasons.values())
    print(f"  {'Reason':<15} {'Count':>7} {'%':>7} {'Description'}")
    for reason, count in sorted(block_reasons.items(), key=lambda x: -x[1]):
        pct = count / total_blocked * 100 if total_blocked > 0 else 0
        desc = {'held': 'Gate undecided at cd>210, BN too weak for release',
                'dedup': 'Already fired this side this market',
                'bn-veto': 'Fire opposes BN at |bn|>=0.05',
                'bn-floor': 'Market too quiet (|bn|<0.02)',
                'wblock': 'Opp-gate dominance block',
                'max-fires': 'Max fires per market cap'}.get(reason, '')
        print(f"  {reason:<15} {count:>7} {pct:>6.1f}% {desc}")
    print(f"  {'TOTAL':<15} {total_blocked:>7}")

    # ═══════════════════════════════════════════════════════════════════
    # 8. EQUITY CURVE COMPARISON (10-market windows)
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 110)
    print("  8. EQUITY CURVE — full stack vs no gates (10-market windows)")
    print("=" * 110)

    nogates_results, _, _ = run_scenario(base_cfg)
    full_results2, _, _ = run_scenario(full_cfg)

    print(f"  {'Window':<10} {'NoGates':>10} {'FullStack':>10} {'Diff':>10}")
    cum_ng = 0
    cum_fs = 0
    for i in range(len(full_results2)):
        cum_ng += nogates_results[i]['pnl'] if i < len(nogates_results) else 0
        cum_fs += full_results2[i]['pnl']
        if (i + 1) % 10 == 0 or i == len(full_results2) - 1:
            diff = cum_fs - cum_ng
            print(f"  Mkt {i+1:<5} {cum_ng:>+10.2f} {cum_fs:>+10.2f} {diff:>+10.2f}")

    # ═══════════════════════════════════════════════════════════════════
    # 9. TEMPORAL STABILITY (20-market rolling windows)
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 110)
    print("  9. TEMPORAL STABILITY — 20-market rolling PnL (full stack)")
    print("=" * 110)

    window = 20
    print(f"  {'Window':<16} {'PnL':>8} {'Trades':>7} {'WR%':>6} {'Sharpe':>8}")
    for start in range(0, len(full_results2) - window + 1, window):
        chunk = full_results2[start:start + window]
        chunk_pnl = sum(r['pnl'] for r in chunk)
        chunk_traded = sum(1 for r in chunk if r['fires'] > 0)
        chunk_wins = sum(1 for r in chunk if r['fires'] > 0 and r['pnl'] > 0)
        chunk_losses = sum(1 for r in chunk if r['fires'] > 0 and r['pnl'] < 0)
        chunk_wr = chunk_wins / (chunk_wins + chunk_losses) * 100 if (chunk_wins + chunk_losses) > 0 else 0
        chunk_pnls = [r['pnl'] for r in chunk]
        chunk_sharpe = 0
        if len(chunk_pnls) > 1:
            cm = statistics.mean(chunk_pnls)
            cs = statistics.stdev(chunk_pnls)
            chunk_sharpe = cm / cs if cs > 0 else 0
        print(f"  Mkt {start+1:<3}-{start+window:<3}   {chunk_pnl:>+8.2f} {chunk_traded:>7} {chunk_wr:>5.1f}% {chunk_sharpe:>+8.3f}")


def main():
    parser = argparse.ArgumentParser(description="BN gate live simulator")
    parser.add_argument('--bn-floor', type=float, default=0.0,
                        help='Min |bn_delta_pct| to allow ANY fire (default: 0.0 = no floor)')
    parser.add_argument('--held-release', action='store_true', default=True,
                        help='Release BN-aligned held fires (default: True)')
    parser.add_argument('--no-held-release', action='store_true', default=False,
                        help='Disable held release')
    parser.add_argument('--max-fires', type=int, default=99,
                        help='Max fires per market (default: 99 = no cap)')
    parser.add_argument('--data-dir', type=str, default='.',
                        help='Directory containing market_history_lite.jsonl and market_recap_history.jsonl')
    parser.add_argument('--compare', action='store_true', default=False,
                        help='Run scenario comparison mode')
    parser.add_argument('--verbose', action='store_true', default=False,
                        help='Show per-fire detail for every market')

    args = parser.parse_args()
    if args.no_held_release:
        args.held_release = False

    bn_by_slug, winner_by_slug, recaps = load_data(args.data_dir)

    if args.compare:
        compare_scenarios(bn_by_slug, winner_by_slug, recaps)
        return

    results, strat_stats, block_reasons, gated = simulate(
        bn_by_slug, winner_by_slug, recaps, args)
    report(results, strat_stats, block_reasons, gated, args)

    if args.verbose:
        print(f"\n{'='*90}")
        print(f"  FULL MARKET DETAIL")
        print(f"{'='*90}")
        for r in results:
            bn_side = "UP" if r['bn_delta'] > 0 else "DN"
            bn_ok = "✅" if bn_side == r['winner'] else "❌"
            print(f"\n  {r['slug']} w={r['winner']} bn={r['bn_delta']:+.4f} {bn_ok} "
                  f"fires={r['fires']} pnl=${r['pnl']:+.2f}")
            for strat, side, price, cd, hypo, action in r['detail']:
                print(f"    {strat:<26} {side} @${price:.3f} cd={cd:>3} ${hypo:+.2f} {action}")


if __name__ == "__main__":
    main()
