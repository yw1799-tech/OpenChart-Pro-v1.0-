/* ============================================================
   Signals 模块 — 策略信号面板 (Phase 4)

   功能：
   - 列表展示策略信号（按生成时间倒序）
   - WebSocket 实时推送信号 + Toast + 声音
   - 点击信号跳转主图并标记触发点
   - 显示策略绑定情况
   ============================================================ */

const Signals = (function () {
  let _items = [];
  let _filterMarket = 'all';
  let _filterVerdict = 'all';   // all | confirm | warn | reject | none
  let _autoRefreshTimer = null;
  let _renderPending = false;
  let _lastToastAt = 0;
  let _sharedAudio = null;

  const ACTION_COLOR = { buy: 'var(--color-up)', sell: 'var(--color-down)' };
  const ACTION_ICON = { buy: '🟢 买入', sell: '🔴 卖出' };
  const STRATEGY_LABEL = {
    // 通用型
    ma_cross: '均线金叉死叉',
    donchian_breakout: '唐奇安通道突破',
    bollinger_reversion: '布林带均值回归',
    rsi_divergence: 'RSI 背离',  // 已废弃但保留兼容
    volume_breakout: '成交量突破',
    flash_event: '新闻事件驱动',
    chanlun: '缠论买卖点',
    macd_cross: 'MACD 金叉死叉',
    ema_triple: 'EMA 三线排列',
    squeeze_breakout: '布林挤压突破',
    adx_trend_follow: 'ADX 趋势跟随',
    // RSI 组合系列 (v12.16.5)
    rsi_pullback: 'RSI 趋势回踩',
    rsi_real_divergence: 'RSI 真背离',
    rsi_breakout_50: 'RSI 50 上穿',
    // v12.17.0 通用型
    volume_price_divergence: '量价背离',
    triple_screen: '三重过滤',
    // 共振合并
    resonance: '🌟 多策略共振',
    // 加密专属
    funding_extreme: '资金费率极值反转',
    oi_breakout: 'OI 持仓突破',
    long_short_ratio: '多空比反转',
    fear_greed_reversal: 'F&G 极值反转',
    // v12.17.0 加密新增
    whale_activity: '链上巨鲸大单',
    stablecoin_flow: '稳定币流入',
    // A 股专属
    limit_up_followup: '涨停后回踩',
    northbound_flow_top: '北向资金排名',
    sector_momentum: '板块联动',
    // v12.17.0 A 股新增
    lhb_follow: '龙虎榜跟盘',
    margin_breakout: '融资余额突破',
    // 港股专属
    southbound_inflow: '港股通南向',
    ah_spread_revert: 'AH 价差回归',
    // 美股专属
    gap_up_continuation: '高开延续',
    vwap_pullback: 'VWAP 回踩',
    // v12.17.0 美股新增
    premarket_breakout: '盘前/开盘突破',
    vix_extreme: 'VIX 极值反转',
    relative_strength_top: '相对大盘强势',
  };
  const MARKET_LABEL = { crypto: '加密', us: '美股', hk: '港股', cn: 'A股' };
  const AI_VERDICT_LABEL = {
    confirm:   { label: '🤖 AI 确认', color: 'var(--color-up)' },
    warn:      { label: '🤖 AI 警告', color: 'var(--color-warning)' },
    reject:    { label: '🤖 AI 否决', color: 'var(--color-down)' },
    stale:     { label: '⌛ 已过期', color: 'var(--text-tertiary)' },
    llm_error: { label: '⛔ LLM 失败', color: 'var(--color-down)' },
    skipped:   { label: '⊘ 无需验证', color: 'var(--text-tertiary)' },
    deferred:  { label: '🌙 闭市待验', color: '#bc8cff' },  // v12.26.5: 闭市股票信号, 等开市批量验证
  };

  // 快速通道源标签（不走 LLM verify）→ 显示中文徽章
  const FAST_PATH_LABEL = {
    '强买直通':       { label: '⚡ 强买直通', color: 'var(--color-up)' },
    '买入·无利空':    { label: '✓ 买入·无利空', color: 'var(--color-up)' },
    '买入·遇利空':    { label: '✗ 买入·遇利空', color: 'var(--color-down)' },
  };

  // rAF 合并：多条信号在同一帧内只触发一次 DOM 重建
  function _scheduleRender() {
    if (_renderPending) return;
    _renderPending = true;
    requestAnimationFrame(() => {
      _renderPending = false;
      render();
    });
  }

  let _pendingSignals = [];
  let _signalFlushTimer = null;

  let _inited = false;
  function init() {
    if (_inited) { console.warn('[Signals] 已初始化，跳过重复 init'); return; }
    _inited = true;
    _ensureDom();
    if (typeof ws !== 'undefined' && ws) {
      ws.on('signal', (msg) => {
        if (!msg || !msg.data) return;
        const d = msg.data;
        // AI 验证更新（不创建新行，更新已有行的 AI 字段）
        if (d._ai_update) {
          // 同时搜索 _items 和 _pendingSignals：如果信号刚到（在 500ms 批次窗口内），
          // AI 更新可能比 flush 早到，只搜 _items 会漏掉
          const target = _items.find(x => x.id === d.id)
                      || _pendingSignals.find(x => x.id === d.id);
          if (target) {
            target.ai_verdict = d.ai_verdict;
            target.ai_confidence = d.ai_confidence;
            target.ai_reason = d.ai_reason;
            target.ai_stop_loss = d.ai_stop_loss;
            target.ai_take_profit = d.ai_take_profit;
            _scheduleRender();
          }
          return;
        }
        // 简单去重：同 symbol+action+strategy+interval+生成时间相近的视为重复
        const dupe = _items.find(
          (x) => x.symbol === d.symbol && x.action === d.action && x.strategy_name === d.strategy_name
                 && (x.interval || '1H') === (d.interval || '1H')
                 && Math.abs((x.generated_at || 0) - (d.generated_at || 0)) < 60000
        );
        if (dupe) return;
        // 硬节流：500ms 收集所有信号，统一处理一次
        _pendingSignals.push(d);
        if (_signalFlushTimer) return;
        _signalFlushTimer = setTimeout(() => {
          _signalFlushTimer = null;
          if (_pendingSignals.length === 0) return;
          _items.unshift(..._pendingSignals);
          if (_items.length > 100) _items.length = 100;
          const flushedCount = _pendingSignals.length;
          _pendingSignals = [];
          _scheduleRender();
          // 多条信号汇总成一个 toast，避免弹窗风暴
          const now = Date.now();
          if (flushedCount > 0 && now - _lastToastAt > 3000 && typeof showToast === 'function') {
            _lastToastAt = now;
            const summary = flushedCount === 1
              ? `📡 ${ACTION_ICON[d.action]||d.action} ${d.symbol} ${d.interval||'1H'} (${d.confidence}%)`
              : `📡 ${flushedCount} 条新信号`;
            showToast(summary, 'info', 3000);
          }
        }, 500);
      });
    }
    refresh();
    _autoRefreshTimer = setInterval(refresh, 120000);  // 2 分钟一次（原 30 秒太频繁）
    if (window.__visibilityHandlers) {
      window.__visibilityHandlers.push(({ hidden }) => {
        if (hidden && _autoRefreshTimer) { clearInterval(_autoRefreshTimer); _autoRefreshTimer = null; }
        else if (!hidden && !_autoRefreshTimer) { refresh(); _autoRefreshTimer = setInterval(refresh, 120000); }
      });
    }
    console.log('[Signals] 已初始化');
  }

  function _ensureDom() {
    const pane = document.querySelector('.bottom-pane[data-pane="signals"]');
    if (!pane || pane.dataset.inited) return;
    pane.dataset.inited = '1';
    pane.innerHTML = `
      <div class="oc-toolbar signals-toolbar">
        <span class="oc-text-lg" style="font-weight:600;">📡 策略信号</span>
        <select id="signals-filter-market" class="select oc-text-sm" style="width:96px;">
          <option value="all">全部市场</option>
          <option value="crypto">加密</option>
          <option value="us">美股</option>
          <option value="hk">港股</option>
          <option value="cn">A股</option>
        </select>
        <div id="signals-verdict-chips" style="display:flex;gap:5px;flex-wrap:wrap;">
          <button class="oc-pill active signals-chip" data-verdict="all">全部</button>
          <button class="oc-pill signals-chip" data-verdict="confirm" style="border-color:var(--color-up);color:var(--color-up);">✅ 确认 <span class="oc-pill-count" data-count="confirm">0</span></button>
          <button class="oc-pill signals-chip" data-verdict="warn" style="border-color:var(--color-warning);color:var(--color-warning);">⚠️ 警告 <span class="oc-pill-count" data-count="warn">0</span></button>
          <button class="oc-pill signals-chip" data-verdict="llm_error" style="border-color:var(--color-down);color:var(--color-down);" title="LLM 调用失败（API Key 余额不足 / 网络 / 限流）。充值后这些信号会被自动重新验证">⛔ LLM失败 <span class="oc-pill-count" data-count="llm_error">0</span></button>
          <button class="oc-pill signals-chip" data-verdict="reject" style="border-color:var(--color-down);color:var(--color-down);">❌ 否决 <span class="oc-pill-count" data-count="reject">0</span></button>
          <button class="oc-pill signals-chip" data-verdict="none" title="还在排队等 LLM 验证（短暂状态，几秒-几分钟）">⏳ 验证中 <span class="oc-pill-count" data-count="none">0</span></button>
          <button class="oc-pill signals-chip" data-verdict="deferred" style="border-color:#bc8cff;color:#bc8cff;" title="v12.26.5: 闭市时段股票信号, 等开市后批量 LLM 验证">🌙 闭市待验 <span class="oc-pill-count" data-count="deferred">0</span></button>
          <button class="oc-pill signals-chip" data-verdict="skipped" title="无需 LLM 验证（如：股票 SELL 无持仓 / 信号置信度不足阈值）">⊘ 无需验证 <span class="oc-pill-count" data-count="skipped">0</span></button>
          <button class="oc-pill signals-chip" data-verdict="stale">⌛ 已过期 <span class="oc-pill-count" data-count="stale">0</span></button>
        </div>
        <span class="oc-toolbar-spacer"></span>
        <button id="signals-refresh-btn" class="btn btn-sm" title="刷新">🔄</button>
        <span id="signals-status" class="oc-text-sm oc-muted" style="width:100%;text-align:right;margin-top:2px;"></span>
      </div>
      <div class="signals-list" style="overflow-y:auto;flex:1;min-height:0;"></div>
    `;
    pane.querySelector('#signals-filter-market').addEventListener('change', (e) => {
      _filterMarket = e.target.value;
      render();
    });
    pane.querySelector('#signals-refresh-btn').addEventListener('click', refresh);
    // 验证 chip 委托点击
    pane.querySelector('#signals-verdict-chips').addEventListener('click', (e) => {
      const chip = e.target.closest('.signals-chip');
      if (!chip) return;
      _filterVerdict = chip.dataset.verdict;
      pane.querySelectorAll('.signals-chip').forEach(c => {
        c.classList.toggle('active', c.dataset.verdict === _filterVerdict);
      });
      render();
    });
  }

  async function refresh() {
    try {
      const resp = await fetch('/api/signals?limit=100');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = await resp.json();
      _items = d.items || [];
      render();
      const statusEl = document.querySelector('.bottom-pane[data-pane="signals"] #signals-status');
      if (statusEl) statusEl.textContent = `共 ${_items.length} 个 · ${new Date().toLocaleTimeString()}`;
    } catch (e) {
      console.warn('[Signals] 刷新失败:', e);
    }
  }

  function render() {
    const listEl = document.querySelector('.bottom-pane[data-pane="signals"] .signals-list');
    if (!listEl) return;
    // 1) 市场过滤
    let visible = _items;
    if (_filterMarket !== 'all') visible = visible.filter((s) => s.market === _filterMarket);
    // 2) 更新 chip 计数（基于市场过滤后的集合）
    // v12.26.5: deferred = 闭市时段股票信号 (开市后批量验证), 独立 chip
    const counts = { confirm: 0, warn: 0, reject: 0, none: 0, stale: 0, llm_error: 0, skipped: 0, deferred: 0 };
    const KNOWN_VERDICTS = ['confirm', 'warn', 'reject', 'stale', 'llm_error', 'skipped', 'deferred'];
    for (const s of visible) {
      const v = s.ai_verdict;
      if (KNOWN_VERDICTS.includes(v)) counts[v]++;
      else counts.none++;  // null / undefined / '' / 真未知值 → 排队中
    }
    const pane = document.querySelector('.bottom-pane[data-pane="signals"]');
    if (pane) {
      for (const k of ['confirm', 'warn', 'reject', 'none', 'stale', 'llm_error', 'skipped', 'deferred']) {
        const el = pane.querySelector(`[data-count="${k}"]`);
        if (el) el.textContent = counts[k];
      }
    }
    // 3) verdict 过滤
    if (_filterVerdict !== 'all') {
      visible = visible.filter((s) => {
        if (_filterVerdict === 'none') return !s.ai_verdict;
        return s.ai_verdict === _filterVerdict;
      });
    }
    // 4) 排序：confirm > warn > 验证中 ≈ deferred > reject > skipped > stale；同级按时间倒序
    // v12.26.5: deferred (闭市待验) 与 验证中同优先级
    const verdictWeight = { confirm: 3, warn: 2, '': 1, undefined: 1, null: 1, deferred: 1, reject: 0, skipped: -0.5, stale: -1, llm_error: 0 };
    const getWeight = (v) => {
      if (v === 'stale') return -1;
      if (v === 'skipped') return -0.5;
      if (v in verdictWeight) return verdictWeight[v];
      return 1;  // 未知值当"验证中"处理
    };
    visible = [...visible].sort((a, b) => {
      const wa = getWeight(a.ai_verdict);
      const wb = getWeight(b.ai_verdict);
      if (wa !== wb) return wb - wa;
      return (b.generated_at || 0) - (a.generated_at || 0);
    });
    if (!visible.length) {
      listEl.innerHTML = `
        <div class="oc-empty">
          <div class="oc-empty-icon">📡</div>
          <div class="oc-empty-title">暂无信号</div>
          <div class="oc-empty-hint">置信度 ≥ 75 才触发 · 加密 1H/4H/1D 多周期 + 股票候选池高分股 1D</div>
        </div>`;
      return;
    }
    listEl.innerHTML = `
      <table class="oc-table oc-table-compact">
        <thead>
          <tr>
            <th>时间</th>
            <th>品种</th>
            <th>周期</th>
            <th>操作</th>
            <th class="oc-col-num">价格</th>
            <th class="oc-col-num" title="基础分+技术加分-新闻减分, clamp 0-100">置信度</th>
            <th>策略</th>
            <th>理由</th>
            <th title="高置信度信号(≥70)会自动调 LLM 二次验证">🤖 AI 验证</th>
            <th class="oc-col-num">止损/止盈</th>
            <th class="oc-col-center">操作</th>
          </tr>
        </thead>
        <tbody>${visible.map(_renderRow).join('')}</tbody>
      </table>
    `;
    // 事件委托：在 listEl 上绑一次，通过 closest 找按钮 → 杜绝重复 addEventListener 导致的 detached DOM 累积
    if (!listEl._delegated) {
      listEl._delegated = true;
      listEl.addEventListener('click', (e) => {
        const viewBtn = e.target.closest('[data-action="view"]');
        if (viewBtn) {
          const sym = viewBtn.dataset.symbol;
          const mkt = viewBtn.dataset.market;
          if (typeof switchMarket === 'function' && mkt !== window.currentMarket) {
            switchMarket(mkt);
            setTimeout(() => switchSymbol && switchSymbol(sym, mkt), 200);
          } else if (typeof switchSymbol === 'function') {
            switchSymbol(sym, mkt);
          }
          return;
        }
        const detailBtn = e.target.closest('[data-action="detail"]');
        if (detailBtn) {
          const id = detailBtn.dataset.id;
          const sig = _items.find(s => s.id === id);
          if (sig) _showDetailModal(sig);
        }
      });
    }
  }

  function _showDetailModal(s) {
    let overlay = document.getElementById('signal-detail-modal');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'signal-detail-modal';
      overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
      overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
      document.body.appendChild(overlay);
    } else {
      overlay.innerHTML = '';
      overlay.style.display = 'flex';
    }
    const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const verdictBadge = s.ai_verdict
      ? (() => {
          const v = AI_VERDICT_LABEL[s.ai_verdict] || { label: s.ai_verdict, color: 'var(--text-secondary)' };
          return `<span style="color:${v.color};font-weight:600;">${v.label}</span> <span style="color:var(--text-tertiary);">AI 置信度 ${s.ai_confidence || 0}</span>`;
        })()
      : '<span style="color:var(--text-tertiary);">⏳ 验证中（排队等 LLM 返回）</span>';
    const sysSl = s.stop_loss == null ? '-' : Number(s.stop_loss).toFixed(4);
    const sysTp = s.take_profit == null ? '-' : Number(s.take_profit).toFixed(4);
    const aiSl = s.ai_stop_loss == null ? null : Number(s.ai_stop_loss).toFixed(4);
    const aiTp = s.ai_take_profit == null ? null : Number(s.ai_take_profit).toFixed(4);
    overlay.innerHTML = `
      <div style="background:var(--bg-secondary);border-radius:8px;width:min(720px,90vw);max-height:85vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.4);">
        <div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border-secondary);">
          <span style="font-weight:600;font-size:14px;">📡 信号详情 — ${esc(s.symbol)} · ${esc(s.interval || '1H')} · ${ACTION_ICON[s.action] || s.action}</span>
          <button id="signal-detail-close" class="btn btn-sm" style="font-size:14px;padding:2px 10px;">×</button>
        </div>
        <div style="overflow-y:auto;flex:1;padding:14px 18px;font-size:12px;line-height:1.6;">
          <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px 16px;margin-bottom:12px;">
            <div><span style="color:var(--text-tertiary);">触发时间：</span>${new Date(s.generated_at).toLocaleString()}</div>
            <div><span style="color:var(--text-tertiary);">触发价格：</span>${(s.price||0).toFixed(4)}</div>
            <div><span style="color:var(--text-tertiary);">系统置信度：</span><span style="font-weight:600;">${s.confidence}</span></div>
            <div><span style="color:var(--text-tertiary);">触发策略：</span>${esc(STRATEGY_LABEL[s.strategy_name] || s.strategy_name)}</div>
          </div>
          <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:12px;">
            <div style="color:var(--text-tertiary);margin-bottom:4px;">📋 触发理由</div>
            <div>${esc(s.reason || '-')}</div>
          </div>
          <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:12px;">
            <div style="color:var(--text-tertiary);margin-bottom:4px;">🤖 AI 验证</div>
            <div style="margin-bottom:6px;">${verdictBadge}</div>
            <div style="white-space:pre-wrap;">${esc(s.ai_reason || '（未触发或正在调用中）')}</div>
          </div>
          <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:12px;">
            <div style="color:var(--text-tertiary);margin-bottom:4px;">🛡️ 止损 / 🎯 止盈</div>
            <div>系统：SL ${sysSl} / TP ${sysTp}</div>
            ${aiSl || aiTp ? `<div style="color:var(--color-purple);">AI 调整：SL ${aiSl || sysSl} / TP ${aiTp || sysTp}</div>` : ''}
          </div>
          <div id="signal-detail-links" style="margin-bottom:12px;color:var(--text-tertiary);font-size:11px;">⏳ 加载关联数据...</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;">
            <button class="btn btn-sm" id="signal-detail-view" data-symbol="${esc(s.symbol)}" data-market="${esc(s.market)}">📈 看图</button>
            <button class="btn btn-sm" id="signal-detail-position">💼 加入持仓</button>
            <button class="btn btn-sm" id="signal-detail-alert">🔔 设警报</button>
          </div>
        </div>
      </div>
    `;
    // 异步拉详情用 AbortController：关闭 modal 时取消请求，防僵尸
    const detailCtrl = new AbortController();
    const closeModal = () => { try { detailCtrl.abort(); } catch {} overlay.remove(); };
    overlay.querySelector('#signal-detail-close').addEventListener('click', closeModal);
    // 覆盖默认的 overlay 点击关闭，改用带 abort 的版本
    overlay.onclick = (e) => { if (e.target === overlay) closeModal(); };

    fetch(`/api/signals/${encodeURIComponent(s.id)}`, { signal: detailCtrl.signal })
      .then(r => r.ok ? r.json() : null).then(detail => {
      if (!detail) return;
      const linksEl = overlay.querySelector('#signal-detail-links');
      if (!linksEl) return;
      const newsRows = (detail.related_news || []).map(n => {
        const stars = '★'.repeat(n.importance || 1);
        const sent = { bullish: '🟢', bearish: '🔴', neutral: '🟡' }[n.sentiment || 'neutral'];
        const u = n.url ? `<a href="${esc(n.url)}" target="_blank" rel="noopener" style="color:var(--text-tertiary);">↗</a>` : '';
        return `<li style="margin:2px 0;"><span style="color:var(--color-warning);">${stars}</span> ${sent} <span style="color:var(--text-secondary);font-size:10px;">${esc(n.source)}</span> ${esc(n.title.substring(0, 70))} ${u}</li>`;
      }).join('');
      const pool = detail.pool_context;
      const poolBlock = pool ? (() => {
        const d = pool.diagnosis || {};
        const ratingMap = { strong_buy:'🟢 强买', buy:'🟢 买入', hold:'⚪ 持有', reduce:'🟡 减仓', sell:'🔴 卖出' };
        return `<div style="background:var(--bg-tertiary);padding:8px 10px;border-radius:4px;margin-top:6px;">
          <div style="color:var(--text-secondary);margin-bottom:2px;"><strong>🩺 候选池诊断: ${ratingMap[d.rating] || d.rating || '-'}</strong>（conf ${d.confidence || 0}，池内综合分 ${pool.pool_score || 0}）</div>
          <div style="color:var(--text-tertiary);font-size:10px;">${esc((d.summary || '').substring(0, 100))}</div>
        </div>`;
      })() : '';
      linksEl.innerHTML = `
        ${newsRows ? `<div><strong style="color:var(--text-secondary);">📰 AI 验证基于 ${detail.related_news.length} 条近 24h 新闻：</strong><ul style="margin:4px 0 0 16px;padding:0;">${newsRows}</ul></div>` : '<div style="color:var(--text-tertiary);">📰 近 24h 无相关新闻</div>'}
        ${poolBlock}
      `;
    }).catch((e) => {
      if (e.name === 'AbortError') return;  // 用户关闭了 modal，正常中断
      const linksEl = overlay.querySelector('#signal-detail-links');
      if (linksEl) linksEl.innerHTML = '<div style="color:var(--text-tertiary);">关联数据加载失败</div>';
    });
    overlay.querySelector('#signal-detail-view').addEventListener('click', () => {
      if (typeof switchMarket === 'function' && s.market !== window.currentMarket) {
        switchMarket(s.market);
        setTimeout(() => switchSymbol && switchSymbol(s.symbol, s.market), 200);
      } else if (typeof switchSymbol === 'function') {
        switchSymbol(s.symbol, s.market);
      }
      overlay.remove();
    });
    overlay.querySelector('#signal-detail-position').addEventListener('click', async () => {
      const qty = prompt(`加入持仓 ${s.symbol}\n输入数量（按当前价 ${(s.price||0).toFixed(4)} 计算成本）:`, '0');
      const q = parseFloat(qty);
      if (!q || q <= 0) return;
      try {
        const resp = await fetch('/api/positions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: s.symbol, market: s.market, quantity: q, avg_cost: s.price }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        showToast(`已添加 ${s.symbol} × ${q} 到持仓`, 'success', 3000);
        overlay.remove();
        if (typeof Portfolio !== 'undefined') Portfolio.refresh && Portfolio.refresh();
      } catch (e) {
        showToast(`加持仓失败: ${e.message}`, 'error');
      }
    });
    overlay.querySelector('#signal-detail-alert').addEventListener('click', async () => {
      const target = s.action === 'buy' ? s.take_profit : s.stop_loss;
      if (!target) { showToast('该信号无止损/止盈价，无法快速创建警报', 'warning'); return; }
      const cond = s.action === 'buy' ? 'price_gte' : 'price_lte';
      try {
        const resp = await fetch('/api/alerts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            symbol: s.symbol, condition: cond, price: target,
            message: `${s.action.toUpperCase()} 触达 ${cond === 'price_gte' ? '止盈' : '止损'} ${target}`,
            repeat: false,
          }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        showToast(`警报已创建: ${s.symbol} ${cond === 'price_gte' ? '≥' : '≤'} ${target}`, 'success', 3000);
        if (typeof Alerts !== 'undefined') Alerts.loadAlerts && Alerts.loadAlerts();
      } catch (e) {
        showToast(`创建警报失败: ${e.message}`, 'error');
      }
    });
  }

  function _renderRow(s) {
    const MARKET_FLAG = { us: '🇺🇸', hk: '🇭🇰', cn: '🇨🇳', crypto: '🪙' };
    const time = new Date(s.generated_at).toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', month: 'numeric', day: 'numeric' });
    // SL/TP
    const fmtNum = v => (v == null ? '-' : Number(v).toFixed(4).replace(/\.?0+$/, ''));
    let slTp = '<span class="oc-muted">—</span>';
    if (s.stop_loss || s.take_profit || s.ai_stop_loss || s.ai_take_profit) {
      const sysSl = fmtNum(s.stop_loss), sysTp = fmtNum(s.take_profit);
      const aiSl = s.ai_stop_loss != null && s.ai_stop_loss !== s.stop_loss ? fmtNum(s.ai_stop_loss) : null;
      const aiTp = s.ai_take_profit != null && s.ai_take_profit !== s.take_profit ? fmtNum(s.ai_take_profit) : null;
      const slPart = aiSl ? `<span class="oc-down">SL ${sysSl}</span><span style="color:var(--color-purple);">→${aiSl}</span>` : `<span class="oc-down">SL ${sysSl}</span>`;
      const tpPart = aiTp ? `<span class="oc-up">TP ${sysTp}</span><span style="color:var(--color-purple);">→${aiTp}</span>` : `<span class="oc-up">TP ${sysTp}</span>`;
      slTp = `<div class="oc-text-xs" style="line-height:1.5;">${slPart}<br>${tpPart}</div>`;
    }
    const stratLabel = STRATEGY_LABEL[s.strategy_name] || s.strategy_name;
    const mktLabel = MARKET_LABEL[s.market] || (s.market || '').toUpperCase();
    const intervalLabel = s.interval || '1H';
    // 行背景按 AI verdict
    const rowBg = {
      confirm: 'background:rgba(14,203,129,0.06);',
      warn:    'background:rgba(240,185,11,0.06);',
      reject:  'background:rgba(120,120,120,0.10);opacity:0.65;',
    }[s.ai_verdict] || '';

    // 操作 chip
    const actionChipClass = s.action === 'buy' ? 'oc-chip-up' : 'oc-chip-down';
    const actionChip = `<span class="oc-chip ${actionChipClass}">${ACTION_ICON[s.action] || s.action}</span>`;

    // 置信度带颜色
    const confColor = s.confidence >= 80 ? 'oc-up' : s.confidence >= 70 ? 'oc-warn' : 'oc-secondary';

    // 品种格：代码 + 市场 flag/label
    const symbolCell = `
      <div style="display:flex;flex-direction:column;gap:1px;">
        <span style="font-weight:700;font-size:12px;">${s.symbol}</span>
        <span class="oc-text-xs oc-muted">${MARKET_FLAG[s.market]||''} ${mktLabel}</span>
      </div>`;

    // AI 验证格
    const aiCell = (() => {
      if (!s.ai_verdict) {
        return s.confidence >= 85
          ? '<span class="oc-chip oc-chip-neutral">⏳ 验证中</span>'
          : '<span class="oc-muted">—</span>';
      }
      const reasonText = s.ai_reason || '';
      const fastMatch = reasonText.match(/^\[([^\]]+)\]/);
      const fastSource = fastMatch && FAST_PATH_LABEL[fastMatch[1]];
      const VERDICT_CHIP_CLASS = {
        confirm: 'oc-chip-up',
        warn: 'oc-chip-warn',
        reject: 'oc-chip-down',
        llm_error: 'oc-chip-down',
        stale: 'oc-chip-neutral',
      };
      let chipClass, label;
      if (fastSource) {
        chipClass = 'oc-chip-purple';
        label = `${fastSource.label} ${s.ai_confidence||''}`;
      } else {
        chipClass = VERDICT_CHIP_CLASS[s.ai_verdict] || 'oc-chip-neutral';
        const v = AI_VERDICT_LABEL[s.ai_verdict] || { label: s.ai_verdict };
        label = `${v.label} ${s.ai_confidence||''}`;
      }
      const tail = fastMatch
        ? reasonText.substring(fastMatch[0].length).trim().substring(0, 38)
        : reasonText.substring(0, 40);
      return `
        <div style="display:flex;flex-direction:column;gap:2px;max-width:180px;">
          <span class="oc-chip ${chipClass}">${label}</span>
          <span class="oc-text-xs oc-muted" style="white-space:normal;line-height:1.3;" title="${reasonText.replace(/"/g,'&quot;')}">${tail}${reasonText.length>50?'…':''}</span>
        </div>`;
    })();

    return `
      <tr style="${rowBg}">
        <td class="oc-text-sm oc-muted" style="white-space:nowrap;">${time}</td>
        <td>${symbolCell}</td>
        <td><span class="oc-chip oc-chip-info">${intervalLabel}</span></td>
        <td>${actionChip}</td>
        <td class="oc-col-num oc-num">${(s.price || 0).toFixed(4)}</td>
        <td class="oc-col-num"><span class="${confColor}" style="font-weight:700;font-size:13px;" title="基础${s.strategy_name}+技术加分-新闻减分=${s.confidence}">${s.confidence}</span></td>
        <td class="oc-text-sm oc-secondary">${stratLabel}</td>
        <td class="oc-muted oc-text-sm" style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:help;" title="${(s.reason || '').replace(/"/g,'&quot;')}">${s.reason || '-'}</td>
        <td>${aiCell}</td>
        <td class="oc-col-num">${slTp}</td>
        <td class="oc-col-center" style="white-space:nowrap;">
          <button class="btn btn-sm" data-action="view" data-symbol="${s.symbol}" data-market="${s.market}" style="font-size:10px;padding:2px 6px;" title="查看 K 线">📊</button>
          <button class="btn btn-sm" data-action="detail" data-id="${s.id}" style="font-size:10px;padding:2px 6px;" title="完整 AI 理由 + 一键加持仓/警报">📋</button>
        </td>
      </tr>
    `;
  }

  return { init, refresh, render };
})();

window.Signals = Signals;
