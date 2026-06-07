#!/usr/bin/env python3
"""
Hold-UP-Then-Decide Scanner
==============================
Logic: Hold ALL UP fires until checkpoint cd=X.
At checkpoint, check DN trajectory features.
  - If DN looks strong → BLOCK all UP fires (DN likely wins)
  - If DN doesn't look strong → ALLOW all UP fires

Tests every combination:
  - Checkpoints: cd=250 to cd=100 (every 10s)
  - Features: momentum, pct_above, recovery, up_drop
  - Thresholds: exhaustive sweep
  - DN gate $0.25 floor + UP gate $0.70 always active

Computes REALISTIC PnL ($1 min) with train/OOS split.

Run: python3 hold_up_decide.py market_recap_history.jsonl market_history.jsonl
"""
import json, sys
from collections import defaultdict

CD,UP_BID,UP_ASK,DN_BID,DN_ASK = 0,1,2,3,4

def get_prices_up_to(ticks, checkpoint_cd):
    samples = []
    for t in ticks:
        if t[CD] >= checkpoint_cd:
            up = t[UP_ASK] or 0
            dn = t[DN_ASK] or 0
            if up > 0 and dn > 0:
                samples.append((t[CD], up, dn))
    return samples

def compute_features(samples):
    if len(samples) < 3:
        return None
    dn_prices = [s[2] for s in samples]
    up_prices = [s[1] for s in samples]
    n = len(dn_prices)
    dn_max = max(dn_prices)
    dn_min = min(dn_prices)
    dn_now = dn_prices[-1]
    dn_first = dn_prices[0]
    third = max(n // 3, 1)
    early_avg = sum(dn_prices[:third]) / third
    late_avg = sum(dn_prices[-third:]) / third
    momentum = late_avg - early_avg
    recovery = dn_now - dn_min
    dn_range = dn_max - dn_min
    up_max = max(up_prices)
    up_now = up_prices[-1]
    up_drop = up_max - up_now
    pct_above = {}
    for thresh in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
        above = sum(1 for p in dn_prices if p >= thresh)
        pct_above[thresh] = above / n * 100
    dn_avg = sum(dn_prices) / n
    dn_std = (sum((d - dn_avg)**2 for d in dn_prices) / n) ** 0.5
    up_max = max(up_prices)
    return {
        'momentum': momentum, 'recovery': recovery, 'dn_range': dn_range,
        'dn_now': dn_now, 'dn_max': dn_max, 'dn_min': dn_min,
        'dn_avg': dn_avg, 'dn_std': dn_std,
        'up_now': up_now, 'up_max': up_max, 'up_drop': up_drop,
        'pct_above': pct_above,
    }

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
            samples.append((cd, dn))
            prev = cd
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

    # Load tick data for trajectory features
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
    train_mkts = markets[:split]
    test_mkts = markets[split:]

    # Feature configs to test
    feature_configs = []
    for cp in range(250, 90, -10):
        # Momentum
        for th in [0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30]:
            feature_configs.append(('momentum', cp, th))
        # Recovery
        for th in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]:
            feature_configs.append(('recovery', cp, th))
        # UP drop
        for th in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]:
            feature_configs.append(('up_drop', cp, th))
        # DN pct_above
        for dn_th in [0.50, 0.55, 0.60, 0.65, 0.70]:
            for pct in [30, 40, 50, 60, 70]:
                feature_configs.append(('pct_above', cp, (dn_th, pct)))
        # NEW: DN floor — DN min ≥ $X (never dropped below)
        for fl in [0.40, 0.42, 0.44, 0.46, 0.48, 0.50]:
            feature_configs.append(('dn_floor', cp, fl))
        # NEW: DN stability — std < $X (low volatility = strong support)
        for st in [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]:
            feature_configs.append(('dn_low_std', cp, st))
        # NEW: DN avg — DN average ≥ $X
        for av in [0.46, 0.48, 0.50, 0.52, 0.54, 0.56]:
            feature_configs.append(('dn_avg', cp, av))
        # NEW: UP ceiling — UP max ≤ $X (UP can't break higher)
        for uc in [0.48, 0.50, 0.52, 0.54, 0.56, 0.58, 0.60]:
            feature_configs.append(('up_ceiling', cp, uc))
        # NEW: DN time ≥ $X for Y% — consecutive dominance
        for dn_th in [0.40, 0.44, 0.46, 0.48, 0.50]:
            for pct in [60, 70, 80, 90, 100]:
                feature_configs.append(('dn_time_above', cp, (dn_th, pct)))
        # NEW: COMBO — DN floor ≥ $X AND DN avg ≥ $Y
        for fl in [0.40, 0.44, 0.46, 0.48, 0.50]:
            for av in [0.46, 0.48, 0.50, 0.52]:
                if av >= fl:
                    feature_configs.append(('combo_floor_avg', cp, (fl, av)))
    
    print(f"Testing {len(feature_configs)} configs...")

    for ds_name, dataset in [("TRAIN", train_mkts), ("OOS", test_mkts)]:
        # First compute baseline
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

        # Test each config
        results = []
        for feat_name, checkpoint, thresh in feature_configs:
            total_pnl = 0
            up_blocked = 0; up_allowed = 0; up_blocked_would_win = 0
            
            for market in dataset:
                slug = market.get('slug', '')
                td = tick_data.get(slug)
                if not td: continue
                
                # Compute trajectory at checkpoint
                samples = get_prices_up_to(td['ticks'], checkpoint)
                feat = compute_features(samples)
                
                # Decide: does DN look strong at checkpoint?
                dn_strong = False
                if feat is not None:
                    if feat_name == 'momentum':
                        dn_strong = feat['momentum'] >= thresh
                    elif feat_name == 'recovery':
                        dn_strong = feat['recovery'] >= thresh and feat['dn_range'] >= thresh + 0.05
                    elif feat_name == 'up_drop':
                        dn_strong = feat['up_drop'] >= thresh
                    elif feat_name == 'pct_above':
                        dn_th, pct = thresh
                        dn_strong = feat['pct_above'].get(dn_th, 0) >= pct
                    elif feat_name == 'dn_floor':
                        dn_strong = feat['dn_min'] >= thresh
                    elif feat_name == 'dn_low_std':
                        dn_strong = feat['dn_std'] <= thresh
                    elif feat_name == 'dn_avg':
                        dn_strong = feat['dn_avg'] >= thresh
                    elif feat_name == 'up_ceiling':
                        dn_strong = feat['up_max'] <= thresh
                    elif feat_name == 'dn_time_above':
                        dn_th, pct = thresh
                        dn_strong = feat['pct_above'].get(dn_th, 0) >= pct
                    elif feat_name == 'combo_floor_avg':
                        fl, av = thresh
                        dn_strong = feat['dn_min'] >= fl and feat['dn_avg'] >= av
                
                for fire in market.get('fires', []):
                    key = f"{fire.get('strategy','?')}|{fire.get('side','?')}"
                    if key not in live_set: continue
                    side = fire.get('side','?')
                    entry = fire.get('entry_price', 0) or 0
                    cd = fire.get('cd', 0) or 0
                    
                    # DN gate $0.25 floor (always active)
                    if side == 'DN' and td['dn_gate'] and entry > 0.25: continue
                    # UP gate $0.70 (always active)
                    if side == 'UP' and td['up_gate']: continue
                    
                    # HOLD-UP-THEN-DECIDE:
                    # If DN looks strong at checkpoint → block UP
                    if side == 'UP' and dn_strong:
                        up_blocked += 1
                        if td['winner'] == 'UP':
                            up_blocked_would_win += 1
                        continue
                    
                    if side == 'UP': up_allowed += 1
                    total_pnl += realistic_pnl(entry, side, td['winner'])
            
            improvement = total_pnl - baseline_pnl
            total_up = up_blocked + up_allowed
            false_pos = up_blocked_would_win / up_blocked if up_blocked > 0 else 0
            
            results.append((feat_name, checkpoint, thresh, total_pnl, improvement,
                           up_blocked, up_blocked_would_win, false_pos))
        
        # Sort by PnL
        results.sort(key=lambda x: -x[3])
        
        print(f"\n{'='*120}")
        print(f"  HOLD-UP-THEN-DECIDE — {ds_name} ({len(dataset)} markets)")
        print(f"  Baseline: ${baseline_pnl:+.2f}")
        print(f"{'='*120}")
        
        print(f"\n  TOP 25 BY PnL (improvement over baseline):")
        print(f"  {'Feature':<12}{'CP':>5}{'Thresh':<12}{'PnL':>10}{'Δ':>8}{'UP_blk':>8}{'FP%':>6}")
        print(f"  {'─'*65}")
        
        for feat, cp, th, pnl, imp, blk, bw, fp in results[:25]:
            if isinstance(th, tuple):
                th_str = f"${th[0]:.2f}p{th[1]}%"
            else:
                th_str = f"${th:.2f}"
            marker = '✅' if imp > 0 and fp < 0.35 else '·'
            print(f"  {feat:<12}cd={cp:>3} {th_str:<10}${pnl:>+9.2f}${imp:>+7.2f}{blk:>7} {fp:>5.0%} {marker}")
        
        # Show worst (biggest PnL reduction) for reference
        print(f"\n  WORST 5 (most PnL destroyed):")
        for feat, cp, th, pnl, imp, blk, bw, fp in results[-5:]:
            if isinstance(th, tuple):
                th_str = f"${th[0]:.2f}p{th[1]}%"
            else:
                th_str = f"${th:.2f}"
            print(f"  {feat:<12}cd={cp:>3} {th_str:<10}${pnl:>+9.2f}${imp:>+7.2f}{blk:>7} {fp:>5.0%}")

if __name__ == "__main__":
    main()
