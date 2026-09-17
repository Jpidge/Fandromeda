import duckdb
import nflreadpy as nfl
import pandas as pd

print("🚀 Pulling nflverse player stats...")
# Load recent NFL season weekly data
df_nfl = nfl.load_player_stats([2025]).to_pandas()
df_rosters = pd.read_csv("all_league_rosters.csv")


# Clean player names for accurate matching
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


df_rosters["clean_name"] = df_rosters["player_name"].apply(clean_name)
df_nfl["clean_name"] = df_nfl["player_display_name"].apply(clean_name)

# DuckDB SQL Pipeline: Window Functions & Rolling Trends
trend_query = """
WITH weekly_metrics AS (
    SELECT 
        clean_name,
        player_display_name as player_name,
        position,
        team,
        week,
        COALESCE(target_share, 0.0) as target_share,
        COALESCE(air_yards_share, 0.0) as air_yards_share,
        -- Calculate WOPR (Weighted Opportunity Rating)
        (1.5 * COALESCE(target_share, 0.0) + 0.7 * COALESCE(air_yards_share, 0.0)) as wopr,
        COALESCE(fantasy_points_ppr, 0.0) as fantasy_points_ppr
    FROM df_nfl
    WHERE position IN ('WR', 'TE', 'RB')
),

rolling_stats AS (
    SELECT 
        clean_name,
        player_name,
        position,
        team,
        week,
        target_share,
        air_yards_share,
        wopr,
        fantasy_points_ppr,
        
        -- 3-Week Rolling Averages
        AVG(target_share) OVER (
            PARTITION BY clean_name ORDER BY week 
            ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
        ) as roll_3wk_tgt_share,
        
        AVG(air_yards_share) OVER (
            PARTITION BY clean_name ORDER BY week 
            ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
        ) as roll_3wk_ay_share,
        
        AVG(wopr) OVER (
            PARTITION BY clean_name ORDER BY week 
            ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
        ) as roll_3wk_wopr,
        
        -- Fallback to first available week if player doesn't have 3 active games yet
        target_share - COALESCE(
            LAG(target_share, 2) OVER (PARTITION BY clean_name ORDER BY week),
            LAG(target_share, 1) OVER (PARTITION BY clean_name ORDER BY week),
            target_share
        ) as tgt_share_surge
    FROM weekly_metrics
),

latest_week_snapshot AS (
    -- Grab each player's most recent active week
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY clean_name ORDER BY week DESC) as rn
    FROM rolling_stats
)

SELECT 
    player_name,
    position,
    team,
    ROUND(roll_3wk_tgt_share, 3) as roll_3wk_tgt_share,
    ROUND(roll_3wk_ay_share, 3) as roll_3wk_ay_share,
    ROUND(roll_3wk_wopr, 3) as roll_3wk_wopr,
    ROUND(tgt_share_surge, 3) as tgt_share_surge
FROM latest_week_snapshot
WHERE rn = 1
  -- Exclude rostered players
  AND clean_name NOT IN (SELECT clean_name FROM df_rosters)
ORDER BY roll_3wk_wopr DESC
LIMIT 15;
"""

print(
    "\n🔥 TOP UNCLAIMED WAIVER SURGE TARGETS (Ranked by 3-Wk Target Share Velocity):"
)
df_surges = duckdb.query(trend_query).df()
print(df_surges.to_string(index=False))