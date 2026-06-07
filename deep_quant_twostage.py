#!/usr/bin/env python3
"""
Deep Quant Test — Two-Stage Hold cd=190 vs cd=170
=====================================================
Full statistical analysis: per-market PnL, confidence intervals,
Sharpe ratio, max drawdown, win/loss streaks, rolling performance.

Run: python3 deep_quant_twostage.py market_recap_history.jsonl market_history.jsonl
"""
import json, sys, math
from collections import defaultdict

CD,UP_BID,UP_ASK,DN_BID,DN_ASK = 0,1,2,3,4

def get_samples_up_to(ticks, cp):
    samples = []; prev = 999
    for t in ticks:
        cd = t[CD]
        if cd >= cp and cd < prev - 2.5:
            samples.append((cd, t[UP_ASK] or 0, t[DN_ASK] or 0))
            prev = cd
    return samples

def compute_features(samples):
    if len(samples) < 2: return None
    dn = [s[2] for s in samples]; up = [s[1] for s in samples]
    n = len(dn)
    dn_avg = sum(dn) / n
    dn_std = (sum((d - dn_avg)**2 for d in dn) / n) ** 0.5 if n > 1 else 0
    up_max = max(up); up_now = up[-1]; up_drop = up_max - up_now
    third = max(n // 3, 1)
    momentum = sum(dn[-third:]) / third - sum(dn[:third]) / third
    recovery = dn[-1] - min(dn)
    dn_max = max(dn)
    pct55 = sum(1 for d in dn if d >= 0.55) / n * 100
    pct60 = sum(1 for d in dn if d >= 0.60) / n * 100
    return {'dn_std': dn_std, 'up_drop': up_drop, 'momentum': momentum,
            'recovery': recovery, 'dn_avg': dn_avg, 'dn_max': dn_max,
            'pct55': pct55, 'pct60': pct60}

def realistic_pnl(entry_price, side, winner, base_shares=5):
    cost_per = entry_price + 0.01
    shares = base_shares
    total_cost = shares * cost_per
    if total_cost < 1.0 and cost_per > 0:
        shares = max(shares, int(1.0 / cost_per) + 1)
        total_cost = shares * cost_per
    settle = 1.00 if winner == side else 0.00
    return round(shares * settle - total_cost, 3)

def check_dn_gate(ticks):
    prev = 999; consec = 0
    for t in ticks:
        cd = t[CD]; up = t[UP_ASK] or 0
        if cd < prev - 9.5:
            if up >= 0.50: consec += 1
            else: consec = 0
            if consec >= 2 and cd <= 210: return True
            prev = cd
    return False

def check_up_gate(ticks):
    prev = 999; samples = []
    for t in ticks:
        cd = t[CD]; dn = t[DN_ASK] or 0
        if cd < prev - 2.5:
            samples.append((cd, dn)); prev = cd
            n = len(samples)
            if n >= 3 and cd <= 250:
                above = sum(1 for _, d in samples if d >= 0.70)
                if (above / n * 100) >= 40: return True
    return False

def stats(pnls):
    if not pnls: return {}
    n = len(pnls)
    mean = sum(pnls) / n
    var = sum((p - mean)**2 for p in pnls) / n if n > 1 else 0
    std = var ** 0.5
    sharpe = mean / std if std > 0 else 0
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    # Max drawdown
    cumsum = 0; peak = 0; max_dd = 0
    for p in pnls:
        cumsum += p
        if cumsum > peak: peak = cumsum
        dd = peak - cumsum
        if dd > max_dd: max_dd = dd
    # 95% CI
    se = std / (n ** 0.5) if n > 0 else 0
    ci_low = mean - 1.96 * se
    ci_hi = mean + 1.96 * se
    # Longest losing streak
    streak = 0; max_streak = 0
    for p in pnls:
        if p < 0: streak += 1
        else: streak = 0
        max_streak = max(max_streak, streak)
    return {'mean': mean, 'std': std, 'sharpe': sharpe, 'n': n,
            'wins': wins, 'losses': losses, 'wr': wins/(wins+losses) if wins+losses > 0 else 0,
            'total': sum(pnls), 'max_dd': max_dd, 'ci_low': ci_low, 'ci_hi': ci_hi,
            'max_lose_streak': max_streak}

def main():
    recap_fp = sys.argv[1] if len(sys.argv) > 1 else "market_recap_history.jsonl"
    hist_fp = sys.argv[2] if len(sys.argv) > 2 else "market_history.jsonl"

    live_set = {
        # Promoted from audit (side-restricted by STRATEGY_SIDE_MAP)
        'cl_gated_cheap|UP', 'cl_tight|UP',
        't4_late|DN', 't4_late|UP', 't4_sr_relaxed|DN',
        # Legacy LIVE — DN only (Rule F blocks UP side)
        'depth_collapse_mid|DN',
        'depth_surge|DN',
        'fake_train_detector|DN',
        'dn_deep_ask_collapse|DN', 'dn_deep_spread_collapse|DN', 'dn_mid_gap_count_spike|DN',
        # Legacy LIVE — UP only
        'up_cheap_low_flow|UP',
        'up_depth_collapse_mid_high|UP',
        'up_deep_spread_collapse|UP', 'up_deep_ask_collapse|UP', 'up_mid_gap_count_spike|UP',
        # DDS — own LIVE flag, fires BOTH sides
        'deep_dip_scout|DN', 'deep_dip_scout|UP',
    }

    # Load tick data
    print(f"Loading {hist_fp}...")
    tick_data = {}
    with open(hist_fp) as f:
        for line in f:
            if not line.strip(): continue
            try:
                m = json.loads(line)
                slug = m.get('slug', '')
                if m.get('ticks') and m.get('winner'):
                    tick_data[slug] = {
                        'ticks': m['ticks'],
                        'dn_gate': check_dn_gate(m['ticks']),
                        'up_gate': check_up_gate(m['ticks']),
                        'winner': m['winner'],
                    }
            except: pass

    print(f"Loading {recap_fp}...")
    with open(recap_fp) as f:
        markets = [json.loads(l) for l in f if l.strip()]
    n = len(markets)
    split = int(n * 0.68)

    # Configs to test
    configs = {
        'BASELINE (no two-stage)': {'hold_cp': None, 'hold_std': None, 'dec_cp': None, 'dec_feat': None, 'dec_thresh': None},
        # Winner from OOS
        'cd220→cd170 drop≥0.20 (0%FP)': {'hold_cp': 220, 'hold_std': 0.05, 'dec_cp': 170, 'dec_feat': 'up_drop', 'dec_thresh': 0.20},
        # Catches problem market
        'cd220→cd190 drop≥0.20 (11%FP)': {'hold_cp': 220, 'hold_std': 0.05, 'dec_cp': 190, 'dec_feat': 'up_drop', 'dec_thresh': 0.20},
        # cd190 with recovery
        'cd220→cd190 recovery≥0.20': {'hold_cp': 220, 'hold_std': 0.05, 'dec_cp': 190, 'dec_feat': 'recovery', 'dec_thresh': 0.20},
        # cd190 with momentum
        'cd220→cd190 mom≥0.10': {'hold_cp': 220, 'hold_std': 0.05, 'dec_cp': 190, 'dec_feat': 'momentum', 'dec_thresh': 0.10},
        # Multi-condition at cd=190
        'cd220→cd190 drop≥0.15+mom≥0.08': {'hold_cp': 220, 'hold_std': 0.05, 'dec_cp': 190, 'dec_feat': 'drop_AND_mom', 'dec_thresh': (0.15, 0.08)},
        'cd220→cd190 drop≥0.20+mom≥0.05': {'hold_cp': 220, 'hold_std': 0.05, 'dec_cp': 190, 'dec_feat': 'drop_AND_mom', 'dec_thresh': (0.20, 0.05)},
        # Even earlier decision
        'cd220→cd200 drop≥0.15': {'hold_cp': 220, 'hold_std': 0.05, 'dec_cp': 200, 'dec_feat': 'up_drop', 'dec_thresh': 0.15},
        'cd220→cd200 drop≥0.20': {'hold_cp': 220, 'hold_std': 0.05, 'dec_cp': 200, 'dec_feat': 'up_drop', 'dec_thresh': 0.20},
        # Tighter hold
        'cd210→cd170 drop≥0.20': {'hold_cp': 210, 'hold_std': 0.04, 'dec_cp': 170, 'dec_feat': 'up_drop', 'dec_thresh': 0.20},
        'cd210→cd190 drop≥0.20': {'hold_cp': 210, 'hold_std': 0.04, 'dec_cp': 190, 'dec_feat': 'up_drop', 'dec_thresh': 0.20},
        # BOTH (cascade): check cd=190 first, if no trigger check cd=170
        'CASCADE cd190+cd170 drop≥0.20': {'hold_cp': 220, 'hold_std': 0.05, 'dec_cp': 'cascade_190_170', 'dec_feat': 'up_drop', 'dec_thresh': 0.20},
    }

    for ds_name, dataset in [("TRAIN", markets[:split]), ("OOS", markets[split:]), ("ALL", markets)]:
        print(f"\n{'='*120}")
        print(f"  {ds_name} ({len(dataset)} markets)")
        print(f"{'='*120}")

        all_results = {}
        for cname, cfg in configs.items():
            market_pnls = []
            total_up_blocked = 0; total_up_blocked_won = 0
            total_fires = 0; total_wins = 0

            for market in dataset:
                slug = market.get('slug', '')
                td = tick_data.get(slug)
                if not td: continue
                ticks = td['ticks']

                # Two-stage logic
                should_hold = False
                dn_confirmed = False

                if cfg['hold_cp'] is not None:
                    s1 = get_samples_up_to(ticks, cfg['hold_cp'])
                    f1 = compute_features(s1)
                    should_hold = f1 is not None and f1['dn_std'] <= cfg['hold_std']

                    if should_hold:
                        if cfg['dec_cp'] == 'cascade_190_170':
                            # Try cd=190 first
                            s2 = get_samples_up_to(ticks, 190)
                            f2 = compute_features(s2)
                            if f2 and f2['up_drop'] >= cfg['dec_thresh']:
                                dn_confirmed = True
                            else:
                                # Try cd=170
                                s3 = get_samples_up_to(ticks, 170)
                                f3 = compute_features(s3)
                                if f3 and f3['up_drop'] >= cfg['dec_thresh']:
                                    dn_confirmed = True
                        else:
                            s2 = get_samples_up_to(ticks, cfg['dec_cp'])
                            f2 = compute_features(s2)
                            if f2:
                                if cfg['dec_feat'] == 'up_drop':
                                    dn_confirmed = f2['up_drop'] >= cfg['dec_thresh']
                                elif cfg['dec_feat'] == 'recovery':
                                    dn_confirmed = f2['recovery'] >= cfg['dec_thresh']
                                elif cfg['dec_feat'] == 'momentum':
                                    dn_confirmed = f2['momentum'] >= cfg['dec_thresh']
                                elif cfg['dec_feat'] == 'drop_AND_mom':
                                    d, m = cfg['dec_thresh']
                                    dn_confirmed = f2['up_drop'] >= d and f2['momentum'] >= m

                mkt_pnl = 0
                for fire in market.get('fires', []):
                    key = f"{fire.get('strategy','?')}|{fire.get('side','?')}"
                    if key not in live_set: continue
                    side = fire.get('side','?')
                    entry = fire.get('entry_price', 0) or 0

                    if side == 'DN' and td['dn_gate'] and entry > 0.25: continue
                    if side == 'UP' and td['up_gate']: continue

                    if side == 'UP' and should_hold and dn_confirmed:
                        total_up_blocked += 1
                        if td['winner'] == 'UP': total_up_blocked_won += 1
                        continue

                    pnl = realistic_pnl(entry, side, td['winner'])
                    mkt_pnl += pnl
                    total_fires += 1
                    if pnl > 0: total_wins += 1

                market_pnls.append(mkt_pnl)

            s = stats(market_pnls)
            fp = total_up_blocked_won / total_up_blocked if total_up_blocked > 0 else 0
            all_results[cname] = {**s, 'blocked': total_up_blocked, 'fp': fp,
                                  'fires': total_fires, 'fire_wins': total_wins}

        # Print comparison
        print(f"\n  {'Config':<42}{'PnL':>9}{'$/mkt':>7}{'Sharpe':>7}{'MktWR':>6}{'MaxDD':>7}{'95%CI':>16}{'Blk':>5}{'FP%':>5}{'MaxLS':>6}")
        print(f"  {'─'*110}")

        baseline_pnl = all_results.get('BASELINE (no two-stage)', {}).get('total', 0)
        for cname in configs:
            r = all_results[cname]
            ci = f"[{r.get('ci_low',0):+.2f},{r.get('ci_hi',0):+.2f}]"
            delta = r.get('total',0) - baseline_pnl
            marker = '✅' if delta > 0 and r.get('fp',0) < 0.15 else '·'
            print(f"  {cname:<42}${r.get('total',0):>+8.2f}${r.get('mean',0):>+6.2f}"
                  f"  {r.get('sharpe',0):>5.3f} {r.get('wr',0):>4.0%}  ${r.get('max_dd',0):>5.2f}"
                  f" {ci:>16} {r.get('blocked',0):>4} {r.get('fp',0):>4.0%} {r.get('max_lose_streak',0):>4} {marker}")

        # Delta comparison
        print(f"\n  IMPROVEMENT vs BASELINE:")
        for cname in configs:
            if 'BASELINE' in cname: continue
            r = all_results[cname]
            delta = r.get('total',0) - baseline_pnl
            print(f"    {cname:<42} Δ=${delta:>+8.2f}  FP={r.get('fp',0):.0%}  blk={r.get('blocked',0)}")

if __name__ == "__main__":
    main()
