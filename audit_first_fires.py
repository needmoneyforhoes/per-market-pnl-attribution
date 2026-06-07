# audit_first_fires.py — fixed cutoff
import json, time

# Use bot deployment time. Adjust if you launched at a different hour.
DEPLOY_TS = time.time() - 3600  # last hour only

fires = []
with open('market_recap_history.jsonl') as f:
    for line in f:
        try: r = json.loads(line)
        except: continue
        if r.get('ts', 0) < DEPLOY_TS: continue
        for fire in r.get('fires', []):
            if (fire.get('strategy') == 'depth_collapse_mid'
                and fire.get('mode') == 'LIVE'
                and fire.get('cd', 0) >= 15):
                fires.append({
                    'ts': r.get('ts'), 'side': fire.get('side'),
                    'cd': fire.get('cd'), 'entry': fire.get('entry_price'),
                    'shares': fire.get('shares'),
                    'hypo': fire.get('hypo_pnl', 0), 'real': fire.get('real_pnl', 0),
                })

print(f"Post-deploy LIVE fires: {len(fires)}")
if not fires:
    print("None yet — bot is still ramping up. Check again in 30 min.")
else:
    print(f"{'#':>2} {'side':>4} {'cd':>5} {'entry':>6} {'shares':>7} {'hypo':>8} {'real':>8}")
    for i, f in enumerate(fires, 1):
        print(f"{i:>2} {f['side']:>4} {f['cd']:>5.0f} {f['entry']:>6.3f} {f['shares']:>7.2f} ${f['hypo']:>+6.2f} ${f['real']:>+6.2f}")
    
    n_up = sum(1 for f in fires if f['side']=='UP')
    n_dn = sum(1 for f in fires if f['side']=='DN')
    print(f"\nUP fires: {n_up}, DN fires: {n_dn}")
    if n_up > 0 and n_dn > 0:
        print("✅ BOTH sides firing — Path C v3 wired correctly")
    elif n_dn > 0 and n_up == 0:
        print("⚠️  Only DN firing — check STRATEGY_SIDE_MAP['depth_collapse_mid'] = 'BOTH'")
