#!/usr/bin/env python3
"""
missed_opportunity_audit.py — find markets where winning side dipped to cheap buckets
without any strategy firing there.

For each market, analyzes whether the WINNING side ever visited each cheap bucket
($0.01-$0.05, $0.05-$0.10, ..., $0.50-$0.55). If it did, checks whether any strategy
(LIVE, SHADOW, whatever) fired on the winning side in that bucket.

Outputs per bucket:
  - Opportunities CAUGHT:  winning side dipped AND strategy fired on winning side
  - Opportunities MISSED:  winning side dipped but NO strategy fired on that side
  - Potential PnL missed:  what each miss would have earned at hold-to-settle

Also shows ANY fires by losing side (for comparison — these are losing bets).

Usage:
  python3 missed_opportunity_audit.py
  python3 missed_opportunity_audit.py --recap path.jsonl --replays /path
"""
import json, glob, os, sys
from collections import defaultdict

RECAP_PATH = 'market_recap_history.jsonl'
REPLAY_DIR = '.'

args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] == '--recap' and i+1 < len(args): RECAP_PATH = args[i+1]; i += 2
    elif args[i] == '--replays' and i+1 < len(args): REPLAY_DIR = args[i+1]; i += 2
    else: i += 1

BUCKETS = [(0.01, 0.05, '0.01-0.05'), (0.05, 0.10, '0.05-0.10'),
           (0.10, 0.15, '0.10-0.15'), (0.15, 0.20, '0.15-0.20'),
           (0.20, 0.25, '0.20-0.25'), (0.25, 0.30, '0.25-0.30'),
           (0.30, 0.35, '0.30-0.35'), (0.35, 0.40, '0.35-0.40'),
           (0.40, 0.45, '0.40-0.45'), (0.45, 0.50, '0.45-0.50'),
           (0.50, 0.55, '0.50-0.55')]

SHARES = 5
SLIP = 0.01


def bucket_for(p):
    for lo, hi, n in BUCKETS:
        if lo <= p < hi: return n
    return None


def load_market_replays(slug):
    """Return replay for slug, or None."""
    path = os.path.join(REPLAY_DIR, f'replay_btc-updown-5m-{slug}.json')
    if not os.path.exists(path): return None
    try:
        with open(path) as f: return json.load(f)
    except: return None


def analyze_market(m, replay):
    """For one market, determine which cheap buckets each side visited + cheapest entry."""
    cols = replay.get('tick_columns', [])
    try:
        cd_i = cols.index('cd')
        ua_i = cols.index('up_ask')
        da_i = cols.index('dn_ask')
    except ValueError:
        return None

    # For each side, find: did it visit each bucket? What's the MIN ask per bucket?
    # (Min ask = best hypothetical entry price within that bucket)
    up_bucket_min = {}  # bucket_name → (cd_of_min, min_ask)
    dn_bucket_min = {}

    for t in replay['ticks']:
        if not t: continue
        cd = t[cd_i] if cd_i < len(t) else None
        ua = t[ua_i] if ua_i < len(t) else None
        da = t[da_i] if da_i < len(t) else None
        if cd is None: continue

        if ua is not None and 0 < ua < 1:
            b = bucket_for(ua)
            if b and (b not in up_bucket_min or ua < up_bucket_min[b][1]):
                up_bucket_min[b] = (cd, ua)
        if da is not None and 0 < da < 1:
            b = bucket_for(da)
            if b and (b not in dn_bucket_min or da < dn_bucket_min[b][1]):
                dn_bucket_min[b] = (cd, da)

    return {'UP': up_bucket_min, 'DN': dn_bucket_min}


def main():
    if not os.path.exists(RECAP_PATH):
        print(f"ERROR: {RECAP_PATH} not found"); return

    # Load recap markets
    markets = []
    with open(RECAP_PATH) as f:
        for line in f:
            if line.strip():
                markets.append(json.loads(line))

    print(f"Loaded {len(markets)} markets from {RECAP_PATH}\n")

    winners = {'UP': 0, 'DN': 0}
    for m in markets:
        w = m.get('winner')
        if w in winners: winners[w] += 1
    print(f"Winners: UP={winners['UP']}, DN={winners['DN']}")
    if abs(winners['UP'] - winners['DN']) / max(len(markets),1) > 0.2:
        print(f"⚠️  Heavy winner imbalance — results are skewed toward the majority side\n")
    print()

    # For each bucket × side: classify each market's opportunity status
    # Categories:
    #   CAUGHT:   side was winner, side visited bucket, strategy fired on winning side in bucket
    #   MISSED:   side was winner, side visited bucket, NO strategy fired on winning side in bucket
    #   LOST_SHOT: side was LOSER, side visited bucket, strategy fired on losing side (wasted fire)
    #   NA:       side was winner but never visited bucket (nothing to catch)

    stats = defaultdict(lambda: {
        'caught': [], 'missed': [], 'lost_shot': [], 'dipped_total': 0
    })

    for m in markets:
        slug = m['slug'].replace('btc-updown-5m-', '')
        winner = m.get('winner')
        if winner not in ('UP', 'DN'): continue

        replay = load_market_replays(slug)
        if not replay:
            continue

        bucket_visits = analyze_market(m, replay)
        if not bucket_visits: continue

        # For each side, check each bucket
        for side in ('UP', 'DN'):
            visits = bucket_visits[side]
            for bname, (cd_at_min, min_ask) in visits.items():
                stats[bname]['dipped_total'] += 1
                # Did any strategy fire on this side in this bucket?
                side_fires_in_bucket = [
                    f for f in m.get('fires', [])
                    if f.get('side') == side
                    and f.get('entry_price') is not None
                    and bucket_for(f['entry_price']) == bname
                ]

                if side == winner:
                    # Winning side visited bucket
                    if side_fires_in_bucket:
                        # Caught
                        stats[bname]['caught'].append({
                            'slug': slug, 'side': side, 'min_ask': min_ask,
                            'cd_at_min': cd_at_min, 'fires': len(side_fires_in_bucket)
                        })
                    else:
                        # Missed! Winning side dipped, nobody fired
                        stats[bname]['missed'].append({
                            'slug': slug, 'side': side, 'min_ask': min_ask,
                            'cd_at_min': cd_at_min,
                        })
                else:
                    # Losing side visited bucket (and may have fired)
                    if side_fires_in_bucket:
                        stats[bname]['lost_shot'].append({
                            'slug': slug, 'side': side, 'min_ask': min_ask,
                            'fires': len(side_fires_in_bucket)
                        })

    # ─── Print the big table ───
    print("="*120)
    print("MISSED OPPORTUNITY AUDIT — 'winning side dipped to cheap bucket, did a strategy catch it?'")
    print("="*120)
    print()
    print(f"{'bucket':<12} {'total_dipped':<12} {'WINS_CAUGHT':<12} {'WINS_MISSED':<12} "
          f"{'potential_missed_$':<20} {'losing_fires_here':<17} {'lost_$_on_wrong_side':<20}")
    print("-"*115)

    total_missed_pnl = 0.0
    total_caught = 0
    total_missed = 0
    for lo, hi, bname in BUCKETS:
        s = stats[bname]
        caught = len(s['caught'])
        missed = len(s['missed'])
        lost_shots = len(s['lost_shot'])

        # Potential PnL from MISSED wins (what buying the min_ask would have earned at settle)
        missed_pnl = sum(SHARES * 1.00 - SHARES * (e['min_ask'] + SLIP) for e in s['missed'])

        # Losses from fires on losing side in this bucket (wasted capital)
        wasted_on_loser = sum(SHARES * (f['min_ask'] + SLIP) * f['fires'] for f in s['lost_shot'])

        total_missed_pnl += missed_pnl
        total_caught += caught
        total_missed += missed

        flag = ""
        if missed_pnl > 5: flag = "  ⚠️ big miss"

        print(f"{bname:<12} {s['dipped_total']:<12} {caught:<12} {missed:<12} "
              f"${missed_pnl:<+18.2f} {lost_shots:<17} ${wasted_on_loser:<+18.2f}{flag}")

    print("-"*115)
    print(f"\nTOTAL MISSED POTENTIAL: ${total_missed_pnl:+.2f} across {total_missed} wins")
    print(f"TOTAL CAUGHT:           {total_caught} wins")

    # ─── Detailed breakdown of misses ───
    print("\n" + "="*100)
    print("DETAILED MISS LIST — markets where winning side dipped but nothing fired there")
    print("="*100)
    for lo, hi, bname in BUCKETS:
        s = stats[bname]
        if not s['missed']: continue
        print(f"\n{bname}:")
        # Sort by potential profit descending (cheapest ask = biggest profit)
        for e in sorted(s['missed'], key=lambda x: x['min_ask']):
            potential = SHARES * 1.00 - SHARES * (e['min_ask'] + SLIP)
            print(f"  {e['slug']} {e['side']} dipped to ${e['min_ask']:.3f} @ cd={e['cd_at_min']:.0f}  "
                  f"→ potential +${potential:.2f} missed")

    # ─── What strategies DID catch things? ───
    print("\n" + "="*100)
    print("CAUGHT BREAKDOWN — which strategies caught winning-side dips?")
    print("="*100)
    strat_catches = defaultdict(int)
    for m in markets:
        winner = m.get('winner')
        for f in m.get('fires', []):
            if f.get('side') == winner:
                b = bucket_for(f.get('entry_price', 0) or 0)
                if b:
                    strat_catches[(f['strategy'], b)] += 1

    by_strat = defaultdict(int)
    for (strat, b), n in strat_catches.items():
        by_strat[strat] += n

    print(f"\n{'strategy':<18} {'winning-side fires':<20}")
    for strat, n in sorted(by_strat.items(), key=lambda x: -x[1]):
        print(f"  {strat:<16} {n}")


if __name__ == '__main__':
    main()
