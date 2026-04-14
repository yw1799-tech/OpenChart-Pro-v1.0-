/* ============================================================
   OpenChart Pro - 缠论研判看板
   多级别缠论分析 + 操作建议
   ============================================================ */

const ChanlunVerdict = (() => {

  let _lastSymbol = '';
  let _loading = false;

  function init() {
    // 刷新按钮
    document.addEventListener('click', (e) => {
      if (e.target.closest('#chanlun-verdict-refresh')) {
        refresh();
      }
    });
  }

  function refresh() {
    const symbol = window.currentSymbol || 'BTC-USDT';
    const market = window.currentMarket === 'a' ? 'cn' : (window.currentMarket || 'crypto');
    analyze(symbol, market);
  }

  async function analyze(symbol, market) {
    if (_loading) return;
    if (!market) market = window.currentMarket === 'a' ? 'cn' : (window.currentMarket || 'crypto');

    const content = document.getElementById('chanlun-verdict-content');
    if (!content) return;

    _loading = true;
    _lastSymbol = symbol;

    // 港股symbol转换
    let fetchSymbol = symbol;
    if (market === 'hk' && !symbol.endsWith('.HK')) {
      const code = symbol.replace(/^0+/, '') || '0';
      fetchSymbol = code.padStart(4, '0') + '.HK';
    }

    content.innerHTML = `<div class="cv-loading">
      <div class="chart-spinner" style="margin:auto;"></div>
      <div style="margin-top:12px;">正在分析 <strong>${symbol}</strong> 三级别走势结构...</div>
      <div style="font-size:11px;color:var(--text-tertiary);margin-top:4px;">并行获取多周期K线数据，进行缠论笔/中枢/买卖点分析</div>
    </div>`;

    try {
      const resp = await fetch(`/api/chanlun/verdict?symbol=${encodeURIComponent(fetchSymbol)}&market=${encodeURIComponent(market)}`);
      const data = await resp.json();

      if (data.detail) {
        content.innerHTML = `<div class="cv-error">${data.detail}</div>`;
        _loading = false;
        return;
      }

      render(content, data);
    } catch (e) {
      content.innerHTML = `<div class="cv-error">研判失败: ${e.message}</div>`;
    }

    _loading = false;
  }

  function render(container, data) {
    const action = data.action || 'wait';
    const actionCn = data.action_cn || '空仓等待';
    const confidence = data.confidence || 0;
    const reasoning = data.reasoning || '';
    const levels = data.levels || [];
    const zhongshu = data.zhongshu || [];
    const activeBsp = data.active_bsp || [];
    const nextSignals = data.next_signals || [];
    const keyPrices = data.key_prices || [];
    const exitStrategy = data.exit_strategy || '';
    const updatedAt = data.updated_at ? new Date(data.updated_at).toLocaleTimeString('zh-CN') : '--';
    const currentPrice = data.current_price || 0;

    // 动作颜色
    const actionStyles = {
      wait:       { color: '#f59e0b', bg: 'rgba(245,158,11,0.12)', border: 'rgba(245,158,11,0.3)', icon: '\u23f3' },
      buy:        { color: '#00c853', bg: 'rgba(0,200,83,0.12)', border: 'rgba(0,200,83,0.3)', icon: '\u25b2' },
      sell:       { color: '#ff1744', bg: 'rgba(255,23,68,0.12)', border: 'rgba(255,23,68,0.3)', icon: '\u25bc' },
      hold_long:  { color: '#4caf50', bg: 'rgba(76,175,80,0.08)', border: 'rgba(76,175,80,0.25)', icon: '\u2705' },
      hold_short: { color: '#e57373', bg: 'rgba(229,115,115,0.08)', border: 'rgba(229,115,115,0.25)', icon: '\ud83d\udfe1' },
    };
    const st = actionStyles[action] || actionStyles.wait;

    // 置信度条
    const confBarColor = confidence >= 70 ? '#00c853' : confidence >= 45 ? '#f59e0b' : '#ff1744';

    // 级别走势卡片
    let levelsHtml = '';
    for (const lv of levels) {
      const dirIcon = lv.last_bi_dir === 'down' ? '\u2193' : '\u2191';
      const dirColor = lv.last_bi_dir === 'down' ? 'var(--color-down)' : 'var(--color-up)';
      const sureLabel = lv.last_bi_sure ? '\u5df2\u5b8c\u6210' : '\u672a\u5b8c\u6210';
      const sureClass = lv.last_bi_sure ? 'cv-sure' : 'cv-unsure';
      levelsHtml += `
        <div class="cv-level-row">
          <span class="cv-level-tf">${lv.tf}</span>
          <span class="cv-level-dir" style="color:${dirColor}">${dirIcon}</span>
          <span class="cv-level-trend">${lv.trend}</span>
          <span class="cv-level-price">${_fmtPrice(lv.last_bi_from)} \u2192 ${_fmtPrice(lv.last_bi_to)}</span>
          <span class="${sureClass}">[${sureLabel}]</span>
        </div>`;
    }

    // 中枢卡片
    let zsHtml = '';
    if (zhongshu.length === 0) {
      zsHtml = '<div class="cv-empty-hint">\u65e0\u6709\u6548\u4e2d\u67a2</div>';
    } else {
      for (const zs of zhongshu) {
        const posColor = zs.position.includes('\u4e0a\u65b9') ? 'var(--color-up)' :
                          zs.position.includes('\u4e0b\u65b9') ? 'var(--color-down)' : 'var(--color-warning)';
        const posIcon = zs.position.includes('\u4e0a\u65b9') ? '\u25b2' :
                        zs.position.includes('\u4e0b\u65b9') ? '\u25bc' : '\u25c6';
        zsHtml += `
          <div class="cv-zs-item">
            <div class="cv-zs-tf">${zs.tf}\u4e2d\u67a2</div>
            <div class="cv-zs-range">ZG <strong>${_fmtPrice(zs.zg)}</strong> / ZD <strong>${_fmtPrice(zs.zd)}</strong></div>
            <div class="cv-zs-pos" style="color:${posColor}">${posIcon} ${zs.position}</div>
          </div>`;
      }
    }

    // 等待信号
    let signalsHtml = '';
    if (nextSignals.length === 0) {
      signalsHtml = '<div class="cv-empty-hint">\u65e0\u7b49\u5f85\u4fe1\u53f7</div>';
    } else {
      nextSignals.forEach((sig, i) => {
        const impClass = sig.importance.includes('\u64cd\u4f5c') ? 'cv-imp-primary' :
                         sig.importance.includes('\u786e\u8ba4') ? 'cv-imp-secondary' : 'cv-imp-major';
        signalsHtml += `
          <div class="cv-signal-item">
            <div class="cv-signal-num">${i + 1}</div>
            <div class="cv-signal-body">
              <div class="cv-signal-desc">${sig.desc}</div>
              <div class="cv-signal-cond">\u6761\u4ef6: ${sig.condition}</div>
              <span class="cv-signal-imp ${impClass}">${sig.importance}</span>
            </div>
          </div>`;
      });
    }

    // 有效买卖点
    let bspHtml = '';
    if (activeBsp.length > 0) {
      for (const bsp of activeBsp) {
        const bspColor = bsp.is_buy ? 'var(--color-up)' : 'var(--color-down)';
        const bspIcon = bsp.is_buy ? '\u25b2' : '\u25bc';
        bspHtml += `<span class="cv-bsp-tag" style="color:${bspColor}">${bspIcon} ${bsp.desc} @${_fmtPrice(bsp.price)}</span>`;
      }
    }

    // 关键价位
    let pricesHtml = '';
    if (keyPrices.length > 0) {
      // 标记当前价格在哪个区间
      const sorted = [...keyPrices].sort((a, b) => a.price - b.price);
      for (const kp of sorted) {
        const typeColor = kp.type === 'support' ? 'var(--color-up)' :
                          kp.type === 'resistance' ? 'var(--color-down)' : 'var(--text-tertiary)';
        const isCurrent = Math.abs(kp.price - currentPrice) / currentPrice < 0.001;
        pricesHtml += `
          <div class="cv-price-item${isCurrent ? ' cv-price-current' : ''}">
            <span class="cv-price-val" style="color:${typeColor}">${_fmtPrice(kp.price)}</span>
            <span class="cv-price-desc">${kp.desc}</span>
          </div>`;
      }
    }

    container.innerHTML = `
      <div class="cv-container">
        <!-- 顶部栏 -->
        <div class="cv-header">
          <div class="cv-header-left">
            <span class="cv-title">\u7f20\u8bba\u7814\u5224</span>
            <span class="cv-symbol">${data.symbol || ''}</span>
            <span class="cv-current-price">${_fmtPrice(currentPrice)}</span>
          </div>
          <div class="cv-header-right">
            <button id="chanlun-verdict-refresh" class="btn btn-sm cv-refresh-btn">\u21bb \u5237\u65b0</button>
            <span class="cv-updated">${updatedAt} \u66f4\u65b0</span>
          </div>
        </div>

        <!-- 主体 -->
        <div class="cv-body">
          <!-- 左列：操作建议 -->
          <div class="cv-col cv-col-action">
            <div class="cv-card cv-action-card" style="background:${st.bg};border-color:${st.border}">
              <div class="cv-card-title">\u64cd\u4f5c\u5efa\u8bae</div>
              <div class="cv-action-main" style="color:${st.color}">
                <span class="cv-action-icon">${st.icon}</span>
                <span class="cv-action-text">${actionCn}</span>
              </div>
              <div class="cv-confidence">
                <span class="cv-conf-label">\u7f6e\u4fe1\u5ea6</span>
                <div class="cv-conf-bar">
                  <div class="cv-conf-fill" style="width:${confidence}%;background:${confBarColor}"></div>
                </div>
                <span class="cv-conf-val">${confidence}%</span>
              </div>
              <div class="cv-reasoning">${reasoning}</div>
              ${exitStrategy ? `<div class="cv-exit"><span class="cv-exit-label">\u9000\u51fa\u6761\u4ef6:</span> ${exitStrategy}</div>` : ''}
              ${bspHtml ? `<div class="cv-bsp-tags">${bspHtml}</div>` : ''}
            </div>
          </div>

          <!-- 中列：三级别走势 + 中枢 -->
          <div class="cv-col cv-col-levels">
            <div class="cv-card">
              <div class="cv-card-title">\u4e09\u7ea7\u522b\u8d70\u52bf</div>
              ${levelsHtml || '<div class="cv-empty-hint">\u65e0\u6570\u636e</div>'}
            </div>
            <div class="cv-card">
              <div class="cv-card-title">\u4e2d\u67a2\u4f4d\u7f6e</div>
              ${zsHtml}
            </div>
          </div>

          <!-- 右列：等待信号 + 关键价位 -->
          <div class="cv-col cv-col-signals">
            <div class="cv-card">
              <div class="cv-card-title">\u7b49\u5f85\u4fe1\u53f7</div>
              ${signalsHtml}
            </div>
            <div class="cv-card">
              <div class="cv-card-title">\u5173\u952e\u4ef7\u4f4d</div>
              <div class="cv-prices-list">
                ${pricesHtml || '<div class="cv-empty-hint">\u65e0\u6570\u636e</div>'}
              </div>
            </div>
          </div>
        </div>
      </div>`;
  }

  function _fmtPrice(p) {
    if (p == null) return '--';
    p = parseFloat(p);
    if (p >= 1000) return p.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (p >= 1) return p.toFixed(4);
    return p.toFixed(6);
  }

  return { init, analyze, refresh };
})();
