import duckdb
import pandas as pd

# Load clean roster CSV
df_rosters = pd.read_csv("all_league_rosters.csv")

# 1. Query your squad safely using standard single quotes
team_name = "Vader's Raiders"
escaped_team = team_name.replace("'", "''")

my_team_query = f"""
SELECT roster_pos, player_name 
FROM df_rosters 
WHERE fantasy_team = '{escaped_team}';
"""

print(f"🏈 SQUAD ROSTER FOR: {team_name}")
print(duckdb.query(my_team_query).df())
print("-" * 50)

# 2. View full league count breakdown by team
league_summary_query = """
SELECT fantasy_team, COUNT(*) as rostered_players
FROM df_rosters
GROUP BY fantasy_team
ORDER BY rostered_players DESC;
"""

print("📊 LEAGUE ROSTER BREAKDOWN:")
print(duckdb.query(league_summary_query).df())