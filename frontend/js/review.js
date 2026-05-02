/*
   Review 模块 — 交易复盘学习独立面板 (v12.4)
   - 顶部 Hero：总数 / 平均分 / A-D 等级分布 / 总盈亏
   - 子 tab：单笔复盘列表 / 周报
   - 单笔列表：可按池/评级/时间过滤；点开看完整深度复盘 modal
*/

const Review = (function () {
  let _items = [];
  let _weekly = [];
  let _lessons = [];            // v12.5: 高频教训库
  let _filterPool = 'all';      // all | us_hk | cn | crypto
  let _filterGrade = 'all';     // all | A | B | C | D
  let _viewMode = 'reviews';    // reviews | weekly | lessons

  let _inited = false;
  function init() {
    if (_inited) { console.warn('[Review] 已初始化'); return; }
    _inited = true;
    _ensureDom();
    refresh();
    // 30 分钟自动刷新一次（不抢预算）
    setInterval(refresh, 30 * 60 * 1000);
    console.log('[Review] 已初始化');
  }

  function _ensureDom() {
    const pane = document.querySelector('.bottom-pane[data-pane="review"]');
    if (!pane || pane.dataset.inited) return;
    pane.dataset.inited = '1';
    pane.innerHTML = `
      <div class="oc-toolbar">
        <span class="oc-text-lg" style="font-weight:600;">📝 复盘学习</span>
        <div class="oc-tabs">
          <button class="oc-tab active rv-view-tab" data-view="reviews">单笔复盘</button>
          <button class="oc-tab rv-view-tab" data-view="weekly">📈 周报</button>
          <button class="oc-tab rv-view-tab" data-view="lessons" title="高频教训 — 已注入 verify_signal / diagnose_* 的 prompt 形成反馈闭环">🧠 教训库</button>
        </div>
        <select id="rv-filter-pool" class="select oc-text-sm" style="width:110px;">
          <option value="all">全部资金池</option>
          <option value="us_hk">港美股</option>
          <option value="cn">A股</option>
          <option value="crypto">加密</option>
        </select>
        <select id="rv-filter-grade" class="select oc-text-sm" style="width:96px;">
          <option value="all">全部评级</option>
          <option value="A">A 级</option>
          <option value="B">B 级</option>
          <option value="C">C 级</option>
          <option value="D">D 级</option>
        </select>
        <span class="oc-toolbar-spacer"></span>
        <button id="rv-batch-btn" class="btn btn-primary btn-sm" title="对所有未复盘的闭环单调用 LLM 批量复盘（约每笔 30s + 消耗预算）">🤖 批量补全</button>
        <button id="rv-weekly-gen-btn" class="btn btn-sm" title="手动触发上周报告生成">📈 生成周报</button>
        <button id="rv-refresh-btn" class="btn btn-sm" title="刷新">🔄</button>
        <span id="rv-status" class="oc-text-sm oc-muted" style="width:100%;text-align:right;margin-top:2px;"></span>
      </div>
      <div id="rv-hero" style="padding:10px 12px;border-bottom:1px solid var(--border-secondary);background:linear-gradient(180deg,var(--bg-secondary) 0%,var(--bg-primary) 100%);"></div>
      <div class="rv-list" style="overflow-y:auto;flex:1;min-height:0;"></div>
    `;
    const $ = (s) => pane.querySelector(s);
    $('#rv-filter-pool').addEventListener('change', e => { _filterPool = e.target.value; render(); });
    $('#rv-filter-grade').addEventListener('change', e => { _filterGrade = e.target.value; render(); });
    $('#rv-refresh-btn').addEventListener('click', refresh);
    $('#rv-batch-btn').addEventListener('click', _batchReview);
    $('#rv-weekly-gen-btn').addEventListener('click', _generateWeekly);
    pane.querySelectorAll('.rv-view-tab').forEach(t => {
      t.addEventListener('click', () => {
        _viewMode = t.dataset.view;
        pane.querySelectorAll('.rv-view-tab').forEach(x => x.classList.toggle('active', x.dataset.view === _viewMode));
        render();
      });
    });
  }

  async function refresh() {
    const status = document.getElementById('rv-status');
    if (status) status.textContent = '⏳ 加载中...';
    try {
      const [r1, r2, r3] = await Promise.all([
        fetch('/api/trade-review?limit=500').then(r => r.json()),
        fetch('/api/trade-review/weekly/list?limit=12').then(r => r.json()).catch(() => ({items:[]})),
        fetch('/api/trade-review/lessons/top?limit=200').then(r => r.json()).catch(() => ({items:[]})),
      ]);
      _items = r1.items || [];
      _weekly = r2.items || [];
      _lessons = r3.items || [];
      render();
      if (status) status.textContent = `共 ${_items.length} 笔复盘 · ${_weekly.length} 份周报 · ${_lessons.length} 个教训模式 · ${new Date().toLocaleTimeString()}`;
    } catch (e) {
      if (status) status.textContent = `加载失败: ${e.message}`;
    }
  }

  function _renderHero() {
    const hero = document.getElementById('rv-hero');
    if (!hero) return;
    if (!_items.length) { hero.innerHTML = ''; return; }
    const counts = { A:0, B:0, C:0, D:0 };
    let totalPnlUsd = 0; let scoreSum = 0; let wins = 0;
    for (const r of _items) {
      counts[r.grade] = (counts[r.grade]||0) + 1;
      scoreSum += r.score || 0;
      if ((r.realized_pnl_pct||0) > 0) wins++;
      // 粗略 USD 转换
      const fx = r.market === 'cn' ? 0.14 : (r.market === 'hk' ? 0.128 : 1.0);
      totalPnlUsd += (r.realized_pnl_local || 0) * fx;
    }
    const avgScore = (scoreSum / _items.length).toFixed(1);
    const winRate = (wins / _items.length * 100).toFixed(1);
    const pnlColor = totalPnlUsd >= 0 ? 'var(--color-up)' : 'var(--color-down)';
    hero.innerHTML = `
      <div class="port-hero-grid" style="grid-template-columns:repeat(5, minmax(140px, 1fr));">
        <div class="port-hero-card port-hero-equity">
          <div class="port-hero-label">总复盘数</div>
          <div class="port-hero-value-lg">${_items.length}</div>
          <div class="oc-text-xs oc-muted">胜率 ${winRate}%（${wins}/${_items.length}）</div>
        </div>
        <div class="port-hero-card" style="border-left-color:var(--color-purple);">
          <div class="port-hero-label">平均评分</div>
          <div class="port-hero-value-lg" style="color:var(--color-purple);">${avgScore}</div>
          <div class="oc-text-xs oc-muted">满分 100</div>
        </div>
        <div class="port-hero-card" style="border-left-color:var(--color-up);">
          <div class="port-hero-label">A 级 / B 级</div>
          <div style="font-size:18px;font-weight:700;">
            <span style="color:var(--color-up);">${counts.A}</span>
            <span class="oc-muted">/</span>
            <span style="color:var(--color-accent);">${counts.B}</span>
          </div>
          <div class="oc-text-xs oc-muted">优秀 / 不错</div>
        </div>
        <div class="port-hero-card" style="border-left-color:var(--color-down);">
          <div class="port-hero-label">C 级 / D 级</div>
          <div style="font-size:18px;font-weight:700;">
            <span style="color:var(--color-warning);">${counts.C}</span>
            <span class="oc-muted">/</span>
            <span style="color:var(--color-down);">${counts.D}</span>
          </div>
          <div class="oc-text-xs oc-muted">一般 / 失败</div>
        </div>
        <div class="port-hero-card" style="border-left-color:${pnlColor};">
          <div class="port-hero-label">复盘单累计盈亏</div>
          <div class="port-hero-value-md" style="color:${pnlColor};">${totalPnlUsd>=0?'+':''}$${Math.abs(totalPnlUsd).toFixed(2)}</div>
          <div class="oc-text-xs oc-muted">USD 估值</div>
        </div>
      </div>
    `;
  }

  function render() {
    _renderHero();
    const list = document.querySelector('.bottom-pane[data-pane="review"] .rv-list');
    if (!list) return;
    if (_viewMode === 'weekly') return _renderWeekly(list);
    if (_viewMode === 'lessons') return _renderLessons(list);
    // 单笔复盘列表
    let visible = _items;
    if (_filterPool !== 'all') visible = visible.filter(r => r.pool_id === _filterPool);
    if (_filterGrade !== 'all') visible = visible.filter(r => r.grade === _filterGrade);
    if (!visible.length) {
      list.innerHTML = `
        <div class="oc-empty">
          <div class="oc-empty-icon">📝</div>
          <div class="oc-empty-title">${_items.length ? '当前过滤无匹配' : '暂无复盘'}</div>
          <div class="oc-empty-hint">${_items.length ? '清除过滤条件试试' : '平仓后系统每 4h 自动复盘 · 或点「🤖 批量补全」立即触发'}</div>
        </div>`;
      return;
    }
    const GRADE_COLOR = { A:'var(--color-up)', B:'var(--color-accent)', C:'var(--color-warning)', D:'var(--color-down)' };
    const POOL_LABEL = { us_hk:'🌎 港美股', cn:'🇨🇳 A股', crypto:'🪙 加密' };
    const _esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    list.innerHTML = `
      <table class="oc-table oc-table-compact">
        <thead>
          <tr>
            <th>平仓时间</th>
            <th>品种</th>
            <th>资金池</th>
            <th class="oc-col-center" title="综合评级 = 决策×0.6 + 结果×0.4">评级</th>
            <th class="oc-col-num" title="决策分（不看结果，仅看入场时数据是否充分支持）">决策</th>
            <th class="oc-col-num" title="结果分（仅看实际收益和出场时机）">结果</th>
            <th class="oc-col-num">收益率</th>
            <th class="oc-col-num">持仓</th>
            <th class="oc-col-num">错过</th>
            <th>入场分析摘要</th>
            <th class="oc-col-center">详情</th>
          </tr>
        </thead>
        <tbody>
          ${visible.map(r => {
            const time = r.close_at ? new Date(r.close_at*1000).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false}) : '-';
            const gColor = GRADE_COLOR[r.grade] || 'var(--text-secondary)';
            const pnlColor = (r.realized_pnl_pct||0) >= 0 ? 'var(--color-up)' : 'var(--color-down)';
            const ds = r.decision_score, os = r.outcome_score;
            const dColor = ds == null ? 'var(--text-tertiary)' : ds >= 75 ? 'var(--color-up)' : ds >= 55 ? 'var(--color-warning)' : 'var(--color-down)';
            const oColor = os == null ? 'var(--text-tertiary)' : os >= 75 ? 'var(--color-up)' : os >= 55 ? 'var(--color-warning)' : 'var(--color-down)';
            const insight = (r.entry_analysis || '').substring(0, 80);
            // P2 修复 (审计 #27): swap 复盘加 ⚡badge,显示杠杆/方向/强平
            const isSwap = r.is_swap == 1 || r.is_swap === true;
            let swapBadge = '';
            if (isSwap) {
              const sideTxt = r.swap_pos_side === 'long' ? '🟢多' : (r.swap_pos_side === 'short' ? '🔴空' : '');
              const lev = r.swap_leverage ? `${r.swap_leverage}x` : '';
              const liqTag = r.swap_liquidated ? ' <span style="background:var(--color-down);color:#fff;padding:1px 4px;border-radius:3px;font-size:10px;">💀强平</span>' : '';
              swapBadge = `<span style="background:var(--color-purple);color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;margin-left:4px;">⚡${sideTxt}${lev}</span>${liqTag}`;
            }
            return `<tr style="cursor:pointer;" data-pid="${_esc(r.position_id)}" data-sym="${_esc(r.symbol)}" class="rv-row">
              <td class="oc-text-sm oc-muted" style="white-space:nowrap;">${time}</td>
              <td style="font-weight:700;">${_esc(r.symbol)}${swapBadge}</td>
              <td><span class="oc-chip oc-chip-neutral">${POOL_LABEL[r.pool_id]||r.pool_id}</span></td>
              <td class="oc-col-center"><span style="color:${gColor};font-weight:700;font-size:16px;">${r.grade||'-'}</span><div style="font-size:10px;color:${gColor};">${r.score||0}</div></td>
              <td class="oc-col-num"><span style="color:${dColor};font-weight:600;">${ds==null?'-':ds}</span></td>
              <td class="oc-col-num"><span style="color:${oColor};font-weight:600;">${os==null?'-':os}</span></td>
              <td class="oc-col-num"><span style="color:${pnlColor};font-weight:600;">${(r.realized_pnl_pct||0)>=0?'+':''}${(r.realized_pnl_pct||0).toFixed(2)}%</span></td>
              <td class="oc-col-num">${(r.hold_hours||0).toFixed(1)}h</td>
              <td class="oc-col-num"><span style="color:${(r.missed_profit_pct||0)>5?'var(--color-warning)':'var(--text-tertiary)'};">${(r.missed_profit_pct||0).toFixed(2)}%</span></td>
              <td class="oc-text-sm oc-muted" style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${_esc(insight)}">${_esc(insight)}…</td>
              <td class="oc-col-center"><button class="btn btn-sm rv-detail-btn" data-pid="${_esc(r.position_id)}" data-sym="${_esc(r.symbol)}" style="font-size:11px;padding:2px 8px;color:var(--color-purple);">📝</button></td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    `;
    // 行点击 + 详情按钮
    if (!list._delegated) {
      list._delegated = true;
      list.addEventListener('click', (e) => {
        const btn = e.target.closest('.rv-detail-btn') || e.target.closest('.rv-row');
        if (!btn) return;
        const pid = btn.dataset.pid;
        const sym = btn.dataset.sym;
        if (pid) _showReviewModal(pid, sym);
      });
    }
  }

  function _renderLessons(list) {
    if (!_lessons.length) {
      list.innerHTML = `
        <div class="oc-empty">
          <div class="oc-empty-icon">🧠</div>
          <div class="oc-empty-title">教训库为空</div>
          <div class="oc-empty-hint">至少累积 2 笔同类教训才会聚合到这里 · 系统每 6h 自动聚合</div>
        </div>`;
      return;
    }
    const _esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    const TYPE_LABEL = {
      // v12.8 九环节
      signal:             '📡 信号生成',
      ai_verify:          '🤖 AI 验证',
      news_judge:         '📰 新闻判断',
      entry_timing:       '📥 入场时机',
      sl_tp_setup:        '🎯 止盈止损',
      mid_management:     '⚙ 持仓管理',
      diagnose_response:  '🔍 诊断响应',
      exit_quality:       '📤 出场质量',
      money_management:   '💰 资金管理',
      // 旧版 5 类（兼容历史数据）
      entry:       '📥 入场',
      exit:        '📤 出场',
      risk:        '🛡 风控',
      psychology:  '🧠 心态',
      general:     '⚙ 综合',
    };
    const POOL_LABEL = { us_hk: '🌎 港美股', cn: '🇨🇳 A股', crypto: '🪙 加密', all: '🌐 全部' };
    const STATUS_CHIP = {
      adopted:  '<span class="oc-chip oc-chip-up" title="用户已认可 · 永不过期 · prompt 注入优先级最高">✅ 已采纳</span>',
      active:   '<span class="oc-chip oc-chip-info" title="活跃中 · 注入 LLM prompt">🟢 活跃</span>',
      expired:  '<span class="oc-chip oc-chip-neutral" title="60 天没在新复盘中出现 · 已停用 · 出现新同类时自动复活">⏰ 过期</span>',
      disabled: '<span class="oc-chip oc-chip-down" title="用户主动弃用 · 不注入 prompt · 不会自动复活">🚫 弃用</span>',
    };
    // 状态分组统计
    const counts = { adopted:0, active:0, expired:0, disabled:0 };
    for (const l of _lessons) counts[l.status||'active'] = (counts[l.status||'active']||0)+1;
    list.innerHTML = `
      <div style="padding:10px 14px;background:var(--bg-tertiary);border-bottom:1px solid var(--border-secondary);">
        <div class="oc-text-sm" style="color:var(--text-secondary);">
          🧠 <strong>反馈闭环</strong>：✅采纳 + 🟢活跃 的教训会注入 verify_signal / diagnose_* 的 LLM prompt（采纳的优先级最高）。
          ⏰过期/🚫弃用 的不再注入。
        </div>
        <div style="margin-top:6px;display:flex;gap:10px;flex-wrap:wrap;font-size:11px;">
          <span class="oc-muted">分布：</span>
          <span class="oc-up">✅ 采纳 ${counts.adopted}</span>
          <span class="oc-accent">🟢 活跃 ${counts.active}</span>
          <span class="oc-muted">⏰ 过期 ${counts.expired}</span>
          <span class="oc-down">🚫 弃用 ${counts.disabled}</span>
          <button id="rv-aggregate-btn" class="btn btn-sm" style="margin-left:auto;padding:2px 8px;font-size:11px;">🔄 立即重聚合</button>
        </div>
      </div>
      <table class="oc-table oc-table-compact">
        <thead>
          <tr>
            <th>状态</th>
            <th>资金池</th>
            <th>类型</th>
            <th>教训内容</th>
            <th class="oc-col-num">出现次数</th>
            <th class="oc-col-num" title="符合此模式的交易平均收益率">平均收益</th>
            <th class="oc-col-center">操作</th>
          </tr>
        </thead>
        <tbody>
          ${_lessons.map(l => {
            const pnl = l.avg_pnl_pct || 0;
            const pnlColor = pnl >= 0 ? 'var(--color-up)' : 'var(--color-down)';
            const status = l.status || 'active';
            const isExpiredOrDisabled = (status === 'expired' || status === 'disabled');
            const rowOpacity = isExpiredOrDisabled ? 'opacity:0.55;' : '';
            // 操作按钮（按当前状态显示对应可切换的目标）
            const btns = [];
            if (status !== 'adopted') {
              btns.push(`<button class="rv-lesson-btn" data-id="${l.id}" data-action="adopted" title="采纳：永不过期 + 高优先级注入" style="font-size:10px;padding:2px 6px;color:var(--color-up);background:transparent;border:1px solid var(--color-up);border-radius:3px;cursor:pointer;margin-right:3px;">✅ 采纳</button>`);
            }
            if (status !== 'active') {
              btns.push(`<button class="rv-lesson-btn" data-id="${l.id}" data-action="active" title="重新激活" style="font-size:10px;padding:2px 6px;color:var(--color-accent);background:transparent;border:1px solid var(--color-accent);border-radius:3px;cursor:pointer;margin-right:3px;">🔁 激活</button>`);
            }
            if (status !== 'disabled') {
              btns.push(`<button class="rv-lesson-btn" data-id="${l.id}" data-action="disabled" title="弃用：不再注入 prompt" style="font-size:10px;padding:2px 6px;color:var(--color-down);background:transparent;border:1px solid var(--color-down);border-radius:3px;cursor:pointer;">🚫 弃用</button>`);
            }
            return `<tr style="${rowOpacity}">
              <td>${STATUS_CHIP[status] || STATUS_CHIP.active}</td>
              <td><span class="oc-chip oc-chip-neutral">${POOL_LABEL[l.pool_id]||l.pool_id}</span></td>
              <td><span class="oc-chip oc-chip-purple">${TYPE_LABEL[l.type]||l.type}</span></td>
              <td style="max-width:480px;color:var(--text-primary);">${_esc(l.full_text || l.pattern)}</td>
              <td class="oc-col-num" style="font-weight:700;font-size:14px;">${l.occurrences}</td>
              <td class="oc-col-num"><span style="color:${pnlColor};font-weight:600;">${pnl>=0?'+':''}${pnl.toFixed(2)}%</span></td>
              <td class="oc-col-center" style="white-space:nowrap;">${btns.join('')}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    `;
    const aggBtn = document.getElementById('rv-aggregate-btn');
    if (aggBtn) aggBtn.addEventListener('click', async () => {
      aggBtn.disabled = true; aggBtn.textContent = '⏳ 聚合中...';
      try {
        const r = await fetch('/api/trade-review/lessons/aggregate', {method: 'POST'});
        const d = await r.json();
        if (typeof showToast === 'function') showToast(`✅ 聚合到 ${d.patterns} 个模式`, 'success');
        await refresh();
      } catch (e) {
        if (typeof showToast === 'function') showToast(`❌ ${e.message}`, 'error');
      } finally {
        aggBtn.disabled = false; aggBtn.textContent = '🔄 立即重聚合';
      }
    });
    // 状态切换按钮（事件委托）
    list.querySelectorAll('.rv-lesson-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const id = btn.dataset.id;
        const action = btn.dataset.action;
        btn.disabled = true;
        try {
          const r = await fetch(`/api/trade-review/lessons/${id}/status`, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({status: action}),
          });
          const d = await r.json();
          if (!r.ok) throw new Error(d.detail || '失败');
          const STATUS_CN = { adopted:'采纳', active:'激活', disabled:'弃用' };
          if (typeof showToast === 'function') showToast(`✅ 已${STATUS_CN[action]}`, 'success', 2000);
          await refresh();
        } catch (err) {
          if (typeof showToast === 'function') showToast(`❌ ${err.message}`, 'error');
          btn.disabled = false;
        }
      });
    });
  }

  function _renderWeekly(list) {
    if (!_weekly.length) {
      list.innerHTML = `
        <div class="oc-empty">
          <div class="oc-empty-icon">📈</div>
          <div class="oc-empty-title">暂无周报</div>
          <div class="oc-empty-hint">每周一凌晨 03:00 自动生成上周报告 · 或点「📈 生成周报」立即创建</div>
        </div>`;
      return;
    }
    const _esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    list.innerHTML = `<div style="padding:14px 18px;display:flex;flex-direction:column;gap:14px;">
      ${_weekly.map(w => {
        const start = new Date(w.week_start * 1000).toLocaleDateString('zh-CN');
        const end = new Date(w.week_end * 1000).toLocaleDateString('zh-CN');
        const winColor = w.win_rate >= 0.6 ? 'var(--color-up)' : w.win_rate >= 0.4 ? 'var(--color-warning)' : 'var(--color-down)';
        const pnlColor = w.total_pnl_usd >= 0 ? 'var(--color-up)' : 'var(--color-down)';
        const mistakes = (w.recurring_mistakes || []).map(m => `<li>${_esc(m)}</li>`).join('');
        const actions = (w.actionable_changes || []).map(a => `<li>${_esc(a)}</li>`).join('');
        return `
          <div class="oc-card">
            <div class="oc-card-header" style="border-bottom-color:var(--border-secondary);">
              <span>📅 ${start} ~ ${end}</span>
              <span class="oc-text-sm oc-muted">${w.trades_count} 笔 · 平均 ${w.avg_grade}</span>
            </div>
            <div style="padding:14px 18px;">
              <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px;">
                <div class="oc-card oc-card-compact"><div class="oc-text-xs oc-muted">胜率</div><div style="font-size:22px;font-weight:700;color:${winColor};">${(w.win_rate*100).toFixed(1)}%</div><div class="oc-text-xs oc-muted">${w.wins}胜 / ${w.losses}败</div></div>
                <div class="oc-card oc-card-compact"><div class="oc-text-xs oc-muted">总盈亏</div><div style="font-size:22px;font-weight:700;color:${pnlColor};">${w.total_pnl_usd>=0?'+':''}$${Math.abs(w.total_pnl_usd).toFixed(2)}</div></div>
                <div class="oc-card oc-card-compact"><div class="oc-text-xs oc-muted">交易笔数</div><div style="font-size:22px;font-weight:700;">${w.trades_count}</div></div>
                <div class="oc-card oc-card-compact"><div class="oc-text-xs oc-muted">平均评级</div><div style="font-size:22px;font-weight:700;">${w.avg_grade}</div></div>
              </div>
              <h4 style="color:var(--color-accent);margin:0 0 6px;font-size:13px;">📊 综合评估</h4>
              <div style="color:var(--text-primary);font-size:12px;line-height:1.7;margin-bottom:14px;">${_esc(w.summary || '(无)')}</div>
              ${mistakes ? `<h4 style="color:var(--color-warning);margin:0 0 6px;font-size:13px;">⚠️ 重复错误模式</h4><ul style="margin:0 0 14px;padding-left:20px;font-size:12px;line-height:1.7;">${mistakes}</ul>` : ''}
              ${actions ? `<h4 style="color:var(--color-purple);margin:0 0 6px;font-size:13px;">💡 下周可执行改进</h4><ul style="margin:0;padding-left:20px;font-size:12px;line-height:1.7;background:rgba(124,77,255,0.06);padding:10px 10px 10px 28px;border-radius:4px;">${actions}</ul>` : ''}
            </div>
          </div>
        `;
      }).join('')}
    </div>`;
  }

  async function _batchReview() {
    const btn = document.getElementById('rv-batch-btn');
    if (btn) { btn.disabled = true; btn.textContent = '🤖 LLM 批量分析中...'; }
    if (typeof showToast === 'function') showToast('🤖 批量复盘已在后台启动（每笔 30-60s LLM 调用）', 'info', 5000);
    try {
      const r = await fetch('/api/trade-review/batch?limit=100', {method:'POST'});
      const d = await r.json();
      const msg = `📝 处理 ${d.processed} 笔 · 成功 ${d.ok} · 失败 ${d.fail}` + (d.skipped_budget ? ` · 预算跳过 ${d.skipped_budget}` : '');
      if (typeof showToast === 'function') showToast(msg, d.ok > 0 ? 'success' : 'warning', 8000);
      await refresh();
    } catch (e) {
      if (typeof showToast === 'function') showToast(`❌ ${e.message}`, 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '🤖 批量补全'; }
    }
  }

  async function _generateWeekly() {
    if (typeof showToast === 'function') showToast('📈 生成上周报告中...', 'info', 5000);
    try {
      const r = await fetch('/api/trade-review/weekly/generate', {method:'POST'});
      const d = await r.json();
      if (d.ok) {
        if (typeof showToast === 'function') showToast(`✅ 周报生成: ${d.trades_count} 笔 / 胜率 ${(d.win_rate*100).toFixed(1)}%`, 'success', 6000);
      } else {
        if (typeof showToast === 'function') showToast(d.msg || '上周无成交', 'warning');
      }
      await refresh();
      _viewMode = 'weekly';
      document.querySelectorAll('.rv-view-tab').forEach(x => x.classList.toggle('active', x.dataset.view === _viewMode));
      render();
    } catch (e) {
      if (typeof showToast === 'function') showToast(`❌ ${e.message}`, 'error');
    }
  }

  // 复盘详情 modal — 复用 portfolio.js 已有的 render 逻辑（如果存在），否则简版渲染
  async function _showReviewModal(positionId, symbol) {
    // 若 Portfolio 模块的 _showReviewModal 可访问就复用，否则自渲染
    if (typeof Portfolio !== 'undefined' && Portfolio._showReviewModal) {
      return Portfolio._showReviewModal(positionId, symbol);
    }
    // 自渲染
    let overlay = document.getElementById('rv-detail-modal');
    if (overlay) overlay.remove();
    overlay = document.createElement('div');
    overlay.id = 'rv-detail-modal';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
    overlay.innerHTML = `
      <div style="background:var(--bg-secondary);border-radius:8px;width:min(820px,94vw);max-height:90vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.5);">
        <div style="padding:14px 18px;border-bottom:1px solid var(--border-secondary);display:flex;justify-content:space-between;align-items:center;">
          <span style="font-weight:600;">📝 ${symbol} AI 深度复盘</span>
          <div style="display:flex;gap:8px;">
            <button id="rv-modal-regen" class="btn btn-sm">🔄 重做</button>
            <button id="rv-modal-close" class="btn btn-sm" style="font-size:14px;padding:2px 10px;">×</button>
          </div>
        </div>
        <div id="rv-modal-body" style="overflow-y:auto;flex:1;padding:14px 20px;font-size:12px;line-height:1.7;">
          <div style="text-align:center;padding:60px;color:var(--text-tertiary);">⏳ 加载...</div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    overlay.querySelector('#rv-modal-close').addEventListener('click', () => overlay.remove());
    const body = overlay.querySelector('#rv-modal-body');

    async function load(force = false) {
      body.innerHTML = '<div style="text-align:center;padding:60px;color:var(--text-tertiary);">⏳ AI 深度复盘中（约 30-60s）...</div>';
      try {
        let data;
        if (force) {
          await fetch(`/api/trade-review/trigger/${encodeURIComponent(positionId)}?force=true`, {method:'POST'});
        }
        const r = await fetch(`/api/trade-review/${encodeURIComponent(positionId)}`);
        if (r.status === 404) {
          await fetch(`/api/trade-review/trigger/${encodeURIComponent(positionId)}`, {method:'POST'});
          const r2 = await fetch(`/api/trade-review/${encodeURIComponent(positionId)}`);
          data = await r2.json();
        } else {
          data = await r.json();
        }
        body.innerHTML = _renderModalBody(data);
      } catch (e) {
        body.innerHTML = `<div style="color:var(--color-down);padding:30px;text-align:center;">加载失败: ${e.message}</div>`;
      }
    }
    overlay.querySelector('#rv-modal-regen').addEventListener('click', () => load(true));
    load(false);
  }

  function _renderModalBody(d) {
    if (!d || !d.symbol) return '<div style="color:var(--color-down);padding:30px;">数据为空</div>';
    const _esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const GRADE_COLOR = { A:'var(--color-up)', B:'var(--color-accent)', C:'var(--color-warning)', D:'var(--color-down)' };
    const gColor = GRADE_COLOR[d.grade] || 'var(--text-secondary)';
    const pnlColor = (d.realized_pnl_pct||0) >= 0 ? 'var(--color-up)' : 'var(--color-down)';
    const turning = (d.turning_points||[]).map(t => `<li><strong>${_esc(t.time||'?')} @ ${t.price||'?'}</strong> — ${_esc(t.event||'')}<div style="color:var(--text-secondary);font-size:11px;margin-left:10px;">💡 ${_esc(t.ai_note||'')}</div></li>`).join('') || '<li style="color:var(--text-tertiary);">(无)</li>';
    const lessons = (d.lessons||[]).map(l => `<li><span class="oc-chip oc-chip-purple" style="margin-right:6px;">${_esc(l.type||'general')}</span>${_esc(l.content||'')}</li>`).join('') || '<li style="color:var(--text-tertiary);">(无)</li>';
    const fmt = v => v == null ? '—' : (typeof v === 'number' ? v.toFixed(4) : v);
    return `
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px;">
        <div class="oc-card oc-card-compact"><div class="oc-text-xs oc-muted">综合评级</div>
          <div style="font-size:24px;font-weight:700;color:${gColor};">${d.score||0} <span style="font-size:14px;">${d.grade||'-'}</span></div>
          <div class="oc-text-xs oc-muted">决策×0.6 + 结果×0.4</div>
        </div>
        <div class="oc-card oc-card-compact" title="决策分：仅看入场时数据是否充分支持，不看结果"><div class="oc-text-xs oc-muted">决策质量</div>
          <div style="font-size:18px;font-weight:700;color:${(d.decision_score||0)>=75?'var(--color-up)':(d.decision_score||0)>=55?'var(--color-warning)':'var(--color-down)'};">${d.decision_score==null?'-':d.decision_score}</div>
          <div class="oc-text-xs oc-muted">不看结果</div>
        </div>
        <div class="oc-card oc-card-compact" title="结果分：仅看实际收益和出场时机"><div class="oc-text-xs oc-muted">实际结果</div>
          <div style="font-size:18px;font-weight:700;color:${(d.outcome_score||0)>=75?'var(--color-up)':(d.outcome_score||0)>=55?'var(--color-warning)':'var(--color-down)'};">${d.outcome_score==null?'-':d.outcome_score}</div>
          <div class="oc-text-xs oc-muted" style="color:${pnlColor};">${(d.realized_pnl_pct||0)>=0?'+':''}${(d.realized_pnl_pct||0).toFixed(2)}% · ${(d.hold_hours||0).toFixed(1)}h</div>
        </div>
        <div class="oc-card oc-card-compact"><div class="oc-text-xs oc-muted">错过的额外利润</div>
          <div style="font-size:18px;font-weight:600;color:${(d.missed_profit_pct||0)>5?'var(--color-warning)':'var(--text-primary)'};">+${(d.missed_profit_pct||0).toFixed(2)}%</div>
          <div class="oc-text-xs oc-muted">vs 最佳出场</div>
        </div>
      </div>
      ${(d.is_swap == 1 || d.is_swap === true) ? `
      <div style="background:linear-gradient(90deg,rgba(124,77,255,0.18),rgba(124,77,255,0.05));border-left:4px solid var(--color-purple);padding:10px 14px;border-radius:0 6px 6px 0;margin-bottom:14px;">
        <div style="color:var(--color-purple);font-weight:700;font-size:12px;margin-bottom:6px;">⚡ 加密永续合约 ${d.swap_pos_side === 'long' ? '🟢做多' : '🔴做空'} ${d.swap_leverage||'?'}x ${d.swap_liquidated ? '<span style="background:var(--color-down);color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;margin-left:4px;">💀 强平</span>' : ''}</div>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;font-size:11px;">
          <div><span style="color:var(--text-tertiary);">杠杆:</span> <strong>${d.swap_leverage||0}x</strong></div>
          <div><span style="color:var(--text-tertiary);">资金费累积:</span> <strong style="color:${(d.swap_funding_total||0) >= 0 ? 'var(--color-up)' : 'var(--color-down)'};">${(d.swap_funding_total||0) >= 0 ? '+' : ''}$${(d.swap_funding_total||0).toFixed(4)}</strong></div>
          <div><span style="color:var(--text-tertiary);">手续费累积:</span> <strong style="color:var(--color-down);">-$${(d.swap_total_fee||0).toFixed(4)}</strong></div>
        </div>
      </div>` : ''}
      ${d.primary_lesson ? `
      <div style="background:linear-gradient(90deg,rgba(124,77,255,0.15),rgba(124,77,255,0.05));border-left:4px solid var(--color-purple);padding:12px 14px;border-radius:0 6px 6px 0;margin-bottom:14px;">
        <div style="color:var(--color-purple);font-weight:700;font-size:12px;margin-bottom:4px;">🎯 本笔核心教训</div>
        <div style="font-size:13px;line-height:1.6;">${_esc(d.primary_lesson)}</div>
        ${d.what_if_better ? `<div style="margin-top:8px;color:var(--text-secondary);font-size:11px;line-height:1.6;">💭 <strong>若改进:</strong> ${_esc(d.what_if_better)}</div>` : ''}
      </div>` : ''}
      ${(d.pros && d.pros.length) || (d.cons && d.cons.length) ? `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;">
        <div class="oc-card" style="border-left:3px solid var(--color-up);">
          <div style="color:var(--color-up);font-weight:600;font-size:12px;margin-bottom:6px;">✅ 决策合理之处</div>
          <ul style="margin:0;padding-left:18px;font-size:11px;line-height:1.7;">
            ${(d.pros||[]).map(p => `<li>${_esc(p)}</li>`).join('') || '<li style="color:var(--text-tertiary);">(无)</li>'}
          </ul>
        </div>
        <div class="oc-card" style="border-left:3px solid var(--color-down);">
          <div style="color:var(--color-down);font-weight:600;font-size:12px;margin-bottom:6px;">⚠ 决策不合理之处</div>
          <ul style="margin:0;padding-left:18px;font-size:11px;line-height:1.7;">
            ${(d.cons||[]).map(p => `<li>${_esc(p)}</li>`).join('') || '<li style="color:var(--text-tertiary);">(无)</li>'}
          </ul>
        </div>
      </div>` : ''}
      ${_renderLinkEvaluations(d.link_evaluations)}
      <div style="background:var(--bg-tertiary);padding:10px;border-radius:4px;margin-bottom:14px;font-size:11px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;">
        <span>开仓 <strong>${fmt(d.open_price)}</strong></span>
        <span>平仓 <strong>${fmt(d.close_price)}</strong></span>
        <span>期间高 <strong style="color:var(--color-up);">${fmt(d.period_high)}</strong></span>
        <span>期间低 <strong style="color:var(--color-down);">${fmt(d.period_low)}</strong></span>
        <span>最佳出场 <strong style="color:var(--color-purple);">${fmt(d.best_exit_price)}</strong></span>
      </div>
      <h4 style="color:var(--color-accent);margin:16px 0 6px;font-size:13px;">📥 入场质量分析</h4>
      <div style="color:var(--text-primary);">${_esc(d.entry_analysis||'(暂无)')}</div>
      <h4 style="color:var(--color-accent);margin:16px 0 6px;font-size:13px;">📊 持仓管理分析</h4>
      <div style="color:var(--text-primary);">${_esc(d.mid_analysis||'(暂无)')}</div>
      <h4 style="color:var(--color-accent);margin:16px 0 6px;font-size:13px;">📤 出场质量分析</h4>
      <div style="color:var(--text-primary);">${_esc(d.exit_analysis||'(暂无)')}</div>
      <h4 style="color:var(--color-accent);margin:16px 0 6px;font-size:13px;">🎯 关键转折点</h4>
      <ul style="margin:0;padding-left:20px;">${turning}</ul>
      <h4 style="color:var(--color-purple);margin:16px 0 6px;font-size:13px;">💡 改进建议</h4>
      <div style="color:var(--text-primary);background:rgba(124,77,255,0.08);padding:10px;border-left:3px solid var(--color-purple);border-radius:0 4px 4px 0;">${_esc(d.improvements||'(暂无)')}</div>
      <h4 style="color:var(--color-warning);margin:16px 0 6px;font-size:13px;">📚 可复用教训</h4>
      <ul style="margin:0;padding-left:20px;">${lessons}</ul>
      <div style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border-secondary);font-size:10px;color:var(--text-tertiary);">
        复盘于 ${new Date((d.reviewed_at||0)*1000).toLocaleString('zh-CN')} · LLM ${d.llm_model||'-'}
      </div>
    `;
  }

  function _renderLinkEvaluations(links) {
    if (!links || typeof links !== 'object') return '';
    const _esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const ORDER = [
      ['signal',           '📡 信号生成',     '策略信号本身可信度'],
      ['ai_verify',        '🤖 AI 验证',      'verify_signal 给的判断'],
      ['news_judge',       '📰 新闻判断',     '入场时新闻情绪+解读'],
      ['entry_timing',     '📥 入场时机',     '价格/技术面/市况综合'],
      ['sl_tp_setup',     '🎯 SL/TP 设置',  'AI 止盈止损是否合理'],
      ['mid_management',   '⚙ 持仓管理',     '加/减仓时机'],
      ['diagnose_response','🩺 诊断响应',     'AI 诊断变化是否及时'],
      ['exit_quality',     '📤 出场质量',     '触发原因 + 错过的利润'],
      ['money_management', '💰 资金管理',     '仓位大小/盈亏比'],
    ];
    const cards = ORDER.map(([key, label, hint]) => {
      const v = links[key];
      if (!v) return '';
      const score = v.score || 0;
      const verdict = v.verdict || 'neutral';
      const VERDICT_COLOR = { good: 'var(--color-up)', bad: 'var(--color-down)', neutral: 'var(--text-secondary)' };
      const VERDICT_LABEL = { good: '✓ 好', bad: '✗ 差', neutral: '○ 一般' };
      const color = score >= 75 ? 'var(--color-up)' : score >= 55 ? 'var(--color-warning)' : 'var(--color-down)';
      const lessonsHtml = (v.lessons || []).map(l => `<li>${_esc(l.content || '')}</li>`).join('');
      return `
        <div class="oc-card" style="border-left:3px solid ${color};">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;">
            <div>
              <div style="font-weight:600;font-size:12px;">${label}</div>
              <div class="oc-text-xs oc-muted">${hint}</div>
            </div>
            <div style="text-align:right;flex-shrink:0;margin-left:8px;">
              <div style="font-size:20px;font-weight:700;color:${color};">${score}</div>
              <div class="oc-text-xs" style="color:${VERDICT_COLOR[verdict]||'var(--text-tertiary)'};">${VERDICT_LABEL[verdict] || verdict}</div>
            </div>
          </div>
          <div style="font-size:11px;color:var(--text-primary);line-height:1.6;">${_esc(v.summary || '(LLM 未给出)')}</div>
          ${lessonsHtml ? `<ul style="margin:6px 0 0;padding-left:16px;font-size:10px;color:var(--color-purple);line-height:1.5;">${lessonsHtml}</ul>` : ''}
        </div>`;
    }).join('');
    return `
      <h4 style="color:var(--color-accent);margin:18px 0 8px;font-size:13px;">🔗 链路逐环节深度评估（核心）</h4>
      <div class="oc-text-xs oc-muted" style="margin-bottom:10px;">每个环节独立打分。低分环节 = 这笔交易的薄弱链路，需重点改进。</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill, minmax(280px, 1fr));gap:8px;margin-bottom:14px;">
        ${cards}
      </div>
    `;
  }

  return { init, refresh, render };
})();

window.Review = Review;
