"""
V1 vs V2 MODEL COMPARISON — ISOLATED FROM PRODUCTION
====================================================

Trains two models on the same architecture and compares on the same holdout:

  V1 = trained on v1 parquet (Sackmann 2018-2024, 36K matches)
  V2 = trained on v2 parquet (TML 2019-2026 tour+challenger + WTA, 72K matches)
       filtered to date < 2025-10-01 for training

Holdout window: 2025-10-01 → 2026-04-12 (ATP tour-level matches only)
  → Every test match comes from v2 parquet. V1 has zero training exposure to 2025+.
  → For each test match, features are built using each model's OWN source parquet.
    (V1 features use v1's frozen state; V2 features see through 2025-09-30.)

Reports AUC, Brier, log-loss, calibration, and a disagreement spread.

Does NOT touch any production file. Writes outputs under v2_research/.
"""

import sys, json, pickle, time
from pathlib import Path
from datetime import timedelta
from typing import Optional

import numpy as np
import pandas as pd

from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import log_loss, brier_score_loss, roc_auc_score

import warnings
warnings.filterwarnings("ignore")

# ─── PATHS ────────────────────────────────────────────────────────────────────
V2_ROOT   = Path(__file__).parent
V1_PQT    = V2_ROOT.parent / "data" / "raw" / "matches_combined.parquet"   # READ-ONLY
V2_PQT    = V2_ROOT / "data" / "matches_combined_v2.parquet"
REPORT_MD = V2_ROOT / "reports" / "model_metrics.md"
V1_MODEL  = V2_ROOT / "models" / "baseline_v1.pkl"
V2_MODEL  = V2_ROOT / "models" / "tennis_xgb_v2.pkl"

HOLDOUT_START = pd.Timestamp("2025-10-01")
HOLDOUT_END   = pd.Timestamp("2026-04-12")

# Randomness
RNG = np.random.default_rng(42)

# ─── FEATURE ENGINEERING (copied from 02_features_and_train.py for isolation) ──

SURFACE_MAP = {"Hard": 0, "Clay": 1, "Grass": 2, "Carpet": 3}
ROUND_MAP   = {"R128": 1, "R64": 2, "R32": 3, "R16": 4,
               "QF": 5, "SF": 6, "F": 7, "RR": 4, "BR": 5}

def _encode_level(level: str) -> int:
    # V2 adds "C" (challenger), "250", "500" codes
    mapping = {"G": 4, "M": 3, "500": 3, "250": 2, "A": 2,
               "D": 1, "F": 3, "C": 0, "O": 2, "": 2}
    return mapping.get(str(level), 2)

def _safe_diff(a, b):
    if pd.isna(a) or pd.isna(b):
        return np.nan
    return a - b

def _estimate_sets(matches_df: pd.DataFrame, player: str) -> int:
    if "score" not in matches_df.columns or len(matches_df) == 0:
        return 0
    total = 0
    for score in matches_df["score"].astype(str):
        total += sum(1 for s in score.split() if "-" in s and "[" not in s)
    return total


class PlayerStatsEngine:
    """
    Pre-indexed player-match lookup + H2H lookup. Single pass builds per-player
    and per-pair DataFrames. Per-match lookups become simple date filters.
    """

    def __init__(self, df: pd.DataFrame):
        self.df = df.copy()
        self.df["date"] = pd.to_datetime(self.df["date"], errors="coerce")
        self.df = self.df.dropna(subset=["date"]).sort_values("date")
        # Per-player indices
        self._by_winner = {n: g for n, g in self.df.groupby("winner", sort=False)}
        self._by_loser  = {n: g for n, g in self.df.groupby("loser",  sort=False)}
        # Per-pair H2H index — vectorized sorted-string key (fast)
        pair_df = self.df[["winner", "loser", "date", "surface"]].dropna(subset=["winner", "loser"]).copy()
        w = pair_df["winner"].astype(str).values
        l = pair_df["loser"].astype(str).values
        lo = np.where(w < l, w, l)
        hi = np.where(w < l, l, w)
        pair_df["pkey"] = [f"{a}|{b}" for a, b in zip(lo, hi)]
        self._by_pair = {k: g for k, g in pair_df.groupby("pkey", sort=False)}

    def h2h(self, p1: str, p2: str, as_of_date, surface: Optional[str] = None) -> dict:
        a, b = str(p1), str(p2)
        key = f"{a}|{b}" if a < b else f"{b}|{a}"
        g = self._by_pair.get(key)
        if g is None or len(g) == 0:
            return {"h2h_total": 0, "h2h_p1_wins": 0.5, "h2h_surf_p1": 0.5, "h2h_advantage": 0}
        cutoff = pd.Timestamp(as_of_date)
        g = g[g["date"] < cutoff]
        total = len(g)
        if total == 0:
            return {"h2h_total": 0, "h2h_p1_wins": 0.5, "h2h_surf_p1": 0.5, "h2h_advantage": 0}
        p1_wins = int((g["winner"] == p1).sum())
        if surface:
            gs = g[g["surface"] == surface]
            surf_t = len(gs)
            p1_surf = int((gs["winner"] == p1).sum())
        else:
            surf_t = total; p1_surf = p1_wins
        return {
            "h2h_total":     total,
            "h2h_p1_wins":   p1_wins / total,
            "h2h_surf_p1":   p1_surf / surf_t if surf_t > 0 else 0.5,
            "h2h_advantage": int(p1_wins > total / 2),
        }

    def get_stats(self, player: str, as_of_date, surface: str,
                  window_days: int = 365) -> dict:
        cutoff = pd.Timestamp(as_of_date)
        since  = cutoff - timedelta(days=window_days)

        wins = self._by_winner.get(player, self.df.iloc[0:0])
        wins = wins[(wins["date"] < cutoff) & (wins["date"] >= since)]
        losses = self._by_loser.get(player, self.df.iloc[0:0])
        losses = losses[(losses["date"] < cutoff) & (losses["date"] >= since)]

        n_wins, n_losses = len(wins), len(losses)
        n_total = n_wins + n_losses

        s_wins   = wins[wins["surface"] == surface]
        s_losses = losses[losses["surface"] == surface]
        s_total  = len(s_wins) + len(s_losses)

        all_m = pd.concat([wins, losses]).sort_values("date")
        last10 = all_m.tail(10); last20 = all_m.tail(20)
        l10_w = int((last10["winner"] == player).sum())
        l20_w = int((last20["winner"] == player).sum())

        recent = all_m[all_m["date"] >= cutoff - timedelta(days=7)]
        sets_last_7d = _estimate_sets(recent, player)
        days_since_last = int((cutoff - all_m["date"].max()).days) if n_total > 0 else 30

        def mean_stat(src, col):
            if col not in src.columns or len(src) == 0:
                return np.nan
            return pd.to_numeric(src[col], errors="coerce").dropna().mean()

        return {
            "win_rate_52w":       n_wins / n_total if n_total > 5 else np.nan,
            "win_rate_surf_52w":  len(s_wins) / s_total if s_total > 3 else np.nan,
            "win_rate_l10":       l10_w / len(last10) if len(last10) > 0 else np.nan,
            "win_rate_l20":       l20_w / len(last20) if len(last20) > 0 else np.nan,
            "n_matches_52w":      n_total,
            "n_matches_surf_52w": s_total,
            "days_since_last":    days_since_last,
            "sets_last_7d":       sets_last_7d,
            "ace_rate_w":         mean_stat(wins, "w_ace"),
            "df_rate_w":          mean_stat(wins, "w_df"),
            "first_in_pct_w":     mean_stat(wins, "w_1stIn"),
            "win_on_1st_w":       mean_stat(wins, "w_1stWon"),
            "win_on_2nd_w":       mean_stat(wins, "w_2ndWon"),
            "bp_saved_pct_w":     mean_stat(wins, "w_bpSaved"),
            "ace_rate_l":         mean_stat(losses, "l_ace"),
            "bp_conv_l":          mean_stat(losses, "l_bpFaced"),
        }


def get_h2h(df: pd.DataFrame, p1: str, p2: str,
            as_of_date, surface: Optional[str] = None) -> dict:
    cutoff = pd.Timestamp(as_of_date)
    mask = (
        ((df["winner"] == p1) & (df["loser"] == p2)) |
        ((df["winner"] == p2) & (df["loser"] == p1))
    ) & (df["date"] < cutoff)
    h2h = df[mask]
    h2h_surf = h2h[h2h["surface"] == surface] if surface else h2h
    p1_wins = int((h2h["winner"] == p1).sum())
    total   = len(h2h)
    p1_surf = int((h2h_surf["winner"] == p1).sum())
    surf_t  = len(h2h_surf)
    return {
        "h2h_total":    total,
        "h2h_p1_wins":  p1_wins / total if total > 0 else 0.5,
        "h2h_surf_p1":  p1_surf / surf_t if surf_t > 0 else 0.5,
        "h2h_advantage": int(p1_wins > (total / 2)) if total > 0 else 0,
    }


FEATURE_COLS = [
    "surface", "tour", "round", "tourney_level",
    "rank_diff", "rank_ratio", "log_rank_ratio",
    "a_win_rate_52w", "b_win_rate_52w",
    "a_win_rate_surf_52w", "b_win_rate_surf_52w",
    "a_win_rate_l10", "b_win_rate_l10",
    "a_win_rate_l20", "b_win_rate_l20",
    "form_diff_52w", "form_diff_surf",
    "a_days_since_last", "b_days_since_last",
    "a_sets_last_7d", "b_sets_last_7d", "fatigue_diff",
    "h2h_total", "h2h_p1_wins", "h2h_surf_p1", "h2h_advantage",
    "ace_diff", "df_diff", "first_in_diff", "win1st_diff", "win2nd_diff",
    "a_n_matches_52w", "b_n_matches_52w",
]

MODEL_PARAMS = dict(
    max_iter=600,            # trimmed from 800 to keep runtime <15min
    max_depth=5,
    learning_rate=0.03,
    max_leaf_nodes=31,
    min_samples_leaf=10,
    l2_regularization=1.0,
    random_state=42,
    verbose=0,
)


def build_features(df_matches: pd.DataFrame,
                   stats_source: pd.DataFrame,
                   label_name: str,
                   engine: Optional["PlayerStatsEngine"] = None,
                   deterministic_flip: bool = False) -> pd.DataFrame:
    """
    For each match in `df_matches`, build a feature row using `stats_source`
    for the PlayerStatsEngine and H2H lookups.

    deterministic_flip=True uses hash(match_key) % 2 for flip — ensures two
    different stats sources produce the SAME pa/pb assignment for the same
    match. Required for A/B test-set comparison.
    """
    if engine is None:
        engine = PlayerStatsEngine(stats_source)
    rows = []
    n = len(df_matches)
    t0 = time.time()
    for i, (_, m) in enumerate(df_matches.iterrows()):
        if i % 500 == 0:
            print(f"    [{label_name}] {i:,}/{n:,} "
                  f"({(i/max(n,1))*100:.1f}%, {time.time()-t0:.0f}s)", flush=True)
        w = m.get("winner"); l_ = m.get("loser"); date = m.get("date")
        if pd.isna(date) or not w or not l_ or str(w) == "nan" or str(l_) == "nan":
            continue
        surface = m.get("surface", "Hard")
        tour    = m.get("tour", "atp")

        if deterministic_flip:
            # Stable flip based on match identity — same result regardless of stats source
            mk = f"{w}|{l_}|{pd.Timestamp(date).date()}"
            flip = (hash(mk) & 1) == 1
        else:
            flip = RNG.random() < 0.5
        pa = l_ if flip else w
        pb = w  if flip else l_
        label = 0 if flip else 1

        sa = engine.get_stats(pa, date, surface)
        sb = engine.get_stats(pb, date, surface)
        h2 = engine.h2h(pa, pb, date, surface)

        try:
            ra = float(m.get("w_rank" if not flip else "l_rank"))
            rb = float(m.get("l_rank" if not flip else "w_rank"))
        except (TypeError, ValueError):
            ra = rb = np.nan

        rank_diff = ra - rb if not (pd.isna(ra) or pd.isna(rb)) else np.nan
        rank_ratio = ra / rb if rb and rb > 0 and not pd.isna(ra) else np.nan
        log_rank_ratio = (np.log(ra / rb)
                          if ra and rb and ra > 0 and rb > 0 else np.nan)

        rows.append({
            "date":   date,
            "surface": SURFACE_MAP.get(surface, 0),
            "tour":   1 if tour == "wta" else 0,
            "round":  ROUND_MAP.get(str(m.get("round", "")), 3),
            "tourney_level": _encode_level(m.get("tourney_level", "")),
            "rank_a": ra, "rank_b": rb,
            "rank_diff": rank_diff, "rank_ratio": rank_ratio,
            "log_rank_ratio": log_rank_ratio,
            "a_win_rate_52w": sa["win_rate_52w"], "b_win_rate_52w": sb["win_rate_52w"],
            "a_win_rate_surf_52w": sa["win_rate_surf_52w"], "b_win_rate_surf_52w": sb["win_rate_surf_52w"],
            "a_win_rate_l10": sa["win_rate_l10"], "b_win_rate_l10": sb["win_rate_l10"],
            "a_win_rate_l20": sa["win_rate_l20"], "b_win_rate_l20": sb["win_rate_l20"],
            "form_diff_52w": _safe_diff(sa["win_rate_52w"], sb["win_rate_52w"]),
            "form_diff_surf": _safe_diff(sa["win_rate_surf_52w"], sb["win_rate_surf_52w"]),
            "a_days_since_last": sa["days_since_last"], "b_days_since_last": sb["days_since_last"],
            "a_sets_last_7d":   sa["sets_last_7d"],   "b_sets_last_7d":   sb["sets_last_7d"],
            "fatigue_diff":     sa["sets_last_7d"] - sb["sets_last_7d"],
            "h2h_total": h2["h2h_total"], "h2h_p1_wins": h2["h2h_p1_wins"],
            "h2h_surf_p1": h2["h2h_surf_p1"], "h2h_advantage": h2["h2h_advantage"],
            "ace_diff":      _safe_diff(sa["ace_rate_w"], sb["ace_rate_w"]),
            "df_diff":       _safe_diff(sa["df_rate_w"], sb["df_rate_w"]),
            "first_in_diff": _safe_diff(sa["first_in_pct_w"], sb["first_in_pct_w"]),
            "win1st_diff":   _safe_diff(sa["win_on_1st_w"], sb["win_on_1st_w"]),
            "win2nd_diff":   _safe_diff(sa["win_on_2nd_w"], sb["win_on_2nd_w"]),
            "a_n_matches_52w": sa["n_matches_52w"], "b_n_matches_52w": sb["n_matches_52w"],
            "label": label,
            "winner_actual": w, "loser_actual": l_,
            "match_date": date,
        })
    print(f"    [{label_name}] ✓ {len(rows):,} rows built in {time.time()-t0:.0f}s")
    return pd.DataFrame(rows)


# ─── TRAINING ────────────────────────────────────────────────────────────────

def train_calibrated(X, y):
    base = HistGradientBoostingClassifier(**MODEL_PARAMS)
    base.fit(X, y)
    cal = CalibratedClassifierCV(base, method="isotonic", cv=3)
    cal.fit(X, y)
    return cal, base


def score(model, X, y, label):
    probs = model.predict_proba(X)[:, 1]
    out = {
        "label":   label,
        "n":       len(y),
        "auc":     round(float(roc_auc_score(y, probs)), 4),
        "brier":   round(float(brier_score_loss(y, probs)), 4),
        "logloss": round(float(log_loss(y, probs)), 4),
        "accuracy@0.5": round(float(((probs > 0.5) == y.astype(bool)).mean()), 4),
    }
    return out, probs


def calibration_bins(probs, labels, n_bins=10):
    bins = np.linspace(0, 1, n_bins + 1)
    rows = []
    for i in range(n_bins):
        lo, hi = bins[i], bins[i+1]
        mask = (probs >= lo) & (probs < hi) if i < n_bins - 1 else (probs >= lo) & (probs <= hi)
        if mask.sum() == 0:
            rows.append({"bin": f"{lo:.2f}-{hi:.2f}", "n": 0, "pred": np.nan, "actual": np.nan})
        else:
            rows.append({
                "bin":    f"{lo:.2f}-{hi:.2f}",
                "n":      int(mask.sum()),
                "pred":   round(float(probs[mask].mean()), 3),
                "actual": round(float(labels[mask].mean()), 3),
            })
    return pd.DataFrame(rows)


# ─── MAIN ────────────────────────────────────────────────────────────────────

def _normalize_v1(df):
    """V1 parquet uses the production normalization. Make sure date is datetime."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if "tour" not in df.columns:
        df["tour"] = "atp"
    return df


def main():
    print("=" * 72)
    print("  V1 vs V2 COMPARISON — same architecture, different data")
    print("=" * 72)

    # Load both parquets
    print("\n[load] v1 parquet + v2 parquet")
    v1_df = _normalize_v1(pd.read_parquet(V1_PQT))
    v2_df = pd.read_parquet(V2_PQT)
    v2_df["date"] = pd.to_datetime(v2_df["date"], errors="coerce")
    print(f"    v1: {len(v1_df):,} matches ({v1_df['date'].min().date()} → {v1_df['date'].max().date()})")
    print(f"    v2: {len(v2_df):,} matches ({v2_df['date'].min().date()} → {v2_df['date'].max().date()})")

    # ── Define holdout ───────────────────────────────────────────────────────
    # Test set = ATP tour-level matches from v2 in [HOLDOUT_START, HOLDOUT_END].
    # Exclude challenger from the test set — we evaluate the Polymarket-relevant surface.
    test_mask = (
        (v2_df["date"] >= HOLDOUT_START)
        & (v2_df["date"] <= HOLDOUT_END)
        & (v2_df["tour"] == "atp")
        & (v2_df["is_challenger"] == False)
    )
    test_df = v2_df[test_mask].copy()
    print(f"\n[holdout] {len(test_df):,} ATP tour matches from "
          f"{HOLDOUT_START.date()} → {HOLDOUT_END.date()}")

    # ── Training sets ────────────────────────────────────────────────────────
    # V1 training: everything in v1 parquet (all < 2025 by definition)
    v1_train_src = v1_df.copy()
    # V2 training: v2 parquet strictly before holdout start
    v2_train_src = v2_df[v2_df["date"] < HOLDOUT_START].copy()

    print(f"\n[train] v1 source: {len(v1_train_src):,} matches (through {v1_train_src['date'].max().date()})")
    print(f"[train] v2 source: {len(v2_train_src):,} matches (through {v2_train_src['date'].max().date()})")

    # ── Build engines ONCE per source (indexing is expensive; reuse) ─────────
    print("\n[index] Building V1 stats engine…")
    v1_engine = PlayerStatsEngine(v1_train_src)
    print("[index] Building V2 stats engine…")
    v2_engine = PlayerStatsEngine(v2_train_src)

    # ── Build V1 training features ───────────────────────────────────────────
    # Sample to ~2500 matches to keep runtime manageable — HGB has plenty of signal at this size
    TRAIN_SAMPLE_N = 2500
    v1_train_sample = v1_train_src.sample(n=min(TRAIN_SAMPLE_N, len(v1_train_src)), random_state=42)
    print(f"\n[v1] Building TRAIN features on {len(v1_train_sample):,} sampled matches…")
    v1_train_feat = build_features(v1_train_sample, v1_train_src, label_name="v1-train", engine=v1_engine)
    v1_train_feat = v1_train_feat.sort_values("date").reset_index(drop=True)
    v1_train_feat = v1_train_feat.dropna(subset=["rank_a", "rank_b"])
    print(f"    usable: {len(v1_train_feat):,}")

    # ── Build V2 training features ───────────────────────────────────────────
    v2_train_sample = v2_train_src.sample(n=min(TRAIN_SAMPLE_N, len(v2_train_src)), random_state=42)
    print(f"\n[v2] Building TRAIN features on {len(v2_train_sample):,} sampled matches…")
    v2_train_feat = build_features(v2_train_sample, v2_train_src, label_name="v2-train", engine=v2_engine)
    v2_train_feat = v2_train_feat.sort_values("date").reset_index(drop=True)
    v2_train_feat = v2_train_feat.dropna(subset=["rank_a", "rank_b"])
    print(f"    usable: {len(v2_train_feat):,}")

    # ── Train both models ────────────────────────────────────────────────────
    print("\n[train] V1 model…")
    v1_model, v1_base = train_calibrated(v1_train_feat[FEATURE_COLS], v1_train_feat["label"])
    print("[train] V2 model…")
    v2_model, v2_base = train_calibrated(v2_train_feat[FEATURE_COLS], v2_train_feat["label"])

    # ── Build test features (using each model's stats source) ────────────────
    # CRITICAL: deterministic_flip=True — both test sets get the same pa/pb assignment
    # so that features and labels align across v1 and v2.
    print("\n[test] Building V1 test features (deterministic flip, v1 stats — stale)…")
    v1_test_feat = build_features(test_df, v1_train_src, label_name="v1-test",
                                  engine=v1_engine, deterministic_flip=True)
    print("[test] Building V2 test features (deterministic flip, v2 stats — fresh)…")
    v2_test_feat = build_features(test_df, v2_train_src, label_name="v2-test",
                                  engine=v2_engine, deterministic_flip=True)

    # Align on the matches both test sets produced rows for
    v1_test_feat["match_key"] = v1_test_feat["winner_actual"].astype(str) + "|" + v1_test_feat["loser_actual"].astype(str) + "|" + v1_test_feat["match_date"].astype(str)
    v2_test_feat["match_key"] = v2_test_feat["winner_actual"].astype(str) + "|" + v2_test_feat["loser_actual"].astype(str) + "|" + v2_test_feat["match_date"].astype(str)
    shared_keys = set(v1_test_feat["match_key"]).intersection(v2_test_feat["match_key"])
    v1_test_feat = v1_test_feat[v1_test_feat["match_key"].isin(shared_keys)].sort_values("match_key").reset_index(drop=True)
    v2_test_feat = v2_test_feat[v2_test_feat["match_key"].isin(shared_keys)].sort_values("match_key").reset_index(drop=True)

    # SANITY CHECK: labels must match (they should, since flip is deterministic)
    mismatched = (v1_test_feat["label"].to_numpy() != v2_test_feat["label"].to_numpy()).sum()
    if mismatched > 0:
        print(f"  ⚠ WARNING: {mismatched} label mismatches between v1 and v2 test sets "
              "(deterministic flip should prevent this)")

    # For each model, predict on its own feature rows
    print(f"\n[score] shared test matches: {len(shared_keys):,}")

    v1_metrics, v1_probs = score(v1_model, v1_test_feat[FEATURE_COLS], v1_test_feat["label"], "v1")
    v2_metrics, v2_probs = score(v2_model, v2_test_feat[FEATURE_COLS], v2_test_feat["label"], "v2")

    # Calibration bins
    v1_calib = calibration_bins(v1_probs, v1_test_feat["label"].to_numpy())
    v2_calib = calibration_bins(v2_probs, v2_test_feat["label"].to_numpy())

    # Disagreement analysis
    disagree = np.abs(v1_probs - v2_probs)
    biggest = np.argsort(-disagree)[:10]
    disagree_rows = []
    for idx in biggest:
        mk = v1_test_feat.iloc[idx]["match_key"]
        w, l, d = mk.split("|")
        disagree_rows.append({
            "date":    d[:10],
            "winner":  w,
            "loser":   l,
            "v1_prob": round(float(v1_probs[idx]), 3),
            "v2_prob": round(float(v2_probs[idx]), 3),
            "delta":   round(float(v1_probs[idx] - v2_probs[idx]), 3),
            "label":   int(v1_test_feat.iloc[idx]["label"]),
        })

    # Feature importance (v2 base model)
    try:
        fi = v2_base.feature_importances_
    except AttributeError:
        from sklearn.inspection import permutation_importance
        perm = permutation_importance(v2_base, v2_train_feat[FEATURE_COLS], v2_train_feat["label"], n_repeats=3, random_state=42)
        fi = perm.importances_mean
    fi_df = pd.DataFrame({"feature": FEATURE_COLS, "importance": fi}).sort_values("importance", ascending=False)

    # Save models
    V1_MODEL.parent.mkdir(parents=True, exist_ok=True)
    with open(V1_MODEL, "wb") as f:
        pickle.dump({"model": v1_model, "feature_cols": FEATURE_COLS, "metrics": v1_metrics}, f)
    with open(V2_MODEL, "wb") as f:
        pickle.dump({"model": v2_model, "feature_cols": FEATURE_COLS, "metrics": v2_metrics}, f)

    # ── Write report ─────────────────────────────────────────────────────────
    delta = {k: round(v2_metrics[k] - v1_metrics[k], 4) for k in ("auc", "brier", "logloss", "accuracy@0.5")}
    verdict = "V2 WINS" if (delta["auc"] > 0.005 and delta["brier"] < -0.002) else ("V1 WINS" if delta["auc"] < -0.005 else "INCONCLUSIVE")

    lines = [
        "# V1 vs V2 Model Comparison",
        "",
        f"**Holdout:** {HOLDOUT_START.date()} → {HOLDOUT_END.date()} "
        f"(ATP tour-level only, {len(shared_keys):,} matches)",
        "",
        "## Training sets",
        f"- V1: Sackmann 2018–2024 ({len(v1_train_src):,} matches) — sampled {len(v1_train_sample):,}",
        f"- V2: TML 2019–2026 + WTA ({len(v2_train_src):,} pre-holdout) — sampled {len(v2_train_sample):,}",
        "",
        "## Headline metrics",
        "| Metric | V1 | V2 | Δ (V2 − V1) |",
        "|---|---|---|---|",
        f"| AUC (↑ better) | {v1_metrics['auc']} | **{v2_metrics['auc']}** | {delta['auc']:+.4f} |",
        f"| Brier (↓ better) | {v1_metrics['brier']} | **{v2_metrics['brier']}** | {delta['brier']:+.4f} |",
        f"| Log loss (↓ better) | {v1_metrics['logloss']} | **{v2_metrics['logloss']}** | {delta['logloss']:+.4f} |",
        f"| Accuracy @0.5 | {v1_metrics['accuracy@0.5']} | **{v2_metrics['accuracy@0.5']}** | {delta['accuracy@0.5']:+.4f} |",
        "",
        f"**Verdict:** {verdict}",
        "",
        "## Calibration — V1",
        v1_calib.to_markdown(index=False),
        "",
        "## Calibration — V2",
        v2_calib.to_markdown(index=False),
        "",
        "## Top 10 disagreements (|v1 − v2| largest)",
        pd.DataFrame(disagree_rows).to_markdown(index=False),
        "",
        "## V2 feature importance (top 15)",
        fi_df.head(15).to_markdown(index=False),
        "",
        "## Interpretation",
        "- AUC Δ > 0.005 is a meaningful improvement in rank ordering",
        "- Brier Δ < -0.002 means v2 is better-calibrated on average",
        "- Large disagreements (|Δ| > 0.10) on matches where v2 was correct and v1 wasn't are the clearest wins for v2",
        "- If accuracy is close but Brier is very different, v2 is mainly better at *how confident* it is",
        "",
        "_Produced by compare_v1_v2.py — no production files touched_",
    ]
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(lines))
    print(f"\n✓ Report → {REPORT_MD}")
    print(f"✓ V1 model → {V1_MODEL}")
    print(f"✓ V2 model → {V2_MODEL}")
    print("\n" + "=" * 72)
    print(f"  {verdict}  |  AUC: {v1_metrics['auc']} → {v2_metrics['auc']} "
          f"(Δ {delta['auc']:+.4f})  |  Brier: {v1_metrics['brier']} → {v2_metrics['brier']} "
          f"(Δ {delta['brier']:+.4f})")
    print("=" * 72)


if __name__ == "__main__":
    main()
