/* ============================================================
   OpenChart Pro - 公式编辑器（直接使用textarea，确保可用）
   ============================================================ */

const Formula = (() => {
  let textarea = null;

  const DEFAULT_CODE = `// OpenChart Pro 公式编辑器
// 语法: OpenScript (类 Pine Script)
//
// 可用变量: open, high, low, close, volume
// 可用函数: sma, ema, rsi, macd, crossover, crossunder
// 绘图函数: plot(series, color, title)

indicator("双均线交叉", overlay=true)

fast = input(5, title="快线周期")
slow = input(20, title="慢线周期")

plot(sma(close, fast), color="#FF6B6B", title="MA快线")
plot(sma(close, slow), color="#4ECDC4", title="MA慢线")
`;

  function init() {
    const container = document.querySelector('.formula-editor-container');
    if (!container) return;

    // 直接创建textarea编辑器
    textarea = document.createElement('textarea');
    textarea.className = 'formula-textarea';
    textarea.spellcheck = false;
    // 恢复上次保存的代码，没有则用默认
    const savedCode = localStorage.getItem('openchart_formula_current');
    textarea.value = savedCode || DEFAULT_CODE;
    textarea.style.cssText = `
      width: 100%; height: 100%; min-height: 200px;
      resize: none; font-family: 'Consolas','Monaco','Courier New',monospace;
      font-size: 13px; line-height: 1.5; tab-size: 2;
      background: var(--bg-primary); color: var(--text-primary);
      border: 1px solid var(--border-primary); border-radius: 4px;
      padding: 12px; outline: none; box-sizing: border-box;
    `;
    // Tab键输入缩进而不是切换焦点
    textarea.addEventListener('keydown', (e) => {
      if (e.key === 'Tab') {
        e.preventDefault();
        const start = textarea.selectionStart;
        textarea.value = textarea.value.substring(0, start) + '  ' + textarea.value.substring(textarea.selectionEnd);
        textarea.selectionStart = textarea.selectionEnd = start + 2;
      }
    });
    container.appendChild(textarea);

    // 按钮绑定
    document.getElementById('formula-run')?.addEventListener('click', run);
    document.getElementById('formula-save')?.addEventListener('click', save);
    document.getElementById('formula-clear')?.addEventListener('click', clear);
  }

  function getCode() {
    return textarea ? textarea.value : '';
  }

  async function run() {
    const code = getCode();
    if (!code.trim()) {
      showToast('请先编写公式', 'warning');
      return;
    }

    // 自动保存当前代码
    try { localStorage.setItem('openchart_formula_current', code); } catch(e) {}

    const market = window.currentMarket === 'a' ? 'cn' : (window.currentMarket || 'crypto');
    logToConsole('正在执行公式...', 'info');

    try {
      const resp = await fetch('/api/formula/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          code: code,
          mode: 'openscript',
          symbol: window.currentSymbol || 'BTC-USDT',
          interval: window.currentInterval || '1H',
        }),
      });
      const data = await resp.json();
      console.log('[Formula] API返回:', Object.keys(data), 'drawings:', (data.drawings||[]).length, 'result:', (data.result||[]).length);
      if (data.error || data.errors?.length) {
        let errMsg = data.error || data.errors.join('; ');
        // 友好提示不支持的Pine Script语法
        if (errMsg.includes('invalid syntax') || errMsg.includes('解析失败')) {
          errMsg += '\n\n提示: OpenScript 不完全兼容 TradingView Pine Script。\n不支持的语法: :=赋值、array.*、bar_index、strategy.*等。\n支持的函数: sma, ema, rsi, macd, crossover, plot 等基础函数。';
        }
        logToConsole('执行错误: ' + errMsg, 'error');
        showToast('公式执行失败: ' + errMsg.split('\n')[0], 'error');
      } else {
        const plots = data.result || data.plots || [];
        logToConsole(`执行完成，生成 ${plots.length} 条绘图指令`, 'success');

        const drawings = data.drawings || [];

        // 渲染plot指标线
        if (plots.length > 0 && typeof chart !== 'undefined' && chart) {
          renderFormulaPlots(plots);
        }

        // 渲染line/box/label绘图对象
        if (drawings.length > 0 && typeof chart !== 'undefined' && chart) {
          renderFormulaDrawings(drawings);
        }

        const totalCount = plots.length + drawings.length;
        if (totalCount > 0) {
          showToast(`公式执行完成，${plots.length}个指标 + ${drawings.length}个绘图对象`, 'success');
        } else {
          showToast('公式执行完成（无绘图输出）', 'info');
        }
      }
    } catch (e) {
      logToConsole('请求失败: ' + e.message, 'error');
      showToast('公式执行失败', 'error');
    }
  }

  async function save() {
    const code = getCode();
    if (!code.trim()) {
      showToast('请先编写公式', 'warning');
      return;
    }
    const name = prompt('请输入公式名称:');
    if (!name) return;

    // 保存到localStorage
    try {
      const saved = JSON.parse(localStorage.getItem('openchart_formulas') || '{}');
      saved[name] = { code, mode: 'openscript', updatedAt: Date.now() };
      localStorage.setItem('openchart_formulas', JSON.stringify(saved));
      // 同时保存当前编辑器内容
      localStorage.setItem('openchart_formula_current', code);
      logToConsole(`公式 "${name}" 已保存`, 'success');
      showToast(`公式 "${name}" 已保存`, 'success');
    } catch (e) {
      showToast('保存失败: ' + e.message, 'error');
    }
  }

  function clear() {
    if (textarea) textarea.value = '';
  }

  function logToConsole(msg, type) {
    if (typeof appendConsole === 'function') {
      appendConsole(msg, type);
    }
    const output = document.querySelector('.console-output');
    if (!output) return;
    const line = document.createElement('div');
    line.className = 'console-line';
    const time = new Date().toLocaleTimeString('zh-CN');
    const colors = { info: 'var(--color-accent)', warn: 'var(--color-warning)', error: 'var(--color-down)', success: 'var(--color-up)' };
    line.innerHTML = `<span style="color:var(--text-tertiary);margin-right:8px;">[${time}]</span><span style="color:${colors[type] || 'var(--text-secondary)'};">${msg}</span>`;
    output.appendChild(line);
    output.scrollTop = output.scrollHeight;
  }

  // 将公式计算结果渲染到KLineChart
  let formulaNames = [];

  function renderFormulaPlots(plots) {
    if (!chart || !plots || !plots.length) return;

    // 先清除之前的公式指标
    clearFormulaIndicators();

    for (let pi = 0; pi < plots.length; pi++) {
      const p = plots[pi];
      if (p.type !== 'plot' || !p.data) continue;

      const name = 'FORMULA' + pi;
      const title = p.title || ('公式' + pi);
      const color = p.color || ['#FF6B6B','#4ECDC4','#45B7D1','#FFEAA7','#A855F7'][pi % 5];
      const isOverlay = p.overlay !== false;

      // 构建数据映射
      const dataMap = new Map();
      for (const dp of p.data) {
        if (dp.t != null && dp.v != null) {
          dataMap.set(dp.t, dp.v);
        }
      }

      try {
        // KLineChart v9.x 注册指标的正确方式
        klinecharts.registerIndicator({
          name: name,
          shortName: title,
          figures: [
            { key: 'val', title: title + ': ', type: 'line', styles: function() { return { color: color }; } }
          ],
          calc: function(dataList) {
            return dataList.map(function(kl) {
              var v = dataMap.get(kl.timestamp);
              return { val: v !== undefined ? v : null };
            });
          }
        });

        // 添加到图表
        if (isOverlay) {
          chart.createIndicator(name, true, { id: 'candle_pane' });
        } else {
          chart.createIndicator(name, false);
        }

        formulaNames.push(name);
        logToConsole('已添加指标: ' + title + ' (' + color + ')', 'info');
      } catch (e) {
        logToConsole('添加指标失败: ' + title + ' - ' + e.message, 'error');
        console.error('registerIndicator error:', e);
      }
    }
  }

  // 渲染line/box/label绘图对象到KLineChart
  let _formulaOverlayIds = [];

  function renderFormulaDrawings(drawings) {
    console.log('[Formula] renderFormulaDrawings called, count=' + (drawings ? drawings.length : 0));
    if (!chart || !drawings || !drawings.length) return;

    try {
      var chartData = chart.getDataList();
      if (!chartData || !chartData.length) return;

      // 方案：将line drawings转成自定义指标的数据（每条线作为两个点之间的连线）
      // 为每条线创建一个独立的指标figure
      var lineDrawings = drawings.filter(function(d) { return d.type === 'line'; });
      var boxDrawings = drawings.filter(function(d) { return d.type === 'box'; });

      // 先测试一个简单的overlay能不能显示
      try {
        var testData = chartData[chartData.length - 100];
        var testData2 = chartData[chartData.length - 1];
        if (testData && testData2) {
          var testId = chart.createOverlay({
            name: 'segment',
            points: [
              { timestamp: testData.timestamp, value: testData.low },
              { timestamp: testData2.timestamp, value: testData2.high }
            ]
          });
          console.log('[Formula] 测试overlay结果:', testId, typeof testId);
          _formulaOverlayIds.push(testId);
        }
      } catch(e) {
        console.error('[Formula] 测试overlay失败:', e.message, e);
      }

      if (lineDrawings.length > 0) {
        // 把所有线段合并成一个指标，每条线是独立的figure
        var indicatorName = 'FORMULA_LINES';

        // 为每根K线构建数据：每条线在其bar范围内插值
        var calcFn = function(kLineDataList) {
          return kLineDataList.map(function(kl, barIdx) {
            var result = {};
            for (var li = 0; li < lineDrawings.length; li++) {
              var ld = lineDrawings[li];
              var x1 = ld.x1, y1 = ld.y1, x2 = ld.x2, y2 = ld.y2;
              // 只在线段的两个端点bar上有值（KLineChart会自动连线）
              if (barIdx === x1) {
                result['v' + li] = y1;
              } else if (barIdx === x2) {
                result['v' + li] = y2;
              } else if (barIdx > Math.min(x1,x2) && barIdx < Math.max(x1,x2)) {
                // 线性插值
                var ratio = (barIdx - x1) / (x2 - x1);
                result['v' + li] = y1 + ratio * (y2 - y1);
              } else {
                result['v' + li] = null;
              }
            }
            return result;
          });
        };

        // 构建figures配置
        var figures = [];
        for (var li = 0; li < lineDrawings.length; li++) {
          (function(idx, color) {
            figures.push({
              key: 'v' + idx,
              title: '',
              type: 'line',
              styles: function() { return { color: color, size: 1.5 }; }
            });
          })(li, lineDrawings[li].color || '#888');
        }

        try {
          klinecharts.registerIndicator({
            name: indicatorName,
            shortName: '缠论',
            figures: figures,
            calc: calcFn
          });
          chart.createIndicator(indicatorName, true, { id: 'candle_pane' });
          formulaNames.push(indicatorName);
          console.log('[Formula] 线段指标注册成功, ' + lineDrawings.length + '条线');
        } catch(e) {
          console.error('[Formula] 线段指标注册失败:', e.message);
        }
      }

      // Box用overlay方式（矩形）
      for (var bi = 0; bi < boxDrawings.length; bi++) {
        try {
          var bd = boxDrawings[bi];
          var tLeft = chartData[Math.max(0, Math.min(bd.left, chartData.length-1))].timestamp;
          var tRight = chartData[Math.max(0, Math.min(bd.right, chartData.length-1))].timestamp;
          chart.createOverlay({
            name: 'rect',
            points: [
              { timestamp: tLeft, value: bd.top },
              { timestamp: tRight, value: bd.bottom }
            ],
            styles: { polygon: { color: bd.bgcolor || 'rgba(100,100,200,0.2)', borderColor: bd.border_color || '#888', borderSize: 1 } },
            lock: true
          });
        } catch(e) {}
      }

      logToConsole('绘图: ' + lineDrawings.length + '条线段, ' + boxDrawings.length + '个矩形', 'success');
    } catch (e) {
      console.error('[Formula] renderFormulaDrawings异常:', e.message, e);
    }
  }

  function clearFormulaDrawings() {
    if (!chart) return;
    for (var i = 0; i < _formulaOverlayIds.length; i++) {
      try { chart.removeOverlay({ id: _formulaOverlayIds[i] }); } catch(e) {}
    }
    _formulaOverlayIds = [];
  }

  function clearFormulaIndicators() {
    if (!chart) return;
    for (const name of formulaNames) {
      try { chart.removeIndicator('candle_pane', name); } catch(e) {}
      try { chart.removeIndicator(name); } catch(e) {}
    }
    formulaNames = [];
  }

  return { init, run, save, clear, getCode };
})();
