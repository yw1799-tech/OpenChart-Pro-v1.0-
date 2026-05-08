-- ════════════════════════════════════════════════════════════════════════════
-- 诊断: 合约很久没开仓 + 信号池无触发信号
-- ════════════════════════════════════════════════════════════════════════════
-- 用法: sqlite3 data/openchart.db < scripts/diag_swap_no_trade.sql
-- 期望: 找出链路断点
--   信号生成 → AI 验证 → 自动开仓 → 持仓
--    ?              ?           ?          ?
-- ════════════════════════════════════════════════════════════════════════════

.mode column
.headers on
.width 25 12 12 12

.print
.print ════════════════════════════════════════════════════════════════
.print  当前时间
.print ════════════════════════════════════════════════════════════════
SELECT datetime('now') AS '当前 UTC',
       datetime('now', '+8 hours') AS '当前北京';

-- ════════════════════════════════════════════════════════════════════════════
-- ① 加密信号生成: 24h 内有多少, 各状态分布
-- ════════════════════════════════════════════════════════════════════════════
.print
.print ─────────────────────────────────────────────────────────────────
.print  ① 加密 (crypto) 信号生成 — 24h
.print ─────────────────────────────────────────────────────────────────
.print
.print  1.1 24h 加密信号 ai_verdict 分布:
SELECT
    COALESCE(NULLIF(ai_verdict, ''), '(空 / 验证中)') AS 'AI 判定',
    COUNT(*) AS '笔数',
    printf('%.1f', AVG(confidence)) AS '平均系统置信',
    MIN(datetime(generated_at/1000, 'unixepoch', '+8 hours')) AS '最早北京',
    MAX(datetime(generated_at/1000, 'unixepoch', '+8 hours')) AS '最晚北京'
FROM signals
WHERE market = 'crypto'
  AND generated_at >= (strftime('%s','now') - 86400) * 1000
GROUP BY 1
ORDER BY COUNT(*) DESC;

.print
.print  1.2 加密最近 24h 各周期信号生成量:
SELECT
    interval AS '周期',
    COUNT(*) AS '笔数',
    SUM(CASE WHEN ai_verdict='confirm' THEN 1 ELSE 0 END) AS 'confirm',
    SUM(CASE WHEN ai_verdict='reject' THEN 1 ELSE 0 END) AS 'reject',
    SUM(CASE WHEN ai_verdict='warn' THEN 1 ELSE 0 END) AS 'warn'
FROM signals
WHERE market = 'crypto'
  AND generated_at >= (strftime('%s','now') - 86400) * 1000
GROUP BY interval;

.print
.print  1.3 ⚠️  最后一条加密信号生成时间 (距今多久):
SELECT
    datetime(MAX(generated_at)/1000, 'unixepoch', '+8 hours') AS '最后信号北京',
    printf('%.1f', (strftime('%s','now')*1000 - MAX(generated_at))/3600000.0) AS '距今 (h)'
FROM signals
WHERE market = 'crypto';

-- ════════════════════════════════════════════════════════════════════════════
-- ② 加密信号触发开仓: 24h 内 swap 自动交易日志
-- ════════════════════════════════════════════════════════════════════════════
.print
.print ─────────────────────────────────────────────────────────────────
.print  ② 加密 swap 自动交易日志 — 24h
.print ─────────────────────────────────────────────────────────────────
.print
.print  2.1 24h 各 status 分布 (executed=成功开仓, rejected=被拒, pending=挂单):
SELECT
    status AS '状态',
    COUNT(*) AS '笔数',
    GROUP_CONCAT(DISTINCT action) AS '动作'
FROM auto_trade_log
WHERE market = 'crypto'
  AND traded_at >= strftime('%s','now') - 86400
GROUP BY status
ORDER BY COUNT(*) DESC;

.print
.print  2.2 24h 加密拒单 reason 分类 (找出最常见拒因):
SELECT
    SUBSTR(rejected_reason, 1, 40) AS 'reason 前 40 字',
    COUNT(*) AS '次数'
FROM auto_trade_log
WHERE market = 'crypto'
  AND status = 'rejected'
  AND traded_at >= strftime('%s','now') - 86400
GROUP BY 1
ORDER BY COUNT(*) DESC
LIMIT 10;

.print
.print  2.3 ⚠️  最后一笔 加密 executed 开仓 (距今多久):
SELECT
    datetime(MAX(traded_at), 'unixepoch', '+8 hours') AS '最后开仓北京',
    printf('%.1f', (strftime('%s','now') - MAX(traded_at))/3600.0) AS '距今 (h)'
FROM auto_trade_log
WHERE market = 'crypto'
  AND status = 'executed'
  AND action IN ('open', 'add');

-- ════════════════════════════════════════════════════════════════════════════
-- ③ swap_orders 状态 (合约引擎下单是否在跑)
-- ════════════════════════════════════════════════════════════════════════════
.print
.print ─────────────────────────────────────────────────────────────────
.print  ③ swap_orders (合约下单引擎) — 24h
.print ─────────────────────────────────────────────────────────────────
.print
.print  3.1 24h swap_orders 各 status 分布:
SELECT
    status AS '状态',
    COUNT(*) AS '笔数',
    GROUP_CONCAT(DISTINCT intent) AS '意图'
FROM swap_orders
WHERE created_at >= strftime('%s','now') - 86400
GROUP BY status
ORDER BY COUNT(*) DESC;

.print
.print  3.2 ⚠️  最后一笔 swap_order 时间:
SELECT
    datetime(MAX(created_at), 'unixepoch', '+8 hours') AS '最后下单北京',
    printf('%.1f', (strftime('%s','now') - MAX(created_at))/3600.0) AS '距今 (h)'
FROM swap_orders;

.print
.print  3.3 当前持仓 (open):
SELECT
    symbol,
    pos_side AS '方向',
    leverage AS '杠杆',
    qty AS '张数',
    printf('%.4f', avg_open_price) AS '开仓价',
    datetime(opened_at, 'unixepoch', '+8 hours') AS '开仓时间北京',
    printf('%.2f', unrealized_pnl_usd) AS '浮 PnL'
FROM swap_positions
WHERE status = 'open'
ORDER BY opened_at DESC
LIMIT 10;

.print
.print  3.4 swap 账户余额 (字段名按各版本兼容):
SELECT * FROM swap_account LIMIT 1;

-- ════════════════════════════════════════════════════════════════════════════
-- ④ MonitorEngine + AutoTrader 状态 (有没有信号在生成中)
-- ════════════════════════════════════════════════════════════════════════════
.print
.print ─────────────────────────────────────────────────────────────────
.print  ④ MonitorEngine 是否在运行 (各市场 6h 内信号生成量)
.print ─────────────────────────────────────────────────────────────────
SELECT
    market,
    COUNT(*) AS '6h 信号数',
    datetime(MAX(generated_at)/1000, 'unixepoch', '+8 hours') AS '最后信号北京'
FROM signals
WHERE generated_at >= (strftime('%s','now') - 6*3600) * 1000
GROUP BY market
ORDER BY market;

.print
.print  4.1 全部市场 6h 内 confirm 信号 (= 应该开仓的):
SELECT
    market,
    symbol,
    interval AS '周期',
    action AS '方向',
    confidence AS '系统',
    ai_confidence AS 'AI',
    datetime(generated_at/1000, 'unixepoch', '+8 hours') AS '生成北京',
    SUBSTR(ai_reason, 1, 40) AS 'AI 理由前 40 字'
FROM signals
WHERE ai_verdict = 'confirm'
  AND generated_at >= (strftime('%s','now') - 6*3600) * 1000
ORDER BY generated_at DESC
LIMIT 20;

.print
.print  4.2 ⚠️  confirm 但未触发开仓的信号 (链路断点):
SELECT
    s.market,
    s.symbol,
    s.action,
    s.ai_confidence AS 'AI',
    datetime(s.generated_at/1000, 'unixepoch', '+8 hours') AS '生成',
    CASE
        WHEN EXISTS(SELECT 1 FROM auto_trade_log l
                    WHERE l.symbol=s.symbol AND l.market=s.market
                    AND l.traded_at >= s.generated_at/1000)
        THEN '✓ 有交易日志'
        ELSE '✗ 无交易日志 (没尝试开仓?)'
    END AS '后续情况'
FROM signals s
WHERE s.ai_verdict = 'confirm'
  AND s.market = 'crypto'
  AND s.generated_at >= (strftime('%s','now') - 6*3600) * 1000
  AND s.status = 'active'
ORDER BY s.generated_at DESC
LIMIT 20;

-- ════════════════════════════════════════════════════════════════════════════
-- ⑤ 自动交易账户 (enabled 状态需 curl /api/auto-trade/status 查看)
-- ════════════════════════════════════════════════════════════════════════════
.print
.print ─────────────────────────────────────────────────────────────────
.print  ⑤ 账户基本 (auto_trade_enabled 不在 DB, 是内存状态)
.print ─────────────────────────────────────────────────────────────────
SELECT
    initial_capital_usd AS '初始资金 USD',
    cash_usd AS '当前现金 USD',
    datetime(updated_at, 'unixepoch', '+8 hours') AS '上次更新北京'
FROM auto_trade_account
LIMIT 1;

.print
.print  ℹ️ 自动交易开关 (enabled) 需另跑:
.print     curl http://localhost:8000/api/auto-trade/status | python3 -c "import json,sys;d=json.load(sys.stdin);print('enabled:', d.get('enabled'))"

.print
.print ═════════════════════════════════════════════════════════════════
.print  诊断完成 — 请把全部输出发回开发者
.print ═════════════════════════════════════════════════════════════════
