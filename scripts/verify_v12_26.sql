-- ════════════════════════════════════════════════════════════════════════════
-- v12.26.x 24h 验证 SQL
-- ════════════════════════════════════════════════════════════════════════════
-- 用途: 部署 v12.26 系列 24h 后验证 3 项核心改造是否符合设计
--   1. 信号瞬时哲学 — signal.status 6h 后应自动 → expired, 不应"复活"
--   2. LLM 30 并发 — 峰值 token 消耗 (验证并发能力是否生效)
--   3. 闭市 deferred — 港美股收盘后, 触发的 close/reduce 应进 status='pending',
--      而非 status='rejected' (旧版闭市 hard reject 把信号丢掉)
--
-- 使用方法 (在仓库根目录执行):
--   sqlite3 data/openchart.db < scripts/verify_v12_26.sql
--
-- 时间窗: 默认查最近 24h, 可在每段开头改 strftime 偏移
-- ════════════════════════════════════════════════════════════════════════════

.mode column
.headers on
.echo off
.width 30 12 12 12 12 30

-- 让数字读起来好看
.print
.print ════════════════════════════════════════════════════════════════
.print  v12.26.x 24h 验证报告
.print  生成时间: 见下方 SELECT, 时区 = SQLite 默认 (UTC, 减 8 小时是北京)
.print ════════════════════════════════════════════════════════════════

SELECT datetime('now') AS '当前 UTC',
       datetime('now', '+8 hours') AS '当前北京';

-- ════════════════════════════════════════════════════════════════════════════
-- 检查 1: 信号瞬时哲学 (signals.status 生命周期)
-- ════════════════════════════════════════════════════════════════════════════
-- 设计: 信号生成 6h 后, 若仍 status='active' 且未被 act/expire, 应被自动
--       expire 守护进程置为 'expired'. 24h 内不应有 'active' 但 generated_at
--       已超过 6h 的信号.
.print
.print ─────────────────────────────────────────────────────────────────
.print  检查 1: 信号瞬时哲学
.print ─────────────────────────────────────────────────────────────────

-- 1.1 24h 内各 status 信号数 (按预期应该 active 占比小, expired+acted 占比大)
.print  1.1 最近 24h 信号 status 分布:
SELECT
    status AS '状态',
    COUNT(*) AS '笔数',
    printf('%.1f', AVG((strftime('%s','now') - generated_at) / 3600.0)) AS '平均年龄(h)',
    MAX((strftime('%s','now') - generated_at) / 3600) AS '最老(h)'
FROM signals
WHERE generated_at >= strftime('%s','now') - 86400
GROUP BY status
ORDER BY COUNT(*) DESC;

-- 1.2 异常: active 但年龄 >6h (应被 expired 守护进程置为 expired)
.print
.print  1.2 ⚠️ 异常 — active 但年龄 >6h (期望 0 笔, 多则 expired 守护未生效):
SELECT
    id AS 'sig_id',
    symbol,
    market,
    action,
    strategy_name AS '策略',
    printf('%.1f', (strftime('%s','now') - generated_at) / 3600.0) AS '年龄(h)',
    ai_verdict AS 'AI判定'
FROM signals
WHERE status = 'active'
  AND generated_at < strftime('%s','now') - 6 * 3600
ORDER BY generated_at ASC
LIMIT 10;

-- 1.3 验证: expired 信号是否在 ~6h 处集中过期 (验证守护进程节奏)
.print
.print  1.3 expired 信号年龄分布 (期望: 大部分集中在 6-12h):
SELECT
    CASE
        WHEN (strftime('%s','now') - generated_at) / 3600 < 6 THEN '0-6h'
        WHEN (strftime('%s','now') - generated_at) / 3600 < 12 THEN '6-12h (期望集中)'
        WHEN (strftime('%s','now') - generated_at) / 3600 < 24 THEN '12-24h'
        ELSE '>24h'
    END AS '年龄段',
    COUNT(*) AS '笔数'
FROM signals
WHERE status = 'expired'
  AND generated_at >= strftime('%s','now') - 86400
GROUP BY 1
ORDER BY MIN((strftime('%s','now') - generated_at) / 3600);

-- ════════════════════════════════════════════════════════════════════════════
-- 检查 2: LLM 30 并发 (峰值 token / qps)
-- ════════════════════════════════════════════════════════════════════════════
.print
.print ─────────────────────────────────────────────────────────────────
.print  检查 2: LLM 30 并发能力
.print ─────────────────────────────────────────────────────────────────

-- 2.1 24h 各 path 调用次数 + 峰值 (验证不同路径都在跑)
.print
.print  2.1 24h 各 path 调用次数 + 总成本:
SELECT
    COALESCE(path, '(未标注)') AS 'path',
    COUNT(*) AS '次数',
    printf('$%.4f', SUM(cost_usd)) AS '总成本',
    printf('%.0f', AVG(input_tokens + output_tokens)) AS '平均 tokens',
    printf('%.0f', SUM(input_tokens + output_tokens)) AS '总 tokens'
FROM llm_cost_log
WHERE called_at >= strftime('%s','now') - 86400
GROUP BY 1
ORDER BY COUNT(*) DESC;

-- 2.2 峰值 1 分钟 LLM 调用数 (验证 30 并发上限是否触达)
-- 期望: 至少有 1 个分钟段达到 20+ 调用 (说明并发跑起来了)
.print
.print  2.2 峰值分钟 (按分钟桶 24h Top 10):
SELECT
    datetime(called_at - (called_at % 60), 'unixepoch', '+8 hours') AS '北京时间(分钟)',
    COUNT(*) AS '该分钟调用数',
    printf('$%.4f', SUM(cost_usd)) AS '该分钟成本'
FROM llm_cost_log
WHERE called_at >= strftime('%s','now') - 86400
GROUP BY called_at - (called_at % 60)
ORDER BY COUNT(*) DESC
LIMIT 10;

-- 2.3 24h 总成本 (验证单日预算合理)
.print
.print  2.3 24h 总览:
SELECT
    COUNT(*) AS '总调用',
    printf('$%.2f', SUM(cost_usd)) AS '总成本',
    printf('$%.4f', AVG(cost_usd)) AS '平均/笔'
FROM llm_cost_log
WHERE called_at >= strftime('%s','now') - 86400;

-- ════════════════════════════════════════════════════════════════════════════
-- 检查 3: 闭市 deferred (港美股收盘 → status='pending' 不 hard reject)
-- ════════════════════════════════════════════════════════════════════════════
.print
.print ─────────────────────────────────────────────────────────────────
.print  检查 3: 闭市 deferred
.print ─────────────────────────────────────────────────────────────────

-- 3.1 24h 各市场各 status 分布 (期望: pending 在 us/hk 闭市时段非 0)
.print
.print  3.1 24h 各市场 status 分布:
SELECT
    market,
    status,
    COUNT(*) AS '笔数'
FROM auto_trade_log
WHERE traded_at >= strftime('%s','now') - 86400
GROUP BY market, status
ORDER BY market, COUNT(*) DESC;

-- 3.2 闭市 pending 抽样 (期望: 这些条目 reason 含 '连续竞价/pending/闭市/盘后')
.print
.print  3.2 24h pending 状态抽样 (前 10 条):
SELECT
    datetime(traded_at, 'unixepoch', '+8 hours') AS '北京时间',
    symbol,
    market,
    action,
    SUBSTR(rejected_reason, 1, 60) AS 'reason 前 60 字'
FROM auto_trade_log
WHERE traded_at >= strftime('%s','now') - 86400
  AND status = 'pending'
ORDER BY traded_at DESC
LIMIT 10;

-- 3.3 异常: rejected 含闭市关键词 (期望 0 — 应该全部 → pending)
.print
.print  3.3 ⚠️ 异常 — rejected 含闭市关键词 (期望 0 笔):
SELECT
    datetime(traded_at, 'unixepoch', '+8 hours') AS '北京时间',
    symbol,
    market,
    action,
    SUBSTR(rejected_reason, 1, 80) AS 'reason'
FROM auto_trade_log
WHERE traded_at >= strftime('%s','now') - 86400
  AND status = 'rejected'
  AND (rejected_reason LIKE '%连续竞价%'
    OR rejected_reason LIKE '%闭市%'
    OR rejected_reason LIKE '%盘后%'
    OR rejected_reason LIKE '%market closed%')
ORDER BY traded_at DESC
LIMIT 10;

-- 3.4 deferred 转化率: 24h 港美股 pending 中后续是否成功 executed
.print
.print  3.4 24h pending 后续转化情况 (按 position_id 看):
SELECT
    market,
    COUNT(DISTINCT CASE WHEN status='pending' THEN position_id END) AS 'pending 笔数',
    COUNT(DISTINCT CASE WHEN status='executed' THEN position_id END) AS '后续 executed',
    COUNT(DISTINCT position_id) AS '总持仓影响数'
FROM auto_trade_log
WHERE traded_at >= strftime('%s','now') - 86400
  AND market IN ('us', 'hk')
  AND position_id IS NOT NULL
GROUP BY market;

-- ════════════════════════════════════════════════════════════════════════════
-- 总结打分
-- ════════════════════════════════════════════════════════════════════════════
.print
.print ═════════════════════════════════════════════════════════════════
.print  总结 (期望全部 ✓; 任何 ✗ 都需要看上面对应段细节)
.print ═════════════════════════════════════════════════════════════════

SELECT
    CASE WHEN (
        SELECT COUNT(*) FROM signals
        WHERE status='active' AND generated_at < strftime('%s','now') - 6*3600
    ) = 0 THEN '✓ 通过' ELSE '✗ 失败' END AS '检查 1: 信号瞬时哲学',
    CASE WHEN (
        SELECT MAX(c) FROM (
            SELECT COUNT(*) AS c FROM llm_cost_log
            WHERE called_at >= strftime('%s','now') - 86400
            GROUP BY called_at - (called_at % 60)
        )
    ) >= 10 THEN '✓ 通过 (>=10/min)' ELSE '⚠ 弱 (峰值 <10/min, 可能并发未触达)' END AS '检查 2: LLM 并发',
    CASE WHEN (
        SELECT COUNT(*) FROM auto_trade_log
        WHERE traded_at >= strftime('%s','now') - 86400
          AND status='rejected'
          AND (rejected_reason LIKE '%连续竞价%' OR rejected_reason LIKE '%闭市%' OR rejected_reason LIKE '%盘后%')
    ) = 0 THEN '✓ 通过' ELSE '✗ 失败' END AS '检查 3: 闭市 deferred';

.print
.print ═════════════════════════════════════════════════════════════════
.print  报告结束 — 任何 ✗ 或 ⚠ 请发给开发者排查
.print ═════════════════════════════════════════════════════════════════
