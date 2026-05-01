# 性能审计报告 · 2026-04-22

> 由两路 Agent 并行扫描得出。基准：DB=59MB / WAL=**187MB** / 进程内存=261MB / API 延迟 ≤15ms / 前端 JS 未打包总量 ~770KB。

## 总结

**总体健康状况：良好**。API 延迟非常低，代码架构合理。但有 **3 个必修项** 和 **~10 个明显优化机会**。修完"立即可做 + 部署前必做"两级，预计：
- SQLite WAL 从 187MB → <20MB
- API p99 延迟 下降 3-5×
- 首屏加载 从 ~2s → ~0.8s
- 后端内存压力 降低（避免 OOM）

---

## 🚨 P0 必修（影响稳定性 / 部署前一定要改）

### 后端

1. **SQLite WAL 配置缺失** — `backend/db/database.py:77-84`
   - 只设了 `journal_mode=WAL`，没配 `wal_autocheckpoint / synchronous / busy_timeout / cache_size`
   - 叠加 `pool_size=5` 长连接持有读锁，checkpoint 永远跑不满 → WAL 涨到 187MB
   - **修复**：每个 connect 加 PRAGMA 套餐 + `_data_retention_loop` 加 `PRAGMA wal_checkpoint(TRUNCATE)`

2. **候选池评分每只股票单独 commit** — `backend/watchpool/scorer.py:276-292`
   - 500 只股票 × 2 insert = 500 次 fsync，每小时跑一次
   - WAL 暴涨第二元凶
   - **修复**：外层一次 conn，按 50 只批次 commit

3. **连接池只有 5 但要分给 7 个后台循环 + HTTP + WS** — `backend/main.py:71`
   - 信号爆发时 `_evaluate_symbol` 一次要拿 7-8 次连接
   - **修复**：`pool_size=5` → `10`

### 前端

4. **15 个 JS 全同步阻塞首屏** — `frontend/index.html:642-657`
   - script 标签全部同步，浏览器串行 download→parse→execute
   - KLineChart 400KB + chart.js 76KB 都是阻塞加载
   - **修复**：业务 JS 全加 `defer`（顺序不变，但不阻塞 HTML parse）

5. **`console.log` 被全局 noop 覆盖** — `frontend/js/app.js:35-39`
   - 屏蔽了所有调试输出（第三方库也被打死）
   - 我们之前 BTC 日志漏失、LLM 失败这种严重 bug 都是因此难以察觉
   - **修复**：只屏蔽自家前缀的 log（留下 console.warn/error），或用环形 buffer

---

## ⚠️ P1 高价值优化（部署前必做，影响响应时间 / 资源）

### 后端 SQL

6. **`LIKE '%symbol%'` 前导通配符大范围存在** — 8 处全表扫 `flash_news`
   - 位置：`db/database.py:957`、`monitor.py:866/934/947/1005`、`ai_analyzer.py:1155/1361/1738`
   - 信号 verify / 持仓建议 / simplified_verify 都要扫
   - **修复**：新增 `news_symbol_index(news_id, symbol)` 关系表 + 触发器同步，JOIN 走索引；或 FTS5

7. **缺关键索引**（每个都是一条 SQL）
   ```sql
   CREATE INDEX idx_advices_pos ON position_advices(position_id, advised_at DESC);
   CREATE INDEX idx_auto_trade_pos ON auto_trade_log(position_id);
   CREATE INDEX idx_signals_verify ON signals(symbol, market, ai_verdict, generated_at DESC);
   CREATE INDEX idx_flash_collected ON flash_news(collected_at DESC);
   CREATE INDEX idx_pool_diag ON watch_pool(status, ai_diagnosed_at);
   ```

8. **`auto_trade_status` N+1** — `backend/main.py:2397-2435`
   - 每只持仓串行查 K 线 + COUNT(log)，20 只 = 40 次
   - **修复**：批量 IN 查 + GROUP BY 一次查 count

9. **`rescore_pool_items` 每只股票都 `cached_get_klines`** — `watchpool/scorer.py:259-263`
   - 500 次 DB 查询 + `_ensure_kline_table`
   - **修复**：一次 SELECT 所有候选近 60 根日 K，内存分组算

10. **`news_scheduler._fetch_once` 每条新闻独立查 simhash + 每个 category 独立查 watch_pool** — `backend/news/scheduler.py:206,229-249`
    - 30 条新闻 × 300 条 simhash = 9000 次冗余查
    - **修复**：批次入口查一次 simhash 列表，watch_pool 用 IN 批量

### 后端循环

11. **`_pool_fundamentals_refresh_loop` 串行 + sleep 0.3s** — `main.py:360-387`
    - 500 股 × 0.3s = 2.5 分钟一轮
    - **修复**：`asyncio.Semaphore(4) + gather`，CN/HK/US 三源分桶

12. **aiohttp session 每次新建** — `watchpool/anomaly_scanner.py`、`quality_filter.py`
    - DNS + TCP + TLS 每次重来
    - **修复**：模块级持久 session

13. **OpenAI 同步客户端 + `asyncio.to_thread`** — `news/ai_analyzer.py:561-568`
    - 每次调用独占一个 worker 线程，reasoner 120s 超时 = 4 线程挂 2 分钟
    - **修复**：改 `AsyncOpenAI`，免线程切换

14. **监控循环 60 秒一轮不分周期** — `main.py:946`
    - 1H/4H/1D 绑定每分钟白跑
    - **修复**：按 interval 分桶（15m→60s；1H→5min；1D→30min）

### 前端

15. **`_showAddSuccessModal` 拉 581KB 全池只为一个股票名** — `watchpool.js:316`
    - **修复**：用 `/api/symbols?q=` 或让 POST 响应直接返回 stock_name

16. **千行表格全量 innerHTML 重建** — signals.js、portfolio.js、watchpool.js
    - 500 行 × 每行 12 个 `<td>` = 每次 1200 个节点创建
    - **修复**：增量 DOM（只变化行更新），`_renderFills` 做虚拟滚动

17. **modal 没 AbortController** — watchpool.js、portfolio.js
    - 快速开关 modal 触发 N 个并行请求，回调写到 detached DOM
    - **修复**：每个 modal 一个 AbortController

18. **WS 队列 50 条全局上限会挤掉用户态事件** — `websocket.js:99-100`
    - flash_news 洪流冲掉 signal/advice
    - **修复**：按 type 分桶

19. **`setInterval(_updateAutoTradeStatus, 30000)` hidden 时也跑** — `portfolio.js:143`
    - **修复**：接入 `__visibilityHandlers`

### 内存

20. **`_build_tech_snapshot` verify 时重算** — `monitor.py:731-844`
    - **修复**：5 分钟 symbol+interval 缓存

21. **elliott draw 每帧重建 tsIndexMap** — `chart.js:442`
    - **修复**：`window._elliottData` 赋值时一次建好 Map

---

## P2 微优化（可以晚点做）

22. `_refresh_pool_symbols_cache` 用 `status="monitoring"` 实际恒空（main.py:845）
23. `_dedupe` dict 阈值 2000 全量重建，改 OrderedDict LRU（monitor.py:509）
24. `symbol_registry.refresh_from_db` 3 分钟 → 10 分钟（main.py:878）
25. `_data_retention_loop` 信号去重每 interval 独立 ROW_NUMBER（main.py:225）
26. Google Fonts 加 `display=swap`（index.html:14）
27. 部分 `querySelector` 热路径没缓存引用
28. inline `onmouseover="..."` 改 CSS `:hover`（watchpool.js:228）

---

## 三级执行清单

### ✅ 立即可做（30 分钟，零业务风险）

**只是加 PRAGMA + 索引 + config 值**
```sql
-- 建在 DatabaseManager.init 里
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA wal_autocheckpoint = 1000;
PRAGMA cache_size = -20000;   -- 20MB
PRAGMA temp_store = MEMORY;
```
```sql
-- 5 条索引（以后启动时 IF NOT EXISTS）
CREATE INDEX IF NOT EXISTS idx_advices_pos ON position_advices(position_id, advised_at DESC);
CREATE INDEX IF NOT EXISTS idx_auto_trade_pos ON auto_trade_log(position_id);
CREATE INDEX IF NOT EXISTS idx_signals_verify ON signals(symbol, market, ai_verdict, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_flash_collected ON flash_news(collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_pool_diag ON watch_pool(status, ai_diagnosed_at);
```
```python
# main.py:71
DatabaseManager(config.DB_PATH, pool_size=10)  # 5 → 10

# _data_retention_loop 末尾加
await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
```
```html
<!-- index.html: 15 个业务 JS 加 defer -->
<script src="js/toast.js?v=..." defer></script>
...
```
```js
// app.js:35 把 noop 改成只屏蔽自己
const __myLog = (...a) => !VERBOSE && a[0]?.startsWith?.('[Debug]') ? null : origLog(...a);
// 或者直接删掉这段屏蔽
```

**这一级改完 WAL 就会降下来，后端压力立减。**

### 🔧 部署前必做（半天到一天）

8. `rescore_pool_items` 合并事务
9. `auto_trade_status` 批量查询（消除 N+1）
10. `news_scheduler._fetch_once` 批量 simhash + watch_pool
11. `_pool_fundamentals_refresh_loop` 并发化
12. aiohttp session 模块级复用
13. 换 `AsyncOpenAI` 免 `to_thread`
14. 前端 modal 加 AbortController
15. `_showAddSuccessModal` 改用轻 API
16. WS 队列按 type 分桶

### 🎯 长期优化（可选，看负载增长）

17. flash_news 建 `news_symbol_index` 关联表（彻底消灭 LIKE）
18. 监控循环按 interval 分桶
19. 前端上打包工具（esbuild），虚拟滚动，增量 DOM
20. flash_news FTS5 全文搜索
21. 每天 `PRAGMA optimize`

---

## 部署上云服务器前的影响评估

在 **2C4G 60 元/月** 的腾讯云轻量（东京）上，如果**不做优化直接部署**：
- 现有 261MB 进程 + 187MB WAL + OS 开销 ≈ **1.3GB 占用**，4GB 还剩 2.7GB 缓冲，**能跑但有风险**
- 凌晨重评分时 WAL 还会涨，如果叠加 LLM 大响应缓冲，可能 swap

**强烈建议先把"立即可做"一级做完再部署**，能让：
- WAL 降到 < 20MB，进程稳定在 200-300MB
- API p99 延迟从几十 ms 降到几 ms
- 服务器 4GB 内存用得舒舒服服，**2GB 版本也能跑**（可省 20 元/月）

---

*本报告由 OpenChart Pro 代码库自动扫描生成 2026-04-22，文件路径均为 `d:\OpenChart Pro\`。*
