#!/bin/bash
# Phase 0 数据回测脚本 - v2 决策层质量门效果模拟
# 目的: 用 30 天历史数据, 看"如果当时应用 6 道质量门, 会拒掉多少信号 / 保留哪些 / 总 PnL 改善多少"
# 不动任何线上系统, 纯 SQL 查询.

cd /opt/openchart && DB=data/openchart.db

echo "═══════════════════════════════════════════════════════════════"
echo "  Phase 0 数据回测 — v2 质量门效果模拟"
echo "═══════════════════════════════════════════════════════════════"
echo ""

echo "════ 1. 30 天总体样本量 (闭环交易) ════"
sqlite3 -column -header $DB "
WITH closed AS (
  SELECT position_id, market,
    MAX(CASE WHEN action='open' THEN price END) AS open_p,
    MAX(CASE WHEN action='close' THEN price END) AS close_p,
    MAX(CASE WHEN action='open' THEN traded_at END) AS open_at,
    MAX(CASE WHEN action='close' THEN trigger_type END) AS trig
  FROM auto_trade_log
  WHERE traded_at > strftime('%s','now') - 30*86400 AND market IN ('us','hk','cn')
  GROUP BY position_id HAVING close_p IS NOT NULL
)
SELECT
  market AS 市场,
  COUNT(*) AS 闭环数,
  printf('%.0f', SUM(CASE WHEN close_p > open_p THEN 1 ELSE 0 END) * 100.0 / COUNT(*)) AS 胜率pct,
  printf('%.2f', AVG((close_p-open_p)/open_p*100)) AS 均PnLpct,
  printf('%.2f', SUM((close_p-open_p)/open_p*100)) AS 总PnLpct
FROM closed WHERE open_p > 0
GROUP BY market ORDER BY market;
"

echo ""
echo "════ 2. 门 1: 距 20 日高 < 3pct (追高, v12.24.3 阈值) — 拒后效果 ════"
sqlite3 -column -header $DB "
WITH closed AS (
  SELECT position_id, market,
    MAX(CASE WHEN action='open' THEN price END) AS open_p,
    MAX(CASE WHEN action='close' THEN price END) AS close_p,
    MAX(CASE WHEN action='open' THEN traded_at END) AS open_at,
    MAX(CASE WHEN action='open' THEN symbol END) AS sym
  FROM auto_trade_log
  WHERE traded_at > strftime('%s','now') - 30*86400 AND market='us'
  GROUP BY position_id HAVING close_p IS NOT NULL
),
ctx AS (
  SELECT c.*,
    (SELECT MAX(high) FROM [klines_us_1d] k WHERE k.symbol=c.sym
       AND k.timestamp BETWEEN (c.open_at-20*86400)*1000 AND c.open_at*1000) AS hi20
  FROM closed c
)
SELECT
  CASE WHEN (hi20-open_p)/open_p < 0.03 THEN 'A. 追高 <3pct (将拒)'
       WHEN (hi20-open_p)/open_p < 0.05 THEN 'B. 3-5pct'
       WHEN (hi20-open_p)/open_p < 0.10 THEN 'C. 5-10pct'
       ELSE 'D. >10pct (保留)' END AS 距高,
  COUNT(*) AS 笔数,
  printf('%.2f', AVG((close_p-open_p)/open_p*100)) AS 均PnLpct,
  printf('%.2f', SUM((close_p-open_p)/open_p*100)) AS 总PnLpct
FROM ctx WHERE hi20 > 0 AND open_p > 0
GROUP BY 距高 ORDER BY 距高;
"

echo ""
echo "════ 3. 门 2: 已涨幅度门 (信号前 30min 已涨 > 4pct) — 拒后效果 ════"
echo "(简化版: 用开仓当日的 [open_p / 当日开盘价] 估算)"
sqlite3 -column -header $DB "
WITH closed AS (
  SELECT position_id, market,
    MAX(CASE WHEN action='open' THEN price END) AS open_p,
    MAX(CASE WHEN action='close' THEN price END) AS close_p,
    MAX(CASE WHEN action='open' THEN traded_at END) AS open_at,
    MAX(CASE WHEN action='open' THEN symbol END) AS sym
  FROM auto_trade_log
  WHERE traded_at > strftime('%s','now') - 30*86400 AND market='us'
  GROUP BY position_id HAVING close_p IS NOT NULL
),
ctx AS (
  SELECT c.*,
    (SELECT open FROM [klines_us_1d] k WHERE k.symbol=c.sym
       AND k.timestamp/1000 <= c.open_at
       ORDER BY k.timestamp DESC LIMIT 1) AS day_open
  FROM closed c
)
SELECT
  CASE WHEN (open_p-day_open)/day_open > 0.04 THEN 'A. 当日已涨 >4pct (将拒)'
       WHEN (open_p-day_open)/day_open > 0.02 THEN 'B. 2-4pct'
       ELSE 'C. <2pct (保留)' END AS 当日已涨,
  COUNT(*) AS 笔数,
  printf('%.2f', AVG((close_p-open_p)/open_p*100)) AS 均PnLpct,
  printf('%.2f', SUM((close_p-open_p)/open_p*100)) AS 总PnLpct
FROM ctx WHERE day_open > 0 AND open_p > 0
GROUP BY 当日已涨 ORDER BY 当日已涨;
"

echo ""
echo "════ 4. 门 3: R:R < 2.0 (用 AI 给的 SL/TP 算, v12.24.3 校准阈值) — 拒后效果 ════"
sqlite3 -column -header $DB "
WITH closed AS (
  SELECT atl.position_id,
    MAX(CASE WHEN atl.action='open' THEN atl.price END) AS open_p,
    MAX(CASE WHEN atl.action='close' THEN atl.price END) AS close_p,
    MAX(CASE WHEN atl.action='open' THEN
      json_extract(atl.trigger_detail, '\$.signal_id') END) AS sig_id
  FROM auto_trade_log atl
  WHERE atl.market='us' AND atl.traded_at > strftime('%s','now') - 30*86400
  GROUP BY atl.position_id HAVING close_p IS NOT NULL
)
SELECT
  CASE
    WHEN s.ai_take_profit IS NULL OR s.ai_stop_loss IS NULL THEN 'X_缺SL/TP (v2 跳过, 缠论路径)'
    WHEN (s.ai_take_profit - cs.open_p) / NULLIF(cs.open_p - s.ai_stop_loss, 0) < 2.0
      THEN 'A. R:R <2.0 (将拒)'
    WHEN (s.ai_take_profit - cs.open_p) / NULLIF(cs.open_p - s.ai_stop_loss, 0) < 3.0
      THEN 'B. R:R 2.0-3.0'
    WHEN (s.ai_take_profit - cs.open_p) / NULLIF(cs.open_p - s.ai_stop_loss, 0) < 5.0
      THEN 'C. R:R 3.0-5.0'
    ELSE 'D. R:R ≥5.0 (AI 异常高 R:R, 实测易亏)'
  END AS R_R,
  COUNT(*) AS 笔数,
  printf('%.2f', AVG((cs.close_p-cs.open_p)/cs.open_p*100)) AS 均PnLpct,
  printf('%.2f', SUM((cs.close_p-cs.open_p)/cs.open_p*100)) AS 总PnLpct
FROM closed cs
LEFT JOIN signals s ON s.id = cs.sig_id
WHERE cs.open_p > 0
GROUP BY R_R ORDER BY R_R;
"

echo ""
echo "════ 4b. 门 5: 美股 SPY 当日跌幅 > -2.0pct → 美股 BUY 全部拒 ════"
sqlite3 -column -header $DB "
WITH closed AS (
  SELECT atl.position_id,
    MAX(CASE WHEN atl.action='open' THEN atl.price END) AS open_p,
    MAX(CASE WHEN atl.action='close' THEN atl.price END) AS close_p,
    MAX(CASE WHEN atl.action='open' THEN atl.traded_at END) AS open_at
  FROM auto_trade_log atl
  WHERE atl.market='us' AND atl.traded_at > strftime('%s','now') - 30*86400
  GROUP BY atl.position_id HAVING close_p IS NOT NULL
),
ctx AS (
  SELECT c.*,
    (SELECT (close-open)/open*100 FROM [klines_us_1d] k
     WHERE k.symbol='SPY' AND k.timestamp/1000 <= c.open_at
     ORDER BY k.timestamp DESC LIMIT 1) AS spy_chg
  FROM closed c
)
SELECT
  CASE WHEN spy_chg < -2.0 THEN 'A. SPY 当日 <-2pct (将拒 美股 BUY)'
       WHEN spy_chg < -1.0 THEN 'B. SPY -1 ~ -2pct'
       ELSE 'C. SPY > -1pct (保留)' END AS SPY_regime,
  COUNT(*) AS 笔数,
  printf('%.2f', AVG((close_p-open_p)/open_p*100)) AS 均PnLpct,
  printf('%.2f', SUM((close_p-open_p)/open_p*100)) AS 总PnLpct
FROM ctx WHERE open_p > 0 AND spy_chg IS NOT NULL
GROUP BY SPY_regime ORDER BY SPY_regime;
"

echo ""
echo "════ 5. 门 4: 共振机制反向 — 单狼 vs 共振 表现对比 ════"
echo "(看 ai_reason 是否含 '共振' 推断, 简化)"
sqlite3 -column -header $DB "
WITH closed AS (
  SELECT atl.position_id,
    MAX(CASE WHEN atl.action='open' THEN atl.price END) AS open_p,
    MAX(CASE WHEN atl.action='close' THEN atl.price END) AS close_p,
    MAX(CASE WHEN atl.action='open' THEN
      json_extract(atl.trigger_detail, '\$.signal_id') END) AS sig_id
  FROM auto_trade_log atl
  WHERE atl.market='us' AND atl.traded_at > strftime('%s','now') - 30*86400
  GROUP BY atl.position_id HAVING close_p IS NOT NULL
)
SELECT
  CASE
    WHEN s.ai_reason LIKE '%共振%' OR s.ai_reason LIKE '%resonance%' OR s.ai_reason LIKE '%三重%'
      THEN '共振信号'
    WHEN s.strategy_name = 'resonance' THEN '共振策略本身'
    ELSE '单狼信号'
  END AS 类型,
  COUNT(*) AS 笔数,
  printf('%.0f', SUM(CASE WHEN cs.close_p > cs.open_p THEN 1 ELSE 0 END) * 100.0 / COUNT(*)) AS 胜率pct,
  printf('%.2f', AVG((cs.close_p-cs.open_p)/cs.open_p*100)) AS 均PnLpct
FROM closed cs
LEFT JOIN signals s ON s.id = cs.sig_id
WHERE cs.open_p > 0
GROUP BY 类型;
"

echo ""
echo "════ 6. 综合模拟: 6 道门同时应用后 — 保留多少 / 总 PnL 改善多少 ════"
sqlite3 -column -header $DB "
WITH closed AS (
  SELECT atl.position_id,
    MAX(CASE WHEN atl.action='open' THEN atl.price END) AS open_p,
    MAX(CASE WHEN atl.action='close' THEN atl.price END) AS close_p,
    MAX(CASE WHEN atl.action='open' THEN atl.traded_at END) AS open_at,
    MAX(CASE WHEN atl.action='open' THEN atl.symbol END) AS sym,
    MAX(CASE WHEN atl.action='open' THEN
      json_extract(atl.trigger_detail, '\$.signal_id') END) AS sig_id
  FROM auto_trade_log atl
  WHERE atl.market='us' AND atl.traded_at > strftime('%s','now') - 30*86400
  GROUP BY atl.position_id HAVING close_p IS NOT NULL
),
ctx AS (
  SELECT c.*,
    (SELECT MAX(high) FROM [klines_us_1d] k WHERE k.symbol=c.sym
       AND k.timestamp BETWEEN (c.open_at-20*86400)*1000 AND c.open_at*1000) AS hi20,
    (SELECT open FROM [klines_us_1d] k WHERE k.symbol=c.sym
       AND k.timestamp/1000 <= c.open_at ORDER BY k.timestamp DESC LIMIT 1) AS day_open,
    (SELECT (close-open)/open*100 FROM [klines_us_1d] k WHERE k.symbol='SPY'
       AND k.timestamp/1000 <= c.open_at ORDER BY k.timestamp DESC LIMIT 1) AS spy_chg,
    s.ai_take_profit AS ai_tp, s.ai_stop_loss AS ai_sl
  FROM closed c LEFT JOIN signals s ON s.id = c.sig_id
)
SELECT
  CASE
    WHEN hi20 > 0 AND (hi20-open_p)/open_p < 0.03 THEN '门1_拒_追高 <3pct'
    WHEN day_open > 0 AND (open_p-day_open)/day_open > 0.04 THEN '门2_拒_当日已涨>4pct'
    WHEN ai_tp IS NOT NULL AND ai_sl IS NOT NULL
      AND (ai_tp - open_p) / NULLIF(open_p - ai_sl, 0) < 2.0 THEN '门3_拒_R:R<2.0'
    WHEN spy_chg IS NOT NULL AND spy_chg < -2.0 THEN '门5_拒_SPY<-2pct'
    ELSE '通过'
  END AS v2_决策,
  COUNT(*) AS 笔数,
  printf('%.0f', SUM(CASE WHEN close_p > open_p THEN 1 ELSE 0 END) * 100.0 / COUNT(*)) AS 胜率pct,
  printf('%.2f', AVG((close_p-open_p)/open_p*100)) AS 均PnLpct,
  printf('%.2f', SUM((close_p-open_p)/open_p*100)) AS 总PnLpct
FROM ctx WHERE open_p > 0
GROUP BY v2_决策 ORDER BY v2_决策;
"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Phase 0 回测完成. 关键指标解读:"
echo "  - 拒掉的笔数胜率/PnL 应该明显 < 通过的"
echo "  - 通过的'综合 PnL' 应该 > 当前实际 PnL"
echo "  - 如果通过的笔数 < 总数的 50pct, 说明门太严"
echo "  - 如果通过的笔数 > 总数的 80pct, 说明门太松"
echo "═══════════════════════════════════════════════════════════════"
