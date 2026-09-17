import duckdb
import nflreadpy as nfl

# Load weekly player stats directly into a pandas DataFrame via Polars
df_weekly = nfl.load_player_stats([2024, 2025]).to_pandas()

# Query with DuckDB
query = """
SELECT player_name, position, target_share, air_yards_share 
FROM df_weekly 
WHERE season = 2025 AND week = 1
ORDER BY target_share DESC 
LIMIT 10;
"""
print(duckdb.query(query).df())