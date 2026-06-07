#!/usr/bin/env python3
"""
verify_buckets.py — independently verify per-bucket PnL from replay files.

Usage:
  python3 verify_buckets.py                    # runs on ./replay_*.json
  python3 verify_buckets.py /path/to/replays   # specify directory

Output: per-bucket UP/DN touched counts, wins, PnL. Plus sanity checks.
"""
import json, glob, os, sys
from collections import defaultdict

REPLAY_DIR = sys.argv[1] if len(sys.argv) > 1 else '.'
SHARES     = 5         # shares per fire — change if your bot uses different sizing
SLIP       = 0.01      # per-share slippage ($0.01)

# ───── DATA QUALITY FILTERS (critical!) ─────
# Old replays had sparse tick data (1Hz) and often missed market openings.
# Set these to filter out low-quality replays that bias results.
MIN_TICKS          = 1500  # require ≥1500 ticks (newer markets have 1500-2800)
MAX_FIRST_CD_SKIP  = 15    # require first tick within 15s of market open
MAX_TICK_GAP       = 10    # reject markets with any tick-gap > 10s
FILTER_QUALITY     = True  # set False to include all markets

BUCKETS = [(0.01, 0.05, '0.01-0.05'), (0.05, 0.10, '0.05-0.10'),
           (0.10, 0.15, '0.10-0.15'), (0.15, 0.20, '0.15-0.20'),
           (0.20, 0.25, '0.20-0.25'), (0.25, 0.30, '0.25-0.30'),
           (0.30, 0.35, '0.30-0.35'), (0.35, 0.40, '0.35-0.40'),
           (0.40, 0.45, '0.40-0.45'), (0.45, 0.50, '0.45-0.50')]


def bucket_for(price):
    if price is None: return None
    for lo, hi, name in BUCKETS:
        if lo <= price < hi:
            return name
    return None


def main():
    replays = sorted(glob.glob(os.path.join(REPLAY_DIR, 'replay_*.json')))
    print(f"Scanning: {REPLAY_DIR}")
    print(f"Found {len(replays)} replay files")
    if not replays:
        print("No replays found. Specify a directory containing replay_*.json files.")
        return

    # stats[bucket]['UP'/'DN'] = {'touched': n, 'won': n, 'pnl': $, 'wins_from_dip': n}
    stats = defaultdict(lambda: {
        'UP': {'touched': 0, 'won': 0, 'pnl': 0.0, 'asks': []},
        'DN': {'touched': 0, 'won': 0, 'pnl': 0.0, 'asks': []}
    })

    winners = {'UP': 0, 'DN': 0, 'OTHER': 0}
    skipped = 0
    quality_rejected = 0

    for path in replays:
        try:
            with open(path) as f:
                r = json.load(f)
        except Exception as e:
            print(f"  SKIP (bad JSON): {path}: {e}")
            skipped += 1
            continue

        winner = r.get('winner')
        if winner not in ('UP', 'DN'):
            winners['OTHER'] += 1
            continue

        cols = r.get('tick_columns', [])
        try:
            cd_i = cols.index('cd')
            ua_i = cols.index('up_ask')
            da_i = cols.index('dn_ask')
        except ValueError as e:
            skipped += 1
            continue

        # ───── Quality filtering ─────
        if FILTER_QUALITY:
            ticks = r.get('ticks', [])
            valid = [t for t in ticks if t and len(t) > cd_i and t[cd_i] is not None]
            if len(valid) < MIN_TICKS:
                quality_rejected += 1
                continue
            cds = [t[cd_i] for t in valid]
            first_cd = cds[0] if cds else 0
            if first_cd < (300 - MAX_FIRST_CD_SKIP):
                quality_rejected += 1
                continue
            intervals = [abs(cds[i] - cds[i+1]) for i in range(len(cds)-1)]
            if intervals and max(intervals) > MAX_TICK_GAP:
                quality_rejected += 1
                continue

        winners[winner] += 1

        # For each bucket, find EARLIEST tick where ask is in that bucket
        # (earliest = highest cd, since cd counts down from 300 to 0)
        first_up = {}  # bucket -> (cd, ask)
        first_dn = {}

        for t in r.get('ticks', []):
            if not t: continue
            if cd_i >= len(t) or ua_i >= len(t) or da_i >= len(t): continue
            cd = t[cd_i]; ua = t[ua_i]; da = t[da_i]
            if cd is None: continue

            # Guard against bogus zero values — only count if ask is a valid positive price
            if ua is not None and 0 < ua < 1:
                b = bucket_for(ua)
                if b and (b not in first_up or cd > first_up[b][0]):
                    first_up[b] = (cd, ua)
            if da is not None and 0 < da < 1:
                b = bucket_for(da)
                if b and (b not in first_dn or cd > first_dn[b][0]):
                    first_dn[b] = (cd, da)

        # Record first-visit entries per bucket
        for bname, (cd, ask) in first_up.items():
            s = stats[bname]['UP']
            s['touched'] += 1
            s['asks'].append(ask)
            cost = SHARES * (ask + SLIP)
            settle = SHARES if winner == 'UP' else 0
            s['pnl'] += (settle - cost)
            if winner == 'UP': s['won'] += 1

        for bname, (cd, ask) in first_dn.items():
            s = stats[bname]['DN']
            s['touched'] += 1
            s['asks'].append(ask)
            cost = SHARES * (ask + SLIP)
            settle = SHARES if winner == 'DN' else 0
            s['pnl'] += (settle - cost)
            if winner == 'DN': s['won'] += 1

    # ───────── Print results ─────────
    N = winners['UP'] + winners['DN']
    print(f"\nValid markets: {N} (UP winners: {winners['UP']}, DN winners: {winners['DN']})")
    if FILTER_QUALITY:
        print(f"Quality-rejected: {quality_rejected}  (require: ≥{MIN_TICKS} ticks, "
              f"first_cd ≥ {300-MAX_FIRST_CD_SKIP}, max_gap ≤ {MAX_TICK_GAP}s)")
    if skipped: print(f"Skipped (schema/bad JSON): {skipped}")

    print("\n" + "="*100)
    print("BUCKET PnL TABLE")
    print("="*100)
    hdr = f"{'Bucket':<12} {'UP_t':<5} {'UP_W':<5} {'UP_WR':<7} {'UP_PnL':<10} " \
          f"{'DN_t':<5} {'DN_W':<5} {'DN_WR':<7} {'DN_PnL':<10}"
    print(hdr); print("-"*90)

    tot_up, tot_dn = 0.0, 0.0
    for lo, hi, bname in BUCKETS:
        u = stats[bname]['UP']; d = stats[bname]['DN']
        u_wr = f"{u['won']/u['touched']*100:.0f}%" if u['touched'] else '-'
        d_wr = f"{d['won']/d['touched']*100:.0f}%" if d['touched'] else '-'
        print(f"{bname:<12} {u['touched']:<5} {u['won']:<5} {u_wr:<7} ${u['pnl']:<+8.2f} "
              f"{d['touched']:<5} {d['won']:<5} {d_wr:<7} ${d['pnl']:<+8.2f}")
        tot_up += u['pnl']; tot_dn += d['pnl']

    print("-"*90)
    print(f"{'TOTAL':<12} {'':<5} {'':<5} {'':<7} ${tot_up:<+8.2f} {'':<5} {'':<5} {'':<7} ${tot_dn:<+8.2f}")
    print(f"NET: ${tot_up + tot_dn:+.2f}")

    # ───────── Sanity checks ─────────
    print("\n" + "="*100)
    print("SANITY CHECKS")
    print("="*100)

    print("\n1. Touch symmetry — UP and DN should touch each bucket similar # of times:")
    print(f"   {'Bucket':<12} {'UP_touched':<12} {'DN_touched':<12} {'diff':<8}")
    for lo, hi, bname in BUCKETS:
        u_t = stats[bname]['UP']['touched']
        d_t = stats[bname]['DN']['touched']
        flag = '  ⚠️' if abs(u_t - d_t) > 3 else ''
        print(f"   {bname:<12} {u_t:<12} {d_t:<12} {u_t-d_t:+<6}{flag}")

    print("\n2. Win distribution — wins-per-bucket should scale with winner counts:")
    total_up_wins = sum(stats[b]['UP']['won'] for _,_,b in BUCKETS)
    total_dn_wins = sum(stats[b]['DN']['won'] for _,_,b in BUCKETS)
    print(f"   Total UP bucket-wins across all buckets: {total_up_wins}")
    print(f"   Total DN bucket-wins across all buckets: {total_dn_wins}")
    print(f"   Expected ratio (winner counts): UP/DN = {winners['UP']}/{winners['DN']} = "
          f"{winners['UP']/max(winners['DN'],1):.3f}")
    print(f"   Actual ratio of bucket-wins: {total_up_wins/max(total_dn_wins,1):.3f}")
    print(f"   (Should be close — if very different, winning markets had asymmetric volatility)")

    print("\n3. Volatility asymmetry — how often does each side dip to a cheap bucket AND win?")
    print(f"   {'Bucket':<12} {'UP_dip_wins':<14} {'DN_dip_wins':<14} {'asymmetry':<20}")
    for lo, hi, bname in BUCKETS:
        if hi > 0.30: continue  # only interesting for cheap buckets
        u_w = stats[bname]['UP']['won']; d_w = stats[bname]['DN']['won']
        u_pct = f"{u_w}/{winners['UP']} = {u_w/max(winners['UP'],1)*100:.0f}%"
        d_pct = f"{d_w}/{winners['DN']} = {d_w/max(winners['DN'],1)*100:.0f}%"
        note = ''
        if winners['UP'] and winners['DN']:
            u_rate = u_w/winners['UP']; d_rate = d_w/winners['DN']
            if abs(u_rate - d_rate) > 0.10: note = '  ⚠️ asymmetric'
        print(f"   {bname:<12} {u_pct:<14} {d_pct:<14} {note}")

    print("\n4. Ask price distribution within buckets — should be similar for UP vs DN:")
    print(f"   {'Bucket':<12} {'UP_median_ask':<16} {'DN_median_ask':<16}")
    for lo, hi, bname in BUCKETS:
        u_asks = sorted(stats[bname]['UP']['asks'])
        d_asks = sorted(stats[bname]['DN']['asks'])
        u_med = f"${u_asks[len(u_asks)//2]:.4f}" if u_asks else 'n/a'
        d_med = f"${d_asks[len(d_asks)//2]:.4f}" if d_asks else 'n/a'
        print(f"   {bname:<12} {u_med:<16} {d_med:<16}")

    # ───────── Simple recomputation from raw formulas for transparency ─────────
    print("\n" + "="*100)
    print("FORMULA TRANSPARENCY")
    print("="*100)
    print(f"Per-fire cost  = SHARES × (ask + SLIP) = {SHARES} × (ask + ${SLIP})")
    print(f"Per-fire settle = SHARES if side==winner else 0 = ${SHARES} or $0")
    print(f"Per-fire pnl   = settle - cost")
    print(f"\nExample: UP first-visit at $0.22, UP wins market")
    print(f"  cost  = 5 × ($0.22 + $0.01) = $1.15")
    print(f"  settle = $5.00")
    print(f"  pnl   = $5.00 - $1.15 = +$3.85")
    print(f"\nExample: UP first-visit at $0.22, DN wins market")
    print(f"  cost  = 5 × ($0.22 + $0.01) = $1.15")
    print(f"  settle = $0.00")
    print(f"  pnl   = -$1.15")


if __name__ == '__main__':
    main()
