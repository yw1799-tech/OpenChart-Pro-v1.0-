/* ============================================================
   OpenChart Pro - 指标管理
   ============================================================ */

const Indicators = (() => {
  let overlay = null;

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
    { category: '自定义', items: [] },
  ];

  const activeIndicators = new Set(['VOL']);

  function init() {
    overlay = document.getElementById('indicator-modal');
    if (!overlay) return;
    overlay.querySelector('.modal-close')?.addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
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
      if (ind.main) {
        removeIndicator(ind.name, 'candle_pane');
      } else {
        const pane = subPanes.find(p => p.name === ind.name);
        if (pane) removeSubPane(pane.id);
      }
      showToast(`已移除 ${ind.title}`, 'info', 2000);
    } else {
      activeIndicators.add(ind.name);
      if (ind.main) {
        addMainIndicator(ind.name);
      } else {
        addSubIndicator(ind.name);
      }
      showToast(`已添加 ${ind.title}`, 'success', 2000);
    }
    render();
  }

  function addCustom(config) {
    const cat = BUILTIN.find(c => c.category === '自定义');
    if (cat) cat.items.push(config);
    registerCustomIndicator(config);
  }

  return { init, open, close, addCustom };
})();
