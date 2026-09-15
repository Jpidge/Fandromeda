import duckdb
import nflreadpy as nfl
import pandas as pd

print("🏈 Loading 2025 historical data for verdict backtesting...")

# 1. Load historical data (2025 season)
df_nfl = nfl.load_player_stats([2025]).to_pandas()

# Filter valid positions
valid_positions = ["WR", "TE", "RB", "QB"]
df_nfl = df_nfl[df_nfl["position"].isin(valid_positions)].copy()


# 2. Add clean_name column directly to df_nfl
def clean_name(name):
    if not isinstance(name, str):
        return name
    suffixes = [
        r"\bJr\.?$",
        r"\bSr\.?$",
        r"\bIII$",
        r"\bII$",
        r"\bIV$",
        r"\bV$",
    ]
    cleaned = name.strip()
    for s in suffixes:
        cleaned = pd.Series(cleaned).str.replace(s, "", regex=True).iloc[0]
    return cleaned.strip()


if "player_display_name" in df_nfl.columns:
    df_nfl["clean_name"] = df_nfl["player_display_name"].apply(clean_name)
else:
    df_nfl["clean_name"] = df_nfl["player_name"].apply(clean_name)

# Safely handle column nulls
cols = [
    "targets",
    "carries",
    "attempts",
    "target_share",
    "air_yards_share",
    "rushing_share",
    "fantasy_points_ppr",
]
for col in cols:
    if col in df_nfl.columns:
        df_nfl[col] = df_nfl[col].fillna(0.0)
    else:
        df_nfl[col] = 0.0


# 3. Compute opportunity score in Pandas with missing share fallbacks
def calc_pos_opp(row):
    pos = row["position"]
    carries = row.get("carries", 0.0)
    targets = row.get("targets", 0.0)
    attempts = row.get("attempts", 0.0)

    target_share = row.get("target_share", 0.0)
    air_yards_share = row.get("air_yards_share", 0.0)
    rushing_share = row.get("rushing_share", 0.0)

    # Fallback logic if shares are missing/0.0 despite active touches
    if rushing_share == 0.0 and carries > 0:
        rushing_share = min(1.0, carries / 25.0)

    if target_share == 0.0 and targets > 0:
        target_share = min(1.0, targets / 35.0)

    if pos in ["WR", "TE"]:
        return 1.5 * target_share + 0.7 * air_yards_share
    elif pos == "RB":
        return rushing_share + 1.5 * target_share
    elif pos == "QB":
        return (attempts + carries) / 50.0
    return 0.0


df_nfl["pos_opp_score"] = df_nfl.apply(calc_pos_opp, axis=1)

# 4. DuckDB query: Calculate Verdicts for Week N, then Join Week N+1 Actual PPR Points
smoke_test_sql = """
WITH weekly_metrics AS (
    SELECT 
        clean_name,
        COALESCE(player_display_name, player_name) as player_name,
        position,
        week,
        fantasy_points_ppr as ppr_pts,
        pos_opp_score,
        carries,
        targets,
        
        -- Trailing 3-week averages up to Week N
        AVG(pos_opp_score) OVER (PARTITION BY clean_name ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) as trailing_3wk_opp,
        AVG(fantasy_points_ppr) OVER (PARTITION BY clean_name ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) as trailing_3wk_ppr,
        AVG(carries + targets) OVER (PARTITION BY clean_name ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) as trailing_3wk_touches,
        AVG(targets) OVER (PARTITION BY clean_name ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) as trailing_3wk_targets,
        LAG(pos_opp_score, 2, pos_opp_score) OVER (PARTITION BY clean_name ORDER BY week) as prev_opp_base
    FROM df_nfl
),
verdicts_assigned AS (
    SELECT 
        *,
        (trailing_3wk_opp - prev_opp_base) as opp_surge,
        CASE 
            -- BUY LOW: Require minimum target floor so low-volume deep threats aren't flagged
            WHEN trailing_3wk_opp >= 0.45 AND trailing_3wk_targets >= 4.0 AND trailing_3wk_ppr < 10.0 THEN '🚨 BUY LOW / TARGET'
            -- FLUKE RISK: Require low opportunity AND low raw touch count (<8 touches/gm)
            WHEN trailing_3wk_opp < 0.22 AND trailing_3wk_touches < 8.0 AND trailing_3wk_ppr >= 13.0 THEN '⚠️ SELL HIGH / FLUKE'
            WHEN trailing_3wk_opp >= 0.45 AND trailing_3wk_ppr >= 13.0 THEN '🔥 HIGH-VOLUME ALPHA'
            WHEN (trailing_3wk_opp - prev_opp_base) >= 0.100 THEN '📈 SURGING WORKLOAD'
            ELSE '👀 STASH'
        END as predicted_verdict
    FROM weekly_metrics
)
SELECT 
    v.week as test_week,
    v.player_name,
    v.position,
    v.predicted_verdict,
    ROUND(v.trailing_3wk_opp, 3) as opp_score_at_test,
    ROUND(v.trailing_3wk_ppr, 1) as ppr_avg_at_test,
    ROUND(nxt.fantasy_points_ppr, 1) as actual_next_week_ppr
FROM verdicts_assigned v
JOIN df_nfl nxt ON v.clean_name = nxt.clean_name AND nxt.week = v.week + 1
WHERE v.predicted_verdict IN ('🚨 BUY LOW / TARGET', '⚠️ SELL HIGH / FLUKE', '📈 SURGING WORKLOAD')
ORDER BY v.week ASC, v.predicted_verdict;
"""

df_test_results = duckdb.query(smoke_test_sql).df()

print("✅ Historical Backtest Complete! Displaying Sample Results:")
print(df_test_results.head(25).to_string(index=False))