/* ============================================================
   OnDemand 模块 — 按需分析 (v12.22.0)

   用户输入股票/加密代码 + 持仓信息 (可选) →
   AI 分析师给出专业建议 → 用户拍板执行
   ============================================================ */

const OnDemand = (function () {
  let _inited = false;
  let _lastAdvice = null;       // 最近一次分析结果 (含 advice + collected)
  let _historyItems = [];

  const ACTION_LABEL = {
    hold: '继续持有',
    add: '加仓',
    reduce: '减仓 50%',
    close: '平仓',
    open_long: '开多',
    open_short: '开空',
    wait: '暂不建议',
  };
  const ACTION_COLOR = {
    hold: 'var(--text-secondary)',
    add: 'var(--color-up)',
    reduce: 'var(--color-warning)',
    close: 'var(--color-down)',
    open_long: 'var(--color-up)',
    open_short: 'var(--color-down)',
    wait: 'var(--text-tertiary)',
  };
  const ACTION_ICON = {
    hold: '✅', add: '🟢', reduce: '🟡', close: '🔴',
    open_long: '🟢', open_short: '🔴', wait: '⏸',
  };

  function init() {
    if (_inited) return;
    _inited = true;
    _ensureDom();
    console.log('[OnDemand] 已初始化');
  }

  function _ensureDom() {
    const pane = document.querySelector('.bottom-pane[data-pane="on-demand"]');
    if (!pane || pane.dataset.inited) return;
    pane.dataset.inited = '1';

    pane.innerHTML = `
      <div class="od-container" style="padding:12px 16px;height:100%;overflow-y:auto;">
        <div class="od-header" style="margin-bottom:14px;">
          <h3 style="margin:0 0 4px 0;font-size:15px;color:var(--text-primary);">🔍 按需分析</h3>
          <div style="font-size:11px;color:var(--text-tertiary);">输入代码 + 持仓状态 → AI 分析师给出专业建议</div>
        </div>

        <!-- ──────── 输入表单 ──────── -->
        <div class="od-form" style="background:var(--bg-secondary);padding:12px;border-radius:6px;border:1px solid var(--border-secondary);margin-bottom:14px;">
          <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;">
            <div style="flex:0 0 auto;">
              <label style="display:block;font-size:11px;color:var(--text-tertiary);margin-bottom:4px;">代码</label>
              <input id="od-symbol" type="text" placeholder="如: ETH / AAPL / 0981" style="width:160px;padding:5px 8px;background:var(--bg-tertiary);border:1px solid var(--border-secondary);color:var(--text-primary);border-radius:4px;font-size:13px;">
            </div>
            <div style="flex:0 0 auto;">
              <label style="display:block;font-size:11px;color:var(--text-tertiary);margin-bottom:4px;">市场</label>
              <select id="od-market" style="padding:5px 8px;background:var(--bg-tertiary);border:1px solid var(--border-secondary);color:var(--text-primary);border-radius:4px;font-size:13px;">
                <option value="crypto">加密</option>
                <option value="us">美股</option>
                <option value="hk">港股</option>
                <option value="cn">A 股</option>
              </select>
            </div>
            <div style="flex:1 1 auto;min-width:240px;">
              <label style="display:block;font-size:11px;color:var(--text-tertiary);margin-bottom:4px;">持仓状态</label>
              <div style="display:flex;gap:0;">
                <button id="od-no-pos" class="od-pos-tab active" data-pos="no" style="padding:5px 12px;font-size:12px;border:1px solid var(--border-secondary);border-radius:4px 0 0 4px;background:var(--color-accent);color:#fff;cursor:pointer;">📈 暂无持仓 (分析是否值得开仓)</button>
                <button id="od-has-pos" class="od-pos-tab" data-pos="yes" style="padding:5px 12px;font-size:12px;border:1px solid var(--border-secondary);border-left:none;border-radius:0 4px 4px 0;background:transparent;color:var(--text-secondary);cursor:pointer;">💼 已有持仓 (分析继续/加减/平)</button>
              </div>
            </div>
          </div>

          <!-- 持仓信息表单 (条件显示) -->
          <div id="od-pos-form" style="display:none;margin-top:10px;padding-top:10px;border-top:1px dashed var(--border-secondary);">
            <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;">
              <div>
                <label style="display:block;font-size:11px;color:var(--text-tertiary);margin-bottom:4px;">方向</label>
                <select id="od-pos-side" style="padding:5px 8px;background:var(--bg-tertiary);border:1px solid var(--border-secondary);color:var(--text-primary);border-radius:4px;font-size:13px;">
                  <option value="long">多</option>
                  <option value="short">空</option>
                </select>
              </div>
              <div>
                <label style="display:block;font-size:11px;color:var(--text-tertiary);margin-bottom:4px;">均价</label>
                <input id="od-pos-avg" type="number" step="any" placeholder="持仓成本价" style="width:120px;padding:5px 8px;background:var(--bg-tertiary);border:1px solid var(--border-secondary);color:var(--text-primary);border-radius:4px;font-size:13px;">
              </div>
              <div>
                <label style="display:block;font-size:11px;color:var(--text-tertiary);margin-bottom:4px;">数量</label>
                <input id="od-pos-qty" type="number" step="any" placeholder="股数/张数" style="width:100px;padding:5px 8px;background:var(--bg-tertiary);border:1px solid var(--border-secondary);color:var(--text-primary);border-radius:4px;font-size:13px;">
              </div>
              <div>
                <label style="display:block;font-size:11px;color:var(--text-tertiary);margin-bottom:4px;">止损 (选填)</label>
                <input id="od-pos-sl" type="number" step="any" placeholder="留空让 AI 给" style="width:130px;padding:5px 8px;background:var(--bg-tertiary);border:1px solid var(--border-secondary);color:var(--text-primary);border-radius:4px;font-size:13px;">
              </div>
              <div>
                <label style="display:block;font-size:11px;color:var(--text-tertiary);margin-bottom:4px;">止盈 (选填)</label>
                <input id="od-pos-tp" type="number" step="any" placeholder="留空让 AI 给" style="width:130px;padding:5px 8px;background:var(--bg-tertiary);border:1px solid var(--border-secondary);color:var(--text-primary);border-radius:4px;font-size:13px;">
              </div>
              <button id="od-pos-fetch" type="button" style="padding:5px 12px;font-size:11px;background:var(--bg-tertiary);border:1px solid var(--border-secondary);color:var(--text-secondary);border-radius:4px;cursor:pointer;" title="从我的账户读取已有持仓">📥 从账户读取</button>
            </div>
          </div>

          <div style="display:flex;justify-content:flex-end;margin-top:12px;gap:8px;">
            <button id="od-history-btn" style="padding:6px 14px;font-size:12px;background:transparent;border:1px solid var(--border-secondary);color:var(--text-secondary);border-radius:4px;cursor:pointer;">📜 历史记录</button>
            <button id="od-analyze-btn" style="padding:6px 18px;font-size:13px;background:var(--color-accent);color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:600;">🚀 开始分析</button>
          </div>
        </div>

        <!-- ──────── 状态/结果区 ──────── -->
        <div id="od-result" style="font-size:13px;color:var(--text-secondary);">
          <div style="text-align:center;padding:30px;color:var(--text-tertiary);">
            填写代码并点击「开始分析」,系统会收集 K 线/新闻/衍生品数据,由资深分析师给出专业建议
          </div>
        </div>
      </div>
    `;

    _bindEvents();
  }

  function _bindEvents() {
    // 持仓 tab 切换
    document.querySelectorAll('.od-pos-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        const isYes = btn.dataset.pos === 'yes';
        document.querySelectorAll('.od-pos-tab').forEach(b => {
          const active = b === btn;
          b.classList.toggle('active', active);
          b.style.background = active ? 'var(--color-accent)' : 'transparent';
          b.style.color = active ? '#fff' : 'var(--text-secondary)';
        });
        const posForm = document.getElementById('od-pos-form');
        if (posForm) posForm.style.display = isYes ? 'block' : 'none';
      });
    });

    // 从账户读取持仓
    document.getElementById('od-pos-fetch')?.addEventListener('click', _fetchPositionFromAccount);

    // 开始分析
    document.getElementById('od-analyze-btn')?.addEventListener('click', _onAnalyze);

    // 历史记录
    document.getElementById('od-history-btn')?.addEventListener('click', _showHistory);
  }

  async function _fetchPositionFromAccount() {
    const symbol = document.getElementById('od-symbol').value.trim();
    const market = document.getElementById('od-market').value;
    if (!symbol) { _toast('请先填代码', 'warning'); return; }
    try {
      const resp = await fetch('/api/positions');
      const items = await resp.json();
      const arr = Array.isArray(items) ? items : (items.items || []);
      // 模糊匹配 symbol (用户填 ETH 或 ETH-USDT 都能匹配)
      const sUpper = symbol.toUpperCase();
      const found = arr.find(p => {
        const ps = (p.symbol || '').toUpperCase();
        const pm = (p.market || '').toLowerCase();
        return pm === market && (ps === sUpper || ps.startsWith(sUpper + '-') || ps.startsWith(sUpper + '.'));
      });
      if (!found) { _toast(`账户中未找到 ${symbol}(${market}) 持仓`, 'info'); return; }
      document.getElementById('od-pos-side').value = found.side || 'long';
      document.getElementById('od-pos-avg').value = found.avg_cost || '';
      document.getElementById('od-pos-qty').value = found.quantity || '';
      document.getElementById('od-pos-sl').value = found.ai_stop_loss || '';
      document.getElementById('od-pos-tp').value = found.ai_take_profit || '';
      _toast(`已读取 ${found.symbol}: ${found.side} ${found.quantity} @ ${found.avg_cost}`, 'success');
    } catch (e) {
      _toast(`读取持仓失败: ${e.message}`, 'error');
    }
  }

  async function _onAnalyze() {
    const symbol = document.getElementById('od-symbol').value.trim();
    const market = document.getElementById('od-market').value;
    if (!symbol) { _toast('请填代码', 'warning'); return; }

    const activeTab = document.querySelector('.od-pos-tab.active');
    const hasPos = activeTab?.dataset.pos === 'yes';
    let position = null;
    if (hasPos) {
      const qty = parseFloat(document.getElementById('od-pos-qty').value || '0');
      const avg = parseFloat(document.getElementById('od-pos-avg').value || '0');
      if (!(qty > 0 && avg > 0)) { _toast('请填均价和数量', 'warning'); return; }
      const slV = parseFloat(document.getElementById('od-pos-sl').value || '');
      const tpV = parseFloat(document.getElementById('od-pos-tp').value || '');
      position = {
        side: document.getElementById('od-pos-side').value || 'long',
        avg_cost: avg,
        quantity: qty,
        stop_loss: isNaN(slV) ? null : slV,
        take_profit: isNaN(tpV) ? null : tpV,
      };
    }

    const btn = document.getElementById('od-analyze-btn');
    btn.disabled = true;
    btn.textContent = '⏳ 分析中...';
    _renderLoading(symbol, market, hasPos);

    try {
      const resp = await fetch('/api/on-demand/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol, market,
          has_position: hasPos,
          position: position,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.detail || `HTTP ${resp.status}`);
      }
      _lastAdvice = data;
      _renderAdvice(data);
    } catch (e) {
      console.error('[OnDemand] 分析失败', e);
      _renderError(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '🚀 开始分析';
    }
  }

  function _renderLoading(symbol, market, hasPos) {
    const r = document.getElementById('od-result');
    if (!r) return;
    r.innerHTML = `
      <div style="background:var(--bg-secondary);padding:20px;border-radius:6px;border:1px solid var(--border-secondary);text-align:center;">
        <div style="font-size:18px;margin-bottom:10px;">🔄 正在分析 ${_esc(symbol)} (${_esc(market)})</div>
        <div style="font-size:12px;color:var(--text-tertiary);line-height:2;">
          ⏳ 收集 K 线数据 (5 周期)<br>
          ⏳ 抓取近 7 天新闻<br>
          ${market === 'crypto' ? '⏳ 拉取衍生品数据<br>' : '⏳ 查询基本面数据<br>'}
          ${hasPos ? '⏳ 解析持仓信息<br>' : ''}
          ⏳ AI 分析师生成报告中... (~20-30s)
        </div>
      </div>
    `;
  }

  function _renderError(msg) {
    const r = document.getElementById('od-result');
    if (!r) return;
    r.innerHTML = `
      <div style="background:var(--bg-secondary);padding:20px;border-radius:6px;border:1px solid var(--color-down);">
        <div style="font-size:14px;color:var(--color-down);margin-bottom:6px;">❌ 分析失败</div>
        <div style="font-size:12px;color:var(--text-secondary);">${_esc(msg)}</div>
      </div>
    `;
  }

  function _renderAdvice(data) {
    const r = document.getElementById('od-result');
    if (!r) return;
    const advice = data.advice || {};
    const t0 = data.t0_snapshot || {};
    const action = advice.action || 'wait';
    const conf = advice.confidence || 0;
    const confColor = conf >= 70 ? 'var(--color-up)' : conf >= 50 ? 'var(--color-warning)' : 'var(--color-down)';

    const bars = '█'.repeat(Math.round(conf / 10)) + '░'.repeat(10 - Math.round(conf / 10));

    const sigList = advice.supporting_signals || [];
    const counterList = advice.counter_signals || [];
    const risksList = advice.key_risks || [];
    const watchList = advice.watch_signals || [];
    const abortList = advice.abort_conditions || [];

    const entry = advice.entry_strategy || {};
    const exit = advice.exit_strategy || {};
    const sizing = advice.position_sizing || {};

    const pos = data.position;

    let posBlock = '';
    if (pos) {
      const cur = t0.price || 0;
      const cost = pos.avg_cost || 0;
      const isLong = (pos.side || 'long') === 'long';
      const pnlPct = (cost > 0 && cur > 0)
        ? ((isLong ? (cur - cost) : (cost - cur)) / cost * 100) : 0;
      const pnlColor = pnlPct >= 0 ? 'var(--color-up)' : 'var(--color-down)';
      posBlock = `
        <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:5px;margin-bottom:12px;font-size:12px;">
          <div style="color:var(--text-tertiary);margin-bottom:4px;">📌 你的持仓 (${_esc(pos.source || '')}${pos.type === 'swap' ? ' · 合约' : ''})</div>
          <div style="color:var(--text-primary);">
            方向: ${isLong ? '多' : '空'} ·
            数量: ${_fmt(pos.quantity, 4)} ·
            均价: ${_fmt(pos.avg_cost, 4)} ·
            当前: ${_fmt(cur, 4)} ·
            <span style="color:${pnlColor};font-weight:600;">${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%</span>
          </div>
        </div>`;
    }

    let entryBlock = '';
    if (entry && entry.ideal_price) {
      const rng = entry.acceptable_range;
      entryBlock = `
        <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:5px;margin-top:8px;font-size:12px;">
          <div style="color:var(--text-tertiary);margin-bottom:6px;">💡 入场策略</div>
          <div style="color:var(--text-primary);line-height:1.8;">
            理想入场: <strong>${_fmt(entry.ideal_price, 4)}</strong>
            ${rng && rng.length === 2 ? ` · 接受区间: ${_fmt(rng[0], 4)} ~ ${_fmt(rng[1], 4)}` : ''}
            <br>方式: ${_esc(entry.approach || '市价')} · 建议仓位: <strong>${sizing.suggested_pct || 0}%</strong>
            ${sizing.reasoning ? `<div style="color:var(--text-tertiary);font-size:11px;margin-top:4px;">${_esc(sizing.reasoning)}</div>` : ''}
          </div>
        </div>`;
    }

    let exitBlock = '';
    if (exit && (exit.stop_loss || exit.take_profit_1)) {
      exitBlock = `
        <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:5px;margin-top:8px;font-size:12px;">
          <div style="color:var(--text-tertiary);margin-bottom:6px;">🎯 风控参数</div>
          <div style="color:var(--text-primary);line-height:1.8;">
            ${exit.stop_loss ? `止损: <span style="color:var(--color-down);">${_fmt(exit.stop_loss, 4)}</span> · ` : ''}
            ${exit.take_profit_1 ? `目标 1: <span style="color:var(--color-up);">${_fmt(exit.take_profit_1, 4)}</span>` : ''}
            ${exit.take_profit_2 ? ` · 目标 2: <span style="color:var(--color-up);">${_fmt(exit.take_profit_2, 4)}</span>` : ''}
            ${exit.trail_logic ? `<div style="color:var(--text-tertiary);font-size:11px;margin-top:4px;">移动止损: ${_esc(exit.trail_logic)}</div>` : ''}
          </div>
        </div>`;
    }

    const execBtnText = ACTION_ICON[action] + ' ' + (action === 'hold' ? '更新止损/止盈'
      : action === 'wait' ? '设置价格提醒' : '按建议执行 ' + (ACTION_LABEL[action] || action));
    const execBtnColor = action === 'wait' ? 'var(--text-tertiary)' : ACTION_COLOR[action];
    const execDisabled = action === 'wait';

    r.innerHTML = `
      <div style="background:var(--bg-secondary);padding:14px;border-radius:6px;border:1px solid var(--border-secondary);">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">
          <div>
            <div style="font-size:14px;color:var(--text-primary);font-weight:600;">${_esc(_currentSymbolLabel())}</div>
            <div style="font-size:11px;color:var(--text-tertiary);margin-top:2px;">${t0.price ? `T0 价: <strong>${_fmt(t0.price, 4)}</strong> · ⏱ ${_fmtTs(t0.ts)}` : '价格未知'}</div>
          </div>
          <div style="text-align:right;">
            <div style="font-size:18px;color:${ACTION_COLOR[action]};font-weight:600;">${ACTION_ICON[action]} ${_esc(ACTION_LABEL[action] || action)}</div>
            <div style="font-size:11px;color:var(--text-tertiary);margin-top:2px;">把握度: <span style="color:${confColor};font-weight:600;">${conf}</span> <span style="font-family:monospace;color:${confColor};">[${bars}]</span></div>
            ${advice.time_horizon ? `<div style="font-size:11px;color:var(--text-tertiary);">持有: ${_esc(advice.time_horizon)}</div>` : ''}
          </div>
        </div>

        ${posBlock}

        <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:5px;margin-bottom:8px;">
          <div style="color:var(--text-tertiary);font-size:11px;margin-bottom:4px;">💭 核心逻辑</div>
          <div style="color:var(--text-primary);font-size:13px;line-height:1.6;">${_esc(advice.main_thesis || '')}</div>
        </div>

        ${entryBlock}
        ${exitBlock}

        <!-- 折叠区 -->
        ${_renderCollapsible('📈 支撑信号', sigList, _renderSignalRow, sigList.length)}
        ${_renderCollapsible('⚠️ 反向信号', counterList, _renderCounterRow, counterList.length)}
        ${_renderCollapsible('🚨 关键风险', risksList, _renderRiskRow, risksList.length)}
        ${watchList.length ? _renderCollapsible('👀 需关注信号', watchList, _renderTextRow, watchList.length) : ''}
        ${abortList.length ? _renderCollapsible('⛔ 执行前再核对', abortList, _renderTextRow, abortList.length) : ''}
        ${advice.professional_summary ? _renderCollapsible('📄 完整分析报告', [advice.professional_summary], _renderSummaryRow, 1) : ''}
        ${data.missing_data && data.missing_data.length ? `<div style="margin-top:8px;padding:6px 10px;background:rgba(255,193,7,0.1);border-left:3px solid var(--color-warning);font-size:11px;color:var(--text-secondary);">⚠️ 数据缺失项: ${data.missing_data.join(', ')} (建议仅供参考)</div>` : ''}

        <!-- 操作按钮 -->
        <div style="margin-top:14px;display:flex;justify-content:flex-end;gap:8px;flex-wrap:wrap;">
          <button onclick="OnDemand.showRawAdvice()" style="padding:6px 14px;font-size:12px;background:transparent;border:1px solid var(--border-secondary);color:var(--text-secondary);border-radius:4px;cursor:pointer;">📋 查看原始 JSON</button>
          <button onclick="OnDemand._onAnalyzeAgain()" style="padding:6px 14px;font-size:12px;background:transparent;border:1px solid var(--border-secondary);color:var(--text-secondary);border-radius:4px;cursor:pointer;">🔄 重新分析</button>
          <button id="od-execute-btn" ${execDisabled ? 'disabled' : ''} style="padding:6px 18px;font-size:13px;background:${execDisabled ? 'var(--bg-tertiary)' : execBtnColor};color:${execDisabled ? 'var(--text-tertiary)' : '#fff'};border:none;border-radius:4px;cursor:${execDisabled ? 'not-allowed' : 'pointer'};font-weight:600;">${execBtnText}</button>
        </div>
      </div>
    `;

    document.getElementById('od-execute-btn')?.addEventListener('click', _onExecute);
  }

  function _currentSymbolLabel() {
    if (!_lastAdvice) return '';
    const t0 = _lastAdvice.t0_snapshot || {};
    const symbol = document.getElementById('od-symbol')?.value || '';
    const market = document.getElementById('od-market')?.value || '';
    return `${symbol} · ${market}`;
  }

  function _renderCollapsible(title, items, rowFn, count) {
    if (!items || !items.length) return '';
    const id = 'od-coll-' + Math.random().toString(36).slice(2, 8);
    const inner = items.map(rowFn).join('');
    return `
      <details style="margin-top:6px;background:var(--bg-tertiary);border-radius:5px;">
        <summary style="padding:8px 12px;cursor:pointer;font-size:12px;color:var(--text-secondary);">${title} (${count})</summary>
        <div style="padding:6px 12px 10px;font-size:12px;line-height:1.6;">${inner}</div>
      </details>
    `;
  }

  function _renderSignalRow(s) {
    const w = s.weight || '';
    const wColor = w === '强' ? 'var(--color-up)' : w === '中' ? 'var(--color-warning)' : 'var(--text-tertiary)';
    return `<div style="padding:4px 0;border-bottom:1px solid var(--border-secondary);">
      <div style="color:var(--text-primary);">${_esc(s.signal || '')} ${w ? `<span style="color:${wColor};font-size:10px;">[${w}]</span>` : ''}</div>
      ${s.data ? `<div style="color:var(--text-tertiary);font-size:11px;">${_esc(s.data)}</div>` : ''}
    </div>`;
  }

  function _renderCounterRow(s) {
    return `<div style="padding:4px 0;border-bottom:1px solid var(--border-secondary);">
      <div style="color:var(--text-primary);">${_esc(s.signal || '')}</div>
      ${s.data ? `<div style="color:var(--text-tertiary);font-size:11px;">${_esc(s.data)}</div>` : ''}
      ${s.impact ? `<div style="color:var(--text-tertiary);font-size:11px;font-style:italic;">影响: ${_esc(s.impact)}</div>` : ''}
    </div>`;
  }

  function _renderRiskRow(r) {
    return `<div style="padding:4px 0;border-bottom:1px solid var(--border-secondary);">
      <div style="color:var(--color-down);">${_esc(r.risk || '')}</div>
      ${r.trigger ? `<div style="color:var(--text-tertiary);font-size:11px;">触发: ${_esc(r.trigger)}</div>` : ''}
      ${r.magnitude ? `<div style="color:var(--text-tertiary);font-size:11px;">影响: ${_esc(r.magnitude)}</div>` : ''}
    </div>`;
  }

  function _renderTextRow(t) {
    return `<div style="padding:4px 0;border-bottom:1px solid var(--border-secondary);color:var(--text-primary);">• ${_esc(t)}</div>`;
  }

  function _renderSummaryRow(t) {
    return `<div style="color:var(--text-primary);white-space:pre-wrap;">${_esc(t)}</div>`;
  }

  function _onAnalyzeAgain() {
    _onAnalyze();
  }

  function showRawAdvice() {
    if (!_lastAdvice) { _toast('暂无分析结果', 'info'); return; }
    const json = JSON.stringify(_lastAdvice.advice, null, 2);
    const w = window.open('', '_blank', 'width=600,height=700');
    if (w) {
      w.document.write(`<pre style="font-family:monospace;font-size:12px;padding:16px;">${_esc(json)}</pre>`);
    } else {
      navigator.clipboard?.writeText(json);
      _toast('已复制 JSON 到剪贴板', 'success');
    }
  }

  async function _onExecute() {
    if (!_lastAdvice || !_lastAdvice.advice) { _toast('请先分析', 'warning'); return; }
    const advice = _lastAdvice.advice;
    const adviceId = advice.advice_id;
    if (!adviceId) { _toast('advice_id 缺失', 'error'); return; }

    // 二次确认 modal
    const action = advice.action;
    const sizing = advice.position_sizing || {};
    const exit = advice.exit_strategy || {};
    const okConfirm = window.confirm(
      `确认执行?\n\n` +
      `操作: ${ACTION_LABEL[action] || action}\n` +
      `代码: ${document.getElementById('od-symbol').value} (${document.getElementById('od-market').value})\n` +
      `把握度: ${advice.confidence}/100\n` +
      `建议仓位: ${sizing.suggested_pct || 0}%\n` +
      (exit.stop_loss ? `止损: ${exit.stop_loss}\n` : '') +
      (exit.take_profit_1 ? `目标: ${exit.take_profit_1}\n` : '') +
      `\n执行前会重新读取实时价检查漂移 (>0.5% 阻断),并复用同股冷却 + 单股每日上限。`
    );
    if (!okConfirm) return;

    const btn = document.getElementById('od-execute-btn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 执行中...'; }

    try {
      const resp = await fetch('/api/on-demand/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ advice_id: adviceId, confirm: true }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.detail || `HTTP ${resp.status}`);
      }
      _toast(`✅ 执行成功: ${data.action} @ ${data.executed_price} (漂移 ${data.drift_pct}%)`, 'success', 6000);
      // 标记按钮成功状态
      if (btn) {
        btn.style.background = 'var(--color-up)';
        btn.textContent = '✅ 已执行';
      }
      // 自动刷新持仓列表
      try {
        if (typeof Portfolio !== 'undefined' && Portfolio.refresh) Portfolio.refresh();
      } catch {}
    } catch (e) {
      console.error('[OnDemand] 执行失败', e);
      _toast(`❌ 执行失败: ${e.message}`, 'error', 8000);
      if (btn) { btn.disabled = false; btn.textContent = '🔄 重试执行'; }
    }
  }

  async function _showHistory() {
    try {
      const resp = await fetch('/api/on-demand/history?limit=20');
      const data = await resp.json();
      _historyItems = data.items || [];
      _renderHistory();
    } catch (e) {
      _toast(`读取历史失败: ${e.message}`, 'error');
    }
  }

  function _renderHistory() {
    const r = document.getElementById('od-result');
    if (!r) return;
    if (!_historyItems.length) {
      r.innerHTML = `<div style="text-align:center;padding:30px;color:var(--text-tertiary);">暂无历史记录</div>`;
      return;
    }
    r.innerHTML = `
      <div style="background:var(--bg-secondary);padding:12px;border-radius:6px;border:1px solid var(--border-secondary);">
        <div style="font-size:13px;color:var(--text-primary);font-weight:600;margin-bottom:10px;">📜 最近 ${_historyItems.length} 次按需分析</div>
        <div style="font-size:12px;">
          ${_historyItems.map(_renderHistoryRow).join('')}
        </div>
      </div>
    `;
  }

  function _renderHistoryRow(it) {
    const action = it.action || 'wait';
    const ts = new Date((it.created_at || 0) * 1000);
    const tsStr = ts.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    return `<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border-secondary);">
      <div>
        <span style="color:var(--text-primary);font-weight:600;">${_esc(it.symbol)}</span>
        <span style="color:var(--text-tertiary);font-size:11px;margin-left:6px;">${_esc(it.market)}</span>
        <span style="color:${ACTION_COLOR[action]};margin-left:10px;">${ACTION_ICON[action]} ${ACTION_LABEL[action] || action}</span>
        <span style="color:var(--text-tertiary);font-size:11px;margin-left:6px;">把握 ${it.confidence}</span>
      </div>
      <div style="color:var(--text-tertiary);font-size:11px;">
        ${tsStr} ${it.executed ? '✅' : ''}
      </div>
    </div>`;
  }

  // ─── 工具 ─────────────────────────────────────
  function _esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function _fmt(n, d) {
    if (n === null || n === undefined || isNaN(n)) return '--';
    const num = Number(n);
    if (d === 0) return num.toLocaleString();
    return num.toFixed(d);
  }

  function _fmtTs(s) {
    if (!s) return '--';
    try {
      const d = new Date(s);
      return d.toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch { return s; }
  }

  function _toast(msg, type, duration) {
    if (typeof showToast === 'function') {
      showToast(msg, type || 'info', duration || 4000);
    } else {
      console.log(`[Toast/${type}] ${msg}`);
    }
  }

  return {
    init,
    showRawAdvice,
    _onAnalyzeAgain,
  };
})();
