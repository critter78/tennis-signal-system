"""
TENNIS POLYMARKET SIGNAL SYSTEM
Script 2: Feature Engineering + Model Training

Features engineered per match:
  - Player ranking + ELO-style rating (surface-specific)
  - Recent form windows (last 10 / 20 / 52 weeks)
  - Surface-specific win rates
  - Head-to-head record (overall + on surface)
  - Serve/return stats (ace%, DF%, 1stIn%, win on 1st/2nd serve)
  - Fatigue proxy (days since last match, sets played last 7 days)
  - Tournament context (round, tourney level)
  - Pinnacle implied probability (if available — strongest feature)

Target: P(player_a wins)
Model:  XGBoost binary classifier with walk-forward validation
Output: models/tennis_xgb_vN.pkl + models/feature_importance.csv
"""

import warnings
warnings.filterwarnings("ignore")

import pickle
import argparse
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import timedelta
from typing import Optional

from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import log_loss, brier_score_loss, roc_auc_score

DATA_DIR   = Path("data")
MODELS_DIR = Path("models")
MODELS_DIR.mkdir(exist_ok=True)

SURFACE_MAP = {"Hard": 0, "Clay": 1, "Grass": 2, "Carpet": 3}
ROUND_MAP   = {
    "R128": 1, "R64": 2, "R32": 3, "R16": 4,
    "QF": 5, "SF": 6, "F": 7, "RR": 4, "BR": 5
}

# ─── LOAD DATA ────────────────────────────────────────────────────────────────

def load_matches(min_year: int = 2018) -> pd.DataFrame:
    path = DATA_DIR / "raw" / "matches_combined.parquet"
    if not path.exists():
        raise FileNotFoundError(
            "matches_combined.parquet not found — run 01_data_pipeline.py first"
        )
    df = pd.read_parquet(path)
    if "date" in df.columns:
        df = df[df["date"].dt.year >= min_year].copy()
    df = df.sort_values("date").reset_index(drop=True)
    print(f"Loaded {len(df):,} matches from {df['date'].min().date()} "
          f"to {df['date'].max().date()}")
    return df


# ─── PLAYER STATS ENGINE ──────────────────────────────────────────────────────

class PlayerStatsEngine:
    """
    Maintains rolling stats per player.  We compute features using only
    data strictly BEFORE the match date (no lookahead leakage).
    """

    def __init__(self, df: pd.DataFrame):
        self.df = df
        self._build_index()

    def _build_index(self):
        """Build a lookup: (player_name, date) → list of prior matches as winner/loser."""
        self._cache = {}  # populated lazily

    def get_stats(self, player: str, as_of_date, surface: str,
                  window_days: int = 365) -> dict:
        """
        Return feature dict for `player` using matches before `as_of_date`.
        """
        cutoff = pd.Timestamp(as_of_date)
        since  = cutoff - timedelta(days=window_days)

        wins = self.df[
            (self.df["winner"] == player) &
            (self.df["date"] < cutoff) &
            (self.df["date"] >= since)
        ]
        losses = self.df[
            (self.df["loser"] == player) &
            (self.df["date"] < cutoff) &
            (self.df["date"] >= since)
        ]
        all_matches = pd.concat([wins, losses]).sort_values("date")

        n_wins   = len(wins)
        n_losses = len(losses)
        n_total  = n_wins + n_losses

        # Surface-specific
        s_wins   = wins[wins["surface"] == surface]
        s_losses = losses[losses["surface"] == surface]
        s_total  = len(s_wins) + len(s_losses)

        # Last 10 matches
        last10 = all_matches.tail(10)
        l10_w  = (last10["winner"] == player).sum()

        # Last 20 matches
        last20 = all_matches.tail(20)
        l20_w  = (last20["winner"] == player).sum()

        # Fatigue: days since last match + sets in last 7 days
        recent = all_matches[all_matches["date"] >= cutoff - timedelta(days=7)]
        sets_last_7d = _estimate_sets(recent, player)
        days_since_last = (
            (cutoff - all_matches["date"].max()).days
            if n_total > 0 else 30
        )

        # Serve stats (ATP only — WTA sometimes missing)
        def mean_stat(src_df, col):
            if col not in src_df.columns:
                return np.nan
            return src_df[col].dropna().mean()

        return {
            "win_rate_52w":       n_wins / n_total if n_total > 5 else np.nan,
            "win_rate_surf_52w":  len(s_wins) / s_total if s_total > 3 else np.nan,
            "win_rate_l10":       l10_w / len(last10) if len(last10) > 0 else np.nan,
            "win_rate_l20":       l20_w / len(last20) if len(last20) > 0 else np.nan,
            "n_matches_52w":      n_total,
            "n_matches_surf_52w": s_total,
            "days_since_last":    days_since_last,
            "sets_last_7d":       sets_last_7d,
            # Serve stats from wins (winner cols)
            "ace_rate_w":         mean_stat(wins, "w_ace"),
            "df_rate_w":          mean_stat(wins, "w_df"),
            "first_in_pct_w":     mean_stat(wins, "w_1stIn"),
            "win_on_1st_w":       mean_stat(wins, "w_1stWon"),
            "win_on_2nd_w":       mean_stat(wins, "w_2ndWon"),
            "bp_saved_pct_w":     mean_stat(wins, "w_bpSaved"),
            # Return stats from losses (where player is in loser cols)
            "ace_rate_l":         mean_stat(losses, "l_ace"),
            "bp_conv_l":          mean_stat(losses, "l_bpFaced"),
        }


def _estimate_sets(matches_df: pd.DataFrame, player: str) -> int:
    """Rough set count from score strings for fatigue calculation."""
    total = 0
    if "score" not in matches_df.columns:
        return 0
    for _, row in matches_df.iterrows():
        score = str(row.get("score", ""))
        sets  = [s for s in score.split() if "-" in s and not "[" in s]
        total += len(sets)
    return total


def get_h2h(df: pd.DataFrame, p1: str, p2: str,
            as_of_date, surface: Optional[str] = None) -> dict:
    """Head-to-head record between p1 and p2 before as_of_date."""
    cutoff = pd.Timestamp(as_of_date)
    mask = (
        ((df["winner"] == p1) & (df["loser"] == p2)) |
        ((df["winner"] == p2) & (df["loser"] == p1))
    ) & (df["date"] < cutoff)

    h2h = df[mask]
    if surface:
        h2h_surf = h2h[h2h["surface"] == surface]
    else:
        h2h_surf = h2h

    p1_wins = (h2h["winner"] == p1).sum()
    total   = len(h2h)
    p1_surf = (h2h_surf["winner"] == p1).sum()
    surf_t  = len(h2h_surf)

    return {
        "h2h_total":    total,
        "h2h_p1_wins":  p1_wins / total if total > 0 else 0.5,
        "h2h_surf_p1":  p1_surf / surf_t if surf_t > 0 else 0.5,
        "h2h_advantage": int(p1_wins > (total / 2)) if total > 0 else 0,
    }


# ─── FEATURE MATRIX BUILDER ───────────────────────────────────────────────────

def build_feature_matrix(df: pd.DataFrame,
                         sample_frac: float = 1.0) -> pd.DataFrame:
    """
    Build (n_matches × n_features) matrix.  Each row = one match.
    Player assignment is randomised (winner/loser → player_a/player_b)
    to avoid model learning winner-biased positional features.
    """
    print(f"\nBuilding features for {len(df):,} matches...")
    engine = PlayerStatsEngine(df)

    if sample_frac < 1.0:
        df = df.sample(frac=sample_frac, random_state=42)
        print(f"  (sampled {len(df):,} matches at frac={sample_frac})")

    rows = []
    for i, (_, match) in enumerate(df.iterrows()):
        if i % 5000 == 0:
            print(f"  processing {i:,}/{len(df):,}...", end="\r")

        winner  = match.get("winner", "")
        loser   = match.get("loser", "")
        date    = match.get("date")
        surface = match.get("surface", "Hard")
        tour    = match.get("tour", "atp")

        if not winner or not loser or pd.isna(date):
            continue

        # Randomly flip player order — target = 1 means player_a won
        flip  = np.random.random() < 0.5
        pa    = loser   if flip else winner
        pb    = winner  if flip else loser
        label = 0 if flip else 1

        sa = engine.get_stats(pa, date, surface)
        sb = engine.get_stats(pb, date, surface)
        h2 = get_h2h(df, pa, pb, date, surface)

        # Ranking features (lower rank = better)
        ra = match.get("w_rank" if not flip else "l_rank", np.nan)
        rb = match.get("l_rank" if not flip else "w_rank", np.nan)
        try:
            ra = float(ra)
            rb = float(rb)
        except (ValueError, TypeError):
            ra = rb = np.nan

        rank_diff      = ra - rb  # negative = pa is higher ranked
        rank_ratio     = ra / rb if rb and rb > 0 else np.nan
        log_rank_ratio = np.log(ra / rb) if ra and rb and ra > 0 and rb > 0 else np.nan

        feat = {
            "date":          date,
            "surface":       SURFACE_MAP.get(surface, 0),
            "tour":          1 if tour == "wta" else 0,
            "round":         ROUND_MAP.get(str(match.get("round", "")), 3),
            "tourney_level": _encode_level(str(match.get("tourney_level", ""))),

            # Ranking
            "rank_a":          ra,
            "rank_b":          rb,
            "rank_diff":       rank_diff,
            "rank_ratio":      rank_ratio,
            "log_rank_ratio":  log_rank_ratio,

            # Form
            "a_win_rate_52w":      sa["win_rate_52w"],
            "b_win_rate_52w":      sb["win_rate_52w"],
            "a_win_rate_surf_52w": sa["win_rate_surf_52w"],
            "b_win_rate_surf_52w": sb["win_rate_surf_52w"],
            "a_win_rate_l10":      sa["win_rate_l10"],
            "b_win_rate_l10":      sb["win_rate_l10"],
            "a_win_rate_l20":      sa["win_rate_l20"],
            "b_win_rate_l20":      sb["win_rate_l20"],
            "form_diff_52w":       sa["win_rate_52w"] - sb["win_rate_52w"]
                                   if not np.isnan(sa["win_rate_52w"])
                                      and not np.isnan(sb["win_rate_52w"]) else np.nan,
            "form_diff_surf":      sa["win_rate_surf_52w"] - sb["win_rate_surf_52w"]
                                   if not np.isnan(sa["win_rate_surf_52w"])
                                      and not np.isnan(sb["win_rate_surf_52w"]) else np.nan,

            # Fatigue
            "a_days_since_last":  sa["days_since_last"],
            "b_days_since_last":  sb["days_since_last"],
            "a_sets_last_7d":     sa["sets_last_7d"],
            "b_sets_last_7d":     sb["sets_last_7d"],
            "fatigue_diff":       sa["sets_last_7d"] - sb["sets_last_7d"],

            # H2H
            "h2h_total":       h2["h2h_total"],
            "h2h_p1_wins":     h2["h2h_p1_wins"],
            "h2h_surf_p1":     h2["h2h_surf_p1"],
            "h2h_advantage":   h2["h2h_advantage"],

            # Serve/return stats (deltas)
            "ace_diff":        _safe_diff(sa["ace_rate_w"], sb["ace_rate_w"]),
            "df_diff":         _safe_diff(sa["df_rate_w"], sb["df_rate_w"]),
            "first_in_diff":   _safe_diff(sa["first_in_pct_w"], sb["first_in_pct_w"]),
            "win1st_diff":     _safe_diff(sa["win_on_1st_w"], sb["win_on_1st_w"]),
            "win2nd_diff":     _safe_diff(sa["win_on_2nd_w"], sb["win_on_2nd_w"]),

            # Activity volume
            "a_n_matches_52w": sa["n_matches_52w"],
            "b_n_matches_52w": sb["n_matches_52w"],

            # Target
            "label": label,
        }
        rows.append(feat)

    print(f"\n  ✓ Feature matrix: {len(rows):,} rows")
    return pd.DataFrame(rows)


def _encode_level(level: str) -> int:
    mapping = {"G": 4, "M": 3, "A": 2, "D": 1, "F": 3, "": 2}
    return mapping.get(level, 2)


def _safe_diff(a, b):
    if np.isnan(a) or np.isnan(b):
        return np.nan
    return a - b


# ─── MODEL TRAINING ───────────────────────────────────────────────────────────

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

MODEL_PARAMS = {
    "max_iter":          800,
    "max_depth":         5,
    "learning_rate":     0.03,
    "max_leaf_nodes":    31,
    "min_samples_leaf":  10,
    "l2_regularization": 1.0,
    "random_state":      42,
    "verbose":           0,
}


def train_model(feat_df: pd.DataFrame, n_splits: int = 5):
    """Walk-forward cross-validation + final model on full training set."""
    feat_df = feat_df.sort_values("date").reset_index(drop=True)
    X = feat_df[FEATURE_COLS]
    y = feat_df["label"]

    print(f"\nTraining HistGradientBoosting on {len(feat_df):,} samples, "
          f"{len(FEATURE_COLS)} features...")

    # Walk-forward CV
    tscv    = TimeSeriesSplit(n_splits=n_splits)
    metrics = []

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
        X_tr, X_val = X.iloc[train_idx], X.iloc[val_idx]
        y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

        model = HistGradientBoostingClassifier(**MODEL_PARAMS)
        model.fit(X_tr, y_tr)
        probs = model.predict_proba(X_val)[:, 1]
        metrics.append({
            "fold":    fold + 1,
            "n_val":   len(y_val),
            "logloss": round(log_loss(y_val, probs), 4),
            "brier":   round(brier_score_loss(y_val, probs), 4),
            "auc":     round(roc_auc_score(y_val, probs), 4),
        })

    metrics_df = pd.DataFrame(metrics)
    print("\n  Walk-Forward CV Results:")
    print(metrics_df.to_string(index=False))
    print(f"\n  Mean AUC:     {metrics_df['auc'].mean():.4f}")
    print(f"  Mean LogLoss: {metrics_df['logloss'].mean():.4f}")
    print(f"  Mean Brier:   {metrics_df['brier'].mean():.4f}")

    # Final model — train on everything, calibrate with isotonic regression
    print("\nTraining final calibrated model on full dataset...")
    base_model = HistGradientBoostingClassifier(**MODEL_PARAMS)
    base_model.fit(X, y)

    # Isotonic calibration via cross-validation
    calibrated = CalibratedClassifierCV(base_model, method="isotonic", cv=5)
    calibrated.fit(X, y)

    # Feature importance
    try:
        importances = base_model.feature_importances_
    except AttributeError:
        # Fallback: use permutation importance estimate from training
        from sklearn.inspection import permutation_importance
        perm = permutation_importance(base_model, X, y, n_repeats=5, random_state=42, n_jobs=-1)
        importances = perm.importances_mean
    fi = pd.DataFrame({
        "feature":   FEATURE_COLS,
        "importance": importances,
    }).sort_values("importance", ascending=False)
    fi.to_csv(MODELS_DIR / "feature_importance.csv", index=False)
    print("\n  Top 15 features:")
    print(fi.head(15).to_string(index=False))

    # Save model + metadata
    model_version = len(list(MODELS_DIR.glob("tennis_hgb_v*.pkl"))) + 1
    model_path    = MODELS_DIR / f"tennis_hgb_v{model_version}.pkl"
    meta_path     = MODELS_DIR / "latest_model.json"

    with open(model_path, "wb") as f:
        pickle.dump({
            "model":        calibrated,
            "feature_cols": FEATURE_COLS,
            "cv_metrics":   metrics_df.to_dict(),
            "mean_auc":     metrics_df["auc"].mean(),
        }, f)

    with open(meta_path, "w") as f:
        json.dump({
            "path":      str(model_path),
            "version":   model_version,
            "mean_auc":  metrics_df["auc"].mean(),
            "n_samples": len(feat_df),
        }, f, indent=2)

    print(f"\n✓ Model saved → {model_path}")
    return calibrated, metrics_df


# ─── MAIN ─────────────────────────────────────────────────────────────────────

import json

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-year",     type=int, default=2018)
    parser.add_argument("--sample-frac",  type=float, default=1.0,
                        help="Fraction of matches to sample (speed up testing)")
    parser.add_argument("--load-features", action="store_true",
                        help="Load pre-built features instead of recomputing")
    args = parser.parse_args()

    print("=" * 60)
    print("  TENNIS POLYMARKET SYSTEM — STEP 2: TRAIN")
    print("=" * 60)

    feat_path = DATA_DIR / "features.parquet"

    if args.load_features and feat_path.exists():
        print(f"\nLoading pre-built features from {feat_path}...")
        feat_df = pd.read_parquet(feat_path)
        feat_df["date"] = pd.to_datetime(feat_df["date"])
    else:
        df      = load_matches(min_year=args.min_year)
        feat_df = build_feature_matrix(df, sample_frac=args.sample_frac)
        feat_df.to_parquet(feat_path, index=False)
        print(f"\n✓ Features saved → {feat_path}")

    # Drop rows with too many NaN features
    drop_thresh = len(FEATURE_COLS) * 0.5
    before = len(feat_df)
    feat_df = feat_df.dropna(thresh=int(drop_thresh + 2))
    print(f"  Dropped {before - len(feat_df):,} sparse rows, "
          f"{len(feat_df):,} remaining")

    # Fill remaining NaNs with median (HistGradientBoosting handles NaN but calibration doesn't)
    for col in FEATURE_COLS:
        if col in feat_df.columns:
            feat_df[col] = feat_df[col].fillna(feat_df[col].median())

    model, cv_df = train_model(feat_df)

    print("\n" + "=" * 60)
    print("  ✓ TRAINING COMPLETE")
    print("  Next: python 03_signal_generator.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
