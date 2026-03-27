/* ============================================================
   OpenChart Pro - 指标管理
   ============================================================ */

const Indicators = (() => {
  let overlay = null;

  // 叠加主图的指标（与K线价格同比例尺）
  const OVERLAY_INDICATORS = ['MA', 'EMA', 'SMA', 'BOLL', 'SAR', 'VWAP', 'ICHIMOKU', 'DONCHIAN', 'ENVELOPE', 'CHANLUN', 'FIB_RET', 'FIB_EXT'];

  // 附图指标（有自己的Y轴，显示在独立pane）
  const SUB_INDICATORS = ['MACD', 'RSI', 'KDJ', 'CCI', 'DMI', 'TRIX', 'OBV', 'ATR', 'WILLIAMS', 'STOCH', 'MFI', 'CMF', 'VOL', 'WR', 'BIAS', 'BRAR', 'CR', 'PSY', 'DMA', 'VR', 'MTM', 'EMV', 'AO'];

  function isOverlay(name) {
    return OVERLAY_INDICATORS.includes(name.toUpperCase());
  }

  const BUILTIN = [
    { category: '趋势', items: [
      { name: 'MA',   title: '均线 MA',           desc: '简单移动平均线', main: true },
      { name: 'EMA',  title: '指数均线 EMA',       desc: '指数移动平均线', main: true },
      { name: 'BOLL', title: '布林带 BOLL',        desc: '布林带通道',    main: true },
      { name: 'SAR',  title: '抛物线 SAR',         desc: '停损转向指标',  main: true },
    ]},
    { category: '震荡', items: [
      { name: 'MACD', title: 'MACD',              desc: '异同移动平均线', main: false },
      { name: 'RSI',  title: '相对强弱 RSI',       desc: '相对强弱指标',  main: false },
      { name: 'KDJ',  title: 'KDJ',               desc: '随机指标',     main: false },
    ]},
    { category: '成交量', items: [
      { name: 'VOL',  title: '成交量 VOL',         desc: '成交量柱状图', main: false },
      { name: 'OBV',  title: '能量潮 OBV',         desc: '能量潮指标',   main: false },
    ]},
    { category: '缠论', items: [
      { name: 'CHANLUN', title: '缠论分析', desc: '笔/线段/中枢/买卖点', main: true },
    ]},
    { category: '斐波那契', items: [
      { name: 'FIB_RET', title: '斐波那契回撤', desc: '自动识别ZigZag+回撤水平', main: true },
      { name: 'FIB_EXT', title: '斐波那契扩展', desc: '三点法投射扩展目标', main: true },
    ]},
    { category: '自定义', items: [] },
  ];

  // 从localStorage恢复，默认VOL
  const saved = localStorage.getItem('oc_indicators');
  const activeIndicators = new Set(saved ? JSON.parse(saved) : ['VOL']);

  function saveActive() {
    localStorage.setItem('oc_indicators', JSON.stringify([...activeIndicators]));
  }

  function init() {
    overlay = document.getElementById('indicator-modal');
    if (!overlay) return;
    overlay.querySelector('.modal-close')?.addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

    // 恢复上次的指标（延迟等chart初始化完成）
    setTimeout(() => {
      for (const name of activeIndicators) {
        if (name === 'VOL') continue; // VOL已在initChart中添加
        const ind = BUILTIN.flatMap(c => c.items).find(i => i.name === name);
        if (!ind) continue;
        try {
          if (name === 'CHANLUN') {
            loadChanlun(window.currentSymbol, window.currentInterval, window.currentMarket);
          } else if (name === 'FIB_RET') {
            loadFibonacci('retracement');
          } else if (name === 'FIB_EXT') {
            loadFibonacci('extension');
          } else if (isOverlay(name)) {
            addMainIndicator(name);
          } else {
            addSubIndicator(name);
          }
        } catch(e) { console.warn('[Indicators] 恢复指标失败:', name, e); }
      }
    }, 500);
  }

  function open() {
    if (!overlay) return;
    render();
    overlay.classList.add('show');
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('show');
  }

  function render() {
    const list = overlay.querySelector('.indicator-list');
    if (!list) return;
    list.innerHTML = '';

    for (const cat of BUILTIN) {
      const catEl = document.createElement('div');
      catEl.className = 'indicator-category';
      catEl.textContent = cat.category;
      list.appendChild(catEl);

      for (const ind of cat.items) {
        const el = document.createElement('div');
        el.className = 'indicator-item' + (activeIndicators.has(ind.name) ? ' active' : '');
        el.innerHTML = `
          <div class="ind-info">
            <span class="ind-title">${ind.title}</span>
            <span class="ind-desc">${ind.desc}</span>
          </div>
          <span class="ind-toggle">${activeIndicators.has(ind.name) ? '✓ 已添加' : '+ 添加'}</span>
        `;
        el.addEventListener('click', () => toggle(ind));
        list.appendChild(el);
      }
    }
  }

  function toggle(ind) {
    if (activeIndicators.has(ind.name)) {
      activeIndicators.delete(ind.name);
      if (ind.name === 'CHANLUN') {
        // 缠论特殊处理
        removeChanlun();
      } else if (ind.name === 'FIB_RET') {
        removeFibonacci('retracement');
      } else if (ind.name === 'FIB_EXT') {
        removeFibonacci('extension');
      } else if (isOverlay(ind.name)) {
        // overlay指标：从主图pane移除
        removeIndicator(ind.name, 'candle_pane');
      } else {
        // 副图指标：移除整个pane
        const pane = subPanes.find(p => p.name === ind.name);
        if (pane) removeSubPane(pane.id);
      }
      showToast(`已移除 ${ind.title}`, 'info', 2000);
    } else {
      activeIndicators.add(ind.name);
      if (ind.name === 'CHANLUN') {
        // 缠论特殊处理：从后端获取分析数据
        loadChanlun(window.currentSymbol, window.currentInterval, window.currentMarket);
      } else if (ind.name === 'FIB_RET') {
        loadFibonacci('retracement');
      } else if (ind.name === 'FIB_EXT') {
        loadFibonacci('extension');
      } else if (isOverlay(ind.name)) {
        addMainIndicator(ind.name);
      } else {
        addSubIndicator(ind.name);
      }
      showToast(`已添加 ${ind.title}`, 'success', 2000);
    }
    saveActive();
    render();
  }

  function addCustom(config) {
    const cat = BUILTIN.find(c => c.category === '自定义');
    if (cat) cat.items.push(config);
    registerCustomIndicator(config);
  }

  function isActive(name) {
    return activeIndicators.has(name);
  }

  return { init, open, close, addCustom, isOverlay, isActive, OVERLAY_INDICATORS, SUB_INDICATORS };
})();
