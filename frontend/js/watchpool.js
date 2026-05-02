/* ============================================================
   WatchPool 模块 — 候选池面板 (Phase 3A，仅股票市场)

   功能：
   - 列表展示候选池（按评分降序）
   - 手动添加股票（带市场约束，加密拒绝）
   - 移除条目
   - WebSocket 推送更新（pool_update）
   - 加密市场时整个面板隐藏
   ============================================================ */

const Watchpool = (function () {
  let _items = [];
  let _filterMarket = 'all';
  let _filterDiag = 'all';     // all | diagnosed | undiagnosed
  let _filterRating = 'all';   // all | strong_buy | buy | hold | reduce | sell
  let _viewMode = 'active';    // active | archived
  let _page = 1;
  const _pageSize = 30;
  let _sortKey = 'score';
  let _sortDir = 'desc';
  let _diagController = null;  // AbortController 防僵尸诊断请求

  let _inited = false;
  function init() {
    if (_inited) { console.warn('[WatchPool] 已初始化，跳过重复 init'); return; }
    _inited = true;
    _ensureDom();
    if (typeof ws !== 'undefined' && ws) {
      // 节流：1 秒内多次推送只触发一次 refresh
      let _refreshTimer = null;
      ws.on('pool_update', (msg) => {
        if (!msg) return;
        const data = msg.data || msg;
        // 诊断完成 → 弹 toast 提示，并刷新列表
        if (data.action === 'diagnosed' && data.symbol) {
          if (typeof showToast === 'function') {
            const ratingMap = { strong_buy: '🟢 强烈买入', buy: '🟢 买入', hold: '⚪ 持有', reduce: '🟡 减仓', sell: '🔴 卖出' };
            const r = ratingMap[data.rating] || data.rating || '诊断完成';
            showToast(`🩺 ${data.symbol} ${r}`, 'info', 4000);
          }
        }
        if (_refreshTimer) return;
        _refreshTimer = setTimeout(() => { _refreshTimer = null; refresh(); }, 1000);
      });
      // 手动股票被新闻提及 → 高优先级通知 + 闪烁
      ws.on('flash_news', (msg) => {
        if (!msg || msg.type !== 'manual_stock_news') return;
        const d = msg.data || {};
        if (!d.symbol) return;
        const stars = '★'.repeat(d.importance || 1);
        const sentIcon = { bullish: '🟢', bearish: '🔴', neutral: '🟡' }[d.sentiment] || '🟡';
        if (typeof showToast === 'function') {
          showToast(`📌 关注股 ${d.symbol} 有新闻: ${stars} ${sentIcon} ${(d.news_title || '').substring(0, 50)}`, 'warning', 6000);
        }
        // 桌面通知（若已授权）
        if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
          try {
            new Notification(`📌 ${d.symbol} · ${d.news_source || ''}`, {
              body: d.news_title || '',
              tag: 'manual-news-' + d.symbol + '-' + d.published_at,
            });
          } catch {}
        }
        // 列表里该行闪烁高亮
        setTimeout(() => {
          const row = document.querySelector(`.bottom-pane[data-pane="watchpool"] [data-row-id="${CSS.escape(d.pool_id || '')}"]`);
          if (row) {
            row.style.transition = 'background 0.3s';
            row.style.background = 'rgba(255,180,50,0.3)';
            setTimeout(() => { row.style.background = 'rgba(255,180,50,0.1)'; }, 400);
            setTimeout(() => { row.style.background = 'rgba(255,180,50,0.3)'; }, 800);
            setTimeout(() => { row.style.background = ''; }, 1500);
          }
        }, 100);
      });
    }
    refresh();
    console.log('[WatchPool] 已初始化');
  }

  function _ensureDom() {
    const pane = document.querySelector('.bottom-pane[data-pane="watchpool"]');
    if (!pane) {
      console.warn('[WatchPool] 未找到 [data-pane="watchpool"] 容器');
      return;
    }
    if (pane.dataset.inited) return;
    pane.dataset.inited = '1';
    pane.innerHTML = `
      <div class="oc-toolbar pool-toolbar">
        <span class="oc-text-lg" style="font-weight:600;">📊 候选池</span>
        <div class="oc-tabs" id="pool-view-tabs">
          <button class="oc-tab active pool-view-tab" data-view="active">在池中</button>
          <button class="oc-tab pool-view-tab" data-view="archived">已淘汰</button>
        </div>
        <select id="pool-filter-market" class="select oc-text-sm" style="width:96px;">
          <option value="all">全部市场</option>
          <option value="us">美股</option>
          <option value="hk">港股</option>
          <option value="cn">A股</option>
        </select>
        <select id="pool-filter-diag" class="select oc-text-sm" style="width:104px;" title="按 AI 诊断状态过滤">
          <option value="all">全部诊断</option>
          <option value="diagnosed">已诊断</option>
          <option value="undiagnosed">未诊断</option>
        </select>
        <select id="pool-filter-rating" class="select oc-text-sm" style="width:108px;" title="按 AI 评级过滤">
          <option value="all">全部评级</option>
          <option value="strong_buy">🟢 强买</option>
          <option value="buy">🟢 买入</option>
          <option value="hold">⚪ 持有</option>
          <option value="reduce">🟡 减仓</option>
          <option value="sell">🔴 卖出</option>
        </select>
        <span class="oc-toolbar-spacer"></span>
        <button id="pool-add-btn" class="btn btn-primary btn-sm">+ 添加</button>
        <button id="pool-refresh-btn" class="btn btn-sm" title="刷新">🔄</button>
        <button id="pool-rescore-btn" class="btn btn-sm" title="对所有条目重算技术分+基本面分">📊 重评分</button>
        <span id="pool-status" class="oc-text-sm oc-muted" style="width:100%;text-align:right;margin-top:2px;"></span>
      </div>
      <div class="pool-add-form" style="display:none;padding:10px;background:var(--bg-tertiary);border-bottom:1px solid var(--border-secondary);">
        <div style="display:flex;gap:6px;align-items:flex-start;">
          <div style="flex:1;position:relative;">
            <input id="pool-add-symbol" class="input" placeholder="代码或名称 (如 NVDA / Apple / 600519 / 贵州茅台)" autocomplete="off" style="width:100%;">
            <div id="pool-add-suggest" style="display:none;position:absolute;left:0;right:0;top:100%;max-height:220px;overflow-y:auto;background:var(--bg-secondary);border:1px solid var(--border-secondary);border-radius:4px;z-index:100;box-shadow:0 4px 12px rgba(0,0,0,0.2);"></div>
          </div>
          <select id="pool-add-market" class="select" style="width:80px;">
            <option value="us">美股</option>
            <option value="hk">港股</option>
            <option value="cn">A股</option>
          </select>
          <input id="pool-add-reason" class="input" placeholder="备注 (可选)" style="flex:1;">
          <button id="pool-add-submit" class="btn btn-primary btn-sm">确认添加</button>
          <button id="pool-add-cancel" class="btn btn-sm">取消</button>
        </div>
        <div class="oc-text-sm oc-muted" style="margin-top:6px;">输入至少 2 个字符会自动显示匹配的股票，点击选择即可。</div>
      </div>
      <div class="pool-list" style="overflow-y:auto;flex:1;min-height:0;"></div>
    `;
    // 不在这里改 display，让 .bottom-pane.active CSS 规则控制可见性
    const $ = (s) => pane.querySelector(s);
    $('#pool-filter-market').addEventListener('change', (e) => {
      _filterMarket = e.target.value;
      _page = 1;
      render();
    });
    $('#pool-filter-diag').addEventListener('change', (e) => {
      _filterDiag = e.target.value;
      _page = 1;
      render();
    });
    $('#pool-filter-rating').addEventListener('change', (e) => {
      _filterRating = e.target.value;
      _page = 1;
      render();
    });
    $('#pool-view-tabs').addEventListener('click', (e) => {
      const tab = e.target.closest('.pool-view-tab');
      if (!tab) return;
      _viewMode = tab.dataset.view;
      _page = 1;
      pane.querySelectorAll('.pool-view-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.view === _viewMode);
      });
      refresh();
    });
    $('#pool-refresh-btn').addEventListener('click', refresh);
    $('#pool-rescore-btn').addEventListener('click', async () => {
      if (typeof showToast === 'function') showToast('📊 正在重算评分...', 'info', 2000);
      try {
        const r = await fetch('/api/pool/rescore', { method: 'POST' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        if (typeof showToast === 'function') showToast(`重评分完成: 更新 ${d.updated} / 失败 ${d.failed}`, 'success', 3000);
        await refresh();
      } catch (e) {
        if (typeof showToast === 'function') showToast(`重评分失败: ${e.message}`, 'error');
      }
    });
    $('#pool-add-btn').addEventListener('click', () => {
      const f = $('.pool-add-form');
      f.style.display = f.style.display === 'none' ? 'block' : 'none';
    });
    $('#pool-add-cancel').addEventListener('click', () => {
      $('.pool-add-form').style.display = 'none';
      const sg = $('#pool-add-suggest');
      if (sg) sg.style.display = 'none';
    });
    $('#pool-add-submit').addEventListener('click', _onAddSubmit);
    // 代码联想：输入 ≥ 2 字符 debounce 250ms 查询
    let _sugTimer = null;
    $('#pool-add-symbol').addEventListener('input', (e) => {
      if (_sugTimer) clearTimeout(_sugTimer);
      _sugTimer = setTimeout(() => _querySuggest(e.target.value.trim()), 250);
    });
    $('#pool-add-symbol').addEventListener('blur', () => {
      // 150ms 延迟，允许点击下拉项完成
      setTimeout(() => {
        const sg = document.querySelector('#pool-add-suggest');
        if (sg) sg.style.display = 'none';
      }, 200);
    });
    // 市场切换时清空联想
    $('#pool-add-market').addEventListener('change', () => {
      const q = $('#pool-add-symbol').value.trim();
      if (q.length >= 2) _querySuggest(q);
    });
  }

  async function _querySuggest(q) {
    const pane = document.querySelector('.bottom-pane[data-pane="watchpool"]');
    if (!pane) return;
    const sg = pane.querySelector('#pool-add-suggest');
    if (!sg) return;
    if (q.length < 2) { sg.style.display = 'none'; return; }
    const market = pane.querySelector('#pool-add-market').value;
    try {
      const r = await fetch(`/api/symbols?market=${market}&q=${encodeURIComponent(q)}&limit=12`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const items = await r.json();
      if (!items || !items.length) {
        sg.innerHTML = `<div style="padding:10px;color:var(--text-tertiary);font-size:12px;">未找到匹配 "${_esc(q)}" 的 ${market.toUpperCase()} 股票<br><span style="font-size:10px;">(股票代码库覆盖可能有限，可直接按代码确认添加)</span></div>`;
        sg.style.display = 'block';
        return;
      }
      sg.innerHTML = items.map(it => `
        <div class="pool-sug-item" data-symbol="${_esc(it.symbol)}" style="padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--border-secondary);font-size:12px;display:flex;justify-content:space-between;align-items:center;gap:10px;" onmouseover="this.style.background='var(--bg-tertiary)'" onmouseout="this.style.background=''">
          <div style="flex:1;min-width:0;">
            <span style="font-weight:600;color:var(--color-accent);">${_esc(it.symbol)}</span>
            <span style="margin-left:8px;color:var(--text-primary);">${_esc(it.name || '-')}</span>
          </div>
          <span style="color:var(--text-tertiary);font-size:10px;">${_esc(it.exchange || it.market?.toUpperCase() || '')}</span>
        </div>
      `).join('');
      sg.style.display = 'block';
      // 改用事件委托，避免每次 innerHTML 替换后重复绑定 mousedown 累积内存泄漏
      if (!sg.dataset.delegated) {
        sg.dataset.delegated = '1';
        sg.addEventListener('mousedown', (e) => {
          const el = e.target.closest('.pool-sug-item');
          if (!el) return;
          // mousedown 在 blur 前触发，保证能选中
          e.preventDefault();
          pane.querySelector('#pool-add-symbol').value = el.dataset.symbol;
          sg.style.display = 'none';
        });
      }
    } catch (e) {
      sg.innerHTML = `<div style="padding:10px;color:var(--color-down);font-size:12px;">查询失败: ${_esc(e.message)}</div>`;
      sg.style.display = 'block';
    }
  }

  async function _onAddSubmit() {
    const pane = document.querySelector('.bottom-pane[data-pane="watchpool"]');
    if (!pane) return;
    const symbol = pane.querySelector('#pool-add-symbol').value.trim().toUpperCase();
    const market = pane.querySelector('#pool-add-market').value;
    const reason = pane.querySelector('#pool-add-reason').value.trim();
    if (!symbol) {
      if (typeof showToast === 'function') showToast('请输入股票代码', 'warning');
      return;
    }
    // 禁用按钮防重复点
    const btn = pane.querySelector('#pool-add-submit');
    if (btn) { btn.disabled = true; btn.textContent = '添加中...'; }
    try {
      const resp = await fetch('/api/pool', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, market, reason: reason || '手动添加', score: 50 }),
      });
      const d = await resp.json();
      if (!resp.ok) {
        if (typeof showToast === 'function') showToast(d.detail || '添加失败', 'error', 4000);
        return;
      }
      pane.querySelector('#pool-add-symbol').value = '';
      pane.querySelector('#pool-add-reason').value = '';
      pane.querySelector('.pool-add-form').style.display = 'none';
      const sg = pane.querySelector('#pool-add-suggest');
      if (sg) sg.style.display = 'none';

      // 用后端返回的标准化 symbol（如 "700" → "0700.HK"）
      const finalSymbol = d.symbol || symbol;
      if (d.normalized && typeof showToast === 'function') {
        showToast(`代码已标准化为 ${finalSymbol}`, 'info', 2500);
      }
      // 成功 modal：根据 state 展示不同内容
      _showAddSuccessModal(finalSymbol, market, d.id, d.state || 'new', d.message, d.existing_source);
      // 刷新并高亮新股票
      await refresh();
      _highlightNewItem(d.id);
    } catch (e) {
      console.error('[WatchPool] 添加失败', e);
      if (typeof showToast === 'function') showToast(`网络异常: ${e.message}`, 'error', 4000);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '确认添加'; }
    }
  }

  async function _showAddSuccessModal(symbol, market, itemId, state, message, existingSource) {
    // 延迟 1.2s，等后端异步绑策略 + 基本面
    setTimeout(async () => {
      let stockName = '-';
      let bindCnt = 0;
      try {
        const r = await fetch(`/api/pool/${itemId}/monitoring`);
        if (r.ok) {
          const d = await r.json();
          bindCnt = d.bindings_count || 0;
        }
        // 直接用轻 API /api/symbols，避免拉 500 条候选池（581KB）只为取一个股票名
        try {
          const rs = await fetch(`/api/symbols?market=${market}&q=${symbol}&limit=3`);
          if (rs.ok) {
            const arr = await rs.json();
            const hit = (arr || []).find(x => x.symbol === symbol);
            if (hit && hit.name) stockName = hit.name;
          }
        } catch {}
      } catch {}

      let overlay = document.getElementById('pool-add-success-modal');
      if (overlay) overlay.remove();
      overlay = document.createElement('div');
      overlay.id = 'pool-add-success-modal';
      overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center;';
      overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
      const mktLabel = { us: '美股', hk: '港股', cn: 'A股' }[market] || market.toUpperCase();
      const SRC_LABEL = { news:'新闻驱动', news_ai:'AI 解读', anomaly:'异动榜', macro_theme:'宏观主题', manual:'手动' };

      // 四种状态的差异化展示
      let title, icon, borderColor, bodyBlock;
      if (state === 'upgraded_to_manual') {
        title = '已升级为手动添加';
        icon = '⬆️';
        borderColor = 'var(--color-up)';
        bodyBlock = `
          <div style="background:rgba(66,175,90,0.12);padding:10px 12px;border-radius:6px;margin-bottom:12px;border-left:3px solid var(--color-up);">
            <div style="color:var(--color-up);font-weight:500;">${_esc(message || '升级为手动添加')}</div>
          </div>
          <div style="font-size:12px;line-height:1.8;">
            <div>✅ 来源：<strong>${_esc(SRC_LABEL[existingSource] || existingSource)}</strong> → <strong style="color:var(--color-up);">手动添加</strong></div>
            <div>✅ 免自动淘汰（不再因低分或无新闻被清出候选池）</div>
            <div>✅ 保留原评分 + AI 诊断 + 策略绑定 (<strong>${bindCnt}</strong> 个)</div>
            <div>✅ 策略信号继续监控</div>
          </div>`;
      } else if (state === 'already_exists') {
        title = '股票已在候选池中';
        icon = 'ℹ️';
        borderColor = 'var(--color-warning)';
        bodyBlock = `
          <div style="background:rgba(255,180,50,0.1);padding:10px 12px;border-radius:6px;margin-bottom:12px;border-left:3px solid var(--color-warning);">
            <div style="color:var(--color-warning);font-weight:500;">${_esc(message || '该股票已在池中')}</div>
            <div style="color:var(--text-secondary);font-size:11px;margin-top:4px;">
              当前状态：<strong>手动添加</strong>（免淘汰）· 系统已更新入池时间，无需重复添加。
            </div>
          </div>
          <div style="font-size:12px;line-height:1.8;color:var(--text-secondary);">
            ℹ️ 策略绑定已存在（共 <strong>${bindCnt}</strong> 个），继续监控中
            <br>ℹ️ 可在列表中点击 🩺 查看 AI 诊断，或点 📊 看监控状态
          </div>`;
      } else if (state === 'revived') {
        title = '已从归档恢复并升级为手动';
        icon = '↩';
        borderColor = 'var(--color-accent)';
        bodyBlock = `
          <div style="background:rgba(66,165,245,0.1);padding:10px 12px;border-radius:6px;margin-bottom:12px;border-left:3px solid var(--color-accent);">
            <div style="color:var(--color-accent);font-weight:500;">${_esc(message || '从归档恢复')}</div>
          </div>
          <div style="font-size:12px;line-height:1.8;">
            <div>✅ 状态从"已归档"恢复为"候选中"</div>
            <div>✅ 来源升级为 <strong style="color:var(--color-up);">手动添加</strong>（免自动淘汰）</div>
            <div>✅ 策略绑定 <strong style="color:var(--color-up);">${bindCnt}</strong> 个，继续监控</div>
            <div>⏳ AI 诊断将在下一轮刷新</div>
          </div>`;
      } else {
        title = '成功添加到候选池';
        icon = '✅';
        borderColor = 'var(--color-up)';
        bodyBlock = `
          <div style="font-size:12px;line-height:1.8;">
            <div>✅ 已入候选池（🟢 免自动淘汰）</div>
            <div>✅ 已绑定 <strong style="color:var(--color-up);">${bindCnt}</strong> 个策略（均线 / 布林带 / 成交量）× 1D 周期</div>
            <div>✅ 已启动基本面采集（异步，稍后可见）</div>
            <div>⏳ AI 全面诊断进行中（通常 30-60 秒，点 🩺 查看）</div>
          </div>`;
      }

      overlay.innerHTML = `
        <div style="background:var(--bg-secondary);border-radius:8px;width:min(460px,90vw);padding:18px 22px;box-shadow:0 8px 32px rgba(0,0,0,0.4);border-top:4px solid ${borderColor};">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;">
            <span style="font-size:22px;">${icon}</span>
            <span style="font-weight:600;font-size:15px;">${title}</span>
          </div>
          <div style="background:var(--bg-tertiary);padding:10px 14px;border-radius:6px;margin-bottom:12px;">
            <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
              <span style="color:var(--text-tertiary);">品种</span>
              <span style="font-weight:600;color:var(--color-accent);">${_esc(symbol)}</span>
            </div>
            <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
              <span style="color:var(--text-tertiary);">名称</span>
              <span>${_esc(stockName)}</span>
            </div>
            <div style="display:flex;justify-content:space-between;">
              <span style="color:var(--text-tertiary);">市场</span>
              <span>${mktLabel}</span>
            </div>
          </div>
          ${bodyBlock}
          <div style="text-align:right;margin-top:14px;">
            <button id="pool-add-ok" class="btn btn-primary btn-sm">知道了</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      overlay.querySelector('#pool-add-ok').addEventListener('click', () => overlay.remove());
    }, 1200);
  }

  function _highlightNewItem(itemId) {
    // 找到该行 DOM 并高亮 3 秒
    setTimeout(() => {
      const row = document.querySelector(`.bottom-pane[data-pane="watchpool"] [data-row-id="${CSS.escape(itemId)}"]`);
      if (!row) return;
      row.style.transition = 'background 0.4s';
      row.style.background = 'rgba(30,150,90,0.25)';
      row.scrollIntoView({ behavior: 'smooth', block: 'center' });
      setTimeout(() => { row.style.background = ''; }, 3500);
    }, 200);
  }

  async function refresh() {
    try {
      // v12.20.14: limit 300 → 1500
      // 候选池实际规模 750+ (美 251 / A 361 / 港 138 等), 300 截断导致用户看到的不全
      // 1500 容纳全市场上限 (650+600+200=1450 + buffer)
      const url = _viewMode === 'archived'
        ? '/api/pool?limit=1500&status=archived'
        : '/api/pool?limit=1500';
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = await resp.json();
      _items = d.items || [];
      render();
      const statusEl = document.querySelector('.bottom-pane[data-pane="watchpool"] #pool-status');
      if (statusEl) {
        const label = _viewMode === 'archived' ? '已淘汰' : '在池中';
        // v12.20.13: 按市场分别显示,不再显示无意义的总量 (单市场会占绝大多数)
        // 上限按市场差异化: 美 650 / A 600 / 港 200
        const CAPS = { us: 650, cn: 600, hk: 200 };
        const NAMES = { us: '🇺🇸美', cn: '🇨🇳A', hk: '🇭🇰港' };
        const byMarket = _items.reduce((acc, it) => {
          const m = it.market || 'unknown';
          acc[m] = (acc[m] || 0) + 1;
          return acc;
        }, {});
        const parts = ['us', 'cn', 'hk'].map(m => {
          const n = byMarket[m] || 0;
          const cap = CAPS[m];
          // 占用率高于 80% 标黄,90% 标红
          const ratio = n / cap;
          const color = ratio >= 0.9 ? 'var(--color-down)' : ratio >= 0.8 ? 'var(--color-warning)' : '';
          const colorStr = color ? `color:${color};` : '';
          return `<span style="${colorStr}">${NAMES[m]} ${n}/${cap}</span>`;
        }).join(' · ');
        statusEl.innerHTML = `${label}: ${parts} · ${new Date().toLocaleTimeString()}`;
      }
    } catch (e) {
      console.warn('[WatchPool] 刷新失败:', e);
    }
  }

  function _parseRating(item) {
    if (!item.ai_diagnosis) return null;
    try {
      const d = typeof item.ai_diagnosis === 'string' ? JSON.parse(item.ai_diagnosis) : item.ai_diagnosis;
      return d.rating || null;
    } catch { return null; }
  }

  function render() {
    const listEl = document.querySelector('.bottom-pane[data-pane="watchpool"] .pool-list');
    if (!listEl) return;
    let visible = _items;
    if (_filterMarket !== 'all') visible = visible.filter((it) => it.market === _filterMarket);
    if (_filterDiag === 'diagnosed')   visible = visible.filter((it) => it.ai_diagnosed_at);
    if (_filterDiag === 'undiagnosed') visible = visible.filter((it) => !it.ai_diagnosed_at);
    if (_filterRating !== 'all') visible = visible.filter((it) => _parseRating(it) === _filterRating);
    if (!visible.length) {
      listEl.innerHTML = `
        <div class="oc-empty">
          <div class="oc-empty-icon">📊</div>
          <div class="oc-empty-title">候选池为空</div>
          <div class="oc-empty-hint">高分新闻自动入池 · 异动榜自动扫入 · 或手动添加</div>
        </div>`;
      return;
    }

    // 排序：manual 永远置顶（用户特别关注），其余按当前排序字段
    const dir = _sortDir === 'desc' ? -1 : 1;
    visible = [...visible].sort((a, b) => {
      // manual 置顶
      const aM = a.source === 'manual' ? 0 : 1;
      const bM = b.source === 'manual' ? 0 : 1;
      if (aM !== bM) return aM - bM;
      let av = a[_sortKey], bv = b[_sortKey];
      if (typeof av === 'string') return av.localeCompare(bv) * dir;
      return ((av || 0) - (bv || 0)) * dir;
    });

    // 分页
    const total = visible.length;
    const totalPages = Math.max(1, Math.ceil(total / _pageSize));
    if (_page > totalPages) _page = totalPages;
    const start = (_page - 1) * _pageSize;
    const pageItems = visible.slice(start, start + _pageSize);

    // 异步补齐本页 symbol 名称，完成后重渲染一次（避免无限循环：只在有缺失时重渲染）
    _fetchMissingNames(pageItems).then(hasUpdate => {
      if (hasUpdate) {
        // 重渲染但保持页码/滚动
        render();
      }
    }).catch(() => {});

    const sortIndicator = (k) => _sortKey === k ? (_sortDir === 'desc' ? ' ▼' : ' ▲') : '';

    listEl.innerHTML = `
      <table class="oc-table oc-table-compact">
        <thead>
          <tr>
            <th style="cursor:pointer;" data-sort="symbol">品种${sortIndicator('symbol')}</th>
            <th class="oc-col-num" style="cursor:pointer;" data-sort="score" title="总分 = 事件分(0-50) + 技术分(0-30) + 基本面分(0-20)">评分${sortIndicator('score')}</th>
            <th class="oc-col-num" title="事件分 / 技术分 / 基本面分">明细</th>
            <th class="oc-col-center" title="AI 综合诊断评级">AI 评级</th>
            <th style="cursor:pointer;" data-sort="source">来源${sortIndicator('source')}</th>
            <th>理由</th>
            <th style="cursor:pointer;" data-sort="added_at">添加时间${sortIndicator('added_at')}</th>
            <th class="oc-col-center">操作</th>
          </tr>
        </thead>
        <tbody>
          ${pageItems.map(_renderRow).join('')}
        </tbody>
      </table>
    `;

    // 追加分页控件 + 信息条
    const pageBar = document.createElement('div');
    pageBar.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-top:1px solid var(--border-secondary);font-size:12px;color:var(--text-secondary);';
    pageBar.innerHTML = `
      <span>共 ${total} 只 · 第 ${_page}/${totalPages} 页 · 显示 ${start + 1}-${Math.min(start + _pageSize, total)}</span>
      <div style="display:flex;gap:6px;">
        <button class="btn btn-sm" id="pool-page-first" ${_page === 1 ? 'disabled' : ''} style="padding:2px 8px;font-size:11px;">«</button>
        <button class="btn btn-sm" id="pool-page-prev" ${_page === 1 ? 'disabled' : ''} style="padding:2px 8px;font-size:11px;">上一页</button>
        <button class="btn btn-sm" id="pool-page-next" ${_page === totalPages ? 'disabled' : ''} style="padding:2px 8px;font-size:11px;">下一页</button>
        <button class="btn btn-sm" id="pool-page-last" ${_page === totalPages ? 'disabled' : ''} style="padding:2px 8px;font-size:11px;">»</button>
      </div>
    `;
    listEl.appendChild(pageBar);
    pageBar.querySelector('#pool-page-first').addEventListener('click', () => { _page = 1; render(); });
    pageBar.querySelector('#pool-page-prev').addEventListener('click', () => { if (_page > 1) { _page--; render(); } });
    pageBar.querySelector('#pool-page-next').addEventListener('click', () => { if (_page < totalPages) { _page++; render(); } });
    pageBar.querySelector('#pool-page-last').addEventListener('click', () => { _page = totalPages; render(); });

    // 列头点击排序
    listEl.querySelectorAll('th[data-sort]').forEach(th => {
      th.addEventListener('click', () => {
        const k = th.dataset.sort;
        if (_sortKey === k) {
          _sortDir = _sortDir === 'desc' ? 'asc' : 'desc';
        } else {
          _sortKey = k;
          _sortDir = 'desc';
        }
        _page = 1;
        render();
      });
    });

    // 事件委托：在 listEl 上绑一次（render 多次调用时不会重复绑定）
    if (!listEl._delegated) {
      listEl._delegated = true;
      listEl.addEventListener('click', async (e) => {
        const viewBtn = e.target.closest('[data-action="view"]');
        if (viewBtn) {
          const sym = viewBtn.dataset.symbol;
          const mkt = viewBtn.dataset.market;
          try {
            if (typeof switchMarket === 'function' && mkt !== window.currentMarket) await switchMarket(mkt);
            if (typeof switchSymbol === 'function') await switchSymbol(sym, mkt);
          } catch (err) {
            console.warn('[WatchPool] 看图失败:', err);
            if (typeof showToast === 'function') showToast(`打开 ${sym} 失败`, 'error');
          }
          return;
        }
        const qaBtn = e.target.closest('[data-action="quick-action"]');
        if (qaBtn) {
          _showQuickActionMenu(qaBtn);
          return;
        }
        const monBtn = e.target.closest('[data-action="monitoring"]');
        if (monBtn) {
          _showMonitoringModal(monBtn.dataset.id, monBtn.dataset.symbol);
          return;
        }
        const diagBtn = e.target.closest('[data-action="diagnose"]');
        if (diagBtn) {
          _showDiagnosisModal(diagBtn.dataset.id, diagBtn.dataset.symbol);
          return;
        }
        const restoreBtn = e.target.closest('[data-action="restore"]');
        if (restoreBtn) {
          const id = restoreBtn.dataset.id;
          if (!confirm('恢复此股票到候选池（继续评分 + 监控）？')) return;
          try {
            const r = await fetch(`/api/pool/${id}/restore`, { method: 'POST' });
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            if (typeof showToast === 'function') showToast('已恢复', 'success');
            await refresh();
          } catch (err) {
            if (typeof showToast === 'function') showToast(`恢复失败: ${err.message}`, 'error');
          }
          return;
        }
        const removeBtn = e.target.closest('[data-action="remove"]');
        if (removeBtn) {
          const id = removeBtn.dataset.id;
          if (!confirm('从候选池移除此品种？')) return;
          try {
            await fetch(`/api/pool/${id}`, { method: 'DELETE' });
            if (typeof showToast === 'function') showToast('已移除', 'success');
            await refresh();
          } catch (err) {
            console.warn('[WatchPool] 移除失败:', err);
          }
          return;
        }
      });
    }
  }

  // 名称缓存：{ "us:NVDA": "NVIDIA Corporation", ... }
  // 异步从 /api/symbols 批量查询后注入
  const _nameCache = {};
  const _nameFetching = new Set();

  async function _fetchMissingNames(items) {
    // 找出本次渲染中缺名称的行
    const missing = [];
    for (const it of items) {
      const key = `${it.market}:${it.symbol}`;
      if (_nameCache[key] !== undefined) continue;      // 已有（可能是 '' 表示查过没结果）
      if (_nameFetching.has(key)) continue;             // 正在查
      const nameFromCacheOrReason = _lookupName(it.symbol, it.market, it.reason);
      if (nameFromCacheOrReason) {
        _nameCache[key] = nameFromCacheOrReason;
        continue;
      }
      missing.push(it);
      _nameFetching.add(key);
    }
    if (!missing.length) return false;

    await Promise.all(missing.map(async (it) => {
      const key = `${it.market}:${it.symbol}`;
      try {
        const r = await fetch(`/api/symbols?market=${it.market}&q=${encodeURIComponent(it.symbol)}&limit=3`);
        if (!r.ok) { _nameCache[key] = ''; return; }
        const arr = await r.json();
        const hit = (arr || []).find(s => s.symbol === it.symbol || (s.symbol || '').toUpperCase() === it.symbol.toUpperCase());
        _nameCache[key] = (hit && hit.name) ? hit.name : '';
      } catch {
        _nameCache[key] = '';
      } finally {
        _nameFetching.delete(key);
      }
    }));
    return true;
  }

  const STATUS_LABEL = {
    candidate: '候选',
    monitoring: '监控中',
    archived: '已归档',
  };
  const SOURCE_LABEL = {
    news: '新闻',
    news_ai: 'AI 解读',
    anomaly: '涨幅榜',
    macro_theme: '宏观主题',
    manual: '📌 手动',  // 视觉区分：免淘汰 + 全套监控
  };
  const MARKET_LABEL = { us: '美股', hk: '港股', cn: 'A股' };

  function _lookupName(symbol, market, reason) {
    // 0) 异步填充缓存优先
    const key = `${market}:${symbol}`;
    if (_nameCache[key]) return _nameCache[key];
    // 1) symbolsCache 精确匹配
    try {
      const list = (window.symbolsCache && window.symbolsCache[market]) || [];
      const hit = list.find((s) => s.symbol === symbol);
      if (hit && hit.name) return hit.name;
      // 去掉 .HK / 前导零再尝试一次
      const bare = symbol.replace(/\.HK$/i, '').replace(/^0+/, '');
      const hit2 = list.find((s) => {
        const sb = (s.symbol || '').replace(/\.HK$/i, '').replace(/^0+/, '');
        return sb === bare;
      });
      if (hit2 && hit2.name) return hit2.name;
    } catch {}
    // 2) 兜底：从 reason 字符串解析（涨幅榜 / 新闻 格式：... | 名称 +x.x%... 或 ...: 名称 ...）
    if (reason) {
      // 涨幅榜 #N | 名称 +x.x% 或 -x.x%
      const m1 = reason.match(/\|\s*([^\s|+-][^|]*?)\s*[+\-]\d/);
      if (m1) return m1[1].trim();
      // 通用：取第一个中文段（2-8 字）
      const m2 = reason.match(/[\u4e00-\u9fa5]{2,10}/);
      if (m2) return m2[0];
    }
    return '';
  }

  function _renderRow(item) {
    const MARKET_FLAG = { us: '🇺🇸', hk: '🇭🇰', cn: '🇨🇳', crypto: '🪙' };
    const time = item.added_at
      ? new Date(item.added_at * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
      : '-';
    const SOURCE_CHIP_CLASS = {
      news:        'oc-chip-info',
      news_ai:     'oc-chip-purple',
      anomaly:     'oc-chip-warn',
      macro_theme: 'oc-chip-purple',
      manual:      'oc-chip-neutral',
    };
    const sourceChip = `<span class="oc-chip ${SOURCE_CHIP_CLASS[item.source] || 'oc-chip-neutral'}">${SOURCE_LABEL[item.source] || item.source}</span>`;
    const name = (item.stock_name && item.stock_name.trim()) || _lookupName(item.symbol, item.market, item.reason);
    // 理由分类显示
    const parts = [];
    if (item.reason_anomaly) parts.push(`📈 涨幅榜: ${item.reason_anomaly}`);
    if (item.reason_news)    parts.push(`📰 新闻: ${item.reason_news}`);
    if (item.reason_ai)      parts.push(`🤖 AI: ${item.reason_ai}`);
    const reasonText = parts.length ? parts.join(' / ') : (item.reason || '-');
    const reasonTooltip = parts.length ? parts.join('\n') : (item.reason || '');
    const isArchived = item.status === 'archived';
    const archivedReason = item.archived_reason || '-';
    const reasonCol = isArchived
      ? `<span class="oc-warn" title="淘汰原因">⛔ ${_esc(archivedReason)}</span>`
      : reasonText;
    const reasonTitleAttr = isArchived ? `淘汰原因: ${archivedReason}` : reasonTooltip;

    // 评级 chip
    const rating = _parseRating(item);
    const RATING_CHIP_CLASS = {
      strong_buy: 'oc-chip-up', buy: 'oc-chip-up',
      hold: 'oc-chip-neutral',
      reduce: 'oc-chip-warn', sell: 'oc-chip-down',
    };
    const ratingChip = rating
      ? `<span class="oc-chip ${RATING_CHIP_CLASS[rating] || 'oc-chip-neutral'}">${(RATING_LABEL[rating] || {}).label || rating}</span>`
      : '<span class="oc-text-xs oc-muted">未诊断</span>';
    const quickActionBtn = (rating && rating !== 'hold')
      ? `<button class="btn btn-sm" data-action="quick-action" data-id="${item.id}" data-symbol="${item.symbol}" data-market="${item.market}" data-rating="${rating}" title="根据评级弹出快捷操作" style="font-size:10px;padding:1px 4px;margin-left:3px;background:transparent;border:1px solid var(--color-purple);color:var(--color-purple);border-radius:3px;cursor:pointer;">⚡</button>`
      : '';

    // 评分块（大字 + LED）
    const score = Math.round(item.score || 0);
    const scoreLed = score >= 80 ? 'oc-led-up' : score >= 60 ? 'oc-led-warn' : 'oc-led-down';
    const scoreCell = `<span class="${scoreLed.replace('led','led')} oc-led"></span><span class="oc-warn" style="font-weight:700;font-size:13px;">${score}</span>`;

    // 明细（彩色三段分数）
    const detailCell = `<span class="oc-text-xs"><span class="oc-accent">${(item.event_score||0).toFixed(0)}</span>+<span style="color:#4fc3f7;">${(item.technical_score||0).toFixed(0)}</span>+<span style="color:var(--color-purple);">${(item.fundamentals_score||0).toFixed(0)}</span></span>`;

    // 品种格：代码 + 名 + 市场旗
    const symbolCell = `
      <div style="display:flex;flex-direction:column;gap:1px;">
        <span style="font-weight:700;font-size:12px;">${item.symbol}${name ? ` <span class="oc-text-sm oc-muted" style="font-weight:400;">${name}</span>` : ''}</span>
        <span class="oc-text-xs oc-muted">${MARKET_FLAG[item.market]||''} ${MARKET_LABEL[item.market] || item.market.toUpperCase()}</span>
      </div>`;

    // 操作
    const ops = isArchived
      ? `<button class="btn btn-sm oc-up" data-action="restore" data-id="${item.id}" style="font-size:10px;padding:2px 8px;" title="从已淘汰恢复到候选池">↩ 恢复</button>`
      : `<div style="display:inline-flex;gap:3px;">
           <button class="btn btn-sm" data-action="view" data-symbol="${item.symbol}" data-market="${item.market}" style="font-size:10px;padding:2px 6px;" title="查看 K 线">📊</button>
           <button class="btn btn-sm" data-action="monitoring" data-id="${item.id}" data-symbol="${item.symbol}" style="font-size:10px;padding:2px 6px;" title="策略 + 信号">⚙️</button>
           <button class="btn btn-sm" data-action="diagnose" data-id="${item.id}" data-symbol="${item.symbol}" style="font-size:10px;padding:2px 6px;${item.ai_diagnosed_at ? 'color:var(--color-purple);' : ''}" title="${item.ai_diagnosed_at ? 'AI 已诊断（点查看）' : 'AI 立即诊断'}">${item.ai_diagnosed_at ? '🩺' : '🩺?'}</button>
           <button class="btn btn-sm oc-down" data-action="remove" data-id="${item.id}" style="font-size:10px;padding:2px 6px;" title="移除">×</button>
         </div>`;

    return `
      <tr data-row-id="${item.id}" ${isArchived ? 'style="opacity:0.65;"' : ''}>
        <td>${symbolCell}</td>
        <td class="oc-col-num">${scoreCell}</td>
        <td class="oc-col-num oc-muted" title="事件${(item.event_score||0).toFixed(1)} / 技术${(item.technical_score||0).toFixed(1)} / 基本面${(item.fundamentals_score||0).toFixed(1)}">${detailCell}</td>
        <td class="oc-col-center" style="white-space:nowrap;">${ratingChip}${quickActionBtn}</td>
        <td>${sourceChip}</td>
        <td class="oc-muted" style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:help;" title="${_esc(reasonTitleAttr)}">${reasonCol}</td>
        <td class="oc-text-xs oc-muted">${time}</td>
        <td class="oc-col-center" style="white-space:nowrap;">${ops}</td>
      </tr>
    `;
  }

  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  const RATING_LABEL = {
    strong_buy: { label: '🟢 强烈买入', color: 'var(--color-up)' },
    buy:        { label: '🟢 买入', color: 'var(--color-up)' },
    hold:       { label: '⚪ 持有/观察', color: 'var(--text-secondary)' },
    reduce:     { label: '🟡 减仓', color: 'var(--color-warning)' },
    sell:       { label: '🔴 卖出', color: 'var(--color-down)' },
  };

  async function _runDiagnose(itemId, body) {
    if (_diagController) _diagController.abort();
    _diagController = new AbortController();
    body.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-tertiary);"><div style="font-size:24px;margin-bottom:8px;">⏳</div>AI 诊断中（LLM 调用通常 30-60s）...<br><span style="font-size:11px;">关闭弹窗会取消请求</span></div>';
    try {
      const r = await fetch(`/api/pool/${itemId}/diagnose`, { method: 'POST', signal: _diagController.signal });
      if (!r.ok) {
        const det = await r.json().catch(() => ({}));
        throw new Error(det.detail || `HTTP ${r.status}`);
      }
      const d = await r.json();
      body.innerHTML = _renderDiagBody(d.diagnosis, d.diagnosed_at, d.symbol, d.market);
      _bindDiagActions(body, d.symbol, d.market, d.diagnosis);
      _attachHistoryFetch(body, itemId);
      if (typeof Watchpool !== 'undefined') Watchpool.refresh && Watchpool.refresh();
    } catch (e) {
      if (e.name === 'AbortError') return;  // 用户主动关闭
      body.innerHTML = `<div style="color:var(--color-down);padding:20px;">诊断失败: ${_esc(e.message)}</div>`;
    } finally {
      _diagController = null;
    }
  }

  async function _showMonitoringModal(itemId, symbol) {
    let overlay = document.getElementById('pool-monitoring-modal');
    if (overlay) overlay.remove();
    overlay = document.createElement('div');
    overlay.id = 'pool-monitoring-modal';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
    overlay.innerHTML = `
      <div style="background:var(--bg-secondary);border-radius:8px;width:min(760px,94vw);max-height:85vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.4);">
        <div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border-secondary);">
          <span style="font-weight:600;font-size:14px;">📊 ${_esc(symbol)} 监控链路</span>
          <button class="btn btn-sm mon-close" style="font-size:14px;padding:2px 10px;">×</button>
        </div>
        <div id="pool-mon-body" style="overflow-y:auto;flex:1;padding:14px 18px;font-size:12px;line-height:1.6;">
          <div style="text-align:center;color:var(--text-tertiary);padding:40px;">⏳ 加载中...</div>
        </div>
      </div>`;
    overlay.querySelector('.mon-close').addEventListener('click', () => overlay.remove());
    try {
      const r = await fetch(`/api/pool/${itemId}/monitoring`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      const body = overlay.querySelector('#pool-mon-body');
      const ratingMap = { strong_buy:'🟢 强烈买入', buy:'🟢 买入', hold:'⚪ 持有观察', reduce:'🟡 减仓', sell:'🔴 卖出' };
      const STRATEGY_ZH = {
        ma_cross: '均线金叉死叉',
        bollinger_reversion: '布林带均值回归',
        volume_breakout: '成交量突破',
        rsi_divergence: 'RSI 背离',
        donchian_breakout: '唐奇安通道突破',
        flash_event: '新闻事件驱动',
      };
      const SOURCE_ZH = {
        manual: '📌 手动添加',
        news: '📰 新闻驱动',
        news_ai: '🤖 AI 新闻解读',
        anomaly: '📈 异动榜',
        macro_theme: '🌐 宏观主题',
      };
      const STATUS_ZH = {
        candidate: '候选中',
        monitoring: '监控中',
        archived: '已归档',
      };
      const ACTION_ZH = { buy: '买入', sell: '卖出' };
      const VERDICT_ZH = { confirm: '✅ AI 确认', warn: '⚠️ AI 警告', reject: '❌ AI 否决' };
      const ts = d.ai_diagnosed_at ? new Date(d.ai_diagnosed_at * 1000).toLocaleString() : '尚未诊断';
      const bRows = d.bindings.map(b =>
        `<li>${_esc(STRATEGY_ZH[b.strategy_name] || b.strategy_name)} × ${_esc(b.interval)} 周期 ${b.enabled ? '✅ 已启用' : '❌ 已停用'}</li>`
      ).join('');
      const sRows = d.recent_signals.map(s => {
        const t = new Date(s.generated_at).toLocaleString('zh-CN', { month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit' });
        const actColor = s.action === 'buy' ? 'var(--color-up)' : 'var(--color-down)';
        const verdictText = VERDICT_ZH[s.ai_verdict] || '';
        const strategyZh = STRATEGY_ZH[s.strategy_name] || s.strategy_name;
        return `<li style="margin:2px 0;"><span style="color:var(--text-tertiary);">${t}</span> <span style="color:${actColor};font-weight:600;">${ACTION_ZH[s.action] || s.action}</span> ${_esc(strategyZh)} @ ${(s.price||0).toFixed(2)} 置信度 ${s.confidence} ${verdictText}</li>`;
      }).join('');
      body.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px 16px;margin-bottom:12px;">
          <div><span style="color:var(--text-tertiary);">入池来源：</span>${_esc(SOURCE_ZH[d.source] || d.source)}</div>
          <div><span style="color:var(--text-tertiary);">综合评分：</span><strong>${d.score}</strong></div>
          <div><span style="color:var(--text-tertiary);">当前状态：</span>${_esc(STATUS_ZH[d.status] || d.status)}</div>
        </div>
        <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:10px;">
          <div><strong>🩺 AI 诊断：</strong>${ratingMap[d.ai_rating] || (d.ai_rating ? _esc(d.ai_rating) : '<span style="color:var(--text-tertiary);">尚未诊断</span>')} <span style="color:var(--text-tertiary);font-size:11px;margin-left:8px;">${ts}</span></div>
        </div>
        <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:10px;">
          <strong>🎯 已绑定策略（共 ${d.bindings_count} 条）</strong>
          ${bRows ? `<ul style="margin:6px 0 0 18px;padding:0;">${bRows}</ul>` : '<div style="color:var(--color-warning);margin-top:4px;">⚠️ 暂未绑定任何策略（评分 < 40 且非手动/新闻/宏观来源）</div>'}
        </div>
        <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;">
          <strong>📡 最近 ${d.signals_count} 条信号</strong>
          ${sRows ? `<ul style="margin:6px 0 0 18px;padding:0;">${sRows}</ul>` : '<div style="color:var(--text-tertiary);margin-top:4px;">暂无信号触发</div>'}
        </div>
      `;
    } catch (e) {
      overlay.querySelector('#pool-mon-body').innerHTML = `<div style="color:var(--color-down);padding:20px;">加载失败: ${_esc(e.message)}</div>`;
    }
  }

  async function _showDiagnosisModal(itemId, symbol) {
    let overlay = document.getElementById('pool-diagnosis-modal');
    if (overlay) overlay.remove();
    overlay = document.createElement('div');
    overlay.id = 'pool-diagnosis-modal';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    document.body.appendChild(overlay);

    const closeOverlay = () => {
      if (_diagController) { try { _diagController.abort(); } catch {} }
      _diagController = null;
      overlay.remove();
    };
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeOverlay(); });

    overlay.innerHTML = `
      <div style="background:var(--bg-secondary);border-radius:8px;width:min(760px,94vw);max-height:90vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.4);">
        <div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border-secondary);">
          <span style="font-weight:600;font-size:14px;">🩺 AI 诊断 — ${_esc(symbol)}</span>
          <div style="display:flex;gap:8px;">
            <button id="pool-diag-history" class="btn btn-sm" title="查看历史诊断对比">📜 历史</button>
            <button id="pool-diag-refresh" class="btn btn-sm" title="强制重新调用 LLM 诊断（耗时 30-60s）">🔄 重诊断</button>
            <button id="pool-diag-close" class="btn btn-sm" style="font-size:14px;padding:2px 10px;">×</button>
          </div>
        </div>
        <div style="overflow-y:auto;flex:1;padding:14px 18px;font-size:12px;line-height:1.6;" id="pool-diag-body"></div>
      </div>`;
    const body = overlay.querySelector('#pool-diag-body');
    overlay.querySelector('#pool-diag-close').addEventListener('click', closeOverlay);
    overlay.querySelector('#pool-diag-refresh').addEventListener('click', () => _runDiagnose(itemId, body));
    overlay.querySelector('#pool-diag-history').addEventListener('click', () => _showHistory(itemId, body));

    body.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-tertiary);">⏳ 加载中...</div>';
    try {
      const r = await fetch(`/api/pool/${itemId}/diagnosis`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      if (d.status === 'pending' || !d.diagnosis) {
        // 未诊断 → 直接自动触发，不再让用户多点一步
        await _runDiagnose(itemId, body);
        return;
      }
      body.innerHTML = _renderDiagBody(d.diagnosis, d.diagnosed_at, d.symbol, d.market);
      _bindDiagActions(body, d.symbol, d.market, d.diagnosis);
      _attachHistoryFetch(body, itemId);
    } catch (e) {
      body.innerHTML = `<div style="color:var(--color-down);padding:20px;">加载失败: ${_esc(e.message)}</div>`;
    }
  }

  async function _showHistory(itemId, body) {
    body.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-tertiary);">⏳ 加载历史...</div>';
    try {
      const r = await fetch(`/api/pool/${itemId}/diagnosis-history?limit=10`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      if (!d.history || !d.history.length) {
        body.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-tertiary);">尚无诊断历史</div>';
        return;
      }
      const rows = d.history.map((h, idx) => {
        const r = RATING_LABEL[h.rating] || { label: h.rating || '-', color: 'var(--text-secondary)' };
        const ts = h.diagnosed_at ? new Date(h.diagnosed_at * 1000).toLocaleString() : '-';
        const summary = (h.diagnosis || {}).summary || '';
        const change = idx + 1 < d.history.length
          ? _diffArrow(h.rating, d.history[idx + 1].rating)
          : '';
        return `
          <div style="padding:10px 12px;border-bottom:1px solid var(--border-secondary);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
              <span style="color:${r.color};font-weight:600;">${r.label} ${h.confidence || 0} ${change}</span>
              <span style="font-size:11px;color:var(--text-tertiary);">${ts}</span>
            </div>
            <div style="color:var(--text-secondary);">${_esc(summary)}</div>
          </div>`;
      }).join('');
      body.innerHTML = `<div style="font-size:12px;color:var(--text-tertiary);margin-bottom:8px;">📜 共 ${d.history.length} 次诊断（按时间倒序，箭头表示与上一次对比）</div>${rows}`;
    } catch (e) {
      body.innerHTML = `<div style="color:var(--color-down);padding:20px;">加载历史失败: ${_esc(e.message)}</div>`;
    }
  }

  function _diffArrow(curr, prev) {
    if (!curr || !prev) return '';
    const order = { sell: 0, reduce: 1, hold: 2, buy: 3, strong_buy: 4 };
    const c = order[curr], p = order[prev];
    if (c == null || p == null || c === p) return '';
    return c > p
      ? `<span style="color:var(--color-up);font-size:11px;margin-left:4px;">↑ 较上次提升</span>`
      : `<span style="color:var(--color-down);font-size:11px;margin-left:4px;">↓ 较上次降级</span>`;
  }

  function _attachHistoryFetch(body, itemId) {
    // 在最新诊断 body 末尾静默加载一次"上次诊断 vs 这次"diff（如果有）
    fetch(`/api/pool/${itemId}/diagnosis-history?limit=2`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (!d || !d.history || d.history.length < 2) return;
        const curr = d.history[0], prev = d.history[1];
        const diff = _diffArrow(curr.rating, prev.rating);
        if (!diff) return;
        const banner = document.createElement('div');
        banner.style.cssText = 'background:var(--bg-tertiary);padding:8px 12px;border-radius:6px;margin-bottom:12px;font-size:12px;border-left:3px solid var(--color-purple);';
        banner.innerHTML = `📜 上次诊断 ${new Date(prev.diagnosed_at * 1000).toLocaleDateString()}: ${(RATING_LABEL[prev.rating] || {}).label || prev.rating} → 现在: ${(RATING_LABEL[curr.rating] || {}).label || curr.rating} ${diff}`;
        body.insertBefore(banner, body.firstChild);
      })
      .catch(() => {});
  }

  function _renderDiagBody(diag, diagnosedAt, symbol, market) {
    if (!diag) return '<div style="padding:20px;color:var(--text-tertiary);">无诊断数据</div>';
    const r = RATING_LABEL[diag.rating] || { label: diag.rating || '-', color: 'var(--text-secondary)' };
    const ts = diagnosedAt ? new Date(diagnosedAt * 1000).toLocaleString() : '-';
    const sym = symbol || '';
    const mkt = market || '';
    const list = (arr, color) => Array.isArray(arr) && arr.length
      ? `<ul style="margin:4px 0 0 0;padding-left:18px;color:${color || 'var(--text-secondary)'};">` +
        arr.map(x => `<li>${_esc(x)}</li>`).join('') + '</ul>'
      : '<div style="color:var(--text-tertiary);font-size:11px;">（无）</div>';
    const kl = diag.key_levels || {};
    const horizon = { '1-5d': '短线 (1-5 天)', '1-3m': '中线 (1-3 月)', '3-12m': '长线 (3-12 月)' }[diag.horizon] || diag.horizon || '-';
    return `
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--border-secondary);">
        <div>
          <span style="font-size:18px;font-weight:600;color:${r.color};">${r.label}</span>
          <span style="margin-left:12px;color:var(--text-tertiary);">置信度 ${diag.confidence || 0} · ${horizon}</span>
        </div>
        <span style="font-size:11px;color:var(--text-tertiary);">${ts}</span>
      </div>
      <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:12px;font-weight:600;">
        ${_esc(diag.summary || '-')}
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;">
        <div>
          <div style="color:var(--color-up);font-weight:600;margin-bottom:4px;">✅ 优势</div>
          ${list(diag.strengths, 'var(--text-secondary)')}
        </div>
        <div>
          <div style="color:var(--color-down);font-weight:600;margin-bottom:4px;">⚠️ 风险</div>
          ${list(diag.risks, 'var(--text-secondary)')}
        </div>
      </div>
      <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:12px;">
        <div style="margin-bottom:4px;"><span style="color:var(--text-tertiary);">📈 技术：</span>${_esc(diag.technical_view || '-')}</div>
        <div style="margin-bottom:4px;"><span style="color:var(--text-tertiary);">💼 基本面：</span>${_esc(diag.fundamental_view || '-')}</div>
        <div><span style="color:var(--text-tertiary);">📰 新闻：</span>${_esc(diag.news_view || '-')}</div>
      </div>
      <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:12px;">
        <div style="color:var(--text-tertiary);margin-bottom:4px;">🎯 关键位</div>
        <div>支撑 ${kl.support ?? '-'} · 阻力 ${kl.resistance ?? '-'} · 止损 ${kl.stop_loss ?? '-'}</div>
      </div>
      <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;border-left:3px solid var(--color-purple);margin-bottom:12px;">
        <div style="color:var(--text-tertiary);margin-bottom:4px;">🚦 下一步动作</div>
        <div style="font-weight:500;">${_esc(diag.next_action || '-')}</div>
      </div>
      ${_renderDiagActionsHTML(diag, sym, mkt)}`;
  }

  /* ============================================================
     诊断结果一键行动区：根据评级+关键位给出可执行按钮
     ============================================================ */
  function _renderDiagActionsHTML(diag, symbol, market) {
    if (!symbol || !market) return '';
    const kl = diag.key_levels || {};
    const ops = diag.operations || {};
    const rating = diag.rating || 'hold';
    const isBullish = rating === 'strong_buy' || rating === 'buy';
    const isBearish = rating === 'sell' || rating === 'reduce';

    const btns = [];
    // 阻力警报（多头评级用）
    if (kl.resistance != null && (isBullish || rating === 'hold')) {
      btns.push({
        cls: 'alert-resistance', color: 'var(--color-up)',
        icon: '🔔', label: `设阻力警报 ${kl.resistance}`,
        title: '价格突破阻力位时触发警报（用于确认突破后跟进）',
        data: { symbol, market, price: kl.resistance, condition: 'price_gte', note: '突破阻力' },
      });
    }
    // 支撑/止损警报（熊评级或任何评级都可用）
    if (kl.stop_loss != null) {
      btns.push({
        cls: 'alert-sl', color: 'var(--color-down)',
        icon: '⛔', label: `设止损警报 ${kl.stop_loss}`,
        title: '价格跌破止损位时触发警报（避免亏损扩大）',
        data: { symbol, market, price: kl.stop_loss, condition: 'price_lte', note: '跌破止损' },
      });
    } else if (kl.support != null && isBearish) {
      btns.push({
        cls: 'alert-support', color: 'var(--color-warning)',
        icon: '⚠️', label: `设支撑破位警报 ${kl.support}`,
        title: '价格跌破支撑位触发警报',
        data: { symbol, market, price: kl.support, condition: 'price_lte', note: '破位' },
      });
    }
    // 加入持仓（多头评级）
    if (isBullish) {
      btns.push({
        cls: 'add-position', color: 'var(--color-up)',
        icon: '💼', label: '加入持仓',
        title: '记录为已持仓（可配合 AI SL/TP 自动管理）',
        data: { symbol, market, sl: kl.stop_loss, tp: kl.resistance },
      });
    }
    // 模拟下单（多头评级）
    if (isBullish && market === 'crypto') {
      btns.push({
        cls: 'sim-order', color: 'var(--color-purple)',
        icon: '📝', label: '模拟下单（dry-run）',
        title: '按诊断给的 SL/TP 模拟一次买单（不真实下单）',
        data: { symbol, market, side: 'buy' },
      });
    }
    // 清仓（熊评级）
    if (isBearish) {
      btns.push({
        cls: 'check-position', color: 'var(--color-down)',
        icon: '🏁', label: '查看是否已持仓',
        title: 'AI 建议减仓/清仓，先看你持仓里有没有这只',
        data: { symbol, market },
      });
    }

    if (!btns.length) {
      return `<div style="padding:10px 12px;color:var(--text-tertiary);font-size:11px;text-align:center;">（评级中性，暂无推荐动作）</div>`;
    }
    return `
      <div style="background:rgba(155,109,213,0.08);padding:10px 12px;border-radius:6px;border-left:3px solid var(--color-purple);">
        <div style="color:var(--color-purple);font-weight:600;margin-bottom:8px;">⚡ 一键智能行动（基于 AI 关键位）</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          ${btns.map(b => `
            <button class="btn btn-sm diag-action-btn" data-action-cls="${b.cls}" data-action-payload='${_esc(JSON.stringify(b.data))}'
                    title="${_esc(b.title)}"
                    style="font-size:11px;padding:4px 10px;border:1px solid ${b.color};color:${b.color};background:transparent;border-radius:4px;cursor:pointer;">
              ${b.icon} ${_esc(b.label)}
            </button>
          `).join('')}
        </div>
      </div>`;
  }

  function _bindDiagActions(container, symbol, market, diag) {
    if (!container || container._actBound) return;
    container._actBound = true;
    container.addEventListener('click', async (e) => {
      const btn = e.target.closest('.diag-action-btn');
      if (!btn) return;
      const cls = btn.dataset.actionCls;
      let payload;
      try { payload = JSON.parse(btn.dataset.actionPayload || '{}'); } catch { payload = {}; }
      btn.disabled = true;
      const origHTML = btn.innerHTML;
      btn.innerHTML = '⏳ 处理中...';
      try {
        if (cls === 'alert-resistance' || cls === 'alert-sl' || cls === 'alert-support') {
          await _createPriceAlert(payload);
        } else if (cls === 'add-position') {
          await _quickAddPosition(payload);
        } else if (cls === 'sim-order') {
          await _quickSimOrder(payload);
        } else if (cls === 'check-position') {
          await _checkHoldingPosition(payload);
        }
      } catch (err) {
        if (typeof showToast === 'function') showToast(`操作失败: ${err.message}`, 'error', 3500);
      } finally {
        btn.disabled = false;
        btn.innerHTML = origHTML;
      }
    });
  }

  async function _createPriceAlert(p) {
    const resp = await fetch('/api/alerts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol: p.symbol, condition: p.condition, price: p.price,
        message: `${p.symbol} ${p.note || ''} @${p.price}`,
        repeat: false,
      }),
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      throw new Error(d.detail || `HTTP ${resp.status}`);
    }
    if (typeof showToast === 'function')
      showToast(`✅ 警报已创建: ${p.symbol} ${p.condition === 'price_gte' ? '≥' : '≤'} ${p.price}`, 'success', 3500);
    if (typeof Alerts !== 'undefined' && Alerts.loadAlerts) Alerts.loadAlerts();
  }

  async function _quickAddPosition(p) {
    const priceHint = p.sl && p.tp ? `\n(AI 建议 SL=${p.sl} / TP=${p.tp})` : '';
    const qty = prompt(`加入 ${p.symbol} 到持仓${priceHint}\n输入数量:`, '0');
    const q = parseFloat(qty);
    if (!q || q <= 0) return;
    const costStr = prompt(`成本价（留空 = 按当前市价）:`, '');
    const cost = parseFloat(costStr) || 0;
    const resp = await fetch('/api/positions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: p.symbol, market: p.market, quantity: q, avg_cost: cost, notes: `AI 诊断建议入仓` }),
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      throw new Error(d.detail || `HTTP ${resp.status}`);
    }
    if (typeof showToast === 'function') showToast(`💼 已添加 ${p.symbol} × ${q} 到持仓`, 'success', 3500);
    if (typeof Portfolio !== 'undefined' && Portfolio.refresh) Portfolio.refresh();
    // 若诊断带了 SL → 自动创建止损警报
    if (p.sl) {
      try {
        await _createPriceAlert({ symbol: p.symbol, market: p.market, price: p.sl, condition: 'price_lte', note: '持仓止损' });
      } catch {}
    }
  }

  async function _quickSimOrder(p) {
    const qty = prompt(`模拟下单 ${p.symbol} ${p.side.toUpperCase()}\n输入数量（dry-run）:`, '0.01');
    const q = parseFloat(qty);
    if (!q || q <= 0) return;
    const resp = await fetch('/api/trading/simulate-order', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: p.symbol, side: p.side, quantity: q, price: 0, order_type: 'market' }),
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      throw new Error(d.detail || `HTTP ${resp.status}`);
    }
    const r = await resp.json();
    if (typeof showToast === 'function')
      showToast(`📝 模拟订单已提交 (ID: ${(r.id || '').substring(0, 8)})`, 'success', 4000);
  }

  async function _showQuickActionMenu(btn) {
    const itemId = btn.dataset.id, symbol = btn.dataset.symbol, market = btn.dataset.market;
    // 关闭已有菜单
    document.querySelectorAll('.wp-quickmenu').forEach(el => el.remove());
    // 定位
    const rect = btn.getBoundingClientRect();
    const menu = document.createElement('div');
    menu.className = 'wp-quickmenu';
    menu.style.cssText = `position:fixed;left:${rect.right + 5}px;top:${rect.top}px;background:var(--bg-secondary);border:1px solid var(--border-secondary);border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,0.3);padding:6px;z-index:9999;min-width:220px;font-size:12px;`;
    menu.innerHTML = `<div style="color:var(--text-tertiary);padding:4px 8px;font-size:10px;">⏳ 加载诊断关键位...</div>`;
    document.body.appendChild(menu);
    // 点外关闭
    setTimeout(() => {
      const closer = (e) => {
        if (!menu.contains(e.target) && e.target !== btn) {
          menu.remove();
          document.removeEventListener('click', closer);
        }
      };
      document.addEventListener('click', closer);
    }, 50);

    try {
      const r = await fetch(`/api/pool/${itemId}/diagnosis`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      const diag = d.diagnosis;
      if (!diag) {
        menu.innerHTML = `<div style="padding:10px;color:var(--text-tertiary);">尚无诊断，请先点 🩺 生成</div>`;
        return;
      }
      const kl = diag.key_levels || {};
      const rating = diag.rating || 'hold';
      const isBull = rating === 'strong_buy' || rating === 'buy';
      const isBear = rating === 'sell' || rating === 'reduce';
      const items = [];
      if (kl.resistance != null && (isBull || rating === 'hold')) {
        items.push({ icon:'🔔', label:`突破阻力 ${kl.resistance} 时提醒`, color:'var(--color-up)',
          fn: () => _createPriceAlert({ symbol, market, price: kl.resistance, condition:'price_gte', note:'突破阻力' }) });
      }
      if (kl.stop_loss != null) {
        items.push({ icon:'⛔', label:`跌破止损 ${kl.stop_loss} 时提醒`, color:'var(--color-down)',
          fn: () => _createPriceAlert({ symbol, market, price: kl.stop_loss, condition:'price_lte', note:'止损' }) });
      } else if (kl.support != null && isBear) {
        items.push({ icon:'⚠️', label:`跌破支撑 ${kl.support} 时提醒`, color:'var(--color-warning)',
          fn: () => _createPriceAlert({ symbol, market, price: kl.support, condition:'price_lte', note:'破支撑' }) });
      }
      if (isBull) {
        items.push({ icon:'💼', label:'加入持仓', color:'var(--color-up)',
          fn: () => _quickAddPosition({ symbol, market, sl: kl.stop_loss, tp: kl.resistance }) });
      }
      if (isBear) {
        items.push({ icon:'🏁', label:'查看是否已持仓', color:'var(--color-down)',
          fn: () => _checkHoldingPosition({ symbol, market }) });
      }
      if (!items.length) {
        menu.innerHTML = `<div style="padding:10px;color:var(--text-tertiary);font-size:11px;">该评级暂无推荐动作</div>`;
        return;
      }
      menu.innerHTML = `
        <div style="padding:4px 8px;font-size:10px;color:var(--text-tertiary);border-bottom:1px solid var(--border-secondary);margin-bottom:4px;">
          ${_esc(symbol)} · ${(RATING_LABEL[rating] || {}).label || rating}
        </div>
        ${items.map((it, i) => `
          <div class="wp-qm-item" data-i="${i}" style="padding:6px 10px;cursor:pointer;border-radius:3px;color:${it.color};display:flex;align-items:center;gap:6px;"
               onmouseover="this.style.background='var(--bg-tertiary)'" onmouseout="this.style.background=''">
            <span>${it.icon}</span><span>${_esc(it.label)}</span>
          </div>
        `).join('')}
      `;
      menu.querySelectorAll('.wp-qm-item').forEach(el => {
        el.addEventListener('click', async () => {
          const i = parseInt(el.dataset.i);
          menu.remove();
          try { await items[i].fn(); } catch (e) {
            if (typeof showToast === 'function') showToast(`失败: ${e.message}`, 'error');
          }
        });
      });
    } catch (e) {
      menu.innerHTML = `<div style="padding:10px;color:var(--color-down);font-size:11px;">加载失败: ${_esc(e.message)}</div>`;
    }
  }

  async function _checkHoldingPosition(p) {
    const resp = await fetch('/api/positions');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const arr = await resp.json();
    const list = Array.isArray(arr) ? arr : (arr.items || []);
    const hit = list.find(x => x.symbol === p.symbol && x.market === p.market);
    if (hit) {
      if (typeof showToast === 'function')
        showToast(`⚠️ 你持有 ${p.symbol} × ${hit.quantity}（AI 建议减仓/清仓，请评估）`, 'warning', 6000);
    } else {
      if (typeof showToast === 'function')
        showToast(`✓ 你没有持仓 ${p.symbol}，无需操作`, 'info', 3500);
    }
  }

  return { init, refresh, render };
})();

window.Watchpool = Watchpool;
