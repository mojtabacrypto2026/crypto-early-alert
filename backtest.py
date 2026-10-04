# -*- coding: utf-8 -*-
"""NOBITEX EARLY ALERT V5.1 - scan-history replay.
Uses recorded scan snapshots to test whether a candidate was followed by a move.
This is deliberately labeled replay, not a full candle-level backtest.
"""
import json, os, time

DATASET_FILE="nobitex_early_radar_dataset_v51.jsonl"
HORIZONS=(15*60,30*60,60*60,2*60*60,4*60*60)

def load_rows():
    rows=[]
    if not os.path.exists(DATASET_FILE): return rows
    with open(DATASET_FILE,encoding="utf-8") as f:
        for line in f:
            try:
                x=json.loads(line); x["timestamp"]=int(x.get("timestamp",0)); x["price"]=float(x.get("price",0)); rows.append(x)
            except Exception: pass
    return rows

def main():
    rows=load_rows(); rows.sort(key=lambda x:x["timestamp"])
    print("="*70); print("NOBITEX V5.1 SCAN-HISTORY REPLAY"); print("="*70)
    if not rows:
        print("No dataset yet. Run scanner for several scans first."); return
    candidates=[r for r in rows if r.get("confirmed") or r.get("fast_pre_move") or r.get("micro_early") or r.get("shadow_pre_move")]
    by_type={k:[] for k in ("CONFIRMED","FAST","MICRO","SHADOW")}
    for r in candidates:
        typ="CONFIRMED" if r.get("confirmed") else "FAST" if r.get("fast_pre_move") else "MICRO" if r.get("micro_early") else "SHADOW"
        p=r["price"]; t=r["timestamp"]
        future=[x for x in rows if x.get("symbol")==r.get("symbol") and x["timestamp"]>t and x["timestamp"]<=t+HORIZONS[-1] and x["price"]>0]
        if not future or p<=0: continue
        mfe=max((x["price"]-p)/p*100 for x in future)
        first2=None; lead=None
        for x in future:
            ret=(x["price"]-p)/p*100
            if ret>=2 and first2 is None: first2=x["timestamp"]-t
        by_type[typ].append((mfe,first2))
    for typ, vals in by_type.items():
        if not vals: print(f"{typ:10} n=0"); continue
        hit=sum(v[0]>=2 for v in vals)/len(vals)*100
        leads=[v[1]/60 for v in vals if v[1] is not None]
        print(f"{typ:10} n={len(vals):4}  MFE>=2={hit:5.1f}%  avg lead-to-+2={sum(leads)/len(leads):.1f}m" if leads else f"{typ:10} n={len(vals):4}  MFE>=2={hit:5.1f}%  +2 not observed")
    print("\nWARNING: replay uses recorded scan prices, so it is not equivalent to a tick/candle backtest.")

if __name__ == "__main__": main()
