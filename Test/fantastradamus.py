import duckdb
import pandas as pd

class Fantastradamus:
    def __init__(self):
        self.pbp_url_template = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{year}.parquet"
    
    def fetch_player_passing_stats(self, year=2024, min_attempts=100):
        """Query play-by-play data directly via SQL for passing projections."""
        url = self.pbp_url_template.format(year=year)
        query = f"""
        SELECT 
            passer_player_name AS player_name, 
            COUNT(*) AS pass_attempts, 
            SUM(pass_touchdown) AS total_tds, 
            ROUND(AVG(yards_gained), 2) AS avg_yards_per_pass,
            ROUND(SUM(epa), 2) AS total_epa
        FROM '{url}'
        WHERE pass_attempt = 1 AND passer_player_name IS NOT NULL
        GROUP BY passer_player_name
        HAVING COUNT(*) > {min_attempts}
        ORDER BY total_epa DESC;
        """
        return duckdb.query(query).to_df()

# Run Fantastradamus Engine
if __name__ == "__main__":
    bot = Fantastradamus()
    print("🔮 Fantastradamus is querying the gridiron grid...")
    df_2024 = bot.fetch_player_passing_stats(year=2024)
    print(df_2024.head(10))
