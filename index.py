from datetime import datetime
import json
import os
import webbrowser
import duckdb
import nflreadpy as nfl
import pandas as pd

print(
    "🚀 Loading nflverse multi-position data & building Fandrameda Interactive Dashboard..."
)

# 1. Load Data
df_nfl = nfl.load_player_stats([2025, 2026]).to_pandas()
df_rosters = pd.read_csv("all_league_rosters.csv")

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

# Filter valid positions & fill nulls directly in Pandas
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
]
for col in cols_to_fill:
    if col in df_nfl.columns:
        df_nfl[col] = df_nfl[col].fillna(0.0)
    else:
        df_nfl[col] = 0.0


# 4. Pre-Calculate Position Opportunity Score in Pandas
def calc_pos_opp(row):
    pos = row["position"]
    if pos in ["WR", "TE"]:
        return 1.5 * row["target_share"] + 0.7 * row["air_yards_share"]
    elif pos == "RB":
        return row["rushing_share"] + 1.5 * row["target_share"]
    elif pos == "QB":
        return (row["attempts"] + row["carries"]) / 50.0
    return 0.0


df_nfl["pos_opp_score"] = df_nfl.apply(calc_pos_opp, axis=1)

all_teams = sorted(df_rosters["fantasy_team"].dropna().unique().tolist())
default_team = (
    "Vader's Raiders" if "Vader's Raiders" in all_teams else all_teams[0]
)

# 5. DuckDB Base Analytics Query
base_query = """
WITH prior_season AS (
    SELECT clean_name, AVG(pos_opp_score) as prior_opp
    FROM df_nfl WHERE season = 2025
    GROUP BY clean_name
),

current_season AS (
    SELECT 
        clean_name, 
        player_display_name as player_name, 
        position, 
        team, 
        week, 
        fantasy_points_ppr as ppr_pts, 
        targets, 
        carries, 
        attempts as pass_attempts, 
        target_share, 
        air_yards_share, 
        rushing_share, 
        pos_opp_score,
        AVG(pos_opp_score) OVER (PARTITION BY clean_name ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) as curr_3wk_opp,
        AVG(fantasy_points_ppr) OVER (PARTITION BY clean_name ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) as curr_3wk_ppr,
        LAG(pos_opp_score, 2, pos_opp_score) OVER (PARTITION BY clean_name ORDER BY week) as prev_opp_base,
        COUNT(week) OVER (PARTITION BY clean_name) as weeks_played
    FROM df_nfl WHERE season = 2026
),

latest_curr AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY clean_name ORDER BY week DESC) as rn
    FROM current_season
),

blended_metrics AS (
    SELECT 
        c.player_name, c.clean_name, c.position, c.team, c.curr_3wk_ppr,
        (GREATEST(0.0, (4.0 - c.weeks_played) / 4.0) * COALESCE(p.prior_opp, c.curr_3wk_opp)) +
        ((1.0 - GREATEST(0.0, (4.0 - c.weeks_played) / 4.0)) * c.curr_3wk_opp) as blended_opp,
        ((GREATEST(0.0, (4.0 - c.weeks_played) / 4.0) * COALESCE(p.prior_opp, c.curr_3wk_opp)) +
        ((1.0 - GREATEST(0.0, (4.0 - c.weeks_played) / 4.0)) * c.curr_3wk_opp)) - c.prev_opp_base as opp_surge
    FROM latest_curr c
    LEFT JOIN prior_season p ON c.clean_name = p.clean_name
    WHERE c.rn = 1
)
SELECT * FROM blended_metrics;
"""

df_analytics = duckdb.query(base_query).df()

# 6. Granular Query for Popover Game Logs
granular_query = """
WITH recent_games AS (
    SELECT 
        clean_name, week, position, targets, carries, attempts as pass_attempts,
        ROUND(target_share, 3) as tgt_share,
        ROUND(air_yards_share, 3) as ay_share,
        ROUND(rushing_share, 3) as rush_share,
        ROUND(pos_opp_score, 3) as opp_score,
        ROUND(fantasy_points_ppr, 1) as ppr_pts,
        ROW_NUMBER() OVER (PARTITION BY clean_name ORDER BY week DESC) as game_rn
    FROM df_nfl
    WHERE season = 2026
)
SELECT * FROM recent_games WHERE game_rn <= 3 ORDER BY clean_name, week DESC;
"""
df_granular = duckdb.query(granular_query).df()

# 7. Master Table Queries
squad_master_sql = """
SELECT 
    r.fantasy_team, r.roster_pos as "Slot", bm.player_name as "Player", bm.clean_name, bm.position as "Pos", bm.team as "Team",
    ROUND(bm.blended_opp, 3) as "Opp Score", ROUND(bm.opp_surge, 3) as "Surge", ROUND(bm.curr_3wk_ppr, 1) as "PPR Avg",
    CASE 
        WHEN bm.blended_opp < 0.20 AND bm.curr_3wk_ppr < 8.0 THEN '✂️ DROP CANDIDATE'
        WHEN bm.blended_opp < 0.25 AND bm.curr_3wk_ppr >= 13.0 THEN '⚠️ FLUKE RISK (Trade Out)'
        WHEN bm.blended_opp >= 0.45 AND bm.curr_3wk_ppr >= 13.0 THEN '🔥 CORE STARTER'
        WHEN bm.blended_opp >= 0.40 AND bm.curr_3wk_ppr < 11.0 THEN '🚨 BUY LOW HOLD'
        WHEN bm.opp_surge >= 0.100 THEN '📈 SURGING ROLE'
        ELSE '👀 HOLD'
    END as "Verdict"
FROM df_rosters r
JOIN df_analytics bm ON r.clean_name = bm.clean_name
ORDER BY r.fantasy_team, "Opp Score" DESC;
"""
df_squad_master = duckdb.query(squad_master_sql).df()

# Expand Waiver Wire Pool to allow positional filtering (LIMIT removed from SQL, handled in JS)
waiver_sql = """
SELECT 
    player_name as "Player", clean_name, position as "Pos", team as "Team",
    ROUND(blended_opp, 3) as "Opp Score", ROUND(opp_surge, 3) as "Surge (Velocity)", ROUND(curr_3wk_ppr, 1) as "PPR Avg",
    CASE 
        WHEN blended_opp >= 0.45 AND curr_3wk_ppr < 10.0 THEN '🚨 BUY LOW / TARGET'
        WHEN blended_opp < 0.25 AND curr_3wk_ppr >= 13.0 THEN '⚠️ SELL HIGH / FLUKE'
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
    ROUND(bm.blended_opp, 3) as "Opp Score", ROUND(bm.curr_3wk_ppr, 1) as "PPR Avg",
    '🚨 BUY LOW / TRADE TARGET' as "Verdict"
FROM df_rosters r
JOIN df_analytics bm ON r.clean_name = bm.clean_name
WHERE bm.blended_opp >= 0.40 AND bm.curr_3wk_ppr < 11.0
ORDER BY "Opp Score" DESC;
"""
df_trade_master = duckdb.query(trade_master_sql).df()

team_options_html = "".join(
    [
        f'<option value="{t}" {"selected" if t == default_team else ""}>{t}</option>'
        for t in all_teams
    ]
)

# 8. Build HTML Layout string
html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Fandrameda Interactive Dashboard</title>
    <style>
        body {{ background-color: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 30px; margin: 0; }}
        .header-container {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #334155; padding-bottom: 15px; margin-bottom: 25px; }}
        .section-header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; margin-top: 35px; padding-bottom: 8px; }}
        .section-header h2 {{ color: #94a3b8; font-size: 1.3rem; margin: 0; border: none; padding: 0; }}
        h1 {{ color: #38bdf8; font-size: 2.2rem; margin: 0; }}
        .timestamp-bar {{ display: flex; gap: 20px; font-size: 0.85rem; color: #94a3b8; background-color: #1e293b; padding: 8px 15px; border-radius: 6px; margin-bottom: 20px; border: 1px solid #334155; }}
        .controls {{ display: flex; align-items: center; gap: 10px; }}
        select {{ background-color: #1e293b; color: #38bdf8; border: 1px solid #38bdf8; padding: 6px 12px; border-radius: 6px; font-size: 0.95rem; font-weight: bold; cursor: pointer; outline: none; }}
        select:hover {{ background-color: #334155; }}
        
        .info-card {{ background-color: #1e293b; border-left: 4px solid #38bdf8; padding: 15px 20px; margin-top: 15px; border-radius: 0 8px 8px 0; font-size: 0.9rem; line-height: 1.5; }}
        .info-card ul {{ margin: 5px 0 0 0; padding-left: 20px; }}
        .info-card li {{ margin-bottom: 4px; }}
        
        table {{ width: 100%; border-collapse: collapse; margin-top: 15px; background-color: #1e293b; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3); }}
        th {{ background-color: #334155; color: #38bdf8; text-align: left; padding: 12px 16px; font-size: 0.9rem; text-transform: uppercase; letter-spacing: 0.05em; cursor: pointer; user-select: none; }}
        th:hover {{ background-color: #475569; }}
        th::after {{ content: ' ↕'; font-size: 0.75rem; color: #64748b; }}
        td {{ padding: 12px 16px; border-bottom: 1px solid #334155; font-size: 0.95rem; }}
        tr:hover {{ background-color: #24334d; }}
        
        .player-clickable {{ color: #38bdf8; font-weight: bold; cursor: pointer; text-decoration: underline; }}
        .player-clickable:hover {{ color: #7dd3fc; }}

        .modal-overlay {{
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(15, 23, 42, 0.85); backdrop-filter: blur(4px);
            justify-content: center; align-items: center; z-index: 1000;
        }}
        .modal-card {{
            background: #1e293b; border: 1px solid #38bdf8; border-radius: 10px; padding: 25px;
            width: 90%; max-width: 800px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); position: relative;
        }}
        .close-btn {{
            position: absolute; top: 15px; right: 20px; color: #94a3b8; font-size: 1.5rem;
            cursor: pointer; font-weight: bold;
        }}
        .close-btn:hover {{ color: #f8fafc; }}
    </style>
</head>
<body>

    <div class="header-container">
        <div>
            <h1>🌌 Fandrameda Engine</h1>
            <div style="color: #64748b; font-size: 0.9rem; margin-top: 4px;">Cosmic Fantasy Analytics & Waiver Intelligence Hub | <i>Click headers to sort | Click player names for game logs</i></div>
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

    <div class="info-card">
        <strong>📚 Position-Specific Opportunity Metrics:</strong>
        <ul>
            <li><strong>WR / TE Opportunity (WOPR):</strong> 1.5 × Target Share + 0.7 × Air Yards Share.</li>
            <li><strong>RB Opportunity Share:</strong> Rushing Share + 1.5 × Target Share.</li>
            <li><strong>QB Opportunity Share:</strong> Passing Attempts & Rushing Carries volume share.</li>
        </ul>
    </div>

    <!-- SECTION 1 -->
    <div class="section-header">
        <h2>🏈 Squad Evaluation (<span id="activeTeamHeader">{default_team}</span>)</h2>
    </div>
    <div class="info-card">
        <strong>Squad Verdict Guidance & Master Definitions:</strong>
        <ul>
            <li><strong>🔥 CORE STARTER:</strong> Elite positional workload (Opp ≥ 0.45) matched with high PPR production (PPR ≥ 13.0). Unquestioned weekly start.</li>
            <li><strong>🚨 BUY LOW HOLD:</strong> High opportunity (Opp ≥ 0.40) but temporary low fantasy output (PPR &lt; 11.0). Do not drop; breakout incoming.</li>
            <li><strong>📈 SURGING ROLE:</strong> Workload velocity growing rapidly (Surge ≥ 0.100). A bench player is seeing more playing time and oportunities in recent weeks.</li>
            <li><strong>⚠️ FLUKE RISK (Trade Out):</strong> Scoring fantasy points on weak underlying volume (Opp &lt; 0.25, PPR ≥ 13.0). Sell high before efficiency drops.</li>
            <li><strong>✂️ DROP CANDIDATE:</strong> Weak volume (Opp &lt; 0.20) and poor fantasy output (PPR &lt; 8.0). Safely drop to free up bench space.</li>
            <li><strong>👀 HOLD:</strong> Stable positional role without immediate breakout or drop signals.</li>
        </ul>
    </div>
    <div id="squadTableContainer"></div>

    <!-- SECTION 2 -->
    <div class="section-header">
        <h2>🔥 Unclaimed Waiver Wire (Ranked by Opportunity Surge)</h2>
        <div class="controls">
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
    <div class="info-card">
        <strong>Waiver Verdict Guidance & Master Definitions:</strong>
        <ul>
            <li><strong>🚨 BUY LOW / TARGET:</strong> High opportunity (Opp ≥ 0.45) with weak current fantasy production (PPR &lt; 10.0). Prime waiver target before points explode.</li>
            <li><strong>🔥 HIGH-VOLUME ALPHA:</strong> Unowned player producing elite volume (Opp ≥ 0.45) and strong PPR points (PPR ≥ 13.0). Priority pickup.</li>
            <li><strong>📈 SURGING WORKLOAD:</strong> Workload velocity jumping rapidly over the past 3 weeks (Surge ≥ 0.100).</li>
            <li><strong>⚠️ SELL HIGH / FLUKE:</strong> Scoring fantasy points without volume backing (Opp &lt; 0.25, PPR ≥ 13.0). High risk for waiver priority spend.</li>
            <li><strong>👀 STASH:</strong> Low volume/points currently, but worth monitoring for deep bench storage.</li>
        </ul>
    </div>
    <div id="waiverTableContainer"></div>

    <!-- SECTION 3 -->
    <div class="section-header">
        <h2>🎯 Rival Roster Trade Targets (High Opportunity / Low Output)</h2>
    </div>
    <div class="info-card">
        <strong>Trade Verdict Guidance & Master Definitions:</strong>
        <ul>
            <li><strong>🚨 BUY LOW / TRADE TARGET:</strong> Player rostered by a rival manager who commands significant workload (Opp ≥ 0.40) but is underperforming on points (PPR &lt; 11.0). Target in trade offers while their owner is frustrated.</li>
        </ul>
    </div>
    <div id="tradeTableContainer"></div>

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

        function filterTeamData() {{
            const selectedTeam = document.getElementById('teamSelect').value;
            document.getElementById('activeTeamHeader').innerText = selectedTeam;

            const squadFiltered = masterSquadData.filter(row => row.fantasy_team === selectedTeam);
            renderTable('squadTableContainer', squadFiltered, ['Slot', 'Player', 'Pos', 'Team', 'Opp Score', 'Surge', 'PPR Avg', 'Verdict']);

            const tradeFiltered = masterTradeData.filter(row => row.Owner !== selectedTeam);
            renderTable('tradeTableContainer', tradeFiltered, ['Owner', 'Player', 'Pos', 'Team', 'Opp Score', 'PPR Avg', 'Verdict']);
            
            attachSortListeners();
        }}

        function filterWaiverData() {{
            const selectedPos = document.getElementById('posSelect').value;
            let waiverFiltered = masterWaiverData;
            
            if (selectedPos !== 'ALL') {{
                waiverFiltered = masterWaiverData.filter(row => row.Pos === selectedPos);
            }}
            
            // Limit view to top 15 results for selected position
            renderTable('waiverTableContainer', waiverFiltered.slice(0, 15), ['Player', 'Pos', 'Team', 'Opp Score', 'Surge (Velocity)', 'PPR Avg', 'Verdict']);
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
            
            data.forEach(row => {{
                html += '<tr>';
                columns.forEach(col => {{
                    let val = row[col] !== null ? row[col] : 'N/A';
                    if (col === 'Player') {{
                        html += `<td><span class="player-clickable" onclick="openPlayerModal('${{row.clean_name}}', '${{row.Player}}')">${{val}}</span></td>`;
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
                document.getElementById('modalSubhead').innerText = "No granular game logs available for 2026 season.";
                document.getElementById('modalTableContainer').innerHTML = "";
            }} else {{
                document.getElementById('modalSubhead').innerText = `Trailing 3 Games Breakdown (Position: ${{playerGames[0].position}})`;
                
                let html = '<table><thead><tr><th>Week</th><th>Targets</th><th>Carries</th><th>Pass Att</th><th>Target Share</th><th>Air Yard Share</th><th>Rush Share</th><th>Opp Score</th><th>PPR Pts</th></tr></thead><tbody>';
                playerGames.forEach(g => {{
                    html += `<tr>
                        <td>Week ${{g.week}}</td>
                        <td>${{g.targets}}</td>
                        <td>${{g.carries}}</td>
                        <td>${{g.pass_attempts}}</td>
                        <td>${{(g.tgt_share * 100).toFixed(1)}}%</td>
                        <td>${{(g.ay_share * 100).toFixed(1)}}%</td>
                        <td>${{(g.rush_share * 100).toFixed(1)}}%</td>
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

print(f"✅ Fandrameda Dashboard generated successfully: '{output_file}'")
webbrowser.open("file://" + os.path.realpath(output_file))