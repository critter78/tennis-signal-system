# V2 Dataset Diff Report
_Generated: 2026-04-20T05:19:25_

## Composition
- TML ATP tour-level (2019–2026): **15,636** matches
- TML ATP challenger (2019–2026): **30,000** matches
- Sackmann WTA (2018–2024): **10,690** matches
- Dropped bad-date rows: 126
- Dedup removed: 542
- **V2 total: 55,658 matches**
- V1 total (production): 36,342
- Δ: **+19,316** matches vs v1

## Coverage
- Date range: 2021-01-04 → 2026-04-12
- Sources:
  - `tml_challenger`: 29,813
  - `tml_tour`: 15,155
  - `sackmann_wta`: 10,690

## Tour / level breakdown
- **ATP** (44,968): {'C': 29813, '250': 5376, 'M': 3255, 'G': 2541, '500': 2261, 'D': 1153, 'A': 366, 'O': 128, 'F': 75}
- **WTA** (10,690): {'I': 3382, 'P': 2998, 'G': 2032, 'PM': 1134, 'D': 635, 'W': 306, 'O': 128, 'F': 75}

## Indoor/outdoor (TML only)
- Indoor flag true: 3,797
- Indoor flag false: 51,861

## Notes
- 2018 Sackmann ATP data was **excluded** (TML starts 2019 per user request)
- 2025 and 2026 YTD fully included (16 months of previously-missing data)
- Challenger matches tagged via `tourney_level='C'` and `is_challenger=True`
- `indoor_flag` is new — can be added to feature set in v2 model training
- WTA remains at Sackmann 2018–2024 until a WTA continuation source is wired