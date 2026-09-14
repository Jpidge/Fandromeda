SELECT 
    clean_name,
    team,
    week,
    target_share,
    -- Calculate 3-week moving average
    AVG(target_share) OVER (
        PARTITION BY clean_name 
        ORDER BY week 
        ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
    ) as rolling_3wk_target_share,
    -- Calculate 3-week target share delta (trend direction)
    target_share - LAG(target_share, 2) OVER (
        PARTITION BY clean_name 
        ORDER BY week
    ) as target_share_surge
FROM df_nfl
WHERE position = 'WR';