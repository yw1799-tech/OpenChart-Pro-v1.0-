/* ============================================================
   OpenChart Pro - 选股系统（规则筛选 + AI智能推荐）
   ============================================================ */

const Screener = (() => {
  const RULE_TYPES = [
    { value: 'rsi_below', label: 'RSI < 值', params: [{ key: 'period', label: '周期', default: 14 }, { key: 'value', label: '值', default: 30 }] },
    { value: 'rsi_above', label: 'RSI > 值', params: [{ key: 'period', label: '周期', default: 14 }, { key: 'value', label: '值', default: 70 }] },
    { value: 'price_above_ma', label: '价格 > MA', params: [{ key: 'ma_period', label: 'MA周期', default: 200 }] },
    { value: 'price_below_ma', label: '价格 < MA', params: [{ key: 'ma_period', label: 'MA周期', default: 20 }] },
    { value: 'macd_golden_cross', label: 'MACD金叉', params: [{ key: 'bars_lookback', label: '回看', default: 5 }] },
    { value: 'macd_death_cross', label: 'MACD死叉', params: [{ key: 'bars_lookback', label: '回看', default: 5 }] },
    { value: 'volume_above_ma', label: '放量突破', params: [{ key: 'ma_period', label: '均量周期', default: 20 }, { key: 'multiplier', label: '倍数', default: 2 }] },
    { value: 'price_above', label: '价格 > 值', params: [{ key: 'value', label: '价格', default: 100 }] },
    { value: 'price_below', label: '价格 < 值', params: [{ key: 'value', label: '价格', default: 50 }] },
    { value: 'change_pct_above', label: '涨幅 > %', params: [{ key: 'value', label: '%', default: 3 }] },
    { value: 'change_pct_below', label: '跌幅 < %', params: [{ key: 'value', label: '%', default: -5 }] },
    { value: 'boll_upper_break', label: '突破布林上轨', params: [{ key: 'period', label: '周期', default: 20 }, { key: 'multiplier', label: '倍数', default: 2 }] },
    { value: 'boll_lower_break', label: '跌破布林下轨', params: [{ key: 'period', label: '周期', default: 20 }, { key: 'multiplier', label: '倍数', default: 2 }] },
    { value: 'new_high', label: '创N日新高', params: [{ key: 'days', label: '天数', default: 60 }] },
    { value: 'new_low', label: '创N日新低', params: [{ key: 'days', label: '天数', default: 60 }] },
  ];

  let ruleIndex = 0;

  function init() {
    document.getElementById('screener-add-rule')?.addEventListener('click', addRule);
    document.getElementById('screener-run')?.addEventListener('click', runFilter);
    document.getElementById('screener-ai-run')?.addEventListener('click', runAI);
  }

  function addRule() {
    const container = document.getElementById('screener-rules');
    if (!container) return;
    const ruleId = 'rule-' + (ruleIndex++);
    const div = document.createElement('div');
    div.className = 'screener-rule';
    div.id = ruleId;
    let opts = RULE_TYPES.map(r => `<option value="${r.value}">${r.label}</option>`).join('');
    div.innerHTML = `<select class="rule-type" onchange="Screener._onTypeChange('${ruleId}')">${opts}</select><span class="rule-params" id="${ruleId}-params"></span><span class="rule-remove" onclick="Screener._removeRule('${ruleId}')">✕</span>`;
    container.appendChild(div);
    _onTypeChange(ruleId);
  }

  function _onTypeChange(ruleId) {
    const el = document.getElementById(ruleId);
    if (!el) return;
    const type = el.querySelector('.rule-type')?.value;
    const paramsEl = document.getElementById(ruleId + '-params');
    const rt = RULE_TYPES.find(r => r.value === type);
    if (!rt || !paramsEl) return;
    paramsEl.innerHTML = rt.params.map(p =>
      `<label style="font-size:10px;color:var(--text-tertiary);margin-left:4px;">${p.label}</label><input type="number" class="rule-param" data-key="${p.key}" value="${p.default}" style="width:50px;">`
    ).join('');
  }

  function _removeRule(id) { document.getElementById(id)?.remove(); }

  function collectFilters() {
    const filters = [];
    document.querySelectorAll('.screener-rule').forEach(el => {
      const type = el.querySelector('.rule-type')?.value;
      if (!type) return;
      const f = { type };
      el.querySelectorAll('.rule-param').forEach(inp => { f[inp.dataset.key] = parseFloat(inp.value) || 0; });
      filters.push(f);
    });
    return filters;
  }

  function getMarkets() {
    const m = [];
    document.querySelectorAll('.screener-markets input:checked').forEach(cb => m.push(cb.value));
    return m.length ? m : ['crypto'];
  }

  async function runFilter() {
    const btn = document.getElementById('screener-run');
    const el = document.getElementById('screener-filter-results');
    if (!el) return;
    btn.textContent = '⏳ 筛选中...'; btn.disabled = true;
    el.innerHTML = '<div style="color:var(--text-secondary);padding:20px;text-align:center;">正在筛选...</div>';
    try {
      const resp = await fetch('/api/screener/filter', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ markets: getMarkets(), filters: collectFilters(), sort_by: document.getElementById('screener-sort')?.value || 'change_pct', sort_order: document.getElementById('screener-order')?.value || 'desc', limit: 50 }),
      });
      const data = await resp.json();
      const results = data.results || [];
      if (!results.length) { el.innerHTML = '<div style="color:var(--text-tertiary);padding:20px;text-align:center;">未找到符合条件的品种</div>'; return; }
      let h = `<div style="font-size:12px;color:var(--text-secondary);margin-bottom:6px;">找到 ${data.count || results.length} 个品种</div>`;
      h += '<table class="screener-result-table"><thead><tr><th>品种</th><th>市场</th><th>价格</th><th>涨跌幅</th><th>成交量</th><th>RSI</th></tr></thead><tbody>';
      for (const r of results) {
        const cc = r.change_pct > 0 ? 'up' : r.change_pct < 0 ? 'down' : '';
        h += `<tr onclick="switchSymbol('${r.symbol}')"><td><strong>${r.symbol}</strong></td><td>${r.market||''}</td><td>${r.price!=null?r.price.toLocaleString():'--'}</td><td class="${cc}">${r.change_pct!=null?(r.change_pct>=0?'+':'')+r.change_pct.toFixed(2)+'%':'--'}</td><td>${fmtVol(r.volume)}</td><td>${r.rsi_14!=null?r.rsi_14.toFixed(1):'--'}</td></tr>`;
      }
      h += '</tbody></table>';
      el.innerHTML = h;
    } catch (e) { el.innerHTML = `<div style="color:var(--color-down);padding:20px;text-align:center;">筛选失败: ${e.message}</div>`; }
    finally { btn.textContent = '🔍 开始筛选'; btn.disabled = false; }
  }

  async function runAI() {
    const btn = document.getElementById('screener-ai-run');
    const el = document.getElementById('screener-ai-results');
    if (!el) return;

    // 前置检查：LLM API Key
    try {
      const settingsResp = await fetch('/api/settings');
      if (settingsResp.ok) {
        const settings = (await settingsResp.json()).settings || {};
        const provider = settings.llm_provider || 'deepseek';
        const keyField = provider === 'qwen' ? 'qwen_api_key' : 'deepseek_api_key';
        const key = settings[keyField] || '';
        if (!key || key === '****' || key.length < 5) {
          showToast('请先在设置(⚙)中配置 LLM API Key（DeepSeek 或通义千问）', 'warning', 5000);
          el.innerHTML = `<div style="color:var(--color-warning);padding:20px;text-align:center;">
            ⚠️ LLM API Key 未配置<br>
            <span style="font-size:11px;">请点击右上角 ⚙设置 → AI/LLM配置 中填入 API Key</span>
          </div>`;
          return;
        }
      }
    } catch {}

    btn.textContent = '⏳ 分析中...'; btn.disabled = true;
    el.innerHTML = '<div style="color:var(--text-secondary);padding:20px;text-align:center;">🔄 正在采集新闻...</div>';
    try {
      const resp = await fetch('/api/screener/ai-analyze', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ market: getMarkets()[0] || 'crypto', hours: parseInt(document.getElementById('screener-ai-hours')?.value||'24'), min_score: parseInt(document.getElementById('screener-ai-min-score')?.value||'60') }),
      });
      const data = await resp.json();
      if (!data.task_id) { el.innerHTML = '<div style="color:var(--color-down);padding:20px;text-align:center;">AI分析启动失败</div>'; return; }
      el.innerHTML = '<div style="color:var(--text-secondary);padding:20px;text-align:center;">🤖 AI正在分析新闻...</div>';
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        try {
          const sr = await fetch(`/api/screener/ai-status/${data.task_id}`);
          const sd = await sr.json();
          if (sd.progress) el.innerHTML = `<div style="color:var(--text-secondary);padding:20px;text-align:center;">🤖 ${sd.progress}</div>`;
          if (sd.status === 'done') { clearInterval(poll); renderAI(sd.result || []); }
          else if (sd.status === 'error') { clearInterval(poll); el.innerHTML = `<div style="color:var(--color-down);padding:20px;">AI分析失败: ${sd.error||'未知错误'}</div>`; }
        } catch {}
        if (attempts > 60) { clearInterval(poll); el.innerHTML = '<div style="color:var(--color-warning);padding:20px;">AI分析超时</div>'; }
      }, 2000);
    } catch (e) { el.innerHTML = `<div style="color:var(--color-down);padding:20px;">AI分析失败: ${e.message}</div>`; }
    finally { btn.textContent = '🤖 AI分析'; btn.disabled = false; }
  }

  function renderAI(recs) {
    const el = document.getElementById('screener-ai-results');
    if (!el) return;
    if (!recs || !recs.length) {
      el.innerHTML = `<div style="color:var(--text-tertiary);padding:20px;text-align:center;">
        未找到推荐品种<br>
        <span style="font-size:11px;">可能原因：未配置 LLM API Key 或新闻源暂无数据<br>
        请在 ⚙设置 → AI/LLM配置 中填入 DeepSeek 或通义千问的 API Key</span>
      </div>`;
      return;
    }
    let h = `<div style="font-size:12px;color:var(--text-secondary);margin-bottom:8px;">🤖 推荐 ${recs.length} 个品种：</div>`;
    recs.forEach((r, i) => {
      // 兼容不同字段名
      const score = Math.round(r.score || r.total_score || 0);
      const actionRaw = r.reason || r.action || '';
      const headlines = r.related_news || (r.recent_headlines || []).map(t => ({title: t}));
      const signals = r.signals || [];
      const name = r.name || '';

      // 中文翻译操作建议
      const actionMap = {buy:'强烈看多 📈',strong_buy:'强烈看多 📈',sell:'看空 📉',strong_sell:'强烈看空 📉',hold:'持有观望 ⏸️',watch:'关注观察 👀',neutral:'中性 ➖'};
      const actionCN = actionMap[actionRaw.toLowerCase()] || actionRaw;

      // 评分颜色
      let scoreColor = 'var(--text-tertiary)';
      let scoreBg = 'var(--bg-hover)';
      if (score >= 80) { scoreColor = '#00E676'; scoreBg = 'rgba(0,230,118,0.1)'; }
      else if (score >= 60) { scoreColor = 'var(--color-up)'; scoreBg = 'rgba(0,200,83,0.08)'; }
      else if (score >= 40) { scoreColor = 'var(--color-warning)'; scoreBg = 'rgba(245,158,11,0.08)'; }
      else { scoreColor = 'var(--color-down)'; scoreBg = 'rgba(255,23,68,0.08)'; }

      const stars = '★'.repeat(Math.min(5, Math.round(score/20))) + '☆'.repeat(Math.max(0, 5-Math.round(score/20)));

      // 情绪条
      const sentScore = Math.round(r.sentiment_score || 50);
      const sentColor = sentScore >= 60 ? 'var(--color-up)' : sentScore <= 40 ? 'var(--color-down)' : 'var(--color-warning)';
      const sentLabel = sentScore >= 70 ? '积极' : sentScore >= 55 ? '偏多' : sentScore >= 45 ? '中性' : sentScore >= 30 ? '偏空' : '消极';

      h += `<div class="ai-card" style="border-left:3px solid ${scoreColor};background:${scoreBg};">
        <div class="ai-card-header">
          <div>
            <strong style="font-size:14px;">#${i+1} ${r.symbol}</strong>
            <span style="font-size:11px;color:var(--text-tertiary);margin-left:6px;">${name}</span>
          </div>
          <div style="text-align:right;">
            <div class="ai-card-score" style="color:${scoreColor};font-size:22px;">${score}<span style="font-size:12px;color:var(--text-tertiary);">/100</span></div>
          </div>
        </div>
        <div style="color:var(--color-warning);font-size:13px;margin-bottom:4px;">${stars}</div>
        <div style="font-size:13px;font-weight:600;color:${scoreColor};margin-bottom:6px;">${actionCN}</div>`;

      // 三维评分条
      h += `<div style="display:flex;gap:12px;margin:8px 0;font-size:11px;">
        <div style="flex:1;">
          <div style="display:flex;justify-content:space-between;margin-bottom:2px;"><span style="color:var(--text-tertiary);">新闻情绪</span><span style="color:${sentColor};">${sentLabel} ${sentScore}</span></div>
          <div style="height:4px;background:var(--bg-tertiary);border-radius:2px;overflow:hidden;"><div style="width:${sentScore}%;height:100%;background:${sentColor};border-radius:2px;"></div></div>
        </div>
        <div style="flex:1;">
          <div style="display:flex;justify-content:space-between;margin-bottom:2px;"><span style="color:var(--text-tertiary);">技术面</span><span>${r.technical_score||50}</span></div>
          <div style="height:4px;background:var(--bg-tertiary);border-radius:2px;overflow:hidden;"><div style="width:${r.technical_score||50}%;height:100%;background:var(--color-accent);border-radius:2px;"></div></div>
        </div>
        <div style="flex:1;">
          <div style="display:flex;justify-content:space-between;margin-bottom:2px;"><span style="color:var(--text-tertiary);">新闻热度</span><span>${r.news_count||0}条</span></div>
          <div style="height:4px;background:var(--bg-tertiary);border-radius:2px;overflow:hidden;"><div style="width:${Math.min(100,(r.news_count||0)*25)}%;height:100%;background:var(--color-purple);border-radius:2px;"></div></div>
        </div>
      </div>`;

      if (signals.length) h += `<div class="ai-card-signals">${signals.map(s=>`<span>${s}</span>`).join('')}</div>`;

      if (headlines.length) {
        h += `<div style="font-size:11px;color:var(--text-tertiary);margin-top:6px;">📰 关联新闻：</div>`;
        headlines.slice(0,3).forEach(n => {
          const title = typeof n === 'string' ? n : (n.title || '');
          const url = (typeof n === 'object' && n.url) ? n.url : '#';
          h += `<div style="font-size:11px;padding:3px 0;padding-left:16px;"><a href="${url}" target="_blank" style="color:var(--color-accent2);">${title}</a></div>`;
        });
      }

      h += `<div style="display:flex;gap:8px;margin-top:8px;">
        <button class="btn btn-sm btn-primary" onclick="switchSymbol('${r.symbol}')" style="font-size:11px;">📊 查看K线</button>
        <button class="btn btn-sm" onclick="if(typeof AIJudge!=='undefined')AIJudge.analyze('${r.symbol}')" style="font-size:11px;background:var(--color-purple);color:#fff;">🤖 AI研判</button>
      </div></div>`;
    });
    el.innerHTML = h;
  }

  function fmtVol(v) { if(v==null)return'--'; if(v>=1e9)return(v/1e9).toFixed(2)+'B'; if(v>=1e6)return(v/1e6).toFixed(2)+'M'; if(v>=1e3)return(v/1e3).toFixed(1)+'K'; return v.toFixed(0); }

  return { init, addRule, _onTypeChange, _removeRule, runFilter, runAI };
})();
