import duckdb
import nflreadpy as nfl
import pandas as pd

print("🚀 Loading nflverse player stats...")
df_nfl = nfl.load_player_stats([2025]).to_pandas()
df_rosters = pd.read_csv("all_league_rosters.csv")


# Clean suffixes so "Michael Pittman Jr." matches "Michael Pittman"
def clean_name(name):
    if not isinstance(name, str):
        return name
    # Remove suffixes
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


# Apply clean names to both DataFrames for seamless joining
df_rosters["clean_name"] = df_rosters["player_name"].apply(clean_name)
df_nfl["clean_name"] = df_nfl["player_display_name"].apply(clean_name)

my_team = "Purple Reign"
escaped_team = my_team.replace("'", "''")

# 1. Query your squad roster with normalized names
my_team_sql = f"""
SELECT 
    r.roster_pos, 
    r.player_name, 
    COALESCE(MAX(n.position), r.roster_pos) as pos,
    COALESCE(MAX(n.team), 'N/A') as team,
    COALESCE(ROUND(SUM(n.fantasy_points_ppr), 2), 0.0) as total_ppr_pts
FROM df_rosters r
LEFT JOIN df_nfl n ON r.clean_name = n.clean_name
WHERE r.fantasy_team = '{escaped_team}'
GROUP BY r.roster_pos, r.player_name
ORDER BY total_ppr_pts DESC;
"""

print(f"\n🏈 SQUAD OVERVIEW: {my_team}")
print(duckdb.query(my_team_sql).df().to_string(index=False))

# 2. Waiver Wire Gold Miner (Excludes all rostered clean names)
waiver_sql = """
SELECT 
    n.player_display_name as player_name,
    n.position,
    n.team,
    ROUND(AVG(n.target_share), 3) as avg_target_share,
    ROUND(AVG(n.air_yards_share), 3) as avg_air_yards_share,
    ROUND(SUM(n.fantasy_points_ppr), 1) as total_ppr_pts
FROM df_nfl n
WHERE n.clean_name NOT IN (SELECT clean_name FROM df_rosters)
  AND n.position IN ('WR', 'RB', 'TE')
GROUP BY n.player_display_name, n.position, n.team
HAVING COUNT(n.week) >= 1
ORDER BY avg_target_share DESC
LIMIT 15;
"""

print("\n🔮 FANTASTRADAMUS TOP UNCLAIMED WAIVER TARGETS:")
print(duckdb.query(waiver_sql).df().to_string(index=False))