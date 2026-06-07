#!/usr/bin/env python3
"""
SHADOW AUDIT — shows where every shadow strategy stands relative to
promotion thresholds. Run on VPS:

  python3 shadow_audit.py market_recap_history.jsonl

Or with market_history_lite.jsonl (same format for fires).
"""
import json, sys, os, math
from collections import defaultdict

# ─── Promotion thresholds (must match regression_detector.py) ─────────
PROMOTION_TIER1_MU  = 0.50    # high confidence → 1/2 size
PROMOTION_TIER2_MU  = -0.10   # probable → 1/4 size
PROMOTION_TIER3_MU  = -0.30   # speculative → 1/8 size (needs n≥50)
MIN_FIRES_PROMOTE   = 30
MIN_FIRES_TIER3     = 50

# ─── STRATEGY_SIDE_MAP (must match barbell_deployment.py) ─────────────
STRATEGY_SIDE_MAP = {
    "depth_collapse_mid": "DN",
    "depth_surge": "DN",
    "fake_train_detector": "DN",
    "up_depth_collapse_mid_high": "UP",
    "up_cheap_low_flow": "UP",
    "dn_deep_spread_collapse": "DN",
    "dn_deep_ask_collapse": "DN",
    "dn_mid_gap_count_spike": "DN",
    "up_deep_spread_collapse": "UP",
    "up_deep_ask_collapse": "UP",
    "up_mid_gap_count_spike": "UP",
}

def stats(pnls):
    n = len(pnls)
    if n == 0:
        return {"n": 0, "mu": 0, "sd": 0, "wr": 0, "total": 0, "ci_lo": 0, "ci_hi": 0}
    mu = sum(pnls) / n
    wins = sum(1 for x in pnls if x > 0)
    if n > 1:
        var = sum((x - mu)**2 for x in pnls) / (n - 1)
        sd = math.sqrt(var)
        se = sd / math.sqrt(n)
        ci_lo = mu - 1.96 * se
        ci_hi = mu + 1.96 * se
    else:
        sd = 0; ci_lo = mu; ci_hi = mu
    return {"n": n, "mu": mu, "sd": sd, "wr": wins/n, "total": sum(pnls),
            "ci_lo": ci_lo, "ci_hi": ci_hi}

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "market_recap_history.jsonl"
    if not os.path.exists(path):
        print(f"ERROR: {path} not found")
        sys.exit(1)

    with open(path) as f:
        markets = [json.loads(l) for l in f if l.strip()]

    print(f"Loaded {len(markets)} markets from {path}\n")

    # ── Collect shadow fire data per strategy ─────────────────────────
    shadow_data = defaultdict(lambda: {"pnls": [], "sides": [], "prices": [], 
                                        "cds": [], "winners": []})
    live_data = defaultdict(lambda: {"pnls": [], "sides": []})

    for m in markets:
        winner = m.get("winner", "?")
        for f in m["fires"]:
            s = f["strategy"]
            side = f.get("side", "?")
            hypo = f.get("hypo_pnl", 0)
            price = f.get("entry_price", 0)
            cd = f.get("cd", 0)

            if f["mode"] == "SHADOW":
                shadow_data[s]["pnls"].append(hypo)
                shadow_data[s]["sides"].append(side)
                shadow_data[s]["prices"].append(price)
                shadow_data[s]["cds"].append(cd)
                shadow_data[s]["winners"].append(winner)
            elif f["mode"] == "LIVE":
                live_data[s]["pnls"].append(f.get("real_pnl", hypo))
                live_data[s]["sides"].append(side)

    # ── Print audit ───────────────────────────────────────────────────
    all_strats = sorted(set(list(shadow_data.keys()) + list(live_data.keys())))

    print("=" * 130)
    print(f"{'Strategy':<35}{'In Map?':<8}{'Shadow':<8}{'LIVE':<6}"
          f"{'Shadow μ':<10}{'Shadow WR':<10}{'95% CI':<22}"
          f"{'Tier':<8}{'Status'}")
    print("─" * 130)

    promotable = []
    close_to_promote = []
    negative = []
    insufficient = []

    for s in all_strats:
        sd = shadow_data.get(s, {"pnls": []})
        ld = live_data.get(s, {"pnls": []})
        st = stats(sd["pnls"])
        lt = stats(ld["pnls"])

        in_map = "YES" if s in STRATEGY_SIDE_MAP else ""
        n_shadow = st["n"]
        n_live = lt["n"]

        # Determine promotion tier
        tier = ""
        status = ""
        if n_shadow < MIN_FIRES_PROMOTE:
            tier = "—"
            remaining = MIN_FIRES_PROMOTE - n_shadow
            status = f"need {remaining} more fires"
            insufficient.append((s, st, remaining))
        elif st["mu"] >= PROMOTION_TIER1_MU:
            tier = "T1 ★★★"
            status = "READY → PROBATION (1/2 size)"
            promotable.append((s, st, 1))
        elif st["mu"] >= PROMOTION_TIER2_MU:
            tier = "T2 ★★"
            status = "READY → PROBATION (1/4 size)"
            promotable.append((s, st, 2))
        elif st["mu"] >= PROMOTION_TIER3_MU and n_shadow >= MIN_FIRES_TIER3:
            tier = "T3 ★"
            status = "READY → PROBATION (1/8 size)"
            promotable.append((s, st, 3))
        elif st["mu"] >= PROMOTION_TIER3_MU:
            remaining = MIN_FIRES_TIER3 - n_shadow
            tier = "T3?"
            status = f"T3 eligible at n≥{MIN_FIRES_TIER3} (need {remaining} more)"
            close_to_promote.append((s, st, remaining))
        elif st["mu"] < PROMOTION_TIER3_MU:
            tier = "✗"
            status = f"NEGATIVE μ — not promotable"
            negative.append((s, st))

        ci_str = f"[${st['ci_lo']:+.2f}, ${st['ci_hi']:+.2f}]" if n_shadow > 1 else "—"

        print(f"  {s:<35}{in_map:<8}{n_shadow:<8}{n_live:<6}"
              f"${st['mu']:<+9.3f}{st['wr']*100:<9.0f}%{ci_str:<22}"
              f"{tier:<8}{status}")

    # ── Summary ───────────────────────────────────────────────────────
    print(f"\n{'=' * 130}")
    print("SUMMARY")
    print(f"{'=' * 130}")

    if promotable:
        print(f"\n  🟢 READY FOR PROMOTION ({len(promotable)} strategies):")
        for s, st, tier in promotable:
            map_str = f" [in STRATEGY_SIDE_MAP]" if s in STRATEGY_SIDE_MAP else " [NOT in map — add first!]"
            print(f"     {s:<35} Tier {tier}  μ=${st['mu']:+.3f}  n={st['n']}  "
                  f"WR={st['wr']:.0%}  CI=[${st['ci_lo']:+.2f},${st['ci_hi']:+.2f}]{map_str}")

    if close_to_promote:
        print(f"\n  🟡 CLOSE TO PROMOTION ({len(close_to_promote)} strategies):")
        for s, st, remaining in sorted(close_to_promote, key=lambda x: x[2]):
            print(f"     {s:<35} μ=${st['mu']:+.3f}  n={st['n']}  need {remaining} more fires")

    n_neg = len([s for s, st in negative if st['n'] >= MIN_FIRES_PROMOTE])
    if n_neg:
        print(f"\n  🔴 NEGATIVE WITH SUFFICIENT DATA ({n_neg} strategies):")
        for s, st in negative:
            if st['n'] >= MIN_FIRES_PROMOTE:
                print(f"     {s:<35} μ=${st['mu']:+.3f}  n={st['n']}  WR={st['wr']:.0%}")

    # ── Recent 12 markets trend ───────────────────────────────────────
    print(f"\n{'=' * 130}")
    print(f"RECENT TREND (last 12 markets)")
    print(f"{'=' * 130}")

    recent_markets = markets[-12:]
    recent_shadow = defaultdict(list)
    for m in recent_markets:
        for f in m["fires"]:
            if f["mode"] == "SHADOW":
                recent_shadow[f["strategy"]].append(f.get("hypo_pnl", 0))

    print(f"\n  {'Strategy':<35}{'Last12 n':<10}{'Last12 μ':<12}{'Trend vs All'}")
    print(f"  {'─'*75}")
    for s in sorted(recent_shadow.keys(), key=lambda x: -sum(recent_shadow[x])/max(1,len(recent_shadow[x]))):
        r = recent_shadow[s]
        all_st = stats(shadow_data.get(s, {"pnls": []})["pnls"])
        r_mu = sum(r)/len(r) if r else 0
        trend = "📈 improving" if r_mu > all_st["mu"] + 0.1 else (
                "📉 declining" if r_mu < all_st["mu"] - 0.1 else "→ stable")
        if len(r) >= 3:
            print(f"  {s:<35}{len(r):<10}${r_mu:<+11.3f}{trend}")

if __name__ == "__main__":
    main()
