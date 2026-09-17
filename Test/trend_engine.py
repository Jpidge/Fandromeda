import duckdb
import nflreadpy as nfl
import pandas as pd

print("🚀 Loading nflverse multi-season data...")
df_nfl = nfl.load_player_stats([2025, 2026]).to_pandas()
df_rosters = pd.read_csv("all_league_rosters.csv")


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

# Core Analytical Pipeline
base_query = """
WITH prior_season AS (
    SELECT 
        clean_name,
        AVG(1.5 * COALESCE(target_share, 0) + 0.7 * COALESCE(air_yards_share, 0)) as prior_wopr
    FROM df_nfl
    WHERE season = 2025 AND position IN ('WR', 'TE', 'RB')
    GROUP BY clean_name
),

current_season AS (
    SELECT 
        clean_name,
        player_display_name as player_name,
        position,
        team,
        week,
        COALESCE(fantasy_points_ppr, 0.0) as ppr_pts,
        AVG(1.5 * COALESCE(target_share, 0) + 0.7 * COALESCE(air_yards_share, 0)) OVER (
            PARTITION BY clean_name ORDER BY week 
            ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
        ) as curr_3wk_wopr,
        AVG(COALESCE(fantasy_points_ppr, 0.0)) OVER (
            PARTITION BY clean_name ORDER BY week 
            ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
        ) as curr_3wk_ppr,
        COUNT(week) OVER (PARTITION BY clean_name) as weeks_played
    FROM df_nfl
    WHERE season = 2026 AND position IN ('WR', 'TE', 'RB')
),

latest_curr AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY clean_name ORDER BY week DESC) as rn
    FROM current_season
),

blended_metrics AS (
    SELECT 
        c.player_name,
        c.clean_name,
        c.position,
        c.team,
        c.curr_3wk_ppr,
        (GREATEST(0.0, (4.0 - c.weeks_played) / 4.0) * COALESCE(p.prior_wopr, c.curr_3wk_wopr)) +
        ((1.0 - GREATEST(0.0, (4.0 - c.weeks_played) / 4.0)) * c.curr_3wk_wopr) as blended_wopr,
        
        -- SURGE METRIC: Delta between current blended volume vs. prior week window
        (
            (GREATEST(0.0, (4.0 - c.weeks_played) / 4.0) * COALESCE(p.prior_wopr, c.curr_3wk_wopr)) +
            ((1.0 - GREATEST(0.0, (4.0 - c.weeks_played) / 4.0)) * c.curr_3wk_wopr)
        ) - COALESCE(
            LAG(c.curr_3wk_wopr, 2) OVER (PARTITION BY c.clean_name ORDER BY c.week),
            c.curr_3wk_wopr
        ) as wopr_surge
    FROM latest_curr c
    LEFT JOIN prior_season p ON c.clean_name = p.clean_name
    WHERE c.rn = 1
)
SELECT * FROM blended_metrics;
"""

df_analytics = duckdb.query(base_query).df()

# TABLE 1: UNCLAIMED WAIVER SURGE TARGETS
waiver_query = """
SELECT 
    player_name, position, team,
    ROUND(blended_wopr, 3) as wopr_opp,
    ROUND(wopr_surge, 3) as wopr_surge,
    ROUND(curr_3wk_ppr, 1) as ppr_avg,
    CASE 
        WHEN blended_wopr >= 0.45 AND curr_3wk_ppr < 10.0 THEN '🚨 BUY LOW / WAIVER TARGET'
        WHEN blended_wopr < 0.25 AND curr_3wk_ppr >= 13.0 THEN '⚠️ SELL HIGH / FLUKE RISK'
        WHEN blended_wopr >= 0.45 AND curr_3wk_ppr >= 13.0 THEN '🔥 HIGH-VOLUME ALPHA'
        WHEN wopr_surge >= 0.100 THEN '📈 SURGING WORKLOAD'
        ELSE '👀 BENCH / STASH'
    END as fantastradamus_label
FROM df_analytics
WHERE clean_name NOT IN (SELECT clean_name FROM df_rosters)
ORDER BY wopr_surge DESC
LIMIT 15;
"""

print(
    "\n🔥 TABLE 1: UNCLAIMED WAIVER WIRE (Ranked by WOPR Surge / Workload Velocity):"
)
print(duckdb.query(waiver_query).df().to_string(index=False))

# TABLE 2: SQUAD EVALUATION
my_team = "Vader's Raiders"
my_team_query = f"""
SELECT 
    r.roster_pos, bm.player_name, bm.position, bm.team,
    ROUND(bm.blended_wopr, 3) as wopr_opp,
    ROUND(bm.wopr_surge, 3) as wopr_surge,
    ROUND(bm.curr_3wk_ppr, 1) as ppr_avg,
    CASE 
        WHEN bm.blended_wopr < 0.20 AND bm.curr_3wk_ppr < 8.0 THEN '✂️ DROP CANDIDATE'
        WHEN bm.blended_wopr < 0.25 AND bm.curr_3wk_ppr >= 13.0 THEN '⚠️ FLUKE RISK (Trade Out)'
        WHEN bm.blended_wopr >= 0.45 THEN '🔥 HIGH VOLUME CORE'
        ELSE '👀 HOLD'
    END as roster_verdict
FROM df_rosters r
JOIN df_analytics bm ON r.clean_name = bm.clean_name
WHERE r.fantasy_team = '{my_team.replace("'", "''")}'
ORDER BY wopr_opp DESC;
"""

print(f"\n🏈 TABLE 2: SQUAD EVALUATION FOR: {my_team}")
print(duckdb.query(my_team_query).df().to_string(index=False))