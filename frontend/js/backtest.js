/* ============================================================
   OpenChart Pro - 回测引擎
   ============================================================ */

const Backtest = (() => {
  let running = false;
  let equityChart = null;

  function init() {
    document.getElementById('bt-run')?.addEventListener('click', run);
    document.getElementById('bt-stop')?.addEventListener('click', stop);
    document.getElementById('bt-export')?.addEventListener('click', exportReport);

    // WebSocket 回测进度
    ws.on('backtest_progress', (data) => {
      updateProgress(data.percent, data.message);
    });
    ws.on('backtest_result', (data) => {
      running = false;
      renderReport(data);
      showToast('回测完成', 'success');
    });
  }

  async function run() {
    if (running) {
      showToast('回测正在进行中', 'warning');
      return;
    }

    const config = collectConfig();
    if (!config.symbol) {
      showToast('请先选择品种', 'warning');
      return;
    }

    running = true;
    updateProgress(0, '正在启动回测...');
    switchBottomTab('backtest');

    try {
      const resp = await fetch('/api/backtest/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config),
      });
      const data = await resp.json();
      if (data.error) {
        throw new Error(data.error);
      }
      // 如果是同步返回结果
      if (data.result) {
        running = false;
        renderReport(data.result);
        showToast('回测完成', 'success');
      }
    } catch (e) {
      running = false;
      showToast(`回测失败: ${e.message}`, 'error');
      updateProgress(0, '回测失败');
    }
  }

  function stop() {
    if (!running) return;
    fetch('/api/backtest/stop', { method: 'POST' }).catch(() => {});
    running = false;
    showToast('回测已停止', 'info');
  }

  function collectConfig() {
    return {
      symbol: window.currentSymbol,
      interval: window.currentInterval,
      startDate: document.getElementById('bt-start')?.value || '',
      endDate: document.getElementById('bt-end')?.value || '',
      initialCapital: parseFloat(document.getElementById('bt-capital')?.value) || 100000,
      commission: parseFloat(document.getElementById('bt-commission')?.value) || 0.001,
      slippage: parseFloat(document.getElementById('bt-slippage')?.value) || 0.0005,
      strategy: document.getElementById('bt-strategy')?.value || 'custom',
      formulaCode: typeof Formula !== 'undefined' ? Formula.getCode?.() : '',
    };
  }

  function updateProgress(percent, message) {
    const bar = document.getElementById('bt-progress-bar');
    const text = document.getElementById('bt-progress-text');
    if (bar) bar.style.width = `${percent}%`;
    if (text) text.textContent = message || `${percent}%`;
  }

  function renderReport(result) {
    const container = document.querySelector('.backtest-report');
    if (!container) return;

    const stats = result.stats || {};
    container.innerHTML = `
      <div class="backtest-stats">
        <div class="backtest-stat-card">
          <div class="stat-label">总收益率</div>
          <div class="stat-value" style="color:${stats.totalReturn >= 0 ? 'var(--color-up)' : 'var(--color-down)'}">${(stats.totalReturn || 0).toFixed(2)}%</div>
        </div>
        <div class="backtest-stat-card">
          <div class="stat-label">年化收益</div>
          <div class="stat-value">${(stats.annualReturn || 0).toFixed(2)}%</div>
        </div>
        <div class="backtest-stat-card">
          <div class="stat-label">最大回撤</div>
          <div class="stat-value" style="color:var(--color-down)">${(stats.maxDrawdown || 0).toFixed(2)}%</div>
        </div>
        <div class="backtest-stat-card">
          <div class="stat-label">夏普比率</div>
          <div class="stat-value">${(stats.sharpe || 0).toFixed(2)}</div>
        </div>
        <div class="backtest-stat-card">
          <div class="stat-label">胜率</div>
          <div class="stat-value">${(stats.winRate || 0).toFixed(1)}%</div>
        </div>
        <div class="backtest-stat-card">
          <div class="stat-label">交易次数</div>
          <div class="stat-value">${stats.totalTrades || 0}</div>
        </div>
        <div class="backtest-stat-card">
          <div class="stat-label">盈亏比</div>
          <div class="stat-value">${(stats.profitFactor || 0).toFixed(2)}</div>
        </div>
        <div class="backtest-stat-card">
          <div class="stat-label">持仓天数(均)</div>
          <div class="stat-value">${(stats.avgHoldDays || 0).toFixed(1)}</div>
        </div>
      </div>
      <div><canvas id="equity-canvas" class="backtest-equity-chart"></canvas></div>
      <div class="table-wrapper">
        <table>
          <thead><tr><th>时间</th><th>方向</th><th>价格</th><th>数量</th><th>盈亏</th></tr></thead>
          <tbody id="bt-trades-body"></tbody>
        </table>
      </div>
    `;

    // 渲染交易记录
    const tbody = document.getElementById('bt-trades-body');
    if (tbody && result.trades) {
      for (const t of result.trades.slice(0, 50)) {
        tbody.innerHTML += `<tr>
          <td>${t.time || ''}</td>
          <td style="color:${t.side === 'buy' ? 'var(--color-up)' : 'var(--color-down)'}">${t.side === 'buy' ? '买入' : '卖出'}</td>
          <td class="mono">${t.price}</td>
          <td class="mono">${t.qty}</td>
          <td class="mono ${t.pnl >= 0 ? 'up' : 'down'}">${t.pnl >= 0 ? '+' : ''}${(t.pnl || 0).toFixed(2)}</td>
        </tr>`;
      }
    }

    // 渲染权益曲线
    renderEquityChart(result.equity || []);
  }

  function renderEquityChart(equityData) {
    const canvas = document.getElementById('equity-canvas');
    if (!canvas || !window.Chart) return;

    if (equityChart) equityChart.destroy();

    equityChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: equityData.map((_, i) => i),
        datasets: [{
          label: '权益曲线',
          data: equityData,
          borderColor: '#2196F3',
          backgroundColor: 'rgba(33,150,243,0.1)',
          borderWidth: 1.5,
          pointRadius: 0,
          fill: true,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { display: false },
          y: { grid: { color: '#1C2333' }, ticks: { color: '#7D8590', font: { size: 10 } } },
        },
      },
    });
  }

  function exportReport() {
    showToast('导出功能开发中', 'info');
  }

  return { init, run, stop };
})();
