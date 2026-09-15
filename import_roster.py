import os
import re
import pandas as pd
import pyperclip


def clean_yahoo_roster_with_teams():
    filename = "roster_raw.txt"
    raw_text = ""

    if os.path.exists(filename):
        print(f"📄 Reading raw text from '{filename}'...")
        with open(filename, "r", encoding="utf-8") as f:
            raw_text = f.read()
    else:
        print("📋 Reading clipboard data via pyperclip...")
        try:
            raw_text = pyperclip.paste()
        except Exception as e:
            print(f"❌ Failed to read clipboard. Error: {e}")
            return None

    if not raw_text.strip():
        print("❌ Clipboard is empty!")
        return None

    lines = raw_text.splitlines()

    # Fantasy Roster Slot Labels used by Yahoo
    valid_positions = {
        "QB",
        "RB",
        "WR",
        "TE",
        "W/R/T",
        "Q/W/R/T",
        "K",
        "DEF",
        "BN",
        "IR",
    }

    roster_records = []
    current_team = "Free Agent"
    expect_player_next = False
    current_pos = ""

    for line in lines:
        line_str = line.strip()

        if not line_str:
            continue

        # 1. Detect Manager Header (Line right before 'Pos\tPlayer' or containing the icon artifact)
        if "Pos\tPlayer" in line_str or "Pos Player" in line_str:
            continue

        # Clean non-printable icon characters like '\ue037' from team name lines
        clean_line = re.sub(r"[^\x00-\x7F]+", "", line_str).strip()

        # If a line ends with or was adjacent to team indicators
        if line_str.endswith("") or (
            len(clean_line) > 3
            and not any(clean_line.startswith(p) for p in valid_positions)
            and "Final" not in clean_line
            and " - " not in clean_line
            and "Note" not in clean_line
        ):
            # Verify it's not a player metadata line before updating manager context
            if (
                not expect_player_next
                and "vs" not in clean_line
                and "@" not in clean_line
            ):
                current_team = clean_line
                continue

        # 2. Position Slot Line (Trigger for player name on the next line)
        if line_str in valid_positions:
            expect_player_next = True
            current_pos = line_str
            continue

        # 3. Capture Clean Player Name Line
        if expect_player_next:
            # First line after position slot is always the clean player name
            player_name = line_str.strip()

            roster_records.append(
                {
                    "player_name": player_name,
                    "fantasy_team": current_team,
                    "roster_pos": current_pos,
                }
            )

            # Reset flag until next position slot trigger is hit
            expect_player_next = False

    # Build DataFrame & Deduplicate
    df = pd.DataFrame(roster_records)
    df = df.drop_duplicates(subset=["player_name"]).reset_index(drop=True)

    # Export clean league roster table
    df.to_csv("all_league_rosters.csv", index=False)

    print(
        f"✅ Success! Mapped {len(df)} players to their fantasy teams in 'all_league_rosters.csv'.\n"
    )
    print(df.head(25))
    return df


if __name__ == "__main__":
    clean_yahoo_roster_with_teams()