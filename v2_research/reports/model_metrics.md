# V1 vs V2 Model Comparison

**Holdout:** 2025-10-01 → 2026-04-12 (ATP tour-level only, 1,264 matches)

## Training sets
- V1: Sackmann 2018–2024 (36,342 matches) — sampled 2,500
- V2: TML 2019–2026 + WTA (67,343 pre-holdout) — sampled 2,500

## Headline metrics
| Metric | V1 | V2 | Δ (V2 − V1) |
|---|---|---|---|
| AUC (↑ better) | 0.6578 | **0.6638** | +0.0060 |
| Brier (↓ better) | 0.2349 | **0.2293** | -0.0056 |
| Log loss (↓ better) | 0.6614 | **0.6493** | -0.0121 |
| Accuracy @0.5 | 0.5752 | **0.6052** | +0.0300 |

**Verdict:** V2 WINS

## Calibration — V1
| bin       |   n |    pred |   actual |
|:----------|----:|--------:|---------:|
| 0.00-0.10 |   0 | nan     |  nan     |
| 0.10-0.20 |   1 |   0.107 |    0     |
| 0.20-0.30 |  81 |   0.27  |    0.173 |
| 0.30-0.40 | 251 |   0.356 |    0.311 |
| 0.40-0.50 | 780 |   0.447 |    0.521 |
| 0.50-0.60 | 111 |   0.54  |    0.712 |
| 0.60-0.70 |  36 |   0.651 |    0.806 |
| 0.70-0.80 |   4 |   0.735 |    1     |
| 0.80-0.90 |   0 | nan     |  nan     |
| 0.90-1.00 |   0 | nan     |  nan     |

## Calibration — V2
| bin       |   n |   pred |   actual |
|:----------|----:|-------:|---------:|
| 0.00-0.10 |   2 |  0.094 |    0     |
| 0.10-0.20 |  24 |  0.156 |    0.083 |
| 0.20-0.30 | 102 |  0.254 |    0.225 |
| 0.30-0.40 | 273 |  0.351 |    0.366 |
| 0.40-0.50 | 388 |  0.449 |    0.495 |
| 0.50-0.60 | 345 |  0.549 |    0.568 |
| 0.60-0.70 |  95 |  0.64  |    0.705 |
| 0.70-0.80 |  27 |  0.737 |    0.889 |
| 0.80-0.90 |   6 |  0.834 |    0.667 |
| 0.90-1.00 |   2 |  0.916 |    1     |

## Top 10 disagreements (|v1 − v2| largest)
| date       | winner            | loser              |   v1_prob |   v2_prob |   delta |   label |
|:-----------|:------------------|:-------------------|----------:|----------:|--------:|--------:|
| 2025-10-27 | Cameron Norrie    | Carlos Alcaraz     |     0.404 |     0.839 |  -0.436 |       0 |
| 2026-02-06 | Vilius Gaubas     | Amit Vales         |     0.504 |     0.912 |  -0.408 |       1 |
| 2026-02-06 | Vilius Gaubas     | Ofek Shimanov      |     0.588 |     0.919 |  -0.331 |       1 |
| 2025-10-27 | Alexander Bublik  | Corentin Moutet    |     0.404 |     0.73  |  -0.326 |       1 |
| 2026-02-06 | Valentin Vacherot | Alexander Bublik   |     0.402 |     0.723 |  -0.321 |       0 |
| 2025-10-20 | Cameron Norrie    | Andrey Rublev      |     0.304 |     0.614 |  -0.31  |       0 |
| 2026-03-21 | Matteo Berrettini | Alexander Bublik   |     0.408 |     0.711 |  -0.303 |       0 |
| 2025-10-20 | Alexander Bublik  | Alejandro Tabilo   |     0.447 |     0.75  |  -0.303 |       1 |
| 2026-02-12 | Luciano Darderi   | Tomas Barrios Vera |     0.489 |     0.776 |  -0.286 |       1 |
| 2026-03-07 | Jakub Mensik      | Marcos Giron       |     0.408 |     0.692 |  -0.284 |       1 |

## V2 feature importance (top 15)
| feature             |   importance |
|:--------------------|-------------:|
| rank_ratio          |    0.0637434 |
| df_diff             |    0.0420896 |
| first_in_diff       |    0.0346461 |
| ace_diff            |    0.0286913 |
| a_win_rate_52w      |    0.0273379 |
| win2nd_diff         |    0.0272026 |
| win1st_diff         |    0.0261199 |
| b_n_matches_52w     |    0.0253079 |
| a_win_rate_l20      |    0.0223305 |
| a_days_since_last   |    0.0220598 |
| rank_diff           |    0.0212478 |
| b_win_rate_surf_52w |    0.0209771 |
| a_win_rate_surf_52w |    0.0200298 |
| form_diff_52w       |    0.0197591 |
| b_win_rate_52w      |    0.0139396 |

## Interpretation
- AUC Δ > 0.005 is a meaningful improvement in rank ordering
- Brier Δ < -0.002 means v2 is better-calibrated on average
- Large disagreements (|Δ| > 0.10) on matches where v2 was correct and v1 wasn't are the clearest wins for v2
- If accuracy is close but Brier is very different, v2 is mainly better at *how confident* it is

_Produced by compare_v1_v2.py — no production files touched_