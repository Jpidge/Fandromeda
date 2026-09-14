from yahoo_oauth import OAuth2
import yahoo_fantasy_api as yfa
import pandas as pd

print("🔮 Fantastradamus is contacting Yahoo Fantasy...")

# Force the Fantasy Sports Read scope on initialization
sc = OAuth2(None, None, from_file="oauth2.json", scope="fspt-r")

if not sc.token_is_valid():
    sc.refresh_access_token()

gm = yfa.Game(sc, 'nfl')

# Get available league IDs
league_ids = gm.league_ids()
print(f"Found League IDs: {league_ids}")

if league_ids:
    league_id = league_ids[0]
    league = gm.to_league(league_id)
    
    print(f"✅ Connected to League: {league_id}")
    
    team_key = league.team_key()
    team = league.to_team(team_key)
    my_roster = team.roster()
    
    df_roster = pd.DataFrame(my_roster)
    print("\n--- YOUR CURRENT FANTASY ROSTER ---")
    print(df_roster[['name', 'selected_position', 'position_type']])
