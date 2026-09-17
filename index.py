from datetime import datetime
import json
import os
import webbrowser
import duckdb
import nflreadpy as nfl
import pandas as pd

print(
    "🚀 Loading nflverse multi-position data & building Fandromeda Interactive"
    " Dashboard..."
)

# 1. Load Data
df_nfl = nfl.load_player_stats([2025, 2026]).to_pandas()
df_rosters = pd.read_csv("all_league_rosters.csv")

# Load Depth Charts and Injury Data
try:
    df_depth = nfl.load_depth_charts([2026]).to_pandas()
except Exception:
    df_depth = pd.DataFrame(
        columns=["club_code", "full_name", "depth_team", "position"]
    )

try:
    df_injuries = nfl.load_injuries([2026]).to_pandas()
except Exception:
    df_injuries = pd.DataFrame(
        columns=["team", "full_name", "report_status", "practice_status"]
    )

# Load Schedules for SOS and Weather
try:
    df_schedules = nfl.load_schedules([2026]).to_pandas()
except Exception:
    df_schedules = pd.DataFrame(
        columns=[
            "season",
            "week",
            "home_team",
            "away_team",
            "roof",
            "temp",
            "wind",
        ]
    )

# 2. Timestamps
csv_path = "all_league_rosters.csv"
csv_mtime = os.path.getmtime(csv_path)
yahoo_last_updated = datetime.fromtimestamp(csv_mtime).strftime(
    "%Y-%m-%d %H:%M:%S"
)
nfl_last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 3. Name Cleaning Routine
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
df_depth["clean_name"] = (
    df_depth["full_name"].apply(clean_name)
    if "full_name" in df_depth.columns
    else ""
)
df_injuries["clean_name"] = (
    df_injuries["full_name"].apply(clean_name)
    if "full_name" in df_injuries.columns
    else ""
)

# Depth & Injury Clean
if not df_depth.empty and "depth_team" in df_depth.columns:
    df_depth_clean = (
        df_depth.sort_values("depth_team")
        .groupby("clean_name")
        .first()
        .reset_index()[["clean_name", "depth_team"]]
    )
else:
    df_depth_clean = pd.DataFrame(columns=["clean_name", "depth_team"])

if not df_injuries.empty and "report_status" in df_injuries.columns:
    df_injuries_clean = (
        df_injuries.groupby("clean_name")
        .first()
        .reset_index()[["clean_name", "report_status"]]
    )
else:
    df_injuries_clean = pd.DataFrame(columns=["clean_name", "report_status"])

# Filter Valid Positions
valid_positions = ["WR", "TE", "RB", "QB"]
df_nfl = df_nfl[df_nfl["position"].isin(valid_positions)].copy()

cols_to_fill = [
    "targets",
    "carries",
    "attempts",
    "target_share",
    "air_yards_share",
    "rushing_share",
    "fantasy_points_ppr",
    "air_yards",
    "receiving_yards",
    "receiving_yards_after_catch",
]
for col in cols_to_fill:
    if col in df_nfl.columns:
        df_nfl[col] = df_nfl[col].fillna(0.0)
    else:
        df_nfl[col] = 0.0

# 4. Air Yards Calculation (With Safe Null Handling for Early Season Data)
if "air_yards" in df_nfl.columns:
    df_nfl["air_yards"] = df_nfl["air_yards"].fillna(0.0)
else:
    df_nfl["air_yards"] = 0.0

if "receiving_yards" in df_nfl.columns:
    df_nfl["receiving_yards"] = df_nfl["receiving_yards"].fillna(0.0)
else:
    df_nfl["receiving_yards"] = 0.0

yac_col = (
    "receiving_yards_after_catch"
    if "receiving_yards_after_catch" in df_nfl.columns
    else "yards_after_catch"
)
if yac_col in df_nfl.columns:
    df_nfl[yac_col] = df_nfl[yac_col].fillna(0.0)
else:
    df_nfl[yac_col] = 0.0

df_nfl["completed_air_yards"] = (
    df_nfl["receiving_yards"] - df_nfl[yac_col]
).clip(lower=0.0)
df_nfl["unrealized_air_yards"] = (
    df_nfl["air_yards"] - df_nfl["completed_air_yards"]
).clip(lower=0.0)


# Position Opportunity Score
def calc_pos_opp(row):
    pos = row["position"]
    carries, targets, attempts = (
        row.get("carries", 0.0),
        row.get("targets", 0.0),
        row.get("attempts", 0.0),
    )
    target_share, air_yards_share, rushing_share = (
        row.get("target_share", 0.0),
        row.get("air_yards_share", 0.0),
        row.get("rushing_share", 0.0),
    )
    if rushing_share == 0.0 and carries > 0:
        rushing_share = min(1.0, carries / 25.0)
    if target_share == 0.0 and targets > 0:
        target_share = min(1.0, targets / 35.0)

    if pos in ["WR", "TE"]:
        return (1.5 * target_share) + (0.7 * air_yards_share)
    elif pos == "RB":
        return rushing_share + (1.5 * target_share)
    elif pos == "QB":
        return (attempts + carries) / 50.0
    return 0.0


df_nfl["pos_opp_score"] = df_nfl.apply(calc_pos_opp, axis=1)

all_teams = sorted(df_rosters["fantasy_team"].dropna().unique().tolist())
default_team = (
    "Vader's Raiders" if "Vader's Raiders" in all_teams else all_teams[0]
)

# 5. Calculate Defensive SOS Rankings (Fantasy Points Allowed per Position)
def_points_allowed_query = """
SELECT 
    opponent_team as def_team, position, 
    AVG(fantasy_points_ppr) as avg_pts_allowed,
    RANK() OVER (PARTITION BY position ORDER BY AVG(fantasy_points_ppr) ASC) as def_rank
FROM df_nfl
WHERE season = 2026
GROUP BY opponent_team, position
"""
df_def_sos = duckdb.query(def_points_allowed_query).df()

# 6. Dynamic Current NFL Week Detection (Filtered to 2026 Regular Season Weeks 1-18)
df_curr_season = df_nfl[df_nfl["season"] == 2026]

if not df_curr_season.empty:
    max_active_week = int(df_curr_season["week"].max())
    current_week = min(max_active_week, 15)
else:
    current_week = 1

next_weeks = [current_week + 1, current_week + 2, current_week + 3]

sched_query = f"""
SELECT 
    week, home_team, away_team, roof, COALESCE(wind, 0) as wind, COALESCE(temp, 70) as temp
FROM df_schedules
WHERE season = 2026 AND week IN ({', '.join(map(str, next_weeks))})
"""
df_upcoming_games = duckdb.query(sched_query).df()

# Map Next 3 Matchups & Weather per NFL Team (Weather flags restricted to Next Week ONLY)
team_sos_list = []
nfl_teams = df_nfl["team"].dropna().unique()

for team in nfl_teams:
    team_row = {"Team": team}
    for w_idx, w in enumerate(next_weeks):
        game = df_upcoming_games[
            (df_upcoming_games["week"] == w)
            & (
                (df_upcoming_games["home_team"] == team)
                | (df_upcoming_games["away_team"] == team)
            )
        ]
        if not game.empty:
            g = game.iloc[0]
            is_home = g["home_team"] == team
            opp = g["away_team"] if is_home else g["home_team"]
            prefix = "vs " if is_home else "@ "

            # Weather flags evaluate ONLY for immediate next week (w_idx == 0)
            weather_flags = ""
            if w_idx == 0:
                is_outdoor = str(g["roof"]).lower() in ["outdoors", "open"]
                wind_speed = float(g["wind"])
                temp = float(g["temp"])

                if is_outdoor:
                    if wind_speed >= 15.0:
                        weather_flags += " 💨"
                    if temp <= 32.0:
                        weather_flags += " 🥶"
                    elif temp >= 90.0:
                        weather_flags += " 🔥"

            team_row[f"W{w}"] = f"{prefix}{opp}{weather_flags}"
            team_row[f"W{w}_opp"] = opp
        else:
            team_row[f"W{w}"] = "BYE"
            team_row[f"W{w}_opp"] = "BYE"
    team_sos_list.append(team_row)

df_team_sos = pd.DataFrame(team_sos_list)

# 7. DuckDB Base Analytics Query
base_query = """
WITH prior_season AS (
    SELECT clean_name, AVG(pos_opp_score) as prior_opp
    FROM df_nfl WHERE season = 2025 GROUP BY clean_name
),
current_season AS (
    SELECT 
        clean_name, player_display_name as player_name, position, team, week, 
        fantasy_points_ppr as ppr_pts, targets, carries, attempts as pass_attempts, 
        target_share, air_yards_share, rushing_share, pos_opp_score, unrealized_air_yards,
        AVG(pos_opp_score) OVER (PARTITION BY clean_name ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) as curr_3wk_opp,
        AVG(fantasy_points_ppr) OVER (PARTITION BY clean_name ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) as curr_3wk_ppr,
        AVG(carries + targets) OVER (PARTITION BY clean_name ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) as curr_3wk_touches,
        AVG(targets) OVER (PARTITION BY clean_name ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) as curr_3wk_targets,
        AVG(unrealized_air_yards) OVER (PARTITION BY clean_name ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) as curr_3wk_unrealized_ay,
        LAG(pos_opp_score, 2, pos_opp_score) OVER (PARTITION BY clean_name ORDER BY week) as prev_opp_base,
        COUNT(week) OVER (PARTITION BY clean_name) as weeks_played
    FROM df_nfl WHERE season = 2026
),
latest_curr AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY clean_name ORDER BY week DESC) as rn FROM current_season
),
blended_metrics AS (
    SELECT 
        c.player_name, c.clean_name, c.position, c.team, c.curr_3wk_ppr, c.curr_3wk_touches, c.curr_3wk_targets,
        ROUND(c.curr_3wk_unrealized_ay, 1) as curr_3wk_unrealized_ay,
        COALESCE(d.depth_team, 1) as depth_rank,
        COALESCE(i.report_status, 'Active') as injury_status,
        (GREATEST(0.0, (4.0 - c.weeks_played) / 4.0) * COALESCE(p.prior_opp, c.curr_3wk_opp)) +
        ((1.0 - GREATEST(0.0, (4.0 - c.weeks_played) / 4.0)) * c.curr_3wk_opp) as blended_opp,
        ((GREATEST(0.0, (4.0 - c.weeks_played) / 4.0) * COALESCE(p.prior_opp, c.curr_3wk_opp)) +
        ((1.0 - GREATEST(0.0, (4.0 - c.weeks_played) / 4.0)) * c.curr_3wk_opp)) - c.prev_opp_base as opp_surge
    FROM latest_curr c
    LEFT JOIN prior_season p ON c.clean_name = p.clean_name
    LEFT JOIN df_depth_clean d ON c.clean_name = d.clean_name
    LEFT JOIN df_injuries_clean i ON c.clean_name = i.clean_name
    WHERE c.rn = 1
)
SELECT * FROM blended_metrics;
"""
df_analytics = duckdb.query(base_query).df()

# 8. Granular Query for Modal Game Logs
granular_query = """
WITH active_games AS (
    SELECT 
        clean_name, season, week, position, targets, carries, attempts as pass_attempts,
        ROUND(target_share, 3) as tgt_share, ROUND(air_yards_share, 3) as ay_share,
        ROUND(rushing_share, 3) as rush_share, ROUND(pos_opp_score, 3) as opp_score,
        ROUND(unrealized_air_yards, 1) as unrealized_ay, ROUND(fantasy_points_ppr, 1) as ppr_pts,
        ROW_NUMBER() OVER (PARTITION BY clean_name ORDER BY season DESC, week DESC) as game_rn
    FROM df_nfl
    WHERE season IN (2025, 2026) AND (carries > 0 OR targets > 0 OR attempts > 0)
)
SELECT * FROM active_games ORDER BY clean_name, season ASC, week ASC;
"""
df_granular = duckdb.query(granular_query).df()

# 9. Master Table Queries
squad_master_sql = """
SELECT 
    r.fantasy_team, r.roster_pos as "Slot", bm.player_name as "Player", bm.clean_name, bm.position as "Pos", bm.team as "Team",
    ROUND(bm.blended_opp, 3) as "Opp Score", ROUND(bm.opp_surge, 3) as "Surge", bm.curr_3wk_unrealized_ay as "Unrealized AY", ROUND(bm.curr_3wk_ppr, 1) as "PPR Avg",
    'SPARKLINE' as "Trend (PPR)",
    CASE 
        WHEN bm.depth_rank > 1 AND bm.blended_opp >= 0.35 THEN '⚠️ SHORT-TERM VOLUME (IR Replacement)'
        WHEN bm.position IN ('WR', 'TE') AND bm.curr_3wk_unrealized_ay >= 65.0 AND bm.curr_3wk_ppr < 11.0 THEN '🚨 AIR YARD BUY-LOW'
        WHEN bm.blended_opp < 0.20 AND bm.curr_3wk_ppr < 8.0 THEN '✂️ DROP CANDIDATE'
        WHEN bm.blended_opp < 0.22 AND bm.curr_3wk_touches < 8.0 AND bm.curr_3wk_ppr >= 13.0 THEN '⚠️ FLUKE RISK (Trade Out)'
        WHEN bm.blended_opp >= 0.45 AND bm.curr_3wk_ppr >= 13.0 THEN '🔥 CORE STARTER'
        WHEN bm.blended_opp >= 0.40 AND bm.curr_3wk_targets >= 4.0 AND bm.curr_3wk_ppr < 11.0 THEN '🚨 BUY LOW HOLD'
        WHEN bm.opp_surge >= 0.100 THEN '📈 SURGING ROLE'
        ELSE '👀 HOLD'
    END as "Verdict"
FROM df_rosters r
JOIN df_analytics bm ON r.clean_name = bm.clean_name
ORDER BY r.fantasy_team, "Opp Score" DESC;
"""
df_squad_master = duckdb.query(squad_master_sql).df()

waiver_sql = """
SELECT 
    player_name as "Player", clean_name, position as "Pos", team as "Team",
    ROUND(blended_opp, 3) as "Opp Score", ROUND(opp_surge, 3) as "Surge (Velocity)", curr_3wk_unrealized_ay as "Unrealized AY", ROUND(curr_3wk_ppr, 1) as "PPR Avg",
    'SPARKLINE' as "Trend (PPR)",
    CASE 
        WHEN depth_rank > 1 AND blended_opp >= 0.35 THEN '⚠️ SHORT-TERM VOLUME (IR Replacement)'
        WHEN position IN ('WR', 'TE') AND curr_3wk_unrealized_ay >= 65.0 AND curr_3wk_ppr < 11.0 THEN '🚨 AIR YARD BUY-LOW'
        WHEN blended_opp >= 0.45 AND curr_3wk_targets >= 4.0 AND curr_3wk_ppr < 10.0 THEN '🚨 BUY LOW / TARGET'
        WHEN blended_opp < 0.22 AND curr_3wk_touches < 8.0 AND curr_3wk_ppr >= 13.0 THEN '⚠️ SELL HIGH / FLUKE'
        WHEN blended_opp >= 0.45 AND curr_3wk_ppr >= 13.0 THEN '🔥 HIGH-VOLUME ALPHA'
        WHEN opp_surge >= 0.100 THEN '📈 SURGING WORKLOAD'
        ELSE '👀 STASH'
    END as "Verdict"
FROM df_analytics
WHERE clean_name NOT IN (SELECT clean_name FROM df_rosters)
ORDER BY "Surge (Velocity)" DESC;
"""
df_waiver = duckdb.query(waiver_sql).df()

trade_master_sql = """
SELECT 
    r.fantasy_team as "Owner", bm.player_name as "Player", bm.clean_name, bm.position as "Pos", bm.team as "Team",
    ROUND(bm.blended_opp, 3) as "Opp Score", bm.curr_3wk_unrealized_ay as "Unrealized AY", ROUND(bm.curr_3wk_ppr, 1) as "PPR Avg",
    'SPARKLINE' as "Trend (PPR)",
    CASE 
        WHEN bm.depth_rank > 1 AND bm.blended_opp >= 0.35 THEN '⚠️ SHORT-TERM VOLUME (IR Replacement)'
        WHEN bm.position IN ('WR', 'TE') AND bm.curr_3wk_unrealized_ay >= 65.0 THEN '🚨 AIR YARD BUY-LOW'
        ELSE '🚨 BUY LOW / TRADE TARGET'
    END as "Verdict"
FROM df_rosters r
JOIN df_analytics bm ON r.clean_name = bm.clean_name
WHERE (bm.blended_opp >= 0.40 AND bm.curr_3wk_targets >= 4.0 AND bm.curr_3wk_ppr < 11.0)
   OR (bm.position IN ('WR', 'TE') AND bm.curr_3wk_unrealized_ay >= 65.0 AND bm.curr_3wk_ppr < 11.0)
ORDER BY "Opp Score" DESC;
"""
df_trade_master = duckdb.query(trade_master_sql).df()

team_options_html = "".join(
    [
        f'<option value="{t}" {"selected" if t == default_team else ""}>{t}</option>'
        for t in all_teams
    ]
)

# 10. Build HTML Layout string
html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Fandromeda Interactive Dashboard</title>
    <style>
        body {{ background-color: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 30px; margin: 0; }}
        .header-container {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #334155; padding-bottom: 15px; margin-bottom: 20px; }}
        .section-header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; margin-top: 25px; padding-bottom: 8px; cursor: pointer; user-select: none; }}
        .section-header h2 {{ color: #94a3b8; font-size: 1.3rem; margin: 0; border: none; padding: 0; }}
        .toggle-hint {{ color: #38bdf8; font-size: 0.8rem; font-weight: normal; margin-left: 10px; }}
        h1 {{ color: #38bdf8; font-size: 2.2rem; margin: 0; }}
        .timestamp-bar {{ display: flex; gap: 20px; font-size: 0.85rem; color: #94a3b8; background-color: #1e293b; padding: 8px 15px; border-radius: 6px; margin-bottom: 20px; border: 1px solid #334155; }}
        .controls {{ display: flex; align-items: center; gap: 10px; }}
        select {{ background-color: #1e293b; color: #38bdf8; border: 1px solid #38bdf8; padding: 6px 12px; border-radius: 6px; font-size: 0.95rem; font-weight: bold; cursor: pointer; outline: none; }}
        select:hover {{ background-color: #334155; }}

        /* TAB NAVIGATION STYLES */
        .tab-bar {{ display: flex; gap: 10px; border-bottom: 2px solid #334155; padding-bottom: 0; margin-bottom: 20px; }}
        .tab-btn {{
            background-color: #1e293b; color: #94a3b8; border: 1px solid #334155; border-bottom: none;
            padding: 10px 20px; border-radius: 8px 8px 0 0; font-size: 1rem; font-weight: bold;
            cursor: pointer; transition: all 0.2s; position: relative; top: 2px;
        }}
        .tab-btn:hover {{ background-color: #24334d; color: #38bdf8; }}
        .tab-btn.active {{ background-color: #38bdf8; color: #0f172a; border-color: #38bdf8; }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}
        
        .toggle-btn {{
            background-color: #1e293b; color: #38bdf8; border: 1px solid #38bdf8;
            padding: 6px 14px; border-radius: 6px; font-size: 0.85rem; font-weight: bold;
            cursor: pointer; transition: background-color 0.2s; margin-top: 10px; display: inline-block;
        }}
        .toggle-btn:hover {{ background-color: #334155; }}

        .info-card {{ background-color: #1e293b; border-left: 4px solid #38bdf8; padding: 15px 20px; margin-top: 15px; border-radius: 0 8px 8px 0; font-size: 0.9rem; line-height: 1.5; display: none; }}
        .info-card.always-visible {{ display: block; }}
        .info-card ul {{ margin: 5px 0 0 0; padding-left: 20px; }}
        .info-card li {{ margin-bottom: 4px; }}
        
        /* ENHANCED ENGINE SPECS FORMATTING */
        .info-card.spacious-card {{ padding: 20px 25px; font-size: 0.95rem; }}
        .feature-list {{ list-style: none; padding-left: 0; margin-top: 15px; }}
        .feature-item {{ margin-bottom: 20px; padding-bottom: 15px; border-bottom: 1px solid #334155; }}
        .feature-item:last-child {{ border-bottom: none; margin-bottom: 0; padding-bottom: 0; }}
        .feature-title {{ color: #38bdf8; font-size: 1.05rem; font-weight: bold; margin-bottom: 8px; display: block; }}
        .feature-desc {{ color: #cbd5e1; line-height: 1.6; margin-left: 10px; }}
        .formula-box {{
            background-color: #0f172a; border-left: 3px solid #38bdf8; border-radius: 4px;
            padding: 10px 15px; margin: 10px 0 5px 15px; font-size: 0.9rem;
        }}
        .formula-box code {{ color: #facc15; font-family: monospace; font-size: 0.9rem; }}

        table {{ width: 100%; border-collapse: collapse; margin-top: 15px; background-color: #1e293b; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3); }}
        th {{ background-color: #334155; color: #38bdf8; text-align: left; padding: 12px 16px; font-size: 0.9rem; text-transform: uppercase; letter-spacing: 0.05em; cursor: pointer; user-select: none; }}
        th:hover {{ background-color: #475569; }}
        th::after {{ content: ' ↕'; font-size: 0.75rem; color: #64748b; }}
        td {{ padding: 12px 16px; border-bottom: 1px solid #334155; font-size: 0.95rem; }}
        tr:hover {{ background-color: #24334d; }}
        
        .player-clickable {{ color: #38bdf8; font-weight: bold; cursor: pointer; text-decoration: underline; }}
        .player-clickable:hover {{ color: #7dd3fc; }}

        .matchup-easy {{ color: #4ade80; font-weight: bold; }}
        .matchup-neutral {{ color: #facc15; font-weight: bold; }}
        .matchup-tough {{ color: #f87171; font-weight: bold; }}

        .verdict-badge {{ position: relative; display: inline-block; cursor: help; border-bottom: 1px dashed #64748b; }}
        .verdict-badge .tooltiptext {{
            visibility: hidden; width: 250px; background-color: #0f172a; color: #f8fafc;
            text-align: left; border: 1px solid #38bdf8; border-radius: 6px; padding: 8px 12px;
            position: absolute; z-index: 100; left: 50%; margin-left: -125px;
            opacity: 0; transition: opacity 0.2s; font-size: 0.8rem; font-weight: normal; line-height: 1.3;
            box-shadow: 0 4px 10px rgba(0,0,0,0.5);
        }}
        .verdict-badge:hover .tooltiptext {{ visibility: visible; opacity: 1; }}

        .modal-overlay {{
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(15, 23, 42, 0.85); backdrop-filter: blur(4px);
            justify-content: center; align-items: center; z-index: 1000;
        }}
        .modal-card {{
            background: #1e293b; border: 1px solid #38bdf8; border-radius: 10px; padding: 25px;
            width: 90%; max-width: 900px; max-height: 85vh; overflow-y: auto; box-shadow: 0 10px 25px rgba(0,0,0,0.5); position: relative;
        }}
        .close-btn {{
            position: absolute; top: 15px; right: 20px; color: #94a3b8; font-size: 1.5rem;
            cursor: pointer; font-weight: bold;
        }}
        .close-btn:hover {{ color: #f8fafc; }}

        tr.active-window-row {{ background-color: #1e3a8a !important; border-left: 4px solid #38bdf8; }}
        tr.active-window-row td {{ color: #ffffff; font-weight: 500; }}
        .active-window-badge {{ background-color: #38bdf8; color: #0f172a; font-size: 0.7rem; font-weight: bold; padding: 2px 6px; border-radius: 4px; margin-left: 6px; }}
    </style>
</head>
<body>

    <div class="header-container">
        <div>
            <h1>🌌 Fandromeda Engine</h1>
            <div style="color: #64748b; font-size: 0.9rem; margin-top: 4px;">Fantasy Football Analytics & Waiver Intelligence Hub | <i>Click headers to sort | Hover sparklines for PPR score history | Click player names for game logs</i></div>
        </div>
        <div class="controls">
            <label for="teamSelect" style="color: #94a3b8; font-weight: bold; font-size: 0.95rem;">Active Team Target:</label>
            <select id="teamSelect" onchange="filterTeamData()">
                {team_options_html}
            </select>
        </div>
    </div>

    <div class="timestamp-bar">
        <span>📄 <strong>Yahoo Rosters Updated:</strong> {yahoo_last_updated}</span>
        <span>☁️ <strong>NFLVerse Data Fetched:</strong> {nfl_last_updated}</span>
    </div>

    <!-- TAB NAVIGATION BAR -->
    <div class="tab-bar">
        <button class="tab-btn active" onclick="switchTab('roster-tab', this)">🏈 Roster Analytics</button>
        <button class="tab-btn" onclick="switchTab('market-tab', this)">⚡ Market Intelligence</button>
        <button class="tab-btn" onclick="switchTab('specs-tab', this)">🛠️ Engine Specs</button>
    </div>

    <!-- TAB 1: ROSTER ANALYTICS -->
    <div id="roster-tab" class="tab-content active">
        <!-- SECTION 1: SQUAD EVALUATION -->
        <div class="section-header" onclick="toggleCard('squadCard', 'squadHint')">
            <h2>🏈 Squad Evaluation (<span id="activeTeamHeader">{default_team}</span>) <span class="toggle-hint" id="squadHint">[+ Click to expand definitions]</span></h2>
        </div>
        <div class="info-card" id="squadCard">
            <strong>Squad Verdict Guidance & Master Definitions:</strong>
            <ul>
                <li><strong>⚠️ SHORT-TERM VOLUME (IR Replacement):</strong> Temporary starter workload due to an injury/IR ahead on depth chart. High short-term usage, but expected to drop when starter returns.</li>
                <li><strong>🚨 AIR YARD BUY-LOW:</strong> High downfield target volume (≥65 Unrealized Air Yds/gm) but low PPR points (<11.0). Massive regression breakout candidate.</li>
                <li><strong>🔥 CORE STARTER:</strong> Elite positional workload (Opp ≥ 0.45) matched with high PPR production (PPR ≥ 13.0). Unquestioned weekly start.</li>
                <li><strong>🚨 BUY LOW HOLD:</strong> High opportunity (Opp ≥ 0.40) and minimum target volume but low output (PPR &lt; 11.0). Do not drop.</li>
                <li><strong>📈 SURGING ROLE:</strong> Workload velocity growing rapidly (Surge ≥ 0.100). Bench player earning a larger share of offensive plays.</li>
                <li><strong>⚠️ FLUKE RISK (Trade Out):</strong> Scoring fantasy points on weak volume (&lt;8 touches/gm & Opp &lt; 0.22). Sell high before regression.</li>
                <li><strong>✂️ DROP CANDIDATE:</strong> Weak volume (Opp &lt; 0.20) and poor fantasy output (PPR &lt; 8.0). Safely drop to free up bench space.</li>
                <li><strong>👀 HOLD:</strong> Stable positional role without immediate breakout or drop signals.</li>
            </ul>
        </div>
        <div id="squadTableContainer"></div>

        <!-- SECTION 4: SOS & WEATHER CHEAT SHEET -->
        <div class="section-header" onclick="toggleCard('sosCard', 'sosHint')">
            <h2>🗓️ Strength of Schedule (Next 3 Weeks) <span class="toggle-hint" id="sosHint">[+ Click to expand definitions]</span></h2>
        </div>
        <div class="info-card" id="sosCard">
            <strong>Matchup Difficulty Legend:</strong>
            <ul>
                <li><span class="matchup-easy">🟩 EASY:</span> Facing a defense allowing high fantasy points to this player's position (Top-8 favorable matchup).</li>
                <li><span class="matchup-neutral">🟨 NEUTRAL:</span> Average defensive matchup against this position.</li>
                <li><span class="matchup-tough">🟥 TOUGH:</span> Facing a top-8 defense against this player's position.</li>
                <li><strong>💨 WIND ALERT:</strong> Outdoor game with wind ≥ 15 mph projected.</li>
                <li><strong>🥶 COLD ALERT:</strong> Freezing conditions projected (≤ 32°F).</li>
                <li><strong>🔥 HEAT ALERT:</strong> Extreme heat projected (≥ 90°F).</li>
            </ul>
        </div>
        <div id="sosTableContainer"></div>
    </div>

    <!-- TAB 2: MARKET INTELLIGENCE -->
    <div id="market-tab" class="tab-content">
        <!-- SECTION 2: WAIVER WIRE -->
        <div class="section-header" onclick="toggleCard('waiverCard', 'waiverHint')">
            <h2>🔥 Unclaimed Waiver Wire (Ranked by Opportunity Surge) <span class="toggle-hint" id="waiverHint">[+ Click to expand definitions]</span></h2>
            <div class="controls" onclick="event.stopPropagation();">
                <label for="posSelect" style="color: #94a3b8; font-weight: bold; font-size: 0.9rem;">Filter Position:</label>
                <select id="posSelect" onchange="filterWaiverData()">
                    <option value="ALL" selected>All Positions</option>
                    <option value="RB">RB Only</option>
                    <option value="WR">WR Only</option>
                    <option value="TE">TE Only</option>
                    <option value="QB">QB Only</option>
                </select>
            </div>
        </div>
        <div class="info-card" id="waiverCard">
            <strong>Waiver Verdict Guidance & Master Definitions:</strong>
            <ul>
                <li><strong>⚠️ SHORT-TERM VOLUME (IR Replacement):</strong> Temporary starter workload due to an injury ahead on the depth chart. Ideal short-term rent-a-player.</li>
                <li><strong>🚨 AIR YARD BUY-LOW:</strong> Unowned receiver seeing major deep targets (≥65 Unrealized Air Yds/gm) with low PPR output. Priority stash.</li>
                <li><strong>🚨 BUY LOW / TARGET:</strong> High opportunity (Opp ≥ 0.45) & target floor with weak fantasy points (PPR &lt; 10.0). Priority target.</li>
                <li><strong>🔥 HIGH-VOLUME ALPHA:</strong> Unowned player producing elite volume (Opp ≥ 0.45) and strong PPR points (PPR ≥ 13.0). Priority pickup.</li>
                <li><strong>📈 SURGING WORKLOAD:</strong> Workload velocity jumping rapidly over the past 3 weeks (Surge ≥ 0.100).</li>
                <li><strong>⚠️ SELL HIGH / FLUKE:</strong> Points scored without underlying volume (&lt;8 touches/gm). High risk for waiver spending.</li>
                <li><strong>👀 STASH:</strong> Low volume/points currently, but worth monitoring for deep bench storage.</li>
            </ul>
        </div>
        <div id="waiverTableContainer"></div>

        <!-- SECTION 3: TRADE TARGETS -->
        <div class="section-header" onclick="toggleCard('tradeCard', 'tradeHint')">
            <h2>🎯 Rival Roster Trade Targets (High Opportunity / Low Output) <span class="toggle-hint" id="tradeHint">[+ Click to expand definitions]</span></h2>
        </div>
        <div class="info-card" id="tradeCard">
            <strong>Trade Verdict Guidance & Master Definitions:</strong>
            <ul>
                <li><strong>⚠️ SHORT-TERM VOLUME (IR Replacement):</strong> Backup producing temporarily; sell high before primary starter returns.</li>
                <li><strong>🚨 AIR YARD BUY-LOW:</strong> Target receivers with massive downfield volume (≥65 Unrealized Air Yds/gm) underperforming on points.</li>
                <li><strong>🚨 BUY LOW / TRADE TARGET:</strong> Player rostered by a rival manager with significant workload (Opp ≥ 0.40) underperforming on points (PPR &lt; 11.0).</li>
            </ul>
        </div>
        <div style="margin-top: 10px;">
            <button id="toggleTradeBtn" class="toggle-btn" onclick="toggleTradeList()">👇 Show All Trade Targets</button>
        </div>
        <div id="tradeTableContainer"></div>
    </div>

    <!-- TAB 3: ENGINE SPECS -->
    <div id="specs-tab" class="tab-content">
        <div class="section-header">
            <h2>🛠️ Core Features & Mathematical Formulas</h2>
        </div>
        <div class="info-card always-visible spacious-card" id="engineFeaturesCard">
            <strong style="font-size: 1.1rem; color: #f8fafc;">Active Features & Analytics Engine Breakdown:</strong>
            
            <ul class="feature-list">
                <li class="feature-item">
                    <span class="feature-title">📊 Position-Weighted Opportunity Score (WOPR / Workload Share)</span>
                    <div class="feature-desc">Blends target share, air yards share, rushing share, and passing attempts tailored per position:</div>
                    <div class="formula-box">
                        <div>• <code>WR / TE Opportunity (WOPR):</code> 1.5 × Target Share + 0.7 × Air Yards Share</div>
                        <div>• <code>RB Opportunity Share:</code> Rushing Share + 1.5 × Target Share</div>
                        <div>• <code>QB Opportunity Share:</code> (Passing Attempts + Rushing Carries) / 50.0</div>
                    </div>
                </li>

                <li class="feature-item">
                    <span class="feature-title">🎯 Unrealized Air Yards</span>
                    <div class="feature-desc">
                        Tracks downfield target volume (<code>Air Yards − Completed Air Yards</code>). Automatically flags positive regression candidates underperforming on points with <code>🚨 AIR YARD BUY-LOW</code>.
                    </div>
                </li>

                <li class="feature-item">
                    <span class="feature-title">🗓️ Trailing 3-Week Heuristic Window</span>
                    <div class="feature-desc">
                        Uses DuckDB window functions over a player's last 3 active games to eliminate single-week skewed averages and reflect true current usage trends.
                    </div>
                </li>

                <li class="feature-item">
                    <span class="feature-title">📈 Workload Velocity (Surge)</span>
                    <div class="feature-desc">
                        Calculates multi-week acceleration in role usage to highlight rising bench assets before point production catches up.
                    </div>
                </li>

                <li class="feature-item">
                    <span class="feature-title">📉 Interactive PPR Sparklines</span>
                    <div class="feature-desc">
                        Visualizes recent 4-game scoring trajectories inline within master tables. Hovering over sparklines reveals raw game-by-game PPR scores.
                    </div>
                </li>

                <li class="feature-item">
                    <span class="feature-title">🛡️ Depth Chart & IR Volume Guardrail</span>
                    <div class="feature-desc">
                        Tracks team depth order and injury reports. Automatically tags backup players stepping into starter roles with <code>⚠️ SHORT-TERM VOLUME (IR Replacement)</code> to prevent false trade/breakout signals.
                    </div>
                </li>

                <li class="feature-item">
                    <span class="feature-title">🌦️ Defensive Strength of Schedule & Weather Cheat Sheet</span>
                    <div class="feature-desc">
                        Evaluates upcoming 3-week opponent matchups color-coded by defensive points allowed per position, along with high-wind (💨), extreme cold (🥶), and severe heat (🔥) outdoor flags.
                    </div>
                </li>
            </ul>
        </div>
    </div>

    <div id="playerModal" class="modal-overlay">
        <div class="modal-card">
            <span class="close-btn" onclick="closeModal()">&times;</span>
            <h2 id="modalPlayerName" style="color:#38bdf8; margin-top:0;">Player Details</h2>
            <p id="modalSubhead" style="color:#94a3b8; font-size:0.9rem;"></p>
            <div id="modalTableContainer"></div>
        </div>
    </div>

    <script>
        const masterSquadData = {df_squad_master.to_json(orient='records')};
        const masterWaiverData = {df_waiver.to_json(orient='records')};
        const masterTradeData = {df_trade_master.to_json(orient='records')};
        const granularData = {df_granular.to_json(orient='records')};
        const defSosData = {df_def_sos.to_json(orient='records')};
        const teamSosData = {df_team_sos.to_json(orient='records')};
        const nextWeeks = {json.dumps(next_weeks)};

        let isTradeExpanded = false;

        const verdictTooltips = {{
            '⚠️ SHORT-TERM VOLUME (IR Replacement)': 'Elevated workload resulting from starter injury/IR. High immediate volume, but temporary value.',
            '🚨 AIR YARD BUY-LOW': 'High downfield target volume (≥65 Unrealized Air Yds/gm) but low PPR points (<11.0). Prime positive regression breakout candidate.',
            '🔥 CORE STARTER': 'Elite positional workload (Opp ≥ 0.45) matched with high PPR production (PPR ≥ 13.0). Unquestioned weekly start.',
            '🚨 BUY LOW HOLD': 'High opportunity (Opp ≥ 0.40) & target floor but low output (PPR < 11.0). Do not drop; breakout incoming.',
            '📈 SURGING ROLE': 'Workload velocity growing rapidly (Surge ≥ 0.100). Bench player earning a larger share of offensive plays.',
            '⚠️ FLUKE RISK (Trade Out)': 'Scoring fantasy points on weak volume (<8 touches/gm & Opp < 0.22). Sell high before efficiency drops.',
            '✂️ DROP CANDIDATE': 'Weak volume (Opp < 0.20) and poor fantasy output (PPR < 8.0). Safely drop to free up bench space.',
            '👀 HOLD': 'Stable positional role without immediate breakout or drop signals.',
            '🚨 BUY LOW / TARGET': 'High opportunity (Opp ≥ 0.45) & target floor with weak fantasy points (PPR < 10.0). Prime waiver target.',
            '🔥 HIGH-VOLUME ALPHA': 'Unowned player producing elite volume (Opp ≥ 0.45) and strong PPR points (PPR ≥ 13.0). Priority pickup.',
            '📈 SURGING WORKLOAD': 'Workload velocity jumping rapidly over the past 3 weeks (Surge ≥ 0.100).',
            '⚠️ SELL HIGH / FLUKE': 'Points scored without underlying volume (<8 touches/gm). High risk for waiver spending.',
            '👀 STASH': 'Low volume/points currently, but worth monitoring for deep bench storage.',
            '🚨 BUY LOW / TRADE TARGET': 'Target rostered players with significant workload (Opp ≥ 0.40) who are underperforming on points (PPR < 11.0).'
        }};

        function switchTab(tabId, btn) {{
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            btn.classList.add('active');
        }}

        function generateSparklineSVG(cleanName, rowIndex, width = 85, height = 22) {{
            const playerGames = granularData.filter(g => g.clean_name === cleanName && g.game_rn <= 4);
            if (!playerGames || playerGames.length < 2) return '<span style="color:#64748b;">—</span>';
            
            const scores = playerGames.map(g => g.ppr_pts);
            const min = Math.min(...scores, 0);
            const max = Math.max(...scores, 1);
            const range = max - min || 1;
            
            const points = scores.map((val, idx) => {{
                const x = (idx / (scores.length - 1)) * (width - 10) + 5;
                const y = height - 4 - ((val - min) / range) * (height - 8);
                return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
            }}).join(' ');
            
            const lastPoint = points.split(' ').pop().split(',');
            const isUpTrend = scores[scores.length - 1] >= scores[0];
            const strokeColor = isUpTrend ? '#38bdf8' : '#f43f5e';

            const tooltipText = `PPR Trend (Last ${{scores.length}} Active Games): ` + scores.join(' ➔ ');
            const popDirection = rowIndex === 0 ? 'top: 125%;' : 'bottom: 125%;';

            return `
                <div class="verdict-badge" style="border-bottom: none;">
                    <svg width="${{width}}" height="${{height}}" style="vertical-align: middle; overflow: visible;">
                        <polyline fill="none" stroke="${{strokeColor}}" stroke-width="2" points="${{points}}" />
                        <circle cx="${{lastPoint[0]}}" cy="${{lastPoint[1]}}" r="3" fill="${{strokeColor}}" />
                    </svg>
                    <span class="tooltiptext" style="${{popDirection}} width: 230px; text-align: center;">${{tooltipText}}</span>
                </div>
            `;
        }}

        function toggleCard(cardId, hintId) {{
            const card = document.getElementById(cardId);
            const hint = document.getElementById(hintId);
            if (card.style.display === 'block') {{
                card.style.display = 'none';
                hint.innerText = '[+ Click to expand definitions]';
            }} else {{
                card.style.display = 'block';
                hint.innerText = '[- Click to collapse definitions]';
            }}
        }}

        function toggleTradeList() {{
            isTradeExpanded = !isTradeExpanded;
            const btn = document.getElementById('toggleTradeBtn');
            btn.innerText = isTradeExpanded ? "👆 Show Top 5 Only" : "👇 Show All Trade Targets";
            filterTeamData();
        }}

        function getMatchupClass(oppTeam, position) {{
            if (oppTeam === 'BYE') return '';
            const match = defSosData.find(d => d.def_team === oppTeam && d.position === position);
            if (!match) return 'matchup-neutral';
            const rank = match.def_rank;
            if (rank <= 8) return 'matchup-easy';
            if (rank >= 24) return 'matchup-tough';
            return 'matchup-neutral';
        }}

        function filterTeamData() {{
            const selectedTeam = document.getElementById('teamSelect').value;
            document.getElementById('activeTeamHeader').innerText = selectedTeam;

            const squadFiltered = masterSquadData.filter(row => row.fantasy_team === selectedTeam);
            renderTable('squadTableContainer', squadFiltered, ['Slot', 'Player', 'Pos', 'Team', 'Opp Score', 'Surge', 'Unrealized AY', 'PPR Avg', 'Trend (PPR)', 'Verdict']);

            let tradeFiltered = masterTradeData.filter(row => row.Owner !== selectedTeam);
            if (!isTradeExpanded) {{
                tradeFiltered = tradeFiltered.slice(0, 5);
            }}
            renderTable('tradeTableContainer', tradeFiltered, ['Owner', 'Player', 'Pos', 'Team', 'Opp Score', 'Unrealized AY', 'PPR Avg', 'Trend (PPR)', 'Verdict']);

            renderSOSTable(squadFiltered);
            attachSortListeners();
        }}

        function renderSOSTable(squadData) {{
            if (squadData.length === 0) {{
                document.getElementById('sosTableContainer').innerHTML = '<p style="color:#64748b; padding:15px;">No players available.</p>';
                return;
            }}

            const nextWkNum = nextWeeks[0];
            const wCols = nextWeeks.map(w => `W${{w}}`);
            let html = `<table class="sortable"><thead><tr><th>Slot</th><th>Player</th><th>Pos</th><th>Team</th><th>1-Wk Matchup Outlook (W${{nextWkNum}})</th>`;
            wCols.forEach(col => html += `<th>${{col}}</th>`);
            html += '</tr></thead><tbody>';

            squadData.forEach(p => {{
                const teamSos = teamSosData.find(t => t.Team === p.Team);
                
                let nextWkRatingBadge = '<span class="matchup-neutral">🟨 NEUTRAL</span>';
                let weeklyTds = '';

                wCols.forEach((col, idx) => {{
                    if (teamSos) {{
                        const matchupStr = teamSos[col];
                        const oppTeam = teamSos[col + '_opp'];
                        const mClass = getMatchupClass(oppTeam, p.Pos);

                        if (idx === 0) {{
                            if (mClass === 'matchup-easy') {{
                                nextWkRatingBadge = '<span class="matchup-easy">🟩 EASY</span>';
                            }} else if (mClass === 'matchup-tough') {{
                                nextWkRatingBadge = '<span class="matchup-tough">🟥 TOUGH</span>';
                            }} else {{
                                nextWkRatingBadge = '<span class="matchup-neutral">🟨 NEUTRAL</span>';
                            }}

                            if (matchupStr.includes('💨') || matchupStr.includes('🥶') || matchupStr.includes('🔥')) {{
                                nextWkRatingBadge += ' <span style="font-size:0.85rem; color:#38bdf8;">⚠️ Weather Alert</span>';
                            }}
                        }}

                        weeklyTds += `<td><span class="${{mClass}}">${{matchupStr}}</span></td>`;
                    }} else {{
                        weeklyTds += `<td>N/A</td>`;
                    }}
                }});

                html += `<tr>
                    <td>${{p.Slot}}</td>
                    <td><span class="player-clickable" onclick="openPlayerModal('${{p.clean_name}}', '${{p.Player}}')">${{p.Player}}</span></td>
                    <td>${{p.Pos}}</td>
                    <td>${{p.Team}}</td>
                    <td>${{nextWkRatingBadge}}</td>
                    ${{weeklyTds}}
                </tr>`;
            }});
            html += '</tbody></table>';
            document.getElementById('sosTableContainer').innerHTML = html;
        }}

        function filterWaiverData() {{
            const selectedPos = document.getElementById('posSelect').value;
            let waiverFiltered = masterWaiverData;
            
            if (selectedPos !== 'ALL') {{
                waiverFiltered = masterWaiverData.filter(row => row.Pos === selectedPos);
            }}
            
            renderTable('waiverTableContainer', waiverFiltered.slice(0, 15), ['Player', 'Pos', 'Team', 'Opp Score', 'Surge (Velocity)', 'Unrealized AY', 'PPR Avg', 'Trend (PPR)', 'Verdict']);
            attachSortListeners();
        }}

        function renderTable(containerId, data, columns) {{
            if (data.length === 0) {{
                document.getElementById(containerId).innerHTML = '<p style="color:#64748b; padding:15px;">No players match this criteria.</p>';
                return;
            }}
            let html = '<table class="sortable"><thead><tr>';
            columns.forEach(col => html += `<th>${{col}}</th>`);
            html += '</tr></thead><tbody>';
            
            data.forEach((row, rowIndex) => {{
                html += '<tr>';
                columns.forEach(col => {{
                    let val = row[col] !== null ? row[col] : 'N/A';
                    if (col === 'Player') {{
                        html += `<td><span class="player-clickable" onclick="openPlayerModal('${{row.clean_name}}', '${{row.Player}}')">${{val}}</span></td>`;
                    }} else if (col === 'Trend (PPR)') {{
                        html += `<td>${{generateSparklineSVG(row.clean_name, rowIndex)}}</td>`;
                    }} else if (col === 'Verdict') {{
                        const desc = verdictTooltips[val] || 'Calculated verdict rule based on opportunity score and scoring trends.';
                        const popDirection = rowIndex === 0 ? 'top: 125%;' : 'bottom: 125%;';
                        html += `<td><div class="verdict-badge">${{val}}<span class="tooltiptext" style="${{popDirection}}">${{desc}}</span></div></td>`;
                    }} else {{
                        html += `<td>${{val}}</td>`;
                    }}
                }});
                html += '</tr>';
            }});
            html += '</tbody></table>';
            document.getElementById(containerId).innerHTML = html;
        }}

        function openPlayerModal(cleanName, playerName) {{
            const playerGames = granularData.filter(g => g.clean_name === cleanName);
            document.getElementById('modalPlayerName').innerText = playerName;
            
            if (playerGames.length === 0) {{
                document.getElementById('modalSubhead').innerText = "No active game logs available.";
                document.getElementById('modalTableContainer').innerHTML = "";
            }} else {{
                document.getElementById('modalSubhead').innerHTML = `
                    Full Season Game Logs (Position: <strong>${{playerGames[0].position}}</strong>) | 
                    <span style="color:#38bdf8;">Blue rows indicate active 3-week heuristic window</span>
                `;
                
                let html = '<table><thead><tr><th>Season</th><th>Week</th><th>Targets</th><th>Carries</th><th>Pass Att</th><th>Target Share</th><th>Air Yard Share</th><th>Unrealized AY</th><th>Opp Score</th><th>PPR Pts</th></tr></thead><tbody>';
                playerGames.forEach(g => {{
                    const isActiveWindow = g.game_rn <= 3;
                    const rowClass = isActiveWindow ? 'class="active-window-row"' : '';
                    const activeBadge = isActiveWindow ? '<span class="active-window-badge">★ Active Window</span>' : '';

                    html += `<tr ${{rowClass}}>
                        <td>${{g.season}}</td>
                        <td>Week ${{g.week}}${{activeBadge}}</td>
                        <td>${{g.targets}}</td>
                        <td>${{g.carries}}</td>
                        <td>${{g.pass_attempts}}</td>
                        <td>${{(g.tgt_share * 100).toFixed(1)}}%</td>
                        <td>${{(g.ay_share * 100).toFixed(1)}}%</td>
                        <td><strong>${{g.unrealized_ay}}</strong></td>
                        <td><strong>${{g.opp_score}}</strong></td>
                        <td><strong>${{g.ppr_pts}}</strong></td>
                    </tr>`;
                }});
                html += '</tbody></table>';
                document.getElementById('modalTableContainer').innerHTML = html;
            }}
            
            document.getElementById('playerModal').style.display = 'flex';
        }}

        function closeModal() {{
            document.getElementById('playerModal').style.display = 'none';
        }}

        function attachSortListeners() {{
            document.querySelectorAll('th').forEach(header => {{
                header.onclick = function() {{
                    const table = header.closest('table');
                    const tbody = table.querySelector('tbody');
                    const rows = Array.from(tbody.querySelectorAll('tr'));
                    const index = Array.from(header.parentElement.children).indexOf(header);
                    const isAscending = header.classList.contains('asc');
                    
                    rows.sort((a, b) => {{
                        const valA = a.children[index].innerText.trim();
                        const valB = b.children[index].innerText.trim();
                        const numA = parseFloat(valA);
                        const numB = parseFloat(valB);
                        
                        if (!isNaN(numA) && !isNaN(numB)) {{
                            return isAscending ? numA - numB : numB - numA;
                        }}
                        return isAscending ? valA.localeCompare(valB) : valB.localeCompare(valA);
                    }});
                    
                    rows.forEach(row => tbody.appendChild(row));
                    header.parentElement.querySelectorAll('th').forEach(th => th.classList.remove('asc', 'desc'));
                    header.classList.toggle('asc', !isAscending);
                    header.classList.toggle('desc', isAscending);
                }};
            }});
        }}

        // Initial renders
        filterTeamData();
        filterWaiverData();
    </script>
</body>
</html>
"""

output_file = "index.html"
with open(output_file, "w", encoding="utf-8") as f:
    f.write(html_content)

print(
    "✅ Fandromeda Dashboard updated successfully with all latest features:"
    f" '{output_file}'"
)
webbrowser.open("file://" + os.path.realpath(output_file))