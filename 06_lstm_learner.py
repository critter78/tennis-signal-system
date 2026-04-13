import json, argparse, numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime

LOGS_DIR = Path("logs"); MODELS_DIR = Path("models")
LSTM_MODEL = MODELS_DIR / "lstm_adjuster.pkl"
LSTM_META = MODELS_DIR / "lstm_meta.json"
PICKS_LOG = LOGS_DIR / "picks.jsonl"
MIN_PICKS = 50; SEQ_LEN = 20

def load_resolved_picks():
    if not PICKS_LOG.exists(): return []
    picks = []
    with open(PICKS_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                p = json.loads(line)
                if p.get("outcome") is not None: picks.append(p)
    return sorted(picks, key=lambda x: x.get("logged_at",""))

def show_status():
    total_picks = 0
    if PICKS_LOG.exists():
        with open(PICKS_LOG) as f: total_picks = sum(1 for line in f if line.strip())
    picks = load_resolved_picks()
    print("\n" + "="*60)
    print("  LSTM LEARNER STATUS")
    print("="*60)
    print(f"  Total picks logged:    {total_picks}")
    print(f"  Resolved picks:        {len(picks)}")
    print(f"  Unresolved:            {total_picks - len(picks)}")
    print(f"  Min to train:          {MIN_PICKS}")
    ready = len(picks) >= MIN_PICKS
    print(f"  Ready to train:        {'YES' if ready else f'Need {MIN_PICKS - len(picks)} more'}")
    if LSTM_META.exists():
        with open(LSTM_META) as f: meta = json.load(f)
        print(f"\n  Model type:            {meta.get('type','unknown')}")
        print(f"  Trained at:            {meta.get('trained_at','unknown')}")
        print(f"  Trained on:            {meta.get('n_picks',0)} picks")
        val = meta.get("best_val_loss") or meta.get("val_mse","N/A")
        print(f"  Validation loss:       {val}")
        new_since = len(picks) - meta.get("n_picks",0)
        print(f"  New picks since train: {new_since}")
        if new_since >= 25: print(f"  -> Recommend retraining")
    else:
        print(f"\n  No LSTM model trained yet.")
    if picks:
        last10 = picks[-10:]
        streak = "".join("W" if p["outcome"]=="win" else "L" for p in last10)
        wr = sum(1 for p in last10 if p["outcome"]=="win") / len(last10)
        print(f"\n  Last 10 picks:         {streak}")
        print(f"  Last 10 win rate:      {wr:.0%}")
    print()

def train_lstm():
    picks = load_resolved_picks()
    if len(picks) < MIN_PICKS:
        print(f"  Need {MIN_PICKS} resolved picks to train. Have {len(picks)}.")
        print(f"  Keep logging picks - LSTM activates automatically.")
        return
    print(f"  Training LSTM on {len(picks)} resolved picks...")
    print(f"  (Full training runs after enough data accumulates)")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["train","status"])
    args = parser.parse_args()
    if args.action == "train": train_lstm()
    elif args.action == "status": show_status()
