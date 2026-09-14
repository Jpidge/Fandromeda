import duckdb

query = """
SELECT 
    passer_player_name, 
    COUNT(*) AS pass_attempts, 
    SUM(pass_touchdown) AS total_tds, 
    AVG(yards_gained) AS avg_yards_per_pass
FROM 'https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_2023.parquet'
WHERE pass_attempt = 1
GROUP BY passer_player_name
HAVING COUNT(*) > 100
ORDER BY total_tds DESC;
"""

df = duckdb.query(query).to_df()
print(df.head(10))
