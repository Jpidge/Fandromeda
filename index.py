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

# Load Snap Counts
try:
  df_snaps = nfl.load_snap_counts([2026]).to_pandas()
except Exception:
  df_snaps = pd.DataFrame(
      columns=["player", "pfr_player_id", "season", "week", "offense_pct"]
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


# 3. Optimized Name Cleaning Routine
def clean_name(name):
  if not isinstance(name, str):
    return name
  import re

  suffixes = r"\b(Jr|Sr|III|II|IV|V)\.?$"
  cleaned = re.sub(suffixes, "", name.strip(), flags=re.IGNORECASE)
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
df_snaps["clean_name"] = (
    df_snaps["player"].apply(clean_name) if "player" in df_snaps.columns else ""
)

# Merge Snap Counts into Main Stats DataFrame
if not df_snaps.empty and "offense_pct" in df_snaps.columns:
  df_snaps_clean = (
      df_snaps[df_snaps["season"] == 2026]
      .groupby(["clean_name", "week"])["offense_pct"]
      .mean()
      .reset_index()
  )
  df_nfl = df_nfl.merge(df_snaps_clean, on=["clean_name", "week"], how="left")
  df_nfl["snap_pct"] = df_nfl["offense_pct"].fillna(0.0)
else:
  df_nfl["snap_pct"] = 0.0

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
    "receiving_air_yards",
    "receiving_yards",
    "receiving_yards_after_catch",
    "rushing_tds",
    "receiving_tds",
    "passing_tds",
    "snap_pct",
]
for col in cols_to_fill:
  if col in df_nfl.columns:
    df_nfl[col] = df_nfl[col].fillna(0.0)
  else:
    df_nfl[col] = 0.0

df_nfl["total_tds"] = df_nfl["rushing_tds"] + df_nfl["receiving_tds"]

# 4. Target Air Yards & Unrealized Air Yards Calculation
if "receiving_air_yards" in df_nfl.columns:
  df_nfl["target_air_yards"] = (
      df_nfl["receiving_air_yards"].fillna(df_nfl["air_yards"]).fillna(0.0)
  )
else:
  df_nfl["target_air_yards"] = df_nfl["air_yards"].fillna(0.0)

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
    df_nfl["target_air_yards"] - df_nfl["completed_air_yards"]
).clip(lower=0.0)

# Red Zone Opportunity Approximation Index
df_nfl["rz_opp_score"] = (
    df_nfl["targets"] * 0.35
    + df_nfl["carries"] * 0.25
    + df_nfl["total_tds"] * 1.5
)


# Position Opportunity Score / WOPR Architecture
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
  snap_pct = row.get("snap_pct", 0.0)

  if rushing_share == 0.0 and carries > 0:
    rushing_share = min(1.0, carries / 25.0)
  if target_share == 0.0 and targets > 0:
    target_share = min(1.0, targets / 35.0)

  if pos in ["WR", "TE"]:
    # WOPR derivative formula + snap share weighting
    return (1.2 * target_share) + (0.6 * air_yards_share) + (0.3 * snap_pct)
  elif pos == "RB":
    return (0.8 * rushing_share) + (1.2 * target_share) + (0.3 * snap_pct)
  elif pos == "QB":
    return ((attempts + carries) / 50.0) * (0.8 + (0.2 * snap_pct))
  return 0.0


df_nfl["pos_opp_score"] = df_nfl.apply(calc_pos_opp, axis=1)

all_teams = sorted(df_rosters["fantasy_team"].dropna().unique().tolist())
default_team = (
    "Vader's Raiders" if "Vader's Raiders" in all_teams else all_teams[0]
)

# 5. Defensive SOS Rankings (Safe Multi-Season Query)
def_points_allowed_query = """
SELECT 
    opponent_team as def_team, position, 
    AVG(fantasy_points_ppr) as avg_pts_allowed,
    RANK() OVER (PARTITION BY position ORDER BY AVG(fantasy_points_ppr) DESC) as def_rank
FROM df_nfl
GROUP BY opponent_team, position
"""
df_def_sos = duckdb.query(def_points_allowed_query).df()

# 6. Dynamic Week Detection
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

# Map Matchups & Weather
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
WITH current_active_games AS (
    SELECT 
        clean_name, player_display_name as player_name, position, team, week, season,
        fantasy_points_ppr as ppr_pts, targets, carries, attempts as pass_attempts, 
        target_share, air_yards_share, rushing_share, snap_pct, pos_opp_score, unrealized_air_yards, rz_opp_score, total_tds,
        ROW_NUMBER() OVER (PARTITION BY clean_name ORDER BY season DESC, week DESC) as active_rn,
        LEAD(pos_opp_score, 2) OVER (PARTITION BY clean_name ORDER BY season DESC, week DESC) as lead_prev_opp,
        COUNT(week) OVER (PARTITION BY clean_name) as weeks_played
    FROM df_nfl 
    WHERE (carries > 0 OR targets > 0 OR attempts > 0 OR snap_pct > 0.1)
),
trailing_3wk_aggregates AS (
    SELECT 
        clean_name,
        AVG(pos_opp_score) as curr_3wk_opp,
        ROUND(AVG(ppr_pts), 1) as curr_3wk_ppr,
        AVG(carries + targets) as curr_3wk_touches,
        AVG(targets) as curr_3wk_targets,
        AVG(snap_pct) as curr_3wk_snap_share,
        AVG(unrealized_air_yards) as curr_3wk_unrealized_ay,
        AVG(rz_opp_score) as curr_3wk_rz_opp,
        SUM(total_tds) as curr_3wk_tds
    FROM current_active_games
    WHERE active_rn <= 3
    GROUP BY clean_name
),
latest_curr AS (
    SELECT 
        c.player_name, c.clean_name, c.position, c.team, c.weeks_played, c.pos_opp_score,
        COALESCE(c.lead_prev_opp, c.pos_opp_score) as prev_opp_base
    FROM current_active_games c
    WHERE c.active_rn = 1
),
blended_metrics AS (
    SELECT 
        l.player_name, l.clean_name, l.position, l.team, 
        t.curr_3wk_ppr, t.curr_3wk_touches, t.curr_3wk_targets,
        ROUND(t.curr_3wk_snap_share * 100, 1) as snap_pct_val,
        ROUND(t.curr_3wk_unrealized_ay, 1) as curr_3wk_unrealized_ay,
        ROUND(t.curr_3wk_rz_opp, 1) as curr_3wk_rz_opp,
        t.curr_3wk_tds,
        COALESCE(d.depth_team, 1) as depth_rank,
        COALESCE(i.report_status, 'Active') as injury_status,
        t.curr_3wk_opp as blended_opp,
        t.curr_3wk_opp - l.prev_opp_base as opp_surge
    FROM latest_curr l
    JOIN trailing_3wk_aggregates t ON l.clean_name = t.clean_name
    LEFT JOIN df_depth_clean d ON l.clean_name = d.clean_name
    LEFT JOIN df_injuries_clean i ON l.clean_name = i.clean_name
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
        ROUND(snap_pct * 100, 1) as snap_pct, ROUND(pos_opp_score, 3) as opp_score,
        ROUND(unrealized_air_yards, 1) as unrealized_ay, ROUND(rz_opp_score, 1) as rz_opp, total_tds,
        ROUND(fantasy_points_ppr, 1) as ppr_pts,
        ROW_NUMBER() OVER (PARTITION BY clean_name ORDER BY season DESC, week DESC) as game_rn
    FROM df_nfl
    WHERE season IN (2025, 2026) AND (carries > 0 OR targets > 0 OR attempts > 0 OR snap_pct > 0.1)
)
SELECT * FROM active_games ORDER BY clean_name, season DESC, week DESC;
"""
df_granular = duckdb.query(granular_query).df()

# 9. Master Table Queries
squad_master_sql = """
SELECT 
    r.fantasy_team, r.roster_pos as "Slot", bm.player_name as "Player", bm.clean_name, bm.position as "Pos", bm.team as "Team", bm.injury_status,
    ROUND(bm.blended_opp, 3) as "Opp Score", ROUND(bm.opp_surge, 3) as "Surge", 
    bm.snap_pct_val as "snap_val",
    ROUND(bm.curr_3wk_unrealized_ay, 1) as "air_yds_val", 
    ROUND(bm.curr_3wk_rz_opp, 1) as "rz_opp_val", 
    ROUND(bm.curr_3wk_ppr, 1) as "PPR Avg",
    'SPARKLINE' as "Trend (PPR)",
    CASE 
        WHEN bm.blended_opp >= 0.45 AND bm.curr_3wk_ppr >= 13.0 THEN '🔥 CORE STARTER'
        WHEN bm.opp_surge >= 0.100 THEN '📈 SURGING ROLE'
        WHEN bm.blended_opp >= 0.40 AND bm.curr_3wk_ppr < 11.0 THEN '🚨 BUY LOW HOLD'
        WHEN bm.blended_opp < 0.20 AND bm.curr_3wk_ppr < 8.0 THEN '✂️ DROP CANDIDATE'
        ELSE '👀 HOLD'
    END as "Role Verdict",
    COALESCE(NULLIF(ARRAY_TO_STRING(LIST_FILTER([
        CASE WHEN bm.injury_status IN ('Out', 'IR', 'Doubtful') THEN '🚑 INJURED / OUT' 
             WHEN bm.injury_status IN ('Questionable') THEN '⚠️ QUESTIONABLE' END,
        CASE WHEN bm.snap_pct_val >= 75.0 AND bm.curr_3wk_ppr < 10.0 THEN '⏱️ HIGH SNAP BUY-LOW' END,
        CASE WHEN bm.depth_rank > 1 AND bm.blended_opp >= 0.35 THEN '⚠️ SHORT-TERM VOLUME' END,
        CASE WHEN bm.position IN ('WR', 'TE') AND bm.curr_3wk_unrealized_ay >= 65.0 AND bm.curr_3wk_ppr < 11.0 THEN '🚨 AIR YARD BUY-LOW' END,
        CASE WHEN bm.curr_3wk_rz_opp >= 2.5 AND bm.curr_3wk_tds <= 1 THEN '🎯 RED ZONE BUY-LOW' END,
        CASE WHEN bm.blended_opp < 0.22 AND bm.curr_3wk_touches < 8.0 AND bm.curr_3wk_ppr >= 13.0 THEN '⚠️ FLUKE RISK' END
    ], x -> x IS NOT NULL), ' | '), ''), '—') as "Tactical Flags"
FROM df_rosters r
JOIN df_analytics bm ON r.clean_name = bm.clean_name
ORDER BY r.fantasy_team, "Opp Score" DESC;
"""
df_squad_master = duckdb.query(squad_master_sql).df()

waiver_sql = """
SELECT 
    player_name as "Player", clean_name, position as "Pos", team as "Team", injury_status,
    ROUND(blended_opp, 3) as "Opp Score", ROUND(opp_surge, 3) as "Surge (Velocity)", 
    snap_pct_val as "snap_val",
    ROUND(curr_3wk_unrealized_ay, 1) as "air_yds_val", 
    ROUND(curr_3wk_rz_opp, 1) as "rz_opp_val", 
    ROUND(curr_3wk_ppr, 1) as "PPR Avg",
    'SPARKLINE' as "Trend (PPR)",
    CASE 
        WHEN blended_opp >= 0.45 AND curr_3wk_ppr >= 13.0 THEN '🔥 HIGH-VOLUME ALPHA'
        WHEN blended_opp >= 0.45 AND curr_3wk_targets >= 4.0 AND curr_3wk_ppr < 10.0 THEN '🚨 BUY LOW / TARGET'
        WHEN opp_surge >= 0.100 THEN '📈 SURGING WORKLOAD'
        ELSE '👀 STASH'
    END as "Role Verdict",
    COALESCE(NULLIF(ARRAY_TO_STRING(LIST_FILTER([
        CASE WHEN injury_status IN ('Out', 'IR', 'Doubtful') THEN '🚑 INJURED / OUT' 
             WHEN injury_status IN ('Questionable') THEN '⚠️ QUESTIONABLE' END,
        CASE WHEN snap_pct_val >= 75.0 AND curr_3wk_ppr < 10.0 THEN '⏱️ HIGH SNAP BUY-LOW' END,
        CASE WHEN depth_rank > 1 AND blended_opp >= 0.35 THEN '⚠️ SHORT-TERM VOLUME' END,
        CASE WHEN position IN ('WR', 'TE') AND curr_3wk_unrealized_ay >= 65.0 AND curr_3wk_ppr < 11.0 THEN '🚨 AIR YARD BUY-LOW' END,
        CASE WHEN curr_3wk_rz_opp >= 2.5 AND curr_3wk_tds <= 1 THEN '🎯 RED ZONE BUY-LOW' END,
        CASE WHEN blended_opp < 0.22 AND curr_3wk_touches < 8.0 AND curr_3wk_ppr >= 13.0 THEN '⚠️ SELL HIGH / FLUKE' END
    ], x -> x IS NOT NULL), ' | '), ''), '—') as "Tactical Flags"
FROM df_analytics
WHERE clean_name NOT IN (SELECT clean_name FROM df_rosters)
ORDER BY "Surge (Velocity)" DESC;
"""
df_waiver = duckdb.query(waiver_sql).df()

trade_master_sql = """
SELECT 
    r.fantasy_team as "Owner", bm.player_name as "Player", bm.clean_name, bm.position as "Pos", bm.team as "Team", bm.injury_status,
    ROUND(bm.blended_opp, 3) as "Opp Score", 
    bm.snap_pct_val as "snap_val",
    ROUND(bm.curr_3wk_unrealized_ay, 1) as "air_yds_val", 
    ROUND(bm.curr_3wk_rz_opp, 1) as "rz_opp_val", 
    ROUND(bm.curr_3wk_ppr, 1) as "PPR Avg",
    'SPARKLINE' as "Trend (PPR)",
    '🚨 BUY LOW / TRADE TARGET' as "Role Verdict",
    COALESCE(NULLIF(ARRAY_TO_STRING(LIST_FILTER([
        CASE WHEN bm.injury_status IN ('Out', 'IR', 'Doubtful') THEN '🚑 INJURED / OUT' 
             WHEN bm.injury_status IN ('Questionable') THEN '⚠️ QUESTIONABLE' END,
        CASE WHEN bm.snap_pct_val >= 75.0 AND bm.curr_3wk_ppr < 10.0 THEN '⏱️ HIGH SNAP BUY-LOW' END,
        CASE WHEN bm.depth_rank > 1 AND bm.blended_opp >= 0.35 THEN '⚠️ SHORT-TERM VOLUME' END,
        CASE WHEN bm.position IN ('WR', 'TE') AND bm.curr_3wk_unrealized_ay >= 65.0 THEN '🚨 AIR YARD BUY-LOW' END,
        CASE WHEN bm.curr_3wk_rz_opp >= 2.5 AND bm.curr_3wk_tds <= 1 THEN '🎯 RED ZONE BUY-LOW' END
    ], x -> x IS NOT NULL), ' | '), ''), '—') as "Tactical Flags"
FROM df_rosters r
JOIN df_analytics bm ON r.clean_name = bm.clean_name
WHERE (bm.blended_opp >= 0.40 AND bm.curr_3wk_targets >= 4.0 AND bm.curr_3wk_ppr < 11.0)
   OR (bm.snap_pct_val >= 75.0 AND bm.curr_3wk_ppr < 10.0)
   OR (bm.position IN ('WR', 'TE') AND bm.curr_3wk_unrealized_ay >= 65.0 AND bm.curr_3wk_ppr < 11.0)
   OR (bm.curr_3wk_rz_opp >= 2.5 AND bm.curr_3wk_tds <= 1)
ORDER BY "Opp Score" DESC;
"""
df_trade_master = duckdb.query(trade_master_sql).df()

team_options_html = "".join(
    [
        f'<option value="{t}" {"selected" if t == default_team else ""}>{t}</option>'
        for t in all_teams
    ]
)

# 10. Build HTML Layout
html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
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
        .info-card.always-visible {{ display: block !important; }}
        .info-card ul {{ margin: 5px 0 0 0; padding-left: 20px; }}
        .info-card li {{ margin-bottom: 4px; }}
        
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
        th {{ background-color: #334155; color: #38bdf8; text-align: left; padding: 12px 14px; font-size: 0.88rem; text-transform: uppercase; letter-spacing: 0.04em; cursor: pointer; user-select: none; white-space: nowrap; }}
        th:hover {{ background-color: #475569; }}
        th::after {{ content: ' ↕'; font-size: 0.75rem; color: #64748b; }}
        td {{ padding: 10px 14px; border-bottom: 1px solid #334155; font-size: 0.92rem; white-space: nowrap; }}
        tr:hover {{ background-color: #24334d; }}

        td.col-role-verdict, th.col-role-verdict {{ min-width: 190px; width: 210px; }}
        td.col-tactical-flags, th.col-tactical-flags {{ min-width: 220px; }}
        
        .player-clickable {{ color: #38bdf8; font-weight: bold; cursor: pointer; text-decoration: underline; }}
        .player-clickable:hover {{ color: #7dd3fc; }}

        .matchup-easy {{ color: #4ade80; font-weight: bold; }}
        .matchup-neutral {{ color: #facc15; font-weight: bold; }}
        .matchup-tough {{ color: #f87171; font-weight: bold; }}

        .verdict-badge {{ position: relative; display: inline-block; cursor: help; border-bottom: 1px dashed #64748b; white-space: nowrap; }}
        .verdict-badge .tooltiptext {{
            visibility: hidden; width: 260px; background-color: #0f172a; color: #f8fafc;
            text-align: left; border: 1px solid #38bdf8; border-radius: 6px; padding: 8px 12px;
            position: absolute; z-index: 100; left: 50%; margin-left: -130px;
            opacity: 0; transition: opacity 0.2s; font-size: 0.8rem; font-weight: normal; line-height: 1.3;
            box-shadow: 0 4px 10px rgba(0,0,0,0.5); white-space: normal;
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

        .modal-stat-pill {{
            display: inline-block; background-color: #0f172a; border: 1px solid #38bdf8;
            color: #38bdf8; font-weight: bold; padding: 3px 8px; border-radius: 4px; margin-right: 8px; font-size: 0.85rem;
        }}

        tr.active-window-row {{ background-color: #1e3a8a !important; border-left: 4px solid #38bdf8; }}
        tr.active-window-row td {{ color: #ffffff; font-weight: 500; }}
        .active-window-badge {{ background-color: #38bdf8; color: #0f172a; font-size: 0.7rem; font-weight: bold; padding: 2px 6px; border-radius: 4px; margin-left: 6px; }}

        .injury-badge-out {{ background-color: #ef4444; color: #ffffff; font-size: 0.7rem; font-weight: bold; padding: 2px 5px; border-radius: 4px; margin-left: 6px; }}
        .injury-badge-q {{ background-color: #f59e0b; color: #0f172a; font-size: 0.7rem; font-weight: bold; padding: 2px 5px; border-radius: 4px; margin-left: 6px; }}

        @media screen and (max-width: 768px) {{
            body {{ padding: 12px; }}
            .header-container {{ flex-direction: column; align-items: flex-start; gap: 12px; }}
            .controls {{ width: 100%; justify-content: space-between; }}
            .timestamp-bar {{ flex-direction: column; gap: 6px; }}
            .tab-bar {{ overflow-x: auto; white-space: nowrap; padding-bottom: 4px; }}
            .tab-btn {{ padding: 8px 14px; font-size: 0.9rem; }}
            #squadTableContainer, #waiverTableContainer, #tradeTableContainer, #sosTableContainer, #modalTableContainer {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
            table {{ font-size: 0.82rem; }}
            th, td {{ padding: 8px 10px; }}
            .modal-card {{ width: 95%; padding: 15px; max-height: 90vh; }}
        }}
    </style>
</head>
<body>

    <div class="header-container">
        <div>
            <h1>🌌 Fandromeda Engine</h1>
            <div style="color: #64748b; font-size: 0.9rem; margin-top: 4px;">Fantasy Football Analytics & Waiver Intelligence Hub</div>
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
        <div class="section-header" onclick="toggleCard('squadCard', 'squadHint')">
            <h2>🏈 Squad Evaluation (<span id="activeTeamHeader">{default_team}</span>) <span class="toggle-hint" id="squadHint">[+ Click to expand definitions]</span></h2>
        </div>
        <div class="info-card" id="squadCard">
            <strong>Squad Evaluation Architecture (Role Verdict + Tactical Flags):</strong>
            <ul>
                <li><strong>Role Verdict:</strong> Primary status column (<code>🔥 CORE STARTER</code>, <code>📈 SURGING ROLE</code>, <code>🚨 BUY LOW HOLD</code>, <code>✂️ DROP CANDIDATE</code>, <code>👀 HOLD</code>).</li>
                <li><strong>Dynamic Tactical Hover:</strong> Hovering over flags reveals exact underlying stats (e.g., Snap Share, Air Yds, Red Zone Opps).</li>
                <li><strong>Modal Game Logs:</strong> Click any player name to view their 3-week stat averages and full game log.</li>
            </ul>
        </div>
        <div id="squadTableContainer"></div>

        <div class="section-header" onclick="toggleCard('sosCard', 'sosHint')">
            <h2>🗓️ Strength of Schedule (Next 3 Weeks) <span class="toggle-hint" id="sosHint">[+ Click to expand definitions]</span></h2>
        </div>
        <div class="info-card" id="sosCard">
            <strong>Matchup Difficulty Legend:</strong>
            <ul>
                <li><span class="matchup-easy">🟩 EASY:</span> Facing a defense allowing high fantasy points to this player's position (Top-8 favorable matchup).</li>
                <li><span class="matchup-neutral">🟨 NEUTRAL:</span> Average defensive matchup against this position.</li>
                <li><span class="matchup-tough">🟥 TOUGH:</span> Facing a top-8 defense against this player's position.</li>
            </ul>
        </div>
        <div id="sosTableContainer"></div>
    </div>

    <!-- TAB 2: MARKET INTELLIGENCE -->
    <div id="market-tab" class="tab-content">
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
            <strong>Waiver Wire Evaluation Architecture:</strong>
            <ul>
                <li><strong>Role Verdict:</strong> Priority classification for unowned assets.</li>
                <li><strong>Tactical Flags:</strong> Actionable overlay tags including <code>⏱️ HIGH SNAP BUY-LOW</code>, <code>🎯 RED ZONE BUY-LOW</code>, and <code>🚨 AIR YARD BUY-LOW</code>.</li>
            </ul>
        </div>
        <div id="waiverTableContainer"></div>

        <div class="section-header" onclick="toggleCard('tradeCard', 'tradeHint')">
            <h2>🎯 Rival Roster Trade Targets (High Opportunity / Low Output) <span class="toggle-hint" id="tradeHint">[+ Click to expand definitions]</span></h2>
            <button class="toggle-btn" onclick="openTradeAnalyzerModal()" style="margin-left: 15px; background-color: #38bdf8; color: #0f172a;">⚖️ Open Trade Analyzer</button>
        </div>
        <div class="info-card" id="tradeCard">
            <strong>Trade Targets Architecture:</strong>
            <ul>
                <li>Highlights rival rostered players seeing substantial workload and snap share but underperforming on fantasy points.</li>
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
                    <span class="feature-title">🏈 Position Opportunity Score & WOPR Derivative</span>
                    <div class="feature-desc">Combines target share, air yards share, rushing share, and offensive snap percentage into a single position-specific opportunity metric normalized between 0.00 and 1.00.</div>
                    <div class="formula-box">
                        <div>• <code>WR / TE Opp:</code> 1.2 × Target Share + 0.6 × Air Yards Share + 0.3 × Snap Pct</div>
                        <div>• <code>RB Opp:</code> 0.8 × Rushing Share + 1.2 × Target Share + 0.3 × Snap Pct</div>
                        <div>• <code>QB Opp:</code> ((Attempts + Carries) / 50) × (0.8 + 0.2 × Snap Pct)</div>
                    </div>
                </li>
                <li class="feature-item">
                    <span class="feature-title">⏱️ Snap Count Share Integration</span>
                    <div class="feature-desc">Incorporates official offensive snap percentages directly into positional opportunity calculations. Flags players logging high snap participation (≥75%) who are underperforming in fantasy output.</div>
                </li>
                <li class="feature-item">
                    <span class="feature-title">📊 Trailing 3-Week Rolling PPR Average</span>
                    <div class="feature-desc">Aggregates a player's trailing 3 active games via DuckDB CTE window partitions to guarantee true mathematical rolling averages across all master tables rather than single-week snapshots.</div>
                </li>
                <li class="feature-item">
                    <span class="feature-title">📈 Workload Velocity (Opp Surge)</span>
                    <div class="feature-desc">Measures the 2-week rate of change in a player's Opportunity Score using DuckDB SQL window functions (`LEAD/LAG`) to detect rapidly expanding or shrinking roles.</div>
                    <div class="formula-box">
                        <div>• <code>Surge:</code> Trailing 3-Week Opp Score − Baseline Opp Score (2 Weeks Prior)</div>
                    </div>
                </li>
                <li class="feature-item">
                    <span class="feature-title">🎯 Red Zone & Unrealized Air Yards Buy-Low Index</span>
                    <div class="feature-desc">Tracks target air yards minus completed air yards along with red zone opportunities (targets, carries, and touchdowns inside the 20) to identify positive regression candidates before market breakouts occur.</div>
                </li>
                <li class="feature-item">
                    <span class="feature-title">📉 Micro-Sparklines (PPR Trends)</span>
                    <div class="feature-desc">Renders dynamic inline SVG polyline sparklines in master table rows, visualizing game-by-game PPR scoring trajectories across trailing active games.</div>
                </li>
                <li class="feature-item">
                    <span class="feature-title">🗓️ Strength of Schedule (SOS) & Weather Engine</span>
                    <div class="feature-desc">Ranks opponent defenses by points allowed per position over upcoming 3-game windows (`🟩 EASY`, `🟨 NEUTRAL`, `🟥 TOUGH`) and flags extreme outdoor weather alerts (`💨 Wind ≥15mph`, `🥶 Temp ≤32°F`, `🔥 Temp ≥90°F`).</div>
                </li>
                <li class="feature-item">
                    <span class="feature-title">🏥 Depth Chart Rank & Official IR/Injury Tracking</span>
                    <div class="feature-desc">Joins official nflreadpy depth chart hierarchies and practice status reports directly to player names, flagging backup volume increases (`⚠️ SHORT-TERM VOLUME`) or injury statuses (`OUT`, `Q`, `IR`).</div>
                </li>
                <li class="feature-item">
                    <span class="feature-title">⚖️ Streamlined Trade Impact Analyzer</span>
                    <div class="feature-desc">Interactive modal calculator evaluating proposed multi-player trades by comparing total net Opportunity Score gains/losses and net PPR point differences between managers.</div>
                </li>
            </ul>
        </div>
    </div>

    <!-- PLAYER GAME LOG MODAL -->
    <div id="playerModal" class="modal-overlay">
        <div class="modal-card">
            <span class="close-btn" onclick="closeModal()">&times;</span>
            <h2 id="modalPlayerName" style="color:#38bdf8; margin-top:0;">Player Details</h2>
            <div id="modalSubhead" style="color:#94a3b8; font-size:0.9rem; margin-bottom: 15px;"></div>
            <div id="modalTableContainer"></div>
        </div>
    </div>

    <!-- STREAMLINED TRADE ANALYZER MODAL -->
    <div id="tradeAnalyzerModal" class="modal-overlay">
        <div class="modal-card" style="max-width: 700px;">
            <span class="close-btn" onclick="closeTradeAnalyzerModal()">&times;</span>
            <h2 style="color:#38bdf8; margin-top:0;">⚖️ Trade Impact Analyzer</h2>
            <p style="color:#94a3b8; font-size:0.85rem;">Select assets to calculate net Opportunity Score & PPR impact for multi-player trade proposals.</p>
            
            <div style="display: flex; gap: 20px; margin-top: 20px;">
                <div style="flex: 1; background: #0f172a; padding: 15px; border-radius: 6px; border: 1px solid #334155;">
                    <h3 style="color: #f87171; margin-top:0;">Giving Away (<span id="giveTeamLabel">My Roster</span>)</h3>
                    <select id="givePlayer1" onchange="calculateTradeImpact()" style="width: 100%; margin-bottom: 10px;"><option value="">Select Player 1...</option></select>
                    <select id="givePlayer2" onchange="calculateTradeImpact()" style="width: 100%;"><option value="">Select Player 2 (Optional)...</option></select>
                </div>
                <div style="flex: 1; background: #0f172a; padding: 15px; border-radius: 6px; border: 1px solid #334155;">
                    <h3 style="color: #4ade80; margin-top:0;">Receiving</h3>
                    <select id="receiveTeamSelect" onchange="populateReceivePlayers()" style="width: 100%; margin-bottom: 10px;">
                        <option value="ALL">Select Manager / Team...</option>
                    </select>
                    <select id="getPlayer1" onchange="calculateTradeImpact()" style="width: 100%; margin-bottom: 10px;"><option value="">Select Player 1...</option></select>
                    <select id="getPlayer2" onchange="calculateTradeImpact()" style="width: 100%;"><option value="">Select Player 2 (Optional)...</option></select>
                </div>
            </div>

            <div id="tradeSummaryOutput" style="margin-top: 20px; padding: 15px; background: #0f172a; border-radius: 6px; text-align: center; border-left: 4px solid #38bdf8;">
                <span style="color: #64748b;">Select players above to view net proposal impact.</span>
            </div>
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
            '🚑 INJURED / OUT': 'Player listed as Out, Doubtful, or on IR.',
            '⚠️ QUESTIONABLE': 'Player listed as Questionable on official NFL injury report.',
            '⏱️ HIGH SNAP BUY-LOW': 'Averaging ≥75% snap share but low PPR output (<10.0 pts/gm). High opportunity buy-low target.',
            '⚠️ SHORT-TERM VOLUME': 'Elevated workload resulting from starter injury/IR.',
            '🚨 AIR YARD BUY-LOW': 'High downfield target volume (≥65 Unrealized Air Yds/gm) but low PPR points (<11.0).',
            '🎯 RED ZONE BUY-LOW': 'High red-zone opportunity index (≥2.5) with low touchdown output (≤1 TD over trailing 3 games).',
            '🔥 CORE STARTER': 'Elite positional workload (Opp ≥ 0.45) matched with high PPR production (PPR ≥ 13.0).',
            '🚨 BUY LOW HOLD': 'High opportunity (Opp ≥ 0.40) & target floor but low output (PPR < 11.0).',
            '📈 SURGING ROLE': 'Workload velocity growing rapidly (Surge ≥ 0.100).',
            '⚠️ FLUKE RISK': 'Scoring fantasy points on weak volume (<8 touches/gm & Opp < 0.22).',
            '✂️ DROP CANDIDATE': 'Weak volume (Opp < 0.20) and poor fantasy output (PPR < 8.0).',
            '👀 HOLD': 'Stable positional role without immediate breakout or drop signals.',
            '🚨 BUY LOW / TARGET': 'High opportunity (Opp ≥ 0.45) & target floor with weak fantasy points (PPR < 10.0).',
            '🔥 HIGH-VOLUME ALPHA': 'Unowned player producing elite volume (Opp ≥ 0.45) and strong PPR points (PPR ≥ 13.0).',
            '📈 SURGING WORKLOAD': 'Workload velocity jumping rapidly over the past 3 weeks (Surge ≥ 0.100).',
            '⚠️ SELL HIGH / FLUKE': 'Points scored without underlying volume (<8 touches/gm).',
            '👀 STASH': 'Low volume/points currently, but worth monitoring for deep bench storage.',
            '🚨 BUY LOW / TRADE TARGET': 'Target rostered players with significant workload (Opp ≥ 0.40) who are underperforming on points.'
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

        function populateTradeDropdowns() {{
            const activeTeam = document.getElementById('teamSelect').value;
            document.getElementById('giveTeamLabel').innerText = activeTeam;

            const givePlayers = masterSquadData.filter(p => p.fantasy_team === activeTeam);
            let giveOpts1 = '<option value="">Select Player 1...</option>';
            let giveOpts2 = '<option value="">Select Player 2 (Optional)...</option>';

            givePlayers.forEach(p => {{
                const opt = `<option value="${{p.clean_name}}">${{p.Player}} (${{p.Pos}} - ${{p.Team}})</option>`;
                giveOpts1 += opt;
                giveOpts2 += opt;
            }});

            document.getElementById('givePlayer1').innerHTML = giveOpts1;
            document.getElementById('givePlayer2').innerHTML = giveOpts2;

            const rivalTeams = Array.from(new Set(masterSquadData.map(p => p.fantasy_team)))
                                   .filter(t => t !== activeTeam)
                                   .sort();

            let teamOpts = '<option value="ALL">All Rival Managers</option>';
            rivalTeams.forEach(t => {{
                teamOpts += `<option value="${{t}}">${{t}}</option>`;
            }});
            document.getElementById('receiveTeamSelect').innerHTML = teamOpts;

            populateReceivePlayers();
        }}

        function populateReceivePlayers() {{
            const activeTeam = document.getElementById('teamSelect').value;
            const selectedRival = document.getElementById('receiveTeamSelect').value;

            let receivePlayers = masterSquadData.filter(p => p.fantasy_team !== activeTeam);
            if (selectedRival !== 'ALL') {{
                receivePlayers = receivePlayers.filter(p => p.fantasy_team === selectedRival);
            }}

            let getOpts1 = '<option value="">Select Player 1...</option>';
            let getOpts2 = '<option value="">Select Player 2 (Optional)...</option>';

            receivePlayers.forEach(p => {{
                const teamTag = selectedRival === 'ALL' ? ` [${{p.fantasy_team}}]` : '';
                const opt = `<option value="${{p.clean_name}}">${{p.Player}} (${{p.Pos}} - ${{p.Team}})${{teamTag}}</option>`;
                getOpts1 += opt;
                getOpts2 += opt;
            }});

            document.getElementById('getPlayer1').innerHTML = getOpts1;
            document.getElementById('getPlayer2').innerHTML = getOpts2;
            calculateTradeImpact();
        }}

        function openTradeAnalyzerModal() {{
            populateTradeDropdowns();
            document.getElementById('tradeAnalyzerModal').style.display = 'flex';
        }}

        function closeTradeAnalyzerModal() {{
            document.getElementById('tradeAnalyzerModal').style.display = 'none';
        }}

        function calculateTradeImpact() {{
            const allPlayers = masterSquadData.concat(masterWaiverData);
            
            const getStats = (id) => {{
                const val = document.getElementById(id).value;
                return allPlayers.find(p => p.clean_name === val) || {{ 'Opp Score': 0, 'PPR Avg': 0 }};
            }};

            const give1 = getStats('givePlayer1');
            const give2 = getStats('givePlayer2');
            const get1 = getStats('getPlayer1');
            const get2 = getStats('getPlayer2');

            const totalGiveOpp = (parseFloat(give1['Opp Score']) || 0) + (parseFloat(give2['Opp Score']) || 0);
            const totalGetOpp = (parseFloat(get1['Opp Score']) || 0) + (parseFloat(get2['Opp Score']) || 0);
            const netOpp = (totalGetOpp - totalGiveOpp).toFixed(3);

            const totalGivePPR = (parseFloat(give1['PPR Avg']) || 0) + (parseFloat(give2['PPR Avg']) || 0);
            const totalGetPPR = (parseFloat(get1['PPR Avg']) || 0) + (parseFloat(get2['PPR Avg']) || 0);
            const netPPR = (totalGetPPR - totalGivePPR).toFixed(1);

            const oppColor = netOpp >= 0 ? '#4ade80' : '#f87171';
            const pprColor = netPPR >= 0 ? '#4ade80' : '#f87171';

            document.getElementById('tradeSummaryOutput').innerHTML = `
                <div style="font-size: 1.1rem; font-weight: bold; margin-bottom: 5px;">
                    Net Opportunity Score: <span style="color: ${{oppColor}};">${{netOpp > 0 ? '+' : ''}}${{netOpp}}</span>
                </div>
                <div style="font-size: 1.1rem; font-weight: bold;">
                    Net PPR Output Avg: <span style="color: ${{pprColor}};">${{netPPR > 0 ? '+' : ''}}${{netPPR}} pts/gm</span>
                </div>
            `;
        }}

        function getMatchupClass(oppTeam, position) {{
            if (oppTeam === 'BYE') return '';
            const match = defSosData.find(d => d.def_team === oppTeam && d.position === position);
            if (!match) return 'matchup-neutral';
            const rank = match.def_rank;
            
            if (rank <= 8) return 'matchup-easy'; 
            if (rank >= 25) return 'matchup-tough'; 
            
            return 'matchup-neutral';
        }}

        function filterTeamData() {{
            const selectedTeam = document.getElementById('teamSelect').value;
            document.getElementById('activeTeamHeader').innerText = selectedTeam;

            const squadFiltered = masterSquadData.filter(row => row.fantasy_team === selectedTeam);
            renderTable('squadTableContainer', squadFiltered, ['Slot', 'Player', 'Pos', 'Team', 'Opp Score', 'Surge', 'PPR Avg', 'Trend (PPR)', 'Role Verdict', 'Tactical Flags']);

            let tradeFiltered = masterTradeData.filter(row => row.Owner !== selectedTeam);
            if (!isTradeExpanded) {{
                tradeFiltered = tradeFiltered.slice(0, 5);
            }}
            renderTable('tradeTableContainer', tradeFiltered, ['Owner', 'Player', 'Pos', 'Team', 'Opp Score', 'PPR Avg', 'Trend (PPR)', 'Role Verdict', 'Tactical Flags']);

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

                let injuryBadge = '';
                if (['Out', 'IR', 'Doubtful'].includes(p.injury_status)) {{
                    injuryBadge = '<span class="injury-badge-out">OUT</span>';
                }} else if (p.injury_status === 'Questionable') {{
                    injuryBadge = '<span class="injury-badge-q">Q</span>';
                }}

                html += `<tr>
                    <td>${{p.Slot}}</td>
                    <td><span class="player-clickable" onclick="openPlayerModal('${{p.clean_name}}', '${{p.Player}}')">${{p.Player}}</span>${{injuryBadge}}</td>
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
            
            renderTable('waiverTableContainer', waiverFiltered.slice(0, 15), ['Player', 'Pos', 'Team', 'Opp Score', 'Surge (Velocity)', 'PPR Avg', 'Trend (PPR)', 'Role Verdict', 'Tactical Flags']);
            attachSortListeners();
        }}

        function renderTable(containerId, data, columns) {{
            if (data.length === 0) {{
                document.getElementById(containerId).innerHTML = '<p style="color:#64748b; padding:15px;">No players match this criteria.</p>';
                return;
            }}
            let html = '<table class="sortable"><thead><tr>';
            columns.forEach(col => {{
                let extraClass = '';
                if (col === 'Role Verdict') extraClass = ' class="col-role-verdict"';
                else if (col === 'Tactical Flags') extraClass = ' class="col-tactical-flags"';
                html += `<th${{extraClass}}>${{col}}</th>`;
            }});
            html += '</tr></thead><tbody>';
            
            data.forEach((row, rowIndex) => {{
                html += '<tr>';
                columns.forEach(col => {{
                    let val = row[col] !== null ? row[col] : 'N/A';
                    if (col === 'Player') {{
                        let injuryBadge = '';
                        if (['Out', 'IR', 'Doubtful'].includes(row.injury_status)) {{
                            injuryBadge = '<span class="injury-badge-out">OUT</span>';
                        }} else if (row.injury_status === 'Questionable') {{
                            injuryBadge = '<span class="injury-badge-q">Q</span>';
                        }}
                        html += `<td><span class="player-clickable" onclick="openPlayerModal('${{row.clean_name}}', '${{row.Player}}')">${{val}}</span>${{injuryBadge}}</td>`;
                    }} else if (col === 'Trend (PPR)') {{
                        html += `<td>${{generateSparklineSVG(row.clean_name, rowIndex)}}</td>`;
                    }} else if (col === 'Role Verdict') {{
                        const desc = verdictTooltips[val] || 'Baseline player status classification.';
                        const popDirection = rowIndex === 0 ? 'top: 125%;' : 'bottom: 125%;';
                        html += `<td class="col-role-verdict"><div class="verdict-badge">${{val}}<span class="tooltiptext" style="${{popDirection}}">${{desc}}</span></div></td>`;
                    }} else if (col === 'Tactical Flags') {{
                        if (val === '—') {{
                            html += `<td class="col-tactical-flags" style="color:#64748b;">—</td>`;
                        }} else {{
                            const tags = val.split(' | ');
                            let badgeHtml = tags.map(tag => {{
                                let desc = verdictTooltips[tag] || 'Actionable tactical consideration.';
                                
                                if (tag === '⏱️ HIGH SNAP BUY-LOW' && row.snap_val !== undefined) {{
                                    desc += ' [3-Wk Snap Share: ' + row.snap_val + '%]';
                                }} else if (tag === '🚨 AIR YARD BUY-LOW' && row.air_yds_val !== undefined) {{
                                    desc += ' [3-Wk Air Yds Avg: ' + row.air_yds_val + ' yds/gm]';
                                }} else if (tag === '🎯 RED ZONE BUY-LOW' && row.rz_opp_val !== undefined) {{
                                    desc += ' [3-Wk RZ Opp Index: ' + row.rz_opp_val + ']';
                                }}

                                const popDirection = rowIndex === 0 ? 'top: 125%;' : 'bottom: 125%;';
                                return `<div class="verdict-badge" style="margin-right: 4px;">${{tag}}<span class="tooltiptext" style="${{popDirection}}">${{desc}}</span></div>`;
                            }}).join(' | ');
                            html += `<td class="col-tactical-flags">${{badgeHtml}}</td>`;
                        }}
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
            
            const allMaster = masterSquadData.concat(masterWaiverData);
            const pSummary = allMaster.find(p => p.clean_name === cleanName);
            
            if (playerGames.length === 0) {{
                document.getElementById('modalSubhead').innerText = "No active game logs available.";
                document.getElementById('modalTableContainer').innerHTML = "";
            }} else {{
                let statBadgesHtml = '';
                if (pSummary) {{
                    const snapVal = pSummary.snap_val !== undefined ? pSummary.snap_val + '%' : 'N/A';
                    const airYds = pSummary.air_yds_val !== undefined ? pSummary.air_yds_val : 'N/A';
                    const rzOpp = pSummary.rz_opp_val !== undefined ? pSummary.rz_opp_val : 'N/A';
                    const surgeVal = pSummary.Surge !== undefined ? pSummary.Surge : (pSummary['Surge (Velocity)'] || '0.0');
                    const injStatus = pSummary.injury_status || 'Active';

                    statBadgesHtml = `
                        <div style="margin-top: 8px;">
                            <span class="modal-stat-pill">🏥 Status: ${{injStatus}}</span>
                            <span class="modal-stat-pill">⏱️ 3-Wk Snap Share: ${{snapVal}}</span>
                            <span class="modal-stat-pill">🎯 3-Wk Air Yds: ${{airYds}} yds/gm</span>
                            <span class="modal-stat-pill">🚩 3-Wk RZ Index: ${{rzOpp}}</span>
                            <span class="modal-stat-pill">📈 Surge: ${{surgeVal}}</span>
                        </div>
                    `;
                }}

                document.getElementById('modalSubhead').innerHTML = `
                    Position: <strong>${{playerGames[0].position}}</strong> | 
                    <span style="color:#38bdf8;">Blue rows indicate active 3-week heuristic window</span>
                    ${{statBadgesHtml}}
                `;
                
                let html = '<table class="sortable"><thead><tr><th>Season</th><th>Week</th><th>Snap %</th><th>Targets</th><th>Carries</th><th>Pass Att</th><th>Tgt Share</th><th>Air Yard Share</th><th>Unrealized AY</th><th>RZ Opp</th><th>TDs</th><th>Opp Score</th><th>PPR Pts</th></tr></thead><tbody>';
                playerGames.forEach(g => {{
                    const isActiveWindow = g.game_rn <= 3;
                    const rowClass = isActiveWindow ? 'class="active-window-row"' : '';
                    const activeBadge = isActiveWindow ? '<span class="active-window-badge">★ Active Window</span>' : '';

                    html += `<tr ${{rowClass}}>
                        <td>${{g.season}}</td>
                        <td>Week ${{g.week}}${{activeBadge}}</td>
                        <td><strong>${{g.snap_pct}}%</strong></td>
                        <td>${{g.targets}}</td>
                        <td>${{g.carries}}</td>
                        <td>${{g.pass_attempts}}</td>
                        <td>${{(g.tgt_share * 100).toFixed(1)}}%</td>
                        <td>${{(g.ay_share * 100).toFixed(1)}}%</td>
                        <td><strong>${{g.unrealized_ay}}</strong></td>
                        <td><strong>${{g.rz_opp}}</strong></td>
                        <td><strong>${{g.total_tds}}</strong></td>
                        <td><strong>${{g.opp_score}}</strong></td>
                        <td><strong>${{g.ppr_pts}}</strong></td>
                    </tr>`;
                }});
                html += '</tbody></table>';
                document.getElementById('modalTableContainer').innerHTML = html;

                attachSortListeners();
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
    "✅ Fandromeda Dashboard updated successfully with Snap Count tracking:"
    f" '{output_file}'"
)

if not os.getenv("GITHUB_ACTIONS"):
  webbrowser.open("file://" + os.path.realpath(output_file))