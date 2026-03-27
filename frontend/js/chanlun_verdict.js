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
    const levels = data.levels || [];
    const zhongshu = data.zhongshu || [];
    const nextSignals = data.next_signals || [];
    const keyPrices = data.key_prices || [];
    const exitStrategy = data.exit_strategy || '';
    const updatedAt = data.updated_at ? new Date(data.updated_at).toLocaleTimeString('zh-CN') : '--';
    const currentPrice = data.current_price || 0;

    // 动作颜色方案
    const actionStyles = {
      wait:       { color: '#f59e0b', bg: 'rgba(245,158,11,0.10)', border: 'rgba(245,158,11,0.35)', label: '空仓等待', icon: '⏳' },
      buy:        { color: '#00c853', bg: 'rgba(0,200,83,0.10)', border: 'rgba(0,200,83,0.35)', label: '买入', icon: '▲' },
      sell:       { color: '#ff1744', bg: 'rgba(255,23,68,0.10)', border: 'rgba(255,23,68,0.35)', label: '卖出', icon: '▼' },
      hold_long:  { color: '#4caf50', bg: 'rgba(76,175,80,0.08)', border: 'rgba(76,175,80,0.30)', label: '继续持有', icon: '✅' },
      hold_short: { color: '#e57373', bg: 'rgba(229,115,115,0.08)', border: 'rgba(229,115,115,0.30)', label: '空头持有', icon: '🟡' },
    };
    const st = actionStyles[action] || actionStyles.wait;
    const confBarColor = confidence >= 70 ? '#00c853' : confidence >= 45 ? '#f59e0b' : '#ff1744';

    // ── 1. 白话建议 ──
    const plainText = _getPlainExplanation(action, levels, zhongshu, currentPrice);

    // ── 2. 走势概览 ──
    let levelsHtml = '';
    const tfNameMap = { '1d': '日线', '4h': '4小时', '1h': '1小时', '15m': '15分钟', '30m': '30分钟' };
    let downCount = 0, upCount = 0;
    for (const lv of levels) {
      const isDown = lv.last_bi_dir === 'down';
      if (isDown) downCount++; else upCount++;
      const arrow = isDown ? '↓' : '↑';
      const arrowColor = isDown ? 'var(--color-down)' : 'var(--color-up)';
      const statusText = isDown ? '下跌中' : '上涨中';
      const sureText = lv.last_bi_sure ? '已完成' : '未完成';
      const tfLabel = tfNameMap[lv.tf] || lv.tf;
      levelsHtml += `
        <div class="cv-overview-row">
          <span class="cv-overview-tf">${tfLabel}</span>
          <span class="cv-overview-arrow" style="color:${arrowColor}">${arrow}</span>
          <span class="cv-overview-status">${statusText}（${sureText}）</span>
          <span class="cv-overview-price">${_fmtPrice(lv.last_bi_from)} → ${_fmtPrice(lv.last_bi_to)}</span>
        </div>`;
    }
    // 走势结论
    let trendConclusion = '';
    if (levels.length > 0) {
      if (downCount === levels.length) {
        trendConclusion = `<div class="cv-overview-conclusion down">结论：${levels.length}个周期方向一致向下 ⬇</div>`;
      } else if (upCount === levels.length) {
        trendConclusion = `<div class="cv-overview-conclusion up">结论：${levels.length}个周期方向一致向上 ⬆</div>`;
      } else {
        trendConclusion = `<div class="cv-overview-conclusion mixed">结论：周期方向不一致，处于震荡状态 ↔</div>`;
      }
    }

    // ── 3. 什么时候可以操作 ──
    let whenHtml = '';
    if (nextSignals.length > 0) {
      nextSignals.forEach((sig, i) => {
        // 将专业术语转为白话
        const plainDesc = _simplifySignalDesc(sig.desc);
        const plainCond = _simplifySignalDesc(sig.condition);
        const levelHint = sig.importance.includes('操作') ? '可以轻仓试探' :
                          sig.importance.includes('确认') ? '更可靠的信号，可以加仓' : '强势信号，趋势可能反转';
        whenHtml += `
          <div class="cv-when-item">
            <div class="cv-when-num">${_circledNum(i + 1)}</div>
            <div class="cv-when-body">
              <div class="cv-when-desc">${plainDesc}</div>
              <div class="cv-when-hint">→ ${levelHint}</div>
            </div>
          </div>`;
      });
    } else {
      // 无信号时根据action给默认提示
      if (action === 'wait') {
        whenHtml = `<div class="cv-when-empty">暂时没有明确的操作信号，继续观望，耐心等待即可。</div>`;
      } else if (action === 'hold_long') {
        whenHtml = `<div class="cv-when-empty">当前持有中，暂无卖出信号。出现卖出信号时会提醒你。</div>`;
      } else {
        whenHtml = `<div class="cv-when-empty">当前已有明确操作建议，请参考上方建议执行。</div>`;
      }
    }

    // ── 4. 关键价位可视化 ──
    let priceBarHtml = '';
    if (keyPrices.length > 0) {
      const sorted = [...keyPrices].sort((a, b) => a.price - b.price);
      const allPrices = sorted.map(k => k.price);
      // 确保当前价格也在范围内
      const minP = Math.min(...allPrices, currentPrice) * 0.995;
      const maxP = Math.max(...allPrices, currentPrice) * 1.005;
      const range = maxP - minP || 1;

      // 找最近支撑和阻力
      const supports = sorted.filter(k => k.type === 'support' && k.price < currentPrice);
      const resistances = sorted.filter(k => k.type === 'resistance' && k.price > currentPrice);
      const nearestSupport = supports.length > 0 ? supports[supports.length - 1] : null;
      const nearestResistance = resistances.length > 0 ? resistances[0] : null;

      // 价位标尺
      let markerHtml = '';
      for (const kp of sorted) {
        const pct = ((kp.price - minP) / range * 100).toFixed(1);
        const typeClass = kp.type === 'support' ? 'support' : 'resistance';
        const label = kp.type === 'support' ? '支撑' : '阻力';
        markerHtml += `<div class="cv-pricebar-mark ${typeClass}" style="left:${pct}%">
          <div class="cv-pricebar-tick"></div>
          <div class="cv-pricebar-label">${_fmtPrice(kp.price)}</div>
          <div class="cv-pricebar-type">${label}</div>
        </div>`;
      }
      // 当前价格标记
      const curPct = ((currentPrice - minP) / range * 100).toFixed(1);
      markerHtml += `<div class="cv-pricebar-current" style="left:${curPct}%">
        <div class="cv-pricebar-cur-dot"></div>
        <div class="cv-pricebar-cur-label">当前 ${_fmtPrice(currentPrice)}</div>
      </div>`;

      // 距离提示
      let distanceHtml = '';
      if (nearestSupport) {
        const distPct = ((currentPrice - nearestSupport.price) / currentPrice * 100).toFixed(1);
        distanceHtml += `<div class="cv-dist-item support">离最近支撑: ${_fmtPrice(nearestSupport.price)}（-${distPct}%）</div>`;
      }
      if (nearestResistance) {
        const distPct = ((nearestResistance.price - currentPrice) / currentPrice * 100).toFixed(1);
        distanceHtml += `<div class="cv-dist-item resistance">离最近阻力: ${_fmtPrice(nearestResistance.price)}（+${distPct}%）</div>`;
      }

      priceBarHtml = `
        <div class="cv-pricebar-wrap">
          <div class="cv-pricebar-track">
            <div class="cv-pricebar-line"></div>
            ${markerHtml}
          </div>
        </div>
        ${distanceHtml ? `<div class="cv-dist-wrap">${distanceHtml}</div>` : ''}`;
    }

    // ── 5. 退出/持仓建议文字 ──
    let holdingTip = '';
    if (action === 'wait') {
      holdingTip = '如果已持仓：关注卖出信号，出现就减仓。';
    } else if (action === 'buy') {
      holdingTip = exitStrategy || '买入后设好止损，跌破买点低价立即退出。';
    } else if (action === 'sell') {
      holdingTip = '卖出信号已出现，建议分批减仓。';
    } else if (action === 'hold_long') {
      holdingTip = exitStrategy || '继续持有，出现卖出信号再考虑离场。';
    }

    // ── 组装HTML ──
    container.innerHTML = `
      <div class="cv-container">
        <!-- 顶部栏 -->
        <div class="cv-header">
          <div class="cv-header-left">
            <span class="cv-symbol">${data.symbol || ''}</span>
            <span class="cv-current-price">当前价 ${_fmtPrice(currentPrice)}</span>
          </div>
          <div class="cv-header-right">
            <button id="chanlun-verdict-refresh" class="btn btn-sm cv-refresh-btn">🔄 刷新</button>
            <span class="cv-updated">${updatedAt} 更新</span>
          </div>
        </div>

        <!-- 单列卡片流 -->
        <div class="cv-body">

          <!-- 卡片1: 当前建议（最醒目） -->
          <div class="cv-card cv-action-card" style="background:${st.bg};border-color:${st.border}">
            <div class="cv-action-top">
              <div class="cv-action-main" style="color:${st.color}">
                <span class="cv-action-icon">${st.icon}</span>
                <span class="cv-action-text">当前建议：${actionCn}</span>
              </div>
              <div class="cv-confidence-badge" style="background:${confBarColor}">
                置信度 ${confidence}%
              </div>
            </div>
            <div class="cv-plain-explain">${plainText.summary}</div>
            ${holdingTip ? `<div class="cv-holding-tip">${holdingTip}</div>` : ''}
          </div>

          <!-- 卡片2: 走势概览 -->
          <div class="cv-card">
            <div class="cv-card-title">📊 走势概览</div>
            <div class="cv-overview-list">
              ${levelsHtml || '<div class="cv-empty-hint">无数据</div>'}
            </div>
            ${trendConclusion}
          </div>

          <!-- 卡片3: 中枢与买卖点 -->
          ${_renderZhongshuCard(zhongshu, activeBsp, currentPrice)}

          <!-- 卡片4: 什么时候可以操作 -->
          <div class="cv-card">
            <div class="cv-card-title">🎯 ${action === 'sell' || action === 'hold_short' ? '什么时候该卖？' : '什么时候可以买？'}</div>
            ${whenHtml}
          </div>

          <!-- 卡片4: 关键价位 -->
          <div class="cv-card">
            <div class="cv-card-title">📍 关键价位</div>
            ${priceBarHtml || '<div class="cv-empty-hint">无关键价位数据</div>'}
          </div>

          <!-- 卡片5: 风险提示（固定底部） -->
          <div class="cv-card cv-risk-card">
            <div class="cv-card-title">⚠ 风险提示</div>
            <div class="cv-risk-text">如果买入后价格跌破买点最低价，说明判断错了，应立即退出。</div>
            <div class="cv-risk-text">操作原则：没有买入信号不买，没有卖出信号不卖。</div>
          </div>

        </div>
      </div>`;
  }

  /**
   * 生成白话解读文字（返回对象 { summary }）
   */
  function _renderZhongshuCard(zhongshu, activeBsp, price) {
    let zsHtml = '';
    if (zhongshu.length > 0) {
      for (const zs of zhongshu) {
        const posColor = zs.position.includes('上方') ? 'var(--color-up)' :
                         zs.position.includes('下方') ? 'var(--color-down)' : 'var(--color-warning)';
        const posIcon = zs.position.includes('上方') ? '📈' :
                        zs.position.includes('下方') ? '📉' : '↔️';
        // 白话解释中枢
        const zsExplain = zs.position.includes('上方') ? '价格强势，在震荡区间上方运行' :
                          zs.position.includes('远在') ? '价格已经跌破震荡区间较远，弱势明显' :
                          zs.position.includes('下方') ? '价格跌破了震荡区间，偏弱' :
                          '价格在震荡区间内来回波动，方向不明';
        const tfName = {'1H':'1小时','4H':'4小时','1D':'日线','30m':'30分钟','1W':'周线'}[zs.tf] || zs.tf;
        zsHtml += `
          <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-primary);">
            <span style="font-size:16px;">${posIcon}</span>
            <div style="flex:1;">
              <div style="font-size:13px;font-weight:500;">${tfName}震荡区间</div>
              <div style="font-size:12px;color:var(--text-tertiary);">${_fmtPrice(zs.zd)} ~ ${_fmtPrice(zs.zg)}</div>
            </div>
            <div style="font-size:12px;color:${posColor};text-align:right;">
              ${zsExplain}
            </div>
          </div>`;
      }
    } else {
      zsHtml = '<div style="color:var(--text-tertiary);font-size:12px;padding:8px 0;">当前周期内无明显震荡区间</div>';
    }

    // 已发生的买卖点
    let bspHtml = '';
    if (activeBsp && activeBsp.length > 0) {
      bspHtml = '<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border-primary);">';
      bspHtml += '<div style="font-size:12px;color:var(--text-secondary);margin-bottom:6px;">近期信号回顾：</div>';
      for (const bsp of activeBsp) {
        const icon = bsp.is_buy ? '🟢' : '🔴';
        const label = bsp.is_buy ? '买入信号' : '卖出信号';
        const typeDesc = _simplifySignalDesc(bsp.desc || bsp.type || '');
        bspHtml += `<div style="font-size:12px;padding:3px 0;">${icon} ${label} @ ${_fmtPrice(bsp.price)} — ${typeDesc}</div>`;
      }
      bspHtml += '</div>';
    }

    return `
      <div class="cv-card">
        <div class="cv-card-title">🏛 震荡区间与信号</div>
        ${zsHtml}
        ${bspHtml}
      </div>`;
  }

  function _getPlainExplanation(action, levels, zhongshu, price) {
    let summary = '';

    if (action === 'wait') {
      const downCount = levels.filter(l => l.last_bi_dir === 'down').length;
      if (downCount >= 2) {
        summary = `简单说：现在别买！${downCount}个周期都在下跌，等跌完再说。`;
      } else if (downCount === 1) {
        summary = '简单说：方向不明朗，大小周期走势不一致，先观望。';
      } else {
        summary = '简单说：趋势偏好，但还没出现明确的买入时机，耐心等待回调。';
      }
    } else if (action === 'buy') {
      summary = '简单说：出现了买入信号！可以分批进场，不要一次满仓。';
    } else if (action === 'sell') {
      summary = '简单说：出现了卖出信号！如果持有仓位，建议分批减仓。';
    } else if (action === 'hold_long') {
      summary = '简单说：趋势向上，继续拿着，等出现卖出信号再考虑离场。';
    } else if (action === 'hold_short') {
      summary = '简单说：趋势向下，空头继续持有，等出现反转信号再操作。';
    } else {
      summary = '暂无明确建议，继续观望。';
    }

    // 补充中枢位置的白话
    if (zhongshu.length > 0) {
      const zs = zhongshu[0];
      if (zs.position.includes('下方')) {
        summary += ` 价格在主要震荡区间（${_fmtPrice(zs.zd)}~${_fmtPrice(zs.zg)}）下方，偏弱。`;
      } else if (zs.position.includes('上方')) {
        summary += ` 价格在主要震荡区间（${_fmtPrice(zs.zd)}~${_fmtPrice(zs.zg)}）上方，偏强。`;
      } else if (zs.position.includes('内部')) {
        summary += ` 价格在震荡区间（${_fmtPrice(zs.zd)}~${_fmtPrice(zs.zg)}）内部，方向不明。`;
      }
    }

    return { summary };
  }

  /** 将信号描述中的专业术语替换为白话 */
  function _simplifySignalDesc(text) {
    if (!text) return '';
    return text
      .replace(/T1买点/g, '反转买入信号')
      .replace(/T2买点/g, '回调买入信号')
      .replace(/T3买点/g, '突破买入信号')
      .replace(/T1卖点/g, '反转卖出信号')
      .replace(/T2卖点/g, '反弹卖出信号')
      .replace(/T3卖点/g, '跌破卖出信号')
      .replace(/第[一二三]类买点/g, '买入信号')
      .replace(/第[一二三]类卖点/g, '卖出信号')
      .replace(/背驰/g, '力度衰竭')
      .replace(/中枢/g, '震荡区间')
      .replace(/ZG/g, '区间上沿')
      .replace(/ZD/g, '区间下沿')
      .replace(/笔完成/g, '走势段完成')
      .replace(/线段/g, '走势段')
      .replace(/趋势背驰/g, '上涨/下跌力度减弱');
  }

  /** 带圈数字 */
  function _circledNum(n) {
    const nums = ['', '①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨'];
    return nums[n] || `(${n})`;
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
