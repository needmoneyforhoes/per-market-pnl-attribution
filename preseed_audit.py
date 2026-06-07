#!/usr/bin/env python3
"""
Pre-Seed Audit — What Strategies Would Be Promoted?
=====================================================
Reads market_recap_history.jsonl (all historical fire data with hypo PnL).
Aggregates per strategy+side and shows which meet promotion criteria:
  - ≥30 shadow fires
  - Positive hypo PnL
  - WR > 45%
  - Positive EV per fire

Run: python3 preseed_audit.py market_recap_history.jsonl
"""
import json, sys
from collections import defaultdict

def main():
    fp = sys.argv[1] if len(sys.argv) > 1 else "market_recap_history.jsonl"
    print(f"Loading {fp}...")
    
    # Each line is a market with its fires
    strats = defaultdict(lambda: {'fires': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0,
                                   'hold_pnl': 0.0, 'prices': [], 'pnls': []})
    total_markets = 0
    
    with open(fp) as f:
        for line in f:
            if not line.strip(): continue
            try:
                market = json.loads(line)
            except:
                continue
            total_markets += 1
            fires = market.get('fires', [])
            for fire in fires:
                strat = fire.get('strategy', '?')
                side = fire.get('side', '?')
                mode = fire.get('mode', '?')
                hypo = fire.get('hypo_pnl', 0) or 0
                hold = fire.get('hypo_hold_pnl', hypo) or hypo
                price = fire.get('entry_price', 0) or 0
                
                key = f"{strat}|{side}"
                strats[key]['fires'] += 1
                strats[key]['pnl'] += hypo
                strats[key]['hold_pnl'] += hold
                strats[key]['pnls'].append(hypo)
                strats[key]['prices'].append(price)
                if hypo > 0:
                    strats[key]['wins'] += 1
                elif hypo < 0:
                    strats[key]['losses'] += 1
    
    print(f"Loaded {total_markets} markets, {len(strats)} strategy+side combos")
    
    # Sort by PnL
    sorted_strats = sorted(strats.items(), key=lambda x: -x[1]['pnl'])
    
    # Promotion criteria
    MIN_FIRES = 30
    MIN_WR = 0.45
    
    print(f"\n{'='*110}")
    print(f"PROMOTION CANDIDATES (≥{MIN_FIRES} fires, WR>{MIN_WR*100:.0f}%, PnL>0)")
    print(f"{'='*110}")
    print(f"  {'Strategy':<40}{'Fires':>6}{'W':>5}{'L':>5}{'WR':>6}{'PnL':>9}{'$/fire':>8}{'HoldPnL':>9}{'Promote?':>10}")
    print(f"  {'─'*95}")
    
    promoted = []
    for key, v in sorted_strats:
        fires = v['fires']
        if fires < MIN_FIRES: continue
        wr = v['wins'] / fires if fires > 0 else 0
        ev = v['pnl'] / fires if fires > 0 else 0
        hold_ev = v['hold_pnl'] / fires if fires > 0 else 0
        
        promote = wr >= MIN_WR and v['pnl'] > 0
        status = "✅ YES" if promote else "❌ NO"
        if promote:
            promoted.append(key)
        
        print(f"  {key:<40}{fires:>6}{v['wins']:>5}{v['losses']:>5}{wr:>5.0%}"
              f"${v['pnl']:>+8.2f}${ev:>+7.3f}${v['hold_pnl']:>+8.2f}  {status}")
    
    # Also show strategies with <30 fires but good early signal
    print(f"\n{'='*110}")
    print(f"PROMISING (10-29 fires, positive PnL) — need more data")
    print(f"{'='*110}")
    print(f"  {'Strategy':<40}{'Fires':>6}{'W':>5}{'L':>5}{'WR':>6}{'PnL':>9}{'$/fire':>8}{'Need':>6}")
    print(f"  {'─'*80}")
    
    for key, v in sorted_strats:
        fires = v['fires']
        if fires < 10 or fires >= MIN_FIRES: continue
        if v['pnl'] <= 0: continue
        wr = v['wins'] / fires
        ev = v['pnl'] / fires
        need = MIN_FIRES - fires
        print(f"  {key:<40}{fires:>6}{v['wins']:>5}{v['losses']:>5}{wr:>5.0%}"
              f"${v['pnl']:>+8.2f}${ev:>+7.3f}  {need:>4}")
    
    # Show worst performers too
    print(f"\n{'='*110}")
    print(f"WORST PERFORMERS (≥30 fires, most negative PnL)")
    print(f"{'='*110}")
    print(f"  {'Strategy':<40}{'Fires':>6}{'W':>5}{'L':>5}{'WR':>6}{'PnL':>9}{'$/fire':>8}")
    print(f"  {'─'*80}")
    
    worst = sorted(strats.items(), key=lambda x: x[1]['pnl'])
    for key, v in worst:
        fires = v['fires']
        if fires < MIN_FIRES: continue
        if v['pnl'] >= 0: continue
        wr = v['wins'] / fires
        ev = v['pnl'] / fires
        print(f"  {key:<40}{fires:>6}{v['wins']:>5}{v['losses']:>5}{wr:>5.0%}"
              f"${v['pnl']:>+8.2f}${ev:>+7.3f}")
    
    # Summary
    print(f"\n{'='*110}")
    print(f"SUMMARY")
    print(f"{'='*110}")
    print(f"  Total strategy+side combos: {len(strats)}")
    print(f"  With ≥30 fires: {sum(1 for v in strats.values() if v['fires'] >= 30)}")
    print(f"  PROMOTE: {len(promoted)}")
    for p in promoted:
        v = strats[p]
        print(f"    {p}: {v['fires']} fires, WR={v['wins']/v['fires']:.0%}, PnL=${v['pnl']:+.2f}")
    
    all_pnl = sum(v['pnl'] for v in strats.values())
    promote_pnl = sum(strats[p]['pnl'] for p in promoted)
    print(f"\n  Total hypo PnL (all strategies): ${all_pnl:+.2f}")
    print(f"  Promoted strategies PnL:         ${promote_pnl:+.2f}")
    print(f"  Non-promoted PnL:                ${all_pnl - promote_pnl:+.2f}")

if __name__ == "__main__":
    main()
