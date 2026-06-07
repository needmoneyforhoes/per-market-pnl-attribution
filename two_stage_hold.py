#!/usr/bin/env python3
"""
Two-Stage Hold-UP Gate
========================
Stage 1 (EARLY): If dn_low_std ≤ $X at cd=Y → HOLD all UP fires
Stage 2 (LATE):  At cd=Z, check DN trajectory:
  - DN strong (momentum/pct_above/recovery) → BLOCK UP (keep holding)
  - DN NOT strong → RELEASE UP (allow from now on)

This catches the uncatchable market: DN flat at $0.50 early (hold),
then DN breaks out (block). In UP-winning markets, DN doesn't break
out → release UP fires at cd=Z (only ~40s delay).

Tests every combination of hold threshold, hold checkpoint,
decision checkpoint, and decision feature.

Run: python3 two_stage_hold.py market_recap_history.jsonl market_history.jsonl
"""
import json, sys
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
    if len(samples) < 3: return None
    dn = [s[2] for s in samples]
    up = [s[1] for s in samples]
    n = len(dn)
    dn_avg = sum(dn) / n
    dn_std = (sum((d - dn_avg)**2 for d in dn) / n) ** 0.5
    dn_min = min(dn); dn_max = max(dn)
    third = max(n // 3, 1)
    momentum = sum(dn[-third:]) / third - sum(dn[:third]) / third
    recovery = dn[-1] - dn_min
    up_max = max(up); up_now = up[-1]
    up_drop = up_max - up_now
    pct_above = {}
    for th in [0.50, 0.55, 0.60, 0.65, 0.70]:
        pct_above[th] = sum(1 for d in dn if d >= th) / n * 100
    return {'dn_std': dn_std, 'dn_min': dn_min, 'dn_avg': dn_avg,
            'momentum': momentum, 'recovery': recovery, 'up_drop': up_drop,
            'pct_above': pct_above, 'dn_max': dn_max, 'up_max': up_max}

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

def main():
    recap_fp = sys.argv[1] if len(sys.argv) > 1 else "market_recap_history.jsonl"
    hist_fp = sys.argv[2] if len(sys.argv) > 2 else "market_history.jsonl"

    live_set = {
        'cl_gated_cheap|UP', 'cl_tight|UP',
        't4_late|DN', 't4_late|UP', 't4_sr_relaxed|DN',
        'deep_dip_scout|DN', 'deep_dip_scout|UP',
        'depth_collapse_mid|DN', 'depth_collapse_mid|UP',
        'depth_surge|DN', 'depth_surge|UP',
        'dn_deep_ask_collapse|DN', 'dn_deep_spread_collapse|DN', 'dn_mid_gap_count_spike|DN',
        'fake_train_detector|DN', 'fake_train_detector|UP',
        'up_cheap_low_flow|UP', 'up_deep_ask_collapse|UP', 'up_deep_spread_collapse|UP',
        'up_depth_collapse_mid_high|UP', 'up_mid_gap_count_spike|UP',
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

    # Load recap
    print(f"Loading {recap_fp}...")
    with open(recap_fp) as f:
        markets = [json.loads(l) for l in f if l.strip()]
    n = len(markets)
    split = int(n * 0.68)
    train = markets[:split]
    test = markets[split:]

    # Two-stage configs
    configs = []
    # Stage 1: hold conditions (early)
    for hold_cp in range(250, 190, -10):  # early checkpoint
        for hold_std in [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08]:
            # Stage 2: decision conditions (later)
            for decide_cp in range(hold_cp - 20, 90, -10):  # must be after hold
                # Decision features
                for dec_feat, dec_thresh in [
                    ('momentum', 0.05), ('momentum', 0.08), ('momentum', 0.10),
                    ('momentum', 0.15), ('momentum', 0.20),
                    ('pct_above_55', 40), ('pct_above_55', 50), ('pct_above_55', 60),
                    ('pct_above_60', 30), ('pct_above_60', 40), ('pct_above_60', 50),
                    ('pct_above_65', 30), ('pct_above_65', 40),
                    ('up_drop', 0.10), ('up_drop', 0.15), ('up_drop', 0.20),
                    ('recovery', 0.10), ('recovery', 0.15), ('recovery', 0.20),
                    ('dn_avg', 0.55), ('dn_avg', 0.60), ('dn_avg', 0.65),
                    # MULTI-CONDITION: require 2+ signals to confirm
                    # momentum + pct_above
                    ('mom_AND_pct55', (0.05, 40)), ('mom_AND_pct55', (0.08, 40)),
                    ('mom_AND_pct55', (0.05, 50)), ('mom_AND_pct55', (0.10, 40)),
                    ('mom_AND_pct60', (0.05, 30)), ('mom_AND_pct60', (0.08, 30)),
                    ('mom_AND_pct60', (0.05, 40)), ('mom_AND_pct60', (0.10, 30)),
                    # momentum + dn_avg
                    ('mom_AND_avg', (0.05, 0.55)), ('mom_AND_avg', (0.08, 0.55)),
                    ('mom_AND_avg', (0.05, 0.60)), ('mom_AND_avg', (0.10, 0.55)),
                    ('mom_AND_avg', (0.10, 0.60)), ('mom_AND_avg', (0.15, 0.55)),
                    # momentum + up_drop
                    ('mom_AND_drop', (0.05, 0.10)), ('mom_AND_drop', (0.08, 0.10)),
                    ('mom_AND_drop', (0.05, 0.15)), ('mom_AND_drop', (0.10, 0.10)),
                    # pct_above + dn_avg
                    ('pct55_AND_avg', (40, 0.55)), ('pct55_AND_avg', (50, 0.55)),
                    ('pct55_AND_avg', (40, 0.60)), ('pct60_AND_avg', (30, 0.55)),
                    ('pct60_AND_avg', (30, 0.60)), ('pct60_AND_avg', (40, 0.55)),
                    # TRIPLE: momentum + pct_above + dn_avg
                    ('mom_pct55_avg', (0.05, 40, 0.55)), ('mom_pct55_avg', (0.08, 40, 0.55)),
                    ('mom_pct55_avg', (0.05, 50, 0.55)), ('mom_pct55_avg', (0.05, 40, 0.60)),
                    ('mom_pct55_avg', (0.10, 40, 0.55)), ('mom_pct55_avg', (0.08, 50, 0.55)),
                    ('mom_pct60_avg', (0.05, 30, 0.55)), ('mom_pct60_avg', (0.08, 30, 0.55)),
                    ('mom_pct60_avg', (0.05, 30, 0.60)), ('mom_pct60_avg', (0.10, 30, 0.55)),
                    # TRIPLE: momentum + up_drop + dn_avg
                    ('mom_drop_avg', (0.05, 0.10, 0.55)), ('mom_drop_avg', (0.08, 0.10, 0.55)),
                    ('mom_drop_avg', (0.05, 0.15, 0.55)), ('mom_drop_avg', (0.05, 0.10, 0.60)),
                    ('mom_drop_avg', (0.10, 0.10, 0.55)), ('mom_drop_avg', (0.10, 0.15, 0.55)),
                ]:
                    configs.append((hold_cp, hold_std, decide_cp, dec_feat, dec_thresh))

    print(f"Testing {len(configs)} two-stage configs...")

    for ds_name, dataset in [("OOS", test), ("TRAIN", train)]:
        # Baseline
        baseline_pnl = 0
        for market in dataset:
            slug = market.get('slug', '')
            td = tick_data.get(slug)
            if not td: continue
            for fire in market.get('fires', []):
                key = f"{fire.get('strategy','?')}|{fire.get('side','?')}"
                if key not in live_set: continue
                side = fire.get('side','?')
                entry = fire.get('entry_price', 0) or 0
                if side == 'DN' and td['dn_gate'] and entry > 0.25: continue
                if side == 'UP' and td['up_gate']: continue
                baseline_pnl += realistic_pnl(entry, side, td['winner'])

        results = []
        for hold_cp, hold_std, decide_cp, dec_feat, dec_thresh in configs:
            total_pnl = 0
            up_held_blocked = 0; up_released = 0; up_blocked_won = 0

            for market in dataset:
                slug = market.get('slug', '')
                td = tick_data.get(slug)
                if not td: continue
                ticks = td['ticks']

                # Stage 1: check hold condition at hold_cp
                s1 = get_samples_up_to(ticks, hold_cp)
                f1 = compute_features(s1)
                should_hold = f1 is not None and f1['dn_std'] <= hold_std

                # Stage 2: if holding, check decision at decide_cp
                dn_confirmed = False
                if should_hold:
                    s2 = get_samples_up_to(ticks, decide_cp)
                    f2 = compute_features(s2)
                    if f2 is not None:
                        if dec_feat == 'momentum':
                            dn_confirmed = f2['momentum'] >= dec_thresh
                        elif dec_feat.startswith('pct_above_'):
                            pct_th = float(dec_feat.split('_')[2]) / 100
                            dn_confirmed = f2['pct_above'].get(pct_th, 0) >= dec_thresh
                        elif dec_feat == 'up_drop':
                            dn_confirmed = f2['up_drop'] >= dec_thresh
                        elif dec_feat == 'recovery':
                            dn_confirmed = f2['recovery'] >= dec_thresh
                        elif dec_feat == 'dn_avg':
                            dn_confirmed = f2['dn_avg'] >= dec_thresh
                        # DUAL conditions
                        elif dec_feat == 'mom_AND_pct55':
                            m, p = dec_thresh
                            dn_confirmed = f2['momentum'] >= m and f2['pct_above'].get(0.55, 0) >= p
                        elif dec_feat == 'mom_AND_pct60':
                            m, p = dec_thresh
                            dn_confirmed = f2['momentum'] >= m and f2['pct_above'].get(0.60, 0) >= p
                        elif dec_feat == 'mom_AND_avg':
                            m, a = dec_thresh
                            dn_confirmed = f2['momentum'] >= m and f2['dn_avg'] >= a
                        elif dec_feat == 'mom_AND_drop':
                            m, d = dec_thresh
                            dn_confirmed = f2['momentum'] >= m and f2['up_drop'] >= d
                        elif dec_feat == 'pct55_AND_avg':
                            p, a = dec_thresh
                            dn_confirmed = f2['pct_above'].get(0.55, 0) >= p and f2['dn_avg'] >= a
                        elif dec_feat == 'pct60_AND_avg':
                            p, a = dec_thresh
                            dn_confirmed = f2['pct_above'].get(0.60, 0) >= p and f2['dn_avg'] >= a
                        # TRIPLE conditions
                        elif dec_feat == 'mom_pct55_avg':
                            m, p, a = dec_thresh
                            dn_confirmed = (f2['momentum'] >= m and
                                          f2['pct_above'].get(0.55, 0) >= p and
                                          f2['dn_avg'] >= a)
                        elif dec_feat == 'mom_pct60_avg':
                            m, p, a = dec_thresh
                            dn_confirmed = (f2['momentum'] >= m and
                                          f2['pct_above'].get(0.60, 0) >= p and
                                          f2['dn_avg'] >= a)
                        elif dec_feat == 'mom_drop_avg':
                            m, d, a = dec_thresh
                            dn_confirmed = (f2['momentum'] >= m and
                                          f2['up_drop'] >= d and
                                          f2['dn_avg'] >= a)

                for fire in market.get('fires', []):
                    key = f"{fire.get('strategy','?')}|{fire.get('side','?')}"
                    if key not in live_set: continue
                    side = fire.get('side','?')
                    entry = fire.get('entry_price', 0) or 0
                    cd = fire.get('cd', 0) or 0

                    if side == 'DN' and td['dn_gate'] and entry > 0.25: continue
                    if side == 'UP' and td['up_gate']: continue

                    # Two-stage: hold was triggered AND DN confirmed → block UP
                    if side == 'UP' and should_hold and dn_confirmed:
                        up_held_blocked += 1
                        if td['winner'] == 'UP': up_blocked_won += 1
                        continue

                    if side == 'UP' and should_hold and not dn_confirmed:
                        up_released += 1

                    total_pnl += realistic_pnl(entry, side, td['winner'])

            improvement = total_pnl - baseline_pnl
            fp = up_blocked_won / up_held_blocked if up_held_blocked > 0 else 0
            results.append((hold_cp, hold_std, decide_cp, dec_feat, dec_thresh,
                           total_pnl, improvement, up_held_blocked, fp))

        results.sort(key=lambda x: -x[5])

        print(f"\n{'='*130}")
        print(f"  TWO-STAGE HOLD — {ds_name} ({len(dataset)} markets)")
        print(f"  Baseline: ${baseline_pnl:+.2f}")
        print(f"{'='*130}")
        print(f"\n  TOP 25:")
        print(f"  {'Hold':>6}{'Std':>6}{'Dec':>5}{'Feature':<16}{'Thresh':>7}{'PnL':>10}{'Δ':>8}{'Blk':>6}{'FP%':>6}")
        print(f"  {'─'*75}")

        for hcp, hst, dcp, df, dt, pnl, imp, blk, fp in results[:25]:
            marker = '✅' if imp > 5 and fp < 0.30 else '·'
            dt_str = f"{dt}" if isinstance(dt, (int,float)) else str(dt)
            print(f"  cd={hcp:>3} {hst:.2f} cd={dcp:>3} {df:<14} {dt_str:>7}${pnl:>+9.2f}${imp:>+7.2f}{blk:>5} {fp:>5.0%} {marker}")

if __name__ == "__main__":
    main()
