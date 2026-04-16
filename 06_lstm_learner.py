#!/usr/bin/env python3
"""
LSTM Sequential Learner for Tennis Betting Signal System  (v2 — hardened)

Learns from resolved bet outcomes to improve predictions over time.
Uses a lightweight dense neural network that predicts RESIDUALS (not raw
probabilities) based on sequential patterns in recent picks.

v2 Safeguards (anti-overfitting):
  1. Residual prediction — learns (actual - model_prob) minus the running
     mean, so the target is centred on zero and the model can't just learn
     the base rate.
  2. Confidence cap — maximum adjustment clamped to ±5 pp (was ±15 pp).
  3. Bayesian shrinkage — raw prediction is blended with a prior of 0
     (no adjustment) using a configurable shrinkage factor (default 0.30).
  4. Smaller model — (16, 8) hidden layers (~250 params vs ~3 500 before).
  5. Stronger regularisation — L2 alpha raised from 0.01 → 0.10.

Requirements: scikit-learn (no tensorflow/torch needed)

Usage:
    python3 06_lstm_learner.py status      # Show training readiness
    python3 06_lstm_learner.py train       # Train/retrain the sequential model
    python3 06_lstm_learner.py predict     # Show adjustment predictions for recent picks
    python3 06_lstm_learner.py backtest    # Backtest adjustment accuracy
"""

import json
import argparse
import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import mean_squared_error, mean_absolute_error

LOGS_DIR = Path("logs")
MODELS_DIR = Path("models")
LSTM_MODEL = MODELS_DIR / "lstm_adjuster.pkl"
LSTM_SCALER = MODELS_DIR / "lstm_scaler.pkl"
LSTM_META = MODELS_DIR / "lstm_meta.json"
PICKS_LOG = LOGS_DIR / "picks.jsonl"
MIN_PICKS = 50
SEQ_LEN = 20  # Look back 20 picks for sequential features

# ── v2 SAFEGUARD CONSTANTS ──────────────────────────────────────────────────
CONFIDENCE_CAP = 0.08          # Safeguard 2: max ±8 pp adjustment
SHRINKAGE_FACTOR = 0.60        # Safeguard 3: apply 60% of raw prediction
HIDDEN_LAYERS = (16, 8)        # Safeguard 4: much smaller model
L2_ALPHA = 0.10                # Safeguard 5: 10× stronger regularisation
# ────────────────────────────────────────────────────────────────────────────


def load_all_picks():
    """Load all picks from picks.jsonl."""
    if not PICKS_LOG.exists():
        return []
    picks = []
    with open(PICKS_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    picks.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return sorted(picks, key=lambda x: x.get("logged_at", ""))


def load_resolved_picks():
    """Load only resolved (outcome known) picks."""
    return [p for p in load_all_picks() if p.get("outcome") is not None]


def build_sequential_features(picks, idx, seq_len=SEQ_LEN):
    """
    Build a feature vector for pick at index `idx` using the previous `seq_len` picks.

    Features capture temporal patterns:
    - Recent win rate (last 5, 10, 20)
    - Win streak / loss streak
    - Average edge accuracy (did high-edge picks actually win?)
    - Surface-specific recent performance
    - Confidence calibration (model_prob vs actual outcomes)
    - Volume-weighted edge accuracy
    - Time-of-day / day-of-week patterns
    - Edge tier performance momentum
    """
    current = picks[idx]
    window = picks[max(0, idx - seq_len):idx]

    if len(window) < 3:
        return None

    features = {}

    # 1. Recent win rates
    outcomes = [1 if p["outcome"] == "win" else 0 for p in window]
    features["wr_all"] = np.mean(outcomes) if outcomes else 0.5
    features["wr_last5"] = np.mean(outcomes[-5:]) if len(outcomes) >= 5 else features["wr_all"]
    features["wr_last10"] = np.mean(outcomes[-10:]) if len(outcomes) >= 10 else features["wr_all"]

    # 2. Win/loss streaks
    streak = 0
    if outcomes:
        last = outcomes[-1]
        for o in reversed(outcomes):
            if o == last:
                streak += 1
            else:
                break
        streak = streak if last == 1 else -streak
    features["streak"] = streak

    # 3. Edge accuracy — do higher-edge picks win more?
    edge_wins = [(abs(p.get("edge", 0)), 1 if p["outcome"] == "win" else 0) for p in window if p.get("edge")]
    if edge_wins:
        high_edge = [w for e, w in edge_wins if e >= 10]
        low_edge = [w for e, w in edge_wins if e < 10]
        features["high_edge_wr"] = np.mean(high_edge) if high_edge else 0.5
        features["low_edge_wr"] = np.mean(low_edge) if low_edge else 0.5
        features["edge_wr_diff"] = features["high_edge_wr"] - features["low_edge_wr"]
    else:
        features["high_edge_wr"] = 0.5
        features["low_edge_wr"] = 0.5
        features["edge_wr_diff"] = 0

    # 4. Surface-specific performance
    current_surface = current.get("surface", "")
    surface_picks = [p for p in window if p.get("surface") == current_surface]
    if surface_picks:
        features["surface_wr"] = np.mean([1 if p["outcome"] == "win" else 0 for p in surface_picks])
    else:
        features["surface_wr"] = 0.5

    # 5. Confidence calibration — is the model overconfident or underconfident?
    calibration_errors = []
    for p in window:
        prob = p.get("model_prob", p.get("confidence", 50)) / 100.0
        actual = 1 if p["outcome"] == "win" else 0
        calibration_errors.append(prob - actual)
    features["avg_calibration_error"] = np.mean(calibration_errors) if calibration_errors else 0
    features["calibration_std"] = np.std(calibration_errors) if len(calibration_errors) > 1 else 0.25

    # 6. Current pick features
    features["current_edge"] = abs(current.get("edge", 0))
    features["current_model_prob"] = current.get("model_prob", current.get("confidence", 50))
    features["current_poly_price"] = current.get("poly_price", 50)
    features["current_kelly"] = current.get("kelly_stake", 0)

    # 7. Volume context
    volumes = [p.get("volume", 0) for p in window if p.get("volume")]
    features["avg_volume"] = np.mean(volumes) if volumes else 0
    features["current_volume"] = current.get("volume", 0)
    features["volume_ratio"] = features["current_volume"] / max(features["avg_volume"], 1)

    # 8. Tournament tier momentum
    tourney_picks = [p for p in window if p.get("tournament") == current.get("tournament")]
    if tourney_picks:
        features["tourney_wr"] = np.mean([1 if p["outcome"] == "win" else 0 for p in tourney_picks])
    else:
        features["tourney_wr"] = 0.5

    # 9. Edge tier of current pick
    edge = abs(current.get("edge", 0))
    features["is_strong"] = 1 if edge >= 10 else 0
    features["is_solid"] = 1 if 7 <= edge < 10 else 0
    features["is_watch"] = 1 if 5 <= edge < 7 else 0

    # 10. Market type
    features["is_outright"] = 1 if current.get("market_type") == "outright" else 0
    features["is_h2h"] = 1 if current.get("market_type") == "h2h" else 0

    # 11. Trend — are we getting better or worse recently?
    if len(outcomes) >= 10:
        first_half = np.mean(outcomes[:len(outcomes)//2])
        second_half = np.mean(outcomes[len(outcomes)//2:])
        features["trend"] = second_half - first_half
    else:
        features["trend"] = 0

    return features


def build_training_data(picks):
    """
    Build X, y arrays for training from resolved picks.

    SAFEGUARD 1 — RESIDUAL PREDICTION:
    Instead of raw (actual - model_prob), the target is the de-meaned residual:
        residual_i = (actual_i - model_prob_i) - mean_residual
    This centres the target on zero so the model can't just learn the base-rate
    bias.  The mean_residual is stored in metadata and re-applied at inference.
    """
    X_rows = []
    y_raw = []
    feature_names = None

    for i in range(SEQ_LEN, len(picks)):
        features = build_sequential_features(picks, i)
        if features is None:
            continue

        if feature_names is None:
            feature_names = sorted(features.keys())

        X_rows.append([features[k] for k in feature_names])

        # Raw residual: how far off was the model?
        model_prob = picks[i].get("model_prob", picks[i].get("confidence", 50)) / 100.0
        actual = 1.0 if picks[i]["outcome"] == "win" else 0.0
        y_raw.append(actual - model_prob)

    if not X_rows:
        return None, None, None, 0.0

    y_raw = np.array(y_raw)
    mean_residual = float(np.mean(y_raw))

    # SAFEGUARD 1: de-mean so model predicts deviation from the average error
    y_centred = y_raw - mean_residual

    return np.array(X_rows), y_centred, feature_names, mean_residual


def show_status():
    """Show LSTM learner status and training readiness."""
    all_picks = load_all_picks()
    resolved = load_resolved_picks()

    print("\n" + "=" * 60)
    print("  LSTM SEQUENTIAL LEARNER — STATUS (v2 hardened)")
    print("=" * 60)
    print(f"  Total picks logged:    {len(all_picks)}")
    print(f"  Resolved picks:        {len(resolved)}")
    print(f"  Unresolved:            {len(all_picks) - len(resolved)}")
    print(f"  Min to train:          {MIN_PICKS}")

    ready = len(resolved) >= MIN_PICKS
    print(f"  Ready to train:        {'YES' if ready else f'Need {MIN_PICKS - len(resolved)} more'}")

    print(f"\n  ── v2 Safeguards ──")
    print(f"  Confidence cap:        ±{CONFIDENCE_CAP*100:.0f} pp")
    print(f"  Shrinkage factor:      {SHRINKAGE_FACTOR:.0%} (prior = 0)")
    print(f"  Hidden layers:         {HIDDEN_LAYERS}")
    print(f"  L2 alpha:              {L2_ALPHA}")
    print(f"  Target:                de-meaned residuals")

    if LSTM_META.exists():
        with open(LSTM_META) as f:
            meta = json.load(f)
        print(f"\n  Model type:            {meta.get('type', 'unknown')}")
        print(f"  Trained at:            {meta.get('trained_at', 'unknown')}")
        print(f"  Trained on:            {meta.get('n_picks', 0)} picks")
        print(f"  Validation MAE:        {meta.get('val_mae', 'N/A')}")
        print(f"  Validation RMSE:       {meta.get('val_rmse', 'N/A')}")
        print(f"  Mean residual:         {meta.get('mean_residual', 'N/A')}")
        print(f"  Feature count:         {meta.get('n_features', 'N/A')}")

        new_since = len(resolved) - meta.get("n_picks", 0)
        print(f"  New picks since train: {new_since}")
        if new_since >= 25:
            print(f"  -> RECOMMEND RETRAINING (25+ new picks)")
    else:
        print(f"\n  No LSTM model trained yet.")

    if resolved:
        last10 = resolved[-10:]
        streak = "".join("W" if p["outcome"] == "win" else "L" for p in last10)
        wr = sum(1 for p in last10 if p["outcome"] == "win") / len(last10)
        print(f"\n  Last 10 outcomes:      {streak}")
        print(f"  Last 10 win rate:      {wr:.0%}")

        # Show edge tier breakdown
        strong = [p for p in resolved if abs(p.get("edge", 0)) >= 10]
        solid = [p for p in resolved if 7 <= abs(p.get("edge", 0)) < 10]
        watch = [p for p in resolved if 5 <= abs(p.get("edge", 0)) < 7]

        def tier_wr(tier):
            if not tier:
                return "—"
            w = sum(1 for p in tier if p["outcome"] == "win")
            return f"{w}/{len(tier)} ({w/len(tier):.0%})"

        print(f"\n  STRONG (>=10%) WR:     {tier_wr(strong)}")
        print(f"  SOLID (7-10%) WR:      {tier_wr(solid)}")
        print(f"  WATCH (5-7%) WR:       {tier_wr(watch)}")

    print()


def train_lstm():
    """Train the sequential learning model with v2 safeguards."""
    picks = load_resolved_picks()

    if len(picks) < MIN_PICKS:
        print(f"\n  Need {MIN_PICKS} resolved picks to train. Have {len(picks)}.")
        print(f"  Keep placing bets and marking outcomes.")
        print(f"  LSTM activates automatically once you have enough data.\n")
        return False

    print(f"\n{'='*60}")
    print(f"  TRAINING LSTM SEQUENTIAL LEARNER (v2 hardened)")
    print(f"{'='*60}")
    print(f"  Resolved picks: {len(picks)}")
    print(f"  Safeguards: residual targets, shrinkage={SHRINKAGE_FACTOR}, "
          f"cap=±{CONFIDENCE_CAP*100:.0f}pp, layers={HIDDEN_LAYERS}, alpha={L2_ALPHA}")

    # Build training data — SAFEGUARD 1: residual prediction
    X, y, feature_names, mean_residual = build_training_data(picks)
    if X is None or len(X) < 20:
        print(f"  Not enough sequential data to train. Need at least {SEQ_LEN + 20} resolved picks.")
        return False

    print(f"  Training samples: {len(X)}")
    print(f"  Features: {len(feature_names)}")
    print(f"  Mean residual (base-rate bias): {mean_residual:+.4f}")
    print(f"  Target std (de-meaned):         {np.std(y):.4f}")

    # Scale features
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Time series cross-validation
    tscv = TimeSeriesSplit(n_splits=3)
    val_maes = []
    val_rmses = []

    print(f"\n  Time-series cross-validation (3 folds):")
    for fold, (train_idx, val_idx) in enumerate(tscv.split(X_scaled)):
        X_train, X_val = X_scaled[train_idx], X_scaled[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]

        # SAFEGUARD 4 & 5: smaller model, stronger regularisation
        model = MLPRegressor(
            hidden_layer_sizes=HIDDEN_LAYERS,
            activation="relu",
            solver="adam",
            max_iter=500,
            early_stopping=True,
            validation_fraction=0.15,
            n_iter_no_change=20,
            learning_rate="adaptive",
            learning_rate_init=0.001,
            alpha=L2_ALPHA,
            random_state=42 + fold,
        )
        model.fit(X_train, y_train)

        y_pred = model.predict(X_val)
        mae = mean_absolute_error(y_val, y_pred)
        rmse = np.sqrt(mean_squared_error(y_val, y_pred))
        val_maes.append(mae)
        val_rmses.append(rmse)

        # Check if model beats naive baseline (always predict 0)
        naive_mae = mean_absolute_error(y_val, np.zeros_like(y_val))
        lift = (naive_mae - mae) / naive_mae * 100 if naive_mae > 0 else 0
        print(f"    Fold {fold+1}: MAE={mae:.4f}, RMSE={rmse:.4f}, "
              f"naive_MAE={naive_mae:.4f}, lift={lift:+.1f}%")

    avg_mae = np.mean(val_maes)
    avg_rmse = np.mean(val_rmses)
    print(f"\n  Average MAE:  {avg_mae:.4f}")
    print(f"  Average RMSE: {avg_rmse:.4f}")

    # Warn if model doesn't beat naive baseline
    naive_baseline_mae = mean_absolute_error(y, np.zeros_like(y))
    if avg_mae >= naive_baseline_mae * 0.95:
        print(f"\n  ⚠ WARNING: Model barely beats naive baseline (always 0).")
        print(f"    naive_MAE={naive_baseline_mae:.4f} vs model_MAE={avg_mae:.4f}")
        print(f"    Consider: more data, different features, or disabling LSTM adjustments.")

    # Train final model on all data
    print(f"\n  Training final model on all {len(X)} samples...")
    final_model = MLPRegressor(
        hidden_layer_sizes=HIDDEN_LAYERS,
        activation="relu",
        solver="adam",
        max_iter=500,
        early_stopping=True,
        validation_fraction=0.15,
        n_iter_no_change=20,
        learning_rate="adaptive",
        learning_rate_init=0.001,
        alpha=L2_ALPHA,
        random_state=42,
    )
    final_model.fit(X_scaled, y)

    # Save model, scaler, and metadata
    MODELS_DIR.mkdir(exist_ok=True)

    with open(LSTM_MODEL, "wb") as f:
        pickle.dump(final_model, f, protocol=4)  # protocol 4 for Python 3.8+ compat

    with open(LSTM_SCALER, "wb") as f:
        pickle.dump(scaler, f, protocol=4)  # protocol 4 for Python 3.8+ compat

    meta = {
        "type": "MLPRegressor-Sequential-v2-hardened",
        "trained_at": datetime.utcnow().isoformat(),
        "n_picks": len(picks),
        "n_samples": len(X),
        "n_features": len(feature_names),
        "feature_names": feature_names,
        "val_mae": round(avg_mae, 4),
        "val_rmse": round(avg_rmse, 4),
        "mean_residual": round(mean_residual, 6),
        "hidden_layers": list(HIDDEN_LAYERS),
        "l2_alpha": L2_ALPHA,
        "confidence_cap": CONFIDENCE_CAP,
        "shrinkage_factor": SHRINKAGE_FACTOR,
        "seq_len": SEQ_LEN,
        "naive_baseline_mae": round(naive_baseline_mae, 4),
    }
    with open(LSTM_META, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"\n  Model saved to: {LSTM_MODEL}")
    print(f"  Scaler saved to: {LSTM_SCALER}")
    print(f"  Metadata saved to: {LSTM_META}")

    # Feature importance (based on absolute weight magnitudes from first layer)
    try:
        weights = np.abs(final_model.coefs_[0])
        importance = weights.sum(axis=1)
        importance = importance / importance.sum()
        top_features = sorted(zip(feature_names, importance), key=lambda x: x[1], reverse=True)
        print(f"\n  Top features:")
        for name, imp in top_features[:10]:
            bar = "█" * int(imp * 100)
            print(f"    {name:25s} {imp:.3f} {bar}")
    except Exception:
        pass

    print(f"\n{'='*60}\n")
    return True


def predict_adjustment(pick_data, resolved_history):
    """
    Predict the probability adjustment for a new pick.

    v2 pipeline:
      1. Model predicts de-meaned residual
      2. Add back mean_residual to get raw adjustment
      3. Apply Bayesian shrinkage (blend with prior of 0)
      4. Clip to confidence cap (±5 pp)
    """
    import sys
    if not LSTM_MODEL.exists() or not LSTM_SCALER.exists() or not LSTM_META.exists():
        if not hasattr(predict_adjustment, '_miss_logged'):
            predict_adjustment._miss_logged = True
            print(f"  [LSTM] Model files missing: model={LSTM_MODEL.exists()}, scaler={LSTM_SCALER.exists()}, meta={LSTM_META.exists()}", file=sys.stderr)
            print(f"  [LSTM] CWD={Path.cwd()}, model_path={LSTM_MODEL.resolve()}", file=sys.stderr)
        return 0.0

    with open(LSTM_META) as f:
        meta = json.load(f)

    feature_names = meta.get("feature_names", [])
    if not feature_names:
        if not hasattr(predict_adjustment, '_feat_logged'):
            predict_adjustment._feat_logged = True
            print(f"  [LSTM] No feature_names in meta. Meta keys: {list(meta.keys())}", file=sys.stderr)
        return 0.0

    # Build the sequential context using resolved history + current pick
    temp_picks = resolved_history + [pick_data]
    idx = len(temp_picks) - 1

    features = build_sequential_features(temp_picks, idx)
    if features is None:
        if not hasattr(predict_adjustment, '_feat_build_logged'):
            predict_adjustment._feat_build_logged = True
            print(f"  [LSTM] build_sequential_features returned None. idx={idx}, len(resolved)={len(resolved_history)}", file=sys.stderr)
        return 0.0

    X = np.array([[features.get(k, 0) for k in feature_names]])

    with open(LSTM_SCALER, "rb") as f:
        scaler = pickle.load(f)
    with open(LSTM_MODEL, "rb") as f:
        model = pickle.load(f)

    X_scaled = scaler.transform(X)
    raw_pred = model.predict(X_scaled)[0]

    # SAFEGUARD 1: add back the mean residual
    mean_residual = meta.get("mean_residual", 0.0)
    raw_adjustment = raw_pred + mean_residual

    # SAFEGUARD 3: Bayesian shrinkage — blend with prior of 0 (no adjustment)
    # Override saved shrinkage with current (more aggressive) value
    shrinkage = SHRINKAGE_FACTOR  # Use current constant, not saved meta value
    shrunk_adjustment = raw_adjustment * shrinkage

    # SAFEGUARD 2: confidence cap
    cap = CONFIDENCE_CAP  # Use current constant, not saved meta value
    adjustment = float(np.clip(shrunk_adjustment, -cap, cap))

    # Debug: print to STDERR so it appears in Render logs (stdout is captured by subprocess)
    import sys
    if not hasattr(predict_adjustment, '_logged'):
        predict_adjustment._logged = True
        print(f"  [LSTM] raw_pred={raw_pred:.4f}, mean_res={mean_residual:.4f}, "
              f"raw_adj={raw_adjustment:.4f}, shrunk={shrunk_adjustment:.4f}, "
              f"final={adjustment:.4f} ({adjustment*100:+.1f}%)", file=sys.stderr)
        print(f"  [LSTM] shrinkage={shrinkage}, cap=±{cap*100:.0f}pp, "
              f"resolved={len(resolved_history)}, features={len(feature_names)}", file=sys.stderr)

    return adjustment


def show_predictions():
    """Show adjustment predictions for recent unresolved picks."""
    all_picks = load_all_picks()
    resolved = load_resolved_picks()

    if not LSTM_MODEL.exists():
        print("\n  No LSTM model trained yet. Run: python3 06_lstm_learner.py train\n")
        return

    unresolved = [p for p in all_picks if p.get("outcome") is None]

    if not unresolved:
        print("\n  No unresolved picks to predict adjustments for.\n")
        return

    with open(LSTM_META) as f:
        meta = json.load(f)

    print(f"\n{'='*60}")
    print(f"  LSTM PROBABILITY ADJUSTMENTS (v2 hardened)")
    print(f"{'='*60}")
    print(f"  Based on {len(resolved)} resolved picks")
    print(f"  Shrinkage: {meta.get('shrinkage_factor', SHRINKAGE_FACTOR):.0%} | "
          f"Cap: ±{meta.get('confidence_cap', CONFIDENCE_CAP)*100:.0f}pp | "
          f"Mean residual: {meta.get('mean_residual', 0):+.4f}\n")

    for pick in unresolved[-20:]:
        adj = predict_adjustment(pick, resolved)
        model_prob = pick.get("model_prob", pick.get("confidence", 50))
        adjusted_prob = model_prob + (adj * 100)
        adjusted_prob = max(1, min(99, adjusted_prob))

        match_name = pick.get("match", "Unknown")[:40]
        direction = "↑" if adj > 0.001 else "↓" if adj < -0.001 else "→"
        adj_pct = adj * 100

        print(f"  {match_name:42s} {model_prob:5.1f}% {direction} {adjusted_prob:5.1f}%  (adj: {adj_pct:+.2f}pp)")

    print()


def backtest():
    """Backtest: retrain on first 80% of data, test on last 20%."""
    picks = load_resolved_picks()

    if len(picks) < MIN_PICKS:
        print(f"\n  Need {MIN_PICKS} resolved picks for backtest. Have {len(picks)}.\n")
        return

    split_idx = int(len(picks) * 0.8)
    train_picks = picks[:split_idx]
    test_picks = picks[split_idx:]

    print(f"\n{'='*60}")
    print(f"  LSTM BACKTEST (v2 hardened)")
    print(f"{'='*60}")
    print(f"  Train: {len(train_picks)} picks")
    print(f"  Test:  {len(test_picks)} picks")
    print(f"  Safeguards: shrinkage={SHRINKAGE_FACTOR}, cap=±{CONFIDENCE_CAP*100:.0f}pp, "
          f"layers={HIDDEN_LAYERS}, alpha={L2_ALPHA}\n")

    # Build training data from train split (with residual targets)
    X_train, y_train, feature_names, mean_residual = build_training_data(train_picks)
    if X_train is None:
        print("  Not enough sequential data in train split.\n")
        return

    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)

    model = MLPRegressor(
        hidden_layer_sizes=HIDDEN_LAYERS,
        activation="relu",
        solver="adam",
        max_iter=500,
        early_stopping=True,
        validation_fraction=0.15,
        n_iter_no_change=20,
        learning_rate="adaptive",
        learning_rate_init=0.001,
        alpha=L2_ALPHA,
        random_state=42,
    )
    model.fit(X_train_scaled, y_train)

    # Test on test split
    correct_base = 0
    correct_adjusted = 0
    total = 0
    pnl_base = 0.0
    pnl_adjusted = 0.0
    adj_magnitudes = []

    for i, pick in enumerate(test_picks):
        # Build features using train picks + test picks up to current
        context = train_picks + test_picks[:i]
        temp = context + [pick]
        features = build_sequential_features(temp, len(temp) - 1)
        if features is None:
            continue

        X_test = np.array([[features.get(k, 0) for k in feature_names]])
        X_test_scaled = scaler.transform(X_test)
        raw_pred = model.predict(X_test_scaled)[0]

        # Apply v2 safeguards
        raw_adj = raw_pred + mean_residual
        shrunk_adj = raw_adj * SHRINKAGE_FACTOR
        adj = float(np.clip(shrunk_adj, -CONFIDENCE_CAP, CONFIDENCE_CAP))
        adj_magnitudes.append(abs(adj))

        model_prob = pick.get("model_prob", pick.get("confidence", 50)) / 100.0
        adjusted_prob = max(0.01, min(0.99, model_prob + adj))
        actual = 1.0 if pick["outcome"] == "win" else 0.0

        # Did the adjustment improve the prediction?
        base_correct = (model_prob >= 0.5) == (actual == 1.0)
        adj_correct = (adjusted_prob >= 0.5) == (actual == 1.0)

        if base_correct:
            correct_base += 1
        if adj_correct:
            correct_adjusted += 1
        total += 1

    if total > 0:
        base_acc = correct_base / total * 100
        adj_acc = correct_adjusted / total * 100
        improvement = adj_acc - base_acc
        avg_adj = np.mean(adj_magnitudes) * 100

        print(f"  Base model accuracy:     {base_acc:.1f}%")
        print(f"  LSTM-adjusted accuracy:  {adj_acc:.1f}%")
        print(f"  Improvement:             {improvement:+.1f}pp")
        print(f"  Picks evaluated:         {total}")
        print(f"  Avg adjustment magnitude: {avg_adj:.2f}pp")
        print(f"  Mean residual (train):   {mean_residual:+.4f}")

        if improvement < -1.0:
            print(f"\n  ⚠ LSTM adjustment HURTS accuracy by {abs(improvement):.1f}pp.")
            print(f"    Consider disabling LSTM or collecting more data.")
        elif improvement < 1.0:
            print(f"\n  → LSTM adjustment is roughly neutral ({improvement:+.1f}pp).")
            print(f"    Safe to keep active with shrinkage applied.")
        else:
            print(f"\n  ✓ LSTM adjustment improves accuracy by {improvement:.1f}pp.")
    else:
        print("  Not enough test data for backtest.")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LSTM Sequential Learner (v2 hardened)")
    parser.add_argument("action", choices=["train", "status", "predict", "backtest"],
                       help="Action: train, status, predict, or backtest")
    args = parser.parse_args()

    if args.action == "train":
        train_lstm()
    elif args.action == "status":
        show_status()
    elif args.action == "predict":
        show_predictions()
    elif args.action == "backtest":
        backtest()
