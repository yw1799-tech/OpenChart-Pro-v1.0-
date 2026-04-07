/* ============================================================
   OpenChart Pro - KLineChart 初始化与管理
   ============================================================ */

let chart = null;
let mainPaneId = null;
const subPanes = [];        // 副图 pane 列表 { id, name }
const MAX_SUB_PANES = 4;

/* ---------- 暗色主题配置 (参考TradingView Pro) ---------- */
const darkTheme = {
  grid: {
    show: true,
    horizontal: { show: true, size: 1, color: 'rgba(42,46,57,0.5)', style: 'dash', dashedValue: [3, 3] },
    vertical:   { show: false },
  },
  candle: {
    type: 'candle_solid',
    bar: {
      upColor: 'rgba(14,203,129,0.9)',
      downColor: 'rgba(246,70,93,0.9)',
      noChangeColor: '#838D9E',
      upBorderColor: '#0ecb81',
      downBorderColor: '#f6465d',
      noChangeBorderColor: '#838D9E',
      upWickColor: '#0ecb81',
      downWickColor: '#f6465d',
      noChangeWickColor: '#838D9E',
    },
    priceMark: {
      show: true,
      high: { show: true, color: '#787b86', textSize: 10 },
      low:  { show: true, color: '#787b86', textSize: 10 },
      last: {
        show: true,
        upColor: '#0ecb81',
        downColor: '#f6465d',
        noChangeColor: '#838D9E',
        line: { show: true, style: 'dash', dashedValue: [6, 4], size: 1 },
        text: { show: true, size: 11, paddingLeft: 8, paddingTop: 4, paddingRight: 8, paddingBottom: 4, borderRadius: 2, fontFamily: 'JetBrains Mono, Consolas, monospace' },
      },
    },
    tooltip: {
      showRule: 'always',
      showType: 'standard',
      text: { size: 11, color: '#787b86', marginLeft: 8, marginTop: 6, marginRight: 8, marginBottom: 0 },
    },
  },
  indicator: {
    lastValueMark: { show: false },
    tooltip: { showRule: 'always', showType: 'standard', text: { size: 11 } },
    lines: [
      { color: '#2196F3', size: 1 },   // 蓝
      { color: '#FF9800', size: 1 },   // 橙
      { color: '#AB47BC', size: 1 },   // 紫
      { color: '#26A69A', size: 1 },   // 青绿
      { color: '#EF5350', size: 1 },   // 红
    ],
  },
  xAxis: {
    show: true,
    size: 'auto',
    axisLine: { show: false },
    tickLine: { show: false },
    tickText: { show: true, color: '#787b86', size: 11, fontFamily: 'JetBrains Mono, Consolas, monospace' },
  },
  yAxis: {
    show: true,
    size: 'auto',
    position: 'right',
    type: 'normal',
    inside: false,
    axisLine: { show: false },
    tickLine: { show: false },
    tickText: { show: true, color: '#787b86', size: 11, fontFamily: 'JetBrains Mono, Consolas, monospace' },
  },
  crosshair: {
    show: true,
    horizontal: {
      show: true,
      line: { show: true, style: 'dash', dashedValue: [4, 4], size: 1, color: 'rgba(120,123,134,0.4)' },
      text: { show: true, size: 11, color: '#D1D4DC', borderRadius: 2, paddingLeft: 8, paddingRight: 8, paddingTop: 4, paddingBottom: 4, backgroundColor: '#363A45', borderColor: '#505050', borderSize: 1, fontFamily: 'JetBrains Mono, Consolas, monospace' },
    },
    vertical: {
      show: true,
      line: { show: true, style: 'dash', dashedValue: [4, 4], size: 1, color: 'rgba(120,123,134,0.4)' },
      text: { show: true, size: 11, color: '#D1D4DC', borderRadius: 2, paddingLeft: 8, paddingRight: 8, paddingTop: 4, paddingBottom: 4, backgroundColor: '#363A45', borderColor: '#505050', borderSize: 1, fontFamily: 'JetBrains Mono, Consolas, monospace' },
    },
  },
  separator: { size: 1, color: 'rgba(42,46,57,0.8)', activeBackgroundColor: 'rgba(33,150,243,0.2)' },
};

/* ---------- 图表初始化 ---------- */
function initChart() {
  if (typeof klinecharts === 'undefined') {
    console.error('[Chart] klinecharts 库未加载');
    return;
  }

  const container = document.getElementById('chart-container');
  if (!container) {
    console.error('[Chart] 未找到 #chart-container');
    return;
  }

  // 确保容器有尺寸
  if (container.clientHeight < 50) {
    container.style.height = '100%';
    container.style.minHeight = '400px';
  }

  chart = klinecharts.init(container, {
    styles: darkTheme,
    locale: 'zh-CN',
    customApi: {
      formatDate: (dateTimeFormat, timestamp, format, type) => {
        const d = new Date(timestamp);
        const pad = (n) => String(n).padStart(2, '0');
        if (type === 'xAxis') {
          return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
        }
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
      },
    },
  });

  // 注册BOLL指标 — 高级配色
  try {
    klinecharts.registerIndicator({
      name: 'BOLL',
      shortName: 'BOLL',
      calcParams: [20, 2],
      precision: 2,
      figures: [
        { key: 'up',   title: 'UP: ',   type: 'line' },
        { key: 'mid',  title: 'MID: ',  type: 'line' },
        { key: 'dn',   title: 'DN: ',   type: 'line' },
      ],
      styles: {
        lines: [
          { color: 'rgba(33,150,243,0.45)', size: 1 },   // 上轨 - 淡蓝
          { color: 'rgba(255,152,0,0.6)', size: 1 },      // 中轨 - 暖橙
          { color: 'rgba(33,150,243,0.45)', size: 1 },   // 下轨 - 淡蓝
        ],
      },
      calc: (dataList, { calcParams }) => {
        const period = calcParams[0];
        const stdDevMultiplier = calcParams[1];
        return dataList.map((kLineData, i) => {
          if (i < period - 1) return {};
          let sum = 0;
          for (let j = i - period + 1; j <= i; j++) {
            sum += dataList[j].close;
          }
          const mid = sum / period;
          let devSum = 0;
          for (let j = i - period + 1; j <= i; j++) {
            const diff = dataList[j].close - mid;
            devSum += diff * diff;
          }
          const stdDev = Math.sqrt(devSum / period);
          return {
            up:  mid + stdDevMultiplier * stdDev,
            mid: mid,
            dn:  mid - stdDevMultiplier * stdDev,
          };
        });
      },
    });
    console.log('[Chart] 已注册自定义BOLL指标样式');
  } catch(e) {
    console.warn('[Chart] 注册BOLL样式失败:', e);
  }

  // 注册缠论分析指标（自定义绘制：笔/线段/中枢/买卖点）
  try {
    klinecharts.registerIndicator({
      name: 'CHANLUN',
      shortName: '缠论',
      calcParams: [],
      figures: [],
      draw: ({ ctx, bounding, barSpace, visibleRange, indicator, xAxis, yAxis }) => {
        const dataList = chart.getDataList();
        if (!dataList || !dataList.length || !window._chanlunData) return false;
        const cl = window._chanlunData;

        // loadMore 后旧缠论数据的 bar_index 需要加偏移（新K线插入开头导致索引右移）
        // loadChanlun 重新分析后 offset 自动归零
        const indexOffset = dataList.length - (window._chanlunBarCount || dataList.length);

        function barToX(barIdx) {
          return xAxis.convertToPixel(barIdx + indexOffset);
        }
        // 价格 -> 像素y坐标
        function priceToY(price) {
          return yAxis.convertToPixel(price);
        }

        ctx.save();

        const adjFrom = visibleRange.from - indexOffset;
        const adjTo   = visibleRange.to - indexOffset;

        // ---- 1. 画中枢（TradingView风格：绿色半透明矩形）----
        if (cl.zs_list) {
          for (const zs of cl.zs_list) {
            if (zs.end_x < adjFrom || zs.begin_x > adjTo) continue;
            const x1 = barToX(zs.begin_x);
            const x2 = barToX(zs.end_x);
            const y1 = priceToY(zs.zg);
            const y2 = priceToY(zs.zd);

            if (zs.level === 'seg') {
              // 线段中枢 - 绿色（与TradingView一致）
              ctx.fillStyle = 'rgba(0,200,83,0.08)';
              ctx.strokeStyle = 'rgba(0,200,83,0.30)';
              ctx.lineWidth = 1.5;
            } else {
              // 笔中枢 - 浅绿色
              ctx.fillStyle = 'rgba(0,200,83,0.05)';
              ctx.strokeStyle = 'rgba(0,200,83,0.20)';
              ctx.lineWidth = 1;
            }
            ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
            ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);

            // 中枢上下沿价格标注
            ctx.font = '9px sans-serif';
            ctx.fillStyle = 'rgba(0,200,83,0.5)';
            const zgStr = zs.zg >= 1000 ? zs.zg.toFixed(1) : zs.zg.toFixed(2);
            const zdStr = zs.zd >= 1000 ? zs.zd.toFixed(1) : zs.zd.toFixed(2);
            ctx.fillText(zgStr, x1 + 3, y1 + 10);
            ctx.fillText(zdStr, x1 + 3, y2 - 3);
          }
        }

        // ---- 2. 画笔（灰色细线）----
        if (cl.bi_list) {
          for (const bi of cl.bi_list) {
            if (bi.end_x < adjFrom || bi.begin_x > adjTo) continue;
            const x1 = barToX(bi.begin_x);
            const y1 = priceToY(bi.begin_y);
            const x2 = barToX(bi.end_x);
            const y2 = priceToY(bi.end_y);

            ctx.beginPath();
            ctx.moveTo(x1, y1);
            ctx.lineTo(x2, y2);
            ctx.strokeStyle = bi.is_sure ? 'rgba(150,150,150,0.6)' : 'rgba(150,150,150,0.3)';
            ctx.lineWidth = 1;
            if (!bi.is_sure) ctx.setLineDash([4, 3]);
            else ctx.setLineDash([]);
            ctx.stroke();
          }
          ctx.setLineDash([]);
        }

        // ---- 3. 画线段（橙色虚线，与TradingView一致）----
        if (cl.seg_list) {
          for (const seg of cl.seg_list) {
            if (seg.end_x < adjFrom || seg.begin_x > adjTo) continue;
            const x1 = barToX(seg.begin_x);
            const y1 = priceToY(seg.begin_y);
            const x2 = barToX(seg.end_x);
            const y2 = priceToY(seg.end_y);

            ctx.beginPath();
            ctx.moveTo(x1, y1);
            ctx.lineTo(x2, y2);
            ctx.strokeStyle = seg.is_sure ? '#FF9800' : 'rgba(255,152,0,0.5)';
            ctx.lineWidth = 2;
            // 确认线段用虚线（与TradingView一致），未确认用更疏的虚线
            ctx.setLineDash(seg.is_sure ? [8, 5] : [4, 6]);
            ctx.stroke();
            ctx.setLineDash([]);

            // 线段端点圆点
            ctx.beginPath();
            ctx.arc(x1, y1, 3, 0, Math.PI * 2);
            ctx.fillStyle = '#FF9800';
            ctx.fill();
            ctx.beginPath();
            ctx.arc(x2, y2, 3, 0, Math.PI * 2);
            ctx.fillStyle = '#FF9800';
            ctx.fill();
          }
        }

        // ---- 4. 画买卖点标记（TradingView风格：中文标签 + 底色块）----
        // 买卖点类型转中文：
        //   笔级复合(如"2s,3b"): 取三类（中枢结构优先）
        //   线段级复合(如"S2,3b"): 取首要类型（S2=标准二类更核心）
        function bspTypeToCN(rawType, isSeg) {
          const parts = rawType.split(',').map(s => s.trim());
          const map = { '1':'一', '1p':'一', '2':'二', '2s':'类二', '3':'三', '3a':'三', '3b':'三' };
          if (isSeg) {
            // 线段级：取首要类型（如"2,3b"→"2"→"二"）
            return map[parts[0]] || parts[0];
          }
          // 笔级：三类(中枢) > 标准二类 > 类二 > 一类
          const priority = { '3a':4, '3b':4, '3':4, '2':3, '1':2, '1p':2, '2s':1 };
          let best = parts[0], bestP = priority[parts[0]] || 0;
          for (const p of parts) {
            const pp = priority[p] || 0;
            if (pp > bestP) { bestP = pp; best = p; }
          }
          return map[best] || best;
        }
        if (cl.bsp_list) {
          for (const bsp of cl.bsp_list) {
            if (bsp.x < adjFrom || bsp.x > adjTo) continue;
            const x = barToX(bsp.x);
            const y = priceToY(bsp.y);
            const isSeg = bsp.type.startsWith('S');
            const rawType = isSeg ? bsp.type.substring(1) : bsp.type;

            // 构建中文标签：一买/二买/三买/一卖/二卖/三卖
            const numCN = bspTypeToCN(rawType, isSeg);
            // 线段级加后缀区分：如"二卖" vs "二卖(段)"
            const suffix = isSeg ? '(段)' : '';
            const labelText = bsp.is_buy ? `${numCN}买${suffix}` : `${numCN}卖${suffix}`;

            // 颜色：买=绿，卖=红（与TradingView一致）
            const bgColor = bsp.is_buy ? 'rgba(0,200,83,0.85)' : 'rgba(255,23,68,0.85)';
            const textColor = '#FFFFFF';

            // 标签位置：买点在K线下方，卖点在K线上方
            const fontSize = isSeg ? 11 : 9;
            ctx.font = `bold ${fontSize}px sans-serif`;
            const tw = ctx.measureText(labelText).width;
            const padX = 4, padY = 3;
            const boxW = tw + padX * 2;
            const boxH = fontSize + padY * 2;
            const gap = isSeg ? 12 : 8;

            let boxY;
            if (bsp.is_buy) {
              // 买点标签在下方
              boxY = y + gap;
            } else {
              // 卖点标签在上方
              boxY = y - gap - boxH;
            }

            // 画圆角背景
            const boxX = x - boxW / 2;
            ctx.fillStyle = bgColor;
            ctx.beginPath();
            if (ctx.roundRect) ctx.roundRect(boxX, boxY, boxW, boxH, 3);
            else ctx.rect(boxX, boxY, boxW, boxH);
            ctx.fill();

            // 画文字
            ctx.fillStyle = textColor;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(labelText, x, boxY + boxH / 2);
            ctx.textAlign = 'left';
            ctx.textBaseline = 'alphabetic';

            // 连接线：从标签到K线价格点
            ctx.beginPath();
            ctx.strokeStyle = bgColor;
            ctx.lineWidth = 1;
            ctx.setLineDash([]);
            if (bsp.is_buy) {
              ctx.moveTo(x, y);
              ctx.lineTo(x, boxY);
            } else {
              ctx.moveTo(x, y);
              ctx.lineTo(x, boxY + boxH);
            }
            ctx.stroke();
          }
        }

        ctx.restore();
        return false;
      },
      calc: (dataList) => dataList.map(() => ({})),
    });
    console.log('[Chart] 已注册缠论分析指标');
  } catch(e) {
    console.warn('[Chart] 注册缠论指标失败:', e);
  }

  // 注册艾略特波浪指标（自定义绘制：推动浪/调整浪/预测目标）
  try {
    klinecharts.registerIndicator({
      name: 'ELLIOTT_WAVE',
      shortName: '艾略特',
      calcParams: [],
      figures: [],
      draw: ({ ctx, bounding, barSpace, visibleRange, indicator, xAxis, yAxis }) => {
        const dataList = chart.getDataList();
        if (!dataList || !dataList.length || !window._elliottData) return false;
        const ed = window._elliottData;
        if (!ed.patterns || !ed.patterns.length) return false;

        // 用时间戳反查 dataList 中的真实 bar index，彻底避免 offset 计算错误
        const _tsIndexMap = new Map();
        dataList.forEach((d, i) => _tsIndexMap.set(d.timestamp, i));
        function tsToIndex(ts) {
          if (_tsIndexMap.has(ts)) return _tsIndexMap.get(ts);
          // 找最近的时间戳
          let best = -1, bestDiff = Infinity;
          _tsIndexMap.forEach((idx, t) => {
            const diff = Math.abs(t - ts);
            if (diff < bestDiff) { bestDiff = diff; best = idx; }
          });
          return best;
        }
        function barToX(ts) { return xAxis.convertToPixel(tsToIndex(ts)); }
        function priceToY(p) { return yAxis.convertToPixel(p); }

        // DEBUG
        if (window._elliottDebugOnce !== ed) {
          window._elliottDebugOnce = ed;
          const p0 = ed.patterns[0];
          console.log('[Elliott DEBUG] patterns=', ed.patterns.length,
            'dataList=', dataList.length,
            p0 ? `degree=${p0.degree} waves=${p0.waves?.length} start_ts=${p0.start_ts} end_ts=${p0.end_ts}` : 'no pattern');
          if (p0 && p0.waves && p0.waves[0]) {
            const w = p0.waves[0];
            const idx = tsToIndex(w.begin_ts);
            console.log('[Elliott DEBUG] begin_ts=', w.begin_ts, '-> index=', idx,
              '-> pixelX=', xAxis.convertToPixel(idx), 'begin_y=', w.begin_y,
              '-> pixelY=', yAxis.convertToPixel(w.begin_y));
          }
        }

        // ── 颜色定义（TV风格）──
        const COLOR_MAJOR   = '#2962FF';   // 主浪：蓝色
        const COLOR_SUB     = '#FF6D00';   // 子浪：橙色
        const COLOR_PREDICT = '#26A69A';   // 预测：青绿

        // ── 格式化价格 ──
        function fmtPrice(p) {
          if (p >= 10000) return p.toFixed(0);
          if (p >= 100)   return p.toFixed(1);
          if (p >= 1)     return p.toFixed(2);
          return p.toFixed(5);
        }

        // ── 绘制一层波浪（支持主浪/子浪两种样式）──
        // degree=0: 主浪（蓝色，粗线，大圆圈，括号标签）
        // degree=1: 子浪（橙色，细线，小圆圈，无括号）
        function drawWaveLayer(waves, is_motive, degree) {
          if (!waves || !waves.length) return;
          const isMajor  = degree === 0;
          const color    = isMajor ? COLOR_MAJOR : COLOR_SUB;
          const lineW    = isMajor ? 2.0 : 1.5;
          const R        = isMajor ? 10 : 7;
          const fontSize = isMajor ? 10 : 9;

          // 收集节点（用时间戳查真实像素坐标）
          const pts = [];
          pts.push({ x: barToX(waves[0].begin_ts), y: priceToY(waves[0].begin_y),
                     price: waves[0].begin_y, lbl: null });
          for (const w of waves) {
            pts.push({ x: barToX(w.end_ts), y: priceToY(w.end_y),
                       price: w.end_y, lbl: w.label });
          }

          ctx.save();

          // 折线（只从可视区域内画，起始点在屏幕外时跳过）
          ctx.beginPath();
          ctx.setLineDash([]);
          ctx.lineWidth = lineW;
          ctx.strokeStyle = color;
          let started = false;
          for (let i = 0; i < pts.length; i++) {
            const inView = pts[i].x >= -50 && pts[i].x <= bounding.width + 50;
            if (!started) {
              ctx.moveTo(pts[i].x, pts[i].y);
              if (inView) started = true;
            } else {
              ctx.lineTo(pts[i].x, pts[i].y);
            }
          }
          ctx.stroke();

          // 节点圆圈 + 标签（起始点在屏幕外时不画圆点）
          for (let i = 0; i < pts.length; i++) {
            const pt  = pts[i];
            const lbl = pt.lbl;
            const inView = pt.x >= -20 && pt.x <= bounding.width + 20;

            if (!lbl) {
              if (!inView) continue;   // 起点不在可视区域则跳过
              // 起点小圆
              ctx.beginPath();
              ctx.arc(pt.x, pt.y, isMajor ? 4 : 3, 0, Math.PI * 2);
              ctx.fillStyle = color;
              ctx.fill();
              continue;
            }
            if (!inView) continue;    // 标签点不在可视区域则跳过

            // 标签显示：主浪 (1)(2)(3)(4)(5)，子浪 (a)(b)(c) 小写括号
            let dispLbl;
            if (isMajor) {
              dispLbl = `(${lbl})`;
            } else {
              // ABC 修正浪用小写括号，数字子浪用小写括号
              const isLetter = /^[A-Z]$/.test(lbl);
              dispLbl = isLetter ? `(${lbl.toLowerCase()})` : `(${lbl})`;
            }
            const above   = i > 0 && pt.y <= pts[i - 1].y;
            const cy      = above ? pt.y - R - 5 : pt.y + R + 5;

            // 圆圈
            ctx.beginPath();
            ctx.arc(pt.x, cy, R, 0, Math.PI * 2);
            ctx.fillStyle = color;
            ctx.fill();

            // 标签文字
            ctx.font = `bold ${fontSize}px sans-serif`;
            ctx.fillStyle = '#FFFFFF';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(dispLbl, pt.x, cy);
            ctx.textBaseline = 'alphabetic';

            // 细竖线连接 K 线点与圆圈
            ctx.beginPath();
            ctx.globalAlpha = 0.45;
            ctx.lineWidth = 1;
            ctx.strokeStyle = color;
            ctx.setLineDash([2, 2]);
            ctx.moveTo(pt.x, pt.y);
            ctx.lineTo(pt.x, above ? cy + R : cy - R);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.globalAlpha = 1.0;

            // 价格注释（仅主浪）
            if (isMajor) {
              ctx.font = '9px sans-serif';
              ctx.fillStyle = 'rgba(180,200,220,0.8)';
              ctx.textAlign = 'center';
              ctx.fillText(fmtPrice(pt.price), pt.x, above ? cy - R - 3 : cy + R + 11);
            }
          }

          ctx.restore();
        }

        // ── 绘制预测目标（TV风格：从终点出发的斜线扇形，延伸到右边界）──
        function drawPrediction(pred, endTs) {
          if (!pred || !pred.targets) return;
          const x0 = barToX(endTs);
          // 预测线终点：在价格轴左侧留出标签空间（价格轴约80px宽）
          const x1 = bounding.width - 90;
          // 只有当起点在终点右侧时跳过（无空间画线）
          if (x0 >= x1 - 10) return;

          // 起点 y：优先用预测数据里的 origin_price（如 (b) 顶），兜底用主浪最后一浪终点
          const endPrice = pred.origin_price
            || (majorPat && majorPat.waves && majorPat.waves.length
                ? majorPat.waves[majorPat.waves.length - 1].end_y : null);
          if (!endPrice) { ctx.restore(); return; }
          const startY = priceToY(endPrice);

          const clr = '#2962FF';
          const nextWave = pred.next_wave || '';

          ctx.save();
          ctx.setLineDash([3, 4]);
          ctx.lineWidth = 1;
          ctx.strokeStyle = clr;
          ctx.fillStyle = clr;
          ctx.font = '11px sans-serif';
          ctx.textAlign = 'left';
          ctx.textBaseline = 'middle';

          Object.entries(pred.targets).slice(0, 6).forEach(([name, price]) => {
            const targetY = priceToY(price);
            if (targetY < -50 || targetY > bounding.height + 50) return;

            // 从终点(x0, startY)画斜线到(x1, targetY)
            ctx.globalAlpha = 0.55;
            ctx.beginPath();
            ctx.moveTo(x0, startY);
            ctx.lineTo(x1, targetY);
            ctx.stroke();

            // 末端小圆点
            ctx.globalAlpha = 0.8;
            ctx.setLineDash([]);
            ctx.beginPath();
            ctx.arc(x1, targetY, 2.5, 0, Math.PI * 2);
            ctx.fillStyle = clr;
            ctx.fill();
            ctx.setLineDash([3, 4]);

            // 右侧标签：(A) 0.618 (442.79)
            // 从 key 中提取：等号前 = 浪标签，等号后第一个数 = 比率
            const eqIdx = name.indexOf('=');
            const waveLabel = eqIdx > 0 ? name.substring(0, eqIdx) : (nextWave || name);
            const afterEq = eqIdx >= 0 ? name.substring(eqIdx + 1) : '';
            const ratioMatch = afterEq.match(/^(\d+\.?\d*)/);
            const ratio = ratioMatch ? parseFloat(ratioMatch[1]).toFixed(3).replace(/\.?0+$/, '') : '';
            const label = ratio
              ? `(${waveLabel}) ${ratio} (${fmtPrice(price)})`
              : `(${waveLabel}) (${fmtPrice(price)})`;
            ctx.globalAlpha = 1.0;
            ctx.fillStyle = clr;
            // 标签画在端点左侧，避开右侧价格轴
            ctx.textAlign = 'right';
            ctx.fillText(label, x1 - 6, targetY - 5);
            ctx.textAlign = 'left';
          });

          ctx.restore();
        }

        // ── 绘制模式名 badge ──
        function drawBadge(pat, pts, color) {
          const confPct = Math.round(pat.confidence * 100);
          const txt = `${pat.pattern_name}  ${confPct}%`;
          ctx.save();
          ctx.font = 'bold 11px sans-serif';
          const tw = ctx.measureText(txt).width;
          // badge 锚定到可视区域内的第一个有标签的波浪点（避免跑到顶部或左上角）
          const visiblePts = pts.filter(p => p.x >= 10 && p.x <= bounding.width - 10);
          const anchorPt = visiblePts.length > 0 ? visiblePts[0] : pts[1] || pts[0];
          const bx = Math.max(10, anchorPt.x - tw / 2);
          // y 锚定到第一个可见点的上方（而不是最高价点上方，避免跑到顶部）
          const by = anchorPt.y - 30;
          ctx.fillStyle = 'rgba(10,14,26,0.75)';
          if (ctx.roundRect) ctx.roundRect(bx - 4, by - 13, tw + 10, 18, 3);
          else ctx.rect(bx - 4, by - 13, tw + 10, 18);
          ctx.fill();
          ctx.fillStyle = color;
          ctx.textAlign = 'left';
          ctx.fillText(txt, bx + 1, by);
          ctx.restore();
        }

        // ── 主绘制逻辑（degree=0主浪蓝色，degree=1子浪橙色）──
        const majorPat = ed.patterns.find(p => p.degree === 0);
        const minorPat = ed.patterns.find(p => p.degree === 1);

        // 1. 先画子浪（橙色，在下层）
        if (minorPat) drawWaveLayer(minorPat.waves, minorPat.is_motive, 1);

        // 2. 再画主浪（蓝色，在上层）
        if (majorPat) {
          drawWaveLayer(majorPat.waves, majorPat.is_motive, 0);

          // badge
          const pts = [];
          if (majorPat.waves.length) {
            pts.push({ x: barToX(majorPat.waves[0].begin_ts), y: priceToY(majorPat.waves[0].begin_y) });
            for (const w of majorPat.waves) pts.push({ x: barToX(w.end_ts), y: priceToY(w.end_y) });
          }
          if (pts.length) drawBadge(majorPat, pts, COLOR_MAJOR);

          // 预测目标
          if (ed.predictions && ed.predictions[0]) {
            const pred = ed.predictions[0];
            drawPrediction(pred, pred.end_ts || majorPat.end_ts);
          }
        }

        return false;
      },
      calc: (dataList) => dataList.map(() => ({})),
    });
    console.log('[Chart] 已注册艾略特波浪指标');
  } catch(e) {
    console.warn('[Chart] 注册艾略特波浪指标失败:', e);
  }

  // 注册斐波那契回撤指标（自定义绘制）
  try {
    const FIB_COLORS = {
      0.0:   'rgba(128,128,128,0.8)',
      0.236: 'rgba(244,67,54,0.6)',
      0.382: 'rgba(255,152,0,0.6)',
      0.5:   'rgba(76,175,80,0.7)',
      0.618: 'rgba(33,150,243,0.7)',
      0.786: 'rgba(156,39,176,0.6)',
      1.0:   'rgba(128,128,128,0.8)',
    };
    const FIB_FILL_ALPHA = 0.04;

    function _fibGetColor(ratio, mode) {
      if (FIB_COLORS[ratio]) return FIB_COLORS[ratio];
      // 扩展水平用蓝紫色系
      if (ratio > 1.0) return 'rgba(103,58,183,0.6)';
      // 插值
      return 'rgba(158,158,158,0.5)';
    }

    function _drawFibLevels(ctx, bounding, visibleRange, xAxis, yAxis, fibData, mode) {
      if (!fibData || !fibData.levels || fibData.levels.length === 0) return;
      if (fibData.error) return;

      const dataList = chart.getDataList();
      if (!dataList || !dataList.length) return;

      const trend = fibData.trend;
      const levels = fibData.levels;
      const startPt = fibData.start;
      const endPt = fibData.end;

      // 画布边界
      const leftX = bounding.left || 0;
      const rightX = bounding.width - (bounding.right || 0);
      const topY = bounding.top || 0;
      const bottomY = bounding.height - (bounding.bottom || 0);

      ctx.save();

      // 1. 半透明填充各层之间
      for (let i = 0; i < levels.length - 1; i++) {
        const y1 = yAxis.convertToPixel(levels[i].price);
        const y2 = yAxis.convertToPixel(levels[i + 1].price);
        if (y1 === y2) continue;

        let fillColor;
        if (mode === 'extension') {
          fillColor = 'rgba(103,58,183,' + FIB_FILL_ALPHA + ')';
        } else if (trend === 'up') {
          fillColor = 'rgba(244,67,54,' + FIB_FILL_ALPHA + ')';
        } else {
          fillColor = 'rgba(76,175,80,' + FIB_FILL_ALPHA + ')';
        }
        ctx.fillStyle = fillColor;
        ctx.fillRect(leftX, Math.min(y1, y2), rightX - leftX, Math.abs(y2 - y1));
      }

      // 2. 画趋势线（start到end虚线）
      if (startPt && endPt) {
        const sx = xAxis.convertToPixel(startPt.x);
        const sy = yAxis.convertToPixel(startPt.y);
        const ex = xAxis.convertToPixel(endPt.x);
        const ey = yAxis.convertToPixel(endPt.y);

        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(ex, ey);
        ctx.strokeStyle = 'rgba(255,255,255,0.3)';
        ctx.lineWidth = 1.5;
        ctx.setLineDash([6, 4]);
        ctx.stroke();
        ctx.setLineDash([]);

        // pivot圆点
        ctx.beginPath();
        ctx.arc(sx, sy, 4, 0, Math.PI * 2);
        ctx.fillStyle = trend === 'up' ? '#f44336' : '#4CAF50';
        ctx.fill();
        ctx.beginPath();
        ctx.arc(ex, ey, 4, 0, Math.PI * 2);
        ctx.fillStyle = trend === 'up' ? '#f44336' : '#4CAF50';
        ctx.fill();
      }

      // 扩展模式画第三个点（C点）
      if (mode === 'extension' && fibData.point_c) {
        const cx = xAxis.convertToPixel(fibData.point_c.x);
        const cy = yAxis.convertToPixel(fibData.point_c.y);
        ctx.beginPath();
        ctx.arc(cx, cy, 4, 0, Math.PI * 2);
        ctx.fillStyle = '#7C4DFF';
        ctx.fill();

        // B->C 虚线
        if (endPt) {
          const bx = xAxis.convertToPixel(endPt.x);
          const by = yAxis.convertToPixel(endPt.y);
          ctx.beginPath();
          ctx.moveTo(bx, by);
          ctx.lineTo(cx, cy);
          ctx.strokeStyle = 'rgba(124,77,255,0.4)';
          ctx.lineWidth = 1;
          ctx.setLineDash([4, 3]);
          ctx.stroke();
          ctx.setLineDash([]);
        }
      }

      // 3. 画斐波那契水平线 + 标签
      const startBarX = startPt ? xAxis.convertToPixel(startPt.x) : leftX;

      for (const lv of levels) {
        const y = yAxis.convertToPixel(lv.price);
        if (y < topY - 20 || y > bottomY + 20) continue;

        const color = _fibGetColor(lv.ratio, mode);

        // 水平线：从趋势起点画到最右侧
        ctx.beginPath();
        ctx.moveTo(Math.min(startBarX, leftX), y);
        ctx.lineTo(rightX, y);
        ctx.strokeStyle = color;
        ctx.lineWidth = (lv.ratio === 0.5 || lv.ratio === 0.618) ? 1.5 : 1;
        if (lv.ratio === 0.0 || lv.ratio === 1.0) {
          ctx.setLineDash([]);
        } else {
          ctx.setLineDash([5, 3]);
        }
        ctx.stroke();
        ctx.setLineDash([]);

        // 右侧标签
        const priceStr = lv.price >= 1000 ? lv.price.toLocaleString('en-US', {maximumFractionDigits: 0})
                       : lv.price >= 1 ? lv.price.toFixed(2) : lv.price.toFixed(6);
        const labelText = `${lv.label} (${priceStr})`;

        ctx.font = '10px JetBrains Mono, Consolas, monospace';
        const textWidth = ctx.measureText(labelText).width;

        // 标签背景
        const labelX = rightX - textWidth - 12;
        const labelY = y - 7;
        ctx.fillStyle = 'rgba(30,33,40,0.85)';
        ctx.fillRect(labelX - 4, labelY - 1, textWidth + 8, 14);

        // 标签文字
        ctx.fillStyle = color;
        ctx.textAlign = 'left';
        ctx.fillText(labelText, labelX, y + 3);
      }

      ctx.restore();
    }

    klinecharts.registerIndicator({
      name: 'FIB_RETRACEMENT',
      shortName: 'Fib回撤',
      calcParams: [],
      figures: [],
      draw: ({ ctx, bounding, barSpace, visibleRange, indicator, xAxis, yAxis }) => {
        _drawFibLevels(ctx, bounding, visibleRange, xAxis, yAxis, window._fibRetData, 'retracement');
        return false;
      },
      calc: (dataList) => dataList.map(() => ({})),
    });

    klinecharts.registerIndicator({
      name: 'FIB_EXTENSION',
      shortName: 'Fib扩展',
      calcParams: [],
      figures: [],
      draw: ({ ctx, bounding, barSpace, visibleRange, indicator, xAxis, yAxis }) => {
        _drawFibLevels(ctx, bounding, visibleRange, xAxis, yAxis, window._fibExtData, 'extension');
        return false;
      },
      calc: (dataList) => dataList.map(() => ({})),
    });

    console.log('[Chart] 已注册斐波那契回撤/扩展指标');
  } catch(e) {
    console.warn('[Chart] 注册斐波那契指标失败:', e);
  }

  // 不再自定义RSI，使用KLineChart内置RSI但修改参数为只有1条线
  // 内置RSI默认参数[6,12,24]改为[14]
  // 通过覆盖注册实现

  // 默认添加成交量副图
  try {
    chart.createIndicator('VOL', false, { id: 'vol_pane', height: 80 });
    subPanes.push({ id: 'vol_pane', name: 'VOL' });
  } catch(e) {
    console.warn('[Chart] 添加成交量副图失败:', e);
  }

  // 缩放/滚动后自动刷新艾略特波浪（防抖800ms，避免频繁请求）
  let _elliottDebounce = null;
  function _onViewChange() {
    if (!isElliottActive()) return;
    clearTimeout(_elliottDebounce);
    _elliottDebounce = setTimeout(() => {
      loadElliottWave(window.currentSymbol, window.currentInterval, window.currentMarket);
    }, 800);
  }
  try {
    chart.subscribeAction(klinecharts.ActionType.OnZoom,   _onViewChange);
    chart.subscribeAction(klinecharts.ActionType.OnScroll, _onViewChange);
  } catch(e) {
    console.warn('[Chart] 无法订阅缩放/滚动事件:', e);
  }

  // 懒加载历史K线：监听滚动事件，当可视范围接近左边界时自动加载
  window._loadMoreReady = false;
  window._loadMoreNoMore = false;
  window._loadMoreFetching = false;

  async function fetchMoreHistory() {
    if (!window._loadMoreReady || window._loadMoreFetching || window._loadMoreNoMore) return;

    // 检查是否滚动到了左边界附近
    const visRange = chart.getVisibleRange();
    if (!visRange || visRange.from > 20) return;  // 距左边界还有20根以上，不触发

    window._loadMoreFetching = true;
    try {
      const s   = window.currentSymbol;
      const iv  = window.currentInterval;
      const mkt = window.currentMarket === 'a' ? 'cn' : (window.currentMarket || 'crypto');
      const dataList = chart.getDataList();
      if (!dataList || !dataList.length) return;
      const endTs = dataList[0].timestamp;

      const resp = await fetch(
        `/api/klines?symbol=${encodeURIComponent(s)}&interval=${encodeURIComponent(iv)}&limit=500&market=${encodeURIComponent(mkt)}&end_time=${endTs}`
      );
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = await resp.json();
      const raw = d.candles || d.data || d;
      if (!Array.isArray(raw) || raw.length === 0) {
        window._loadMoreNoMore = true;
        console.log('[Chart] 无更多历史K线');
        return;
      }
      const more = raw.map(k => ({
        timestamp: k.timestamp || k.time || k.t,
        open:      parseFloat(k.open  || k.o),
        high:      parseFloat(k.high  || k.h),
        low:       parseFloat(k.low   || k.l),
        close:     parseFloat(k.close || k.c),
        volume:    parseFloat(k.volume || k.v || 0),
        turnover:  parseFloat(k.turnover || k.amount || 0),
      }));
      if (more.length < 500) window._loadMoreNoMore = true;
      chart.applyMoreData(more, more.length < 500);
      console.log(`[Chart] 懒加载 ${more.length} 根历史K线，总计: ${chart.getDataList().length}`);

      // 加载更多K线后，重新运行缠论分析（等图表数据稳定后再跑）
      if (typeof isChanlunActive === 'function' && isChanlunActive()) {
        setTimeout(() => {
          console.log(`[Chart] loadMore后重新分析缠论，当前K线数: ${chart.getDataList().length}`);
          loadChanlun();
        }, 800);
      }
    } catch (e) {
      console.error('[Chart] 懒加载历史K线失败:', e);
    } finally {
      window._loadMoreFetching = false;
    }
  }

  // 用滚动事件触发（500ms防抖），替代 loadMore 回调
  let _loadMoreTimer = null;
  chart.subscribeAction(klinecharts.ActionType.OnScroll, () => {
    if (_loadMoreTimer) clearTimeout(_loadMoreTimer);
    _loadMoreTimer = setTimeout(fetchMoreHistory, 500);
  });

  console.log('[Chart] 初始化完成');
}

/* ---------- K线数据加载 ---------- */
async function loadKlines(symbol, interval, market) {
  if (!chart) return;

  // 自动推断market
  if (!market) market = window.currentMarket || 'crypto';
  // 前端用'a'表示A股，后端用'cn'
  const apiMarket = market === 'a' ? 'cn' : market;

  const loading = document.getElementById('chart-loading');
  if (loading) loading.classList.add('show');

  try {
    const resp = await fetch(`/api/klines?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=1000&market=${encodeURIComponent(apiMarket)}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    // 服务端返回 { candles: [...] } 或 { data: [...] } 或直接数组
    const raw = data.candles || data.data || data;
    if (!Array.isArray(raw) || raw.length === 0) {
      console.warn('[Chart] 无K线数据');
      return;
    }
    const klines = raw.map(k => ({
      timestamp: k.timestamp || k.time || k.t,
      open:      parseFloat(k.open  || k.o),
      high:      parseFloat(k.high  || k.h),
      low:       parseFloat(k.low   || k.l),
      close:     parseFloat(k.close || k.c),
      volume:    parseFloat(k.volume || k.v || 0),
      turnover:  parseFloat(k.turnover || k.amount || 0),
    }));

    chart.applyNewData(klines);

    // 初始数据加载完毕，500ms后解锁懒加载（等图表渲染稳定）
    window._loadMoreReady = false;
    window._loadMoreFetching = false;
    window._loadMoreNoMore = false;   // 重置：新周期可能有更多历史数据
    setTimeout(() => { window._loadMoreReady = true; }, 500);

    // 更新水印
    const wm = document.getElementById('chart-watermark');
    if (wm) wm.textContent = symbol;

    // 用最后一根K线更新右侧信息面板
    if (klines.length > 0) {
      const last = klines[klines.length - 1];
      const prev = klines.length > 1 ? klines[klines.length - 2] : last;
      updateInfoPanelFromKline(last, prev);
      // 计算并显示指标值
      updateIndicatorValues(klines);
    }

    console.log(`[Chart] 已加载 ${klines.length} 根K线: ${symbol} ${interval}`);

    // 如果缠论分析已启用，自动刷新
    if (isChanlunActive()) {
      loadChanlun(symbol, interval, market);
    }
    // 如果艾略特波浪已启用，自动刷新
    if (isElliottActive()) {
      loadElliottWave(symbol, interval, market);
    }
    // 如果斐波那契已启用，自动刷新
    if (isFibActive('FIB_RET')) loadFibonacci('retracement');
    if (isFibActive('FIB_EXT')) loadFibonacci('extension');
  } catch (err) {
    console.error('[Chart] 加载K线失败:', err);
    showToast(`加载K线数据失败: ${err.message}`, 'error');
  } finally {
    if (loading) loading.classList.remove('show');
  }
}

/* ---------- 实时更新 ---------- */
function updateCandle(candleData) {
  if (!chart) return;
  chart.updateData({
    timestamp: candleData.timestamp || candleData.t,
    open:      parseFloat(candleData.open  || candleData.o),
    high:      parseFloat(candleData.high  || candleData.h),
    low:       parseFloat(candleData.low   || candleData.l),
    close:     parseFloat(candleData.close || candleData.c),
    volume:    parseFloat(candleData.volume || candleData.v || 0),
    turnover:  parseFloat(candleData.turnover || candleData.amount || 0),
  });
}

/* ---------- 切换 ---------- */
async function switchInterval(interval) {
  if (!window.currentSymbol) return;
  const oldInterval = window.currentInterval;
  window.currentInterval = interval;

  // 更新按钮状态
  document.querySelectorAll('.interval-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.interval === interval);
  });

  // 重新加载K线
  await loadKlines(window.currentSymbol, interval, window.currentMarket);

  // 切换 WebSocket 订阅
  if (ws && ws.ws) {
    ws.switch(window.currentSymbol, oldInterval, window.currentSymbol, interval);
  }
}

async function switchSymbol(symbol, market) {
  console.log('[Chart] switchSymbol:', symbol, 'market:', market);
  const oldSymbol = window.currentSymbol;
  const interval = window.currentInterval;
  window.currentSymbol = symbol;
  if (market) window.currentMarket = market;

  // 更新标题
  document.title = `${symbol} - OpenChart Pro`;

  // 重新加载K线
  try {
    await loadKlines(symbol, interval, window.currentMarket);
  } catch(e) {
    console.error('[Chart] switchSymbol loadKlines failed:', e);
  }

  // 切换 WebSocket 订阅
  try {
    if (typeof ws !== 'undefined' && ws && ws.ws && ws.ws.readyState === WebSocket.OPEN) {
      ws.switch(oldSymbol, interval, symbol, interval);
    }
  } catch(e) {
    console.warn('[Chart] WS switch failed:', e);
  }

  // 更新自选列表高亮
  document.querySelectorAll('.watchlist-item').forEach(item => {
    item.classList.toggle('active', item.dataset.symbol === symbol);
  });

  // 如果当前在缠论研判Tab，自动刷新
  const activeTab = document.querySelector('.bottom-tab.active');
  if (activeTab && activeTab.dataset.tab === 'chanlun-verdict' && typeof ChanlunVerdict !== 'undefined') {
    ChanlunVerdict.analyze(symbol, window.currentMarket === 'a' ? 'cn' : window.currentMarket);
  }
}

/* ---------- 副图 Pane 管理 ---------- */
function addSubPane(name, indicatorName) {
  if (subPanes.length >= MAX_SUB_PANES) {
    showToast(`最多添加 ${MAX_SUB_PANES} 个副图`, 'warning');
    return null;
  }
  if (!chart) return null;

  const paneId = chart.createIndicator(indicatorName || name, false, { id: name.toLowerCase() + '_pane' });
  subPanes.push({ id: paneId, name });

  return paneId;
}

function removeSubPane(paneId) {
  if (!chart) return;
  chart.removeIndicator(paneId);
  const idx = subPanes.findIndex(p => p.id === paneId);
  if (idx !== -1) subPanes.splice(idx, 1);
}

/* ---------- 自定义指标注册框架 ---------- */
function registerCustomIndicator(config) {
  if (!klinecharts || !klinecharts.registerIndicator) return;
  try {
    klinecharts.registerIndicator(config);
    console.log(`[Chart] 已注册自定义指标: ${config.name}`);
  } catch (e) {
    console.error(`[Chart] 注册指标失败: ${config.name}`, e);
  }
}

/* ---------- 添加主图/副图指标 ---------- */
function addMainIndicator(name) {
  if (!chart) return;
  // overlay类指标叠加到主图candle_pane，与K线共享Y轴
  chart.createIndicator(name, false, { id: 'candle_pane' });
  console.log(`[Chart] 已叠加主图指标: ${name} -> candle_pane`);
}

function addSubIndicator(name) {
  if (!chart) return;
  const paneId = addSubPane(name, name);

  // RSI: 添加30/50/70水平参考线overlay
  if (name === 'RSI' && paneId) {
    setTimeout(() => {
      try {
        [
          { value: 70, color: 'rgba(239,83,80,0.5)' },
          { value: 50, color: 'rgba(120,123,134,0.35)' },
          { value: 30, color: 'rgba(38,166,154,0.5)' },
        ].forEach(lv => {
          chart.createOverlay({
            name: 'horizontalStraightLine',
            points: [{ value: lv.value }],
            styles: {
              line: { color: lv.color, size: 1, style: 'dashed', dashedValue: [4, 3] },
              text: { show: false },
            },
            lock: true,
          }, paneId);
        });
      } catch(e) { console.warn('[Chart] RSI参考线失败:', e); }
    }, 300);
  }
  return paneId;
}

function removeIndicator(name, paneId) {
  if (!chart) return;
  chart.removeIndicator(paneId, name);
}

/* ---------- 从K线数据更新右侧信息面板 ---------- */
function updateInfoPanelFromKline(lastCandle, prevCandle) {
  console.log('[Chart] updateInfoPanelFromKline called', lastCandle);
  if (!lastCandle) return;

  function setVal(id, text, color) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    if (color) el.style.color = color;
  }

  function fmtPrice(p) {
    if (p == null || isNaN(p)) return '--';
    p = parseFloat(p);
    if (p >= 1000) return p.toLocaleString('en-US', {maximumFractionDigits: 2});
    if (p >= 1) return p.toFixed(4);
    return p.toFixed(6);
  }

  function fmtVol(v) {
    if (!v || isNaN(v)) return '--';
    v = parseFloat(v);
    if (v >= 1e9) return (v/1e9).toFixed(2) + 'B';
    if (v >= 1e6) return (v/1e6).toFixed(2) + 'M';
    if (v >= 1e3) return (v/1e3).toFixed(2) + 'K';
    return v.toFixed(2);
  }

  const o = lastCandle.open, h = lastCandle.high, l = lastCandle.low, c = lastCandle.close;
  const vol = lastCandle.volume;
  const prevClose = prevCandle ? prevCandle.close : o;
  const change = c - prevClose;
  const changePct = prevClose !== 0 ? (change / prevClose * 100) : 0;
  const upColor = 'var(--color-up)', downColor = 'var(--color-down)';
  const clr = change >= 0 ? upColor : downColor;

  setVal('info-open', fmtPrice(o));
  setVal('info-high', fmtPrice(h));
  setVal('info-low', fmtPrice(l));
  setVal('info-close', fmtPrice(c), clr);
  setVal('info-volume', fmtVol(vol));
  setVal('info-change', (change >= 0 ? '+' : '') + change.toFixed(2) + ' (' + (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%)', clr);
}

/* ---------- 计算并更新右侧指标值 ---------- */
function updateIndicatorValues(klines) {
  if (!klines || klines.length < 20) return;

  const closes = klines.map(k => k.close);
  const n = closes.length;

  // 简单MA计算
  function sma(arr, period) {
    if (arr.length < period) return null;
    let sum = 0;
    for (let i = arr.length - period; i < arr.length; i++) sum += arr[i];
    return sum / period;
  }

  // RSI计算
  function rsi(arr, period) {
    if (arr.length < period + 1) return null;
    let gains = 0, losses = 0;
    for (let i = arr.length - period; i < arr.length; i++) {
      const diff = arr[i] - arr[i-1];
      if (diff > 0) gains += diff; else losses -= diff;
    }
    const avgGain = gains / period;
    const avgLoss = losses / period;
    if (avgLoss === 0) return 100;
    const rs = avgGain / avgLoss;
    return 100 - (100 / (1 + rs));
  }

  // EMA计算
  function ema(arr, period) {
    if (arr.length < period) return null;
    const k = 2 / (period + 1);
    let val = sma(arr.slice(0, period), period);
    for (let i = period; i < arr.length; i++) {
      val = arr[i] * k + val * (1 - k);
    }
    return val;
  }

  const container = document.getElementById('info-indicators');
  if (!container) return;

  const ma5 = sma(closes, 5);
  const ma10 = sma(closes, 10);
  const ma20 = sma(closes, 20);
  const rsiVal = rsi(closes, 14);
  const ema12 = ema(closes, 12);
  const ema26 = ema(closes, 26);
  const macdDif = (ema12 && ema26) ? (ema12 - ema26) : null;

  function fmt(v) {
    if (v == null || isNaN(v)) return '--';
    if (Math.abs(v) >= 1000) return v.toLocaleString('en-US', {maximumFractionDigits: 1});
    if (Math.abs(v) >= 1) return v.toFixed(2);
    return v.toFixed(4);
  }

  // BOLL
  const ma20b = sma(closes, 20);
  let bollUp = null, bollDn = null;
  if (ma20b && closes.length >= 20) {
    let devSum = 0;
    for (let i = n - 20; i < n; i++) devSum += (closes[i] - ma20b) ** 2;
    const std = Math.sqrt(devSum / 20);
    bollUp = ma20b + 2 * std;
    bollDn = ma20b - 2 * std;
  }

  // ATR
  let atr14 = null;
  if (klines.length >= 15) {
    let trSum = 0;
    for (let i = n - 14; i < n; i++) {
      const h = klines[i].high, l = klines[i].low, pc = klines[i-1].close;
      trSum += Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
    }
    atr14 = trSum / 14;
  }

  const indicators = [
    { name: 'MA5',    value: ma5,     color: '#2196F3' },
    { name: 'MA10',   value: ma10,    color: '#FF9800' },
    { name: 'MA20',   value: ma20,    color: '#AB47BC' },
    { name: 'RSI',    value: rsiVal,  color: '#AB47BC' },
    { name: 'MACD',   value: macdDif, color: '#2196F3' },
  ];

  let html = '<div class="ind-grid">';
  for (const ind of indicators) {
    html += `<div class="ind-cell">
      <span class="ind-dot" style="background:${ind.color}"></span>
      <span class="ind-label">${ind.name}</span>
      <span class="ind-val">${fmt(ind.value)}</span>
    </div>`;
  }
  html += '</div>';

  container.innerHTML = html;
}

/* ---------- 加载活跃警报到右侧面板 ---------- */
async function loadActiveAlerts() {
  const container = document.querySelector('.info-alerts');
  if (!container) return;

  try {
    const resp = await fetch('/api/alerts');
    if (!resp.ok) return;
    const data = await resp.json();
    const alerts = data.alerts || data || [];

    if (!Array.isArray(alerts) || alerts.length === 0) {
      container.innerHTML = '<div style="color:var(--text-tertiary);font-size:11px;padding:4px 0;">暂无活跃警报</div>';
      return;
    }

    // 只显示当前品种相关的或前5条
    const relevant = alerts.filter(a => a.enabled !== false).slice(0, 5);
    container.innerHTML = relevant.map(a => `
      <div class="alert-item" style="font-size:11px;padding:3px 0;border-bottom:1px solid var(--border-secondary);display:flex;justify-content:space-between;">
        <span>${a.symbol} ${a.condition_type === 'price' ? (a.condition?.operator === 'above' ? '>' : '<') + ' ' + (a.condition?.value || '') : a.condition_type}</span>
        <span style="color:var(--color-warning);">⏳</span>
      </div>
    `).join('');
  } catch {
    container.innerHTML = '<div style="color:var(--text-tertiary);font-size:11px;">加载失败</div>';
  }
}

// 页面加载后自动加载警报
document.addEventListener('DOMContentLoaded', () => {
  setTimeout(loadActiveAlerts, 3000);
});

/* ---------- 缠论分析数据加载 ---------- */
let _chanlunLoading = false;

async function loadChanlun(symbol, interval, market) {
  if (!chart || _chanlunLoading) return;
  _chanlunLoading = true;

  if (!symbol) symbol = window.currentSymbol;
  if (!interval) interval = window.currentInterval || '1H';
  if (!market) market = window.currentMarket || 'crypto';
  const apiMarket = market === 'a' ? 'cn' : market;

  try {
    // 用图表已有的K线数据做缠论分析，确保bar_index完全一致
    const chartData = chart.getDataList();
    if (!chartData || chartData.length < 30) {
      showToast('K线数据不足', 'warning');
      _chanlunLoading = false;
      return;
    }

    const resp = await fetch('/api/chanlun/from-data', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        candles: chartData.map(function(k) {
          return { timestamp: k.timestamp, open: k.open, high: k.high, low: k.low, close: k.close, volume: k.volume || 0 };
        })
      }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    window._chanlunData = data;
    window._chanlunBarCount = chartData.length;

    // 确保 CHANLUN 指标已添加到主图
    if (!window._chanlunAdded) {
      chart.createIndicator('CHANLUN', true, { id: 'candle_pane' });
      window._chanlunAdded = true;
    }
    // 触发重绘
    chart.resize();

    const stats = `笔:${data.bi_list?.length || 0} 段:${data.seg_list?.length || 0} 枢:${data.zs_list?.length || 0} 点:${data.bsp_list?.length || 0}`;
    console.log(`[Chanlun] ${symbol} ${interval} 分析完成 - ${stats}`);
    showToast(`缠论分析: ${stats}`, 'success', 3000);
  } catch (err) {
    console.error('[Chanlun] 加载失败:', err);
    showToast(`缠论分析失败: ${err.message}`, 'error');
  } finally {
    _chanlunLoading = false;
  }
}

function removeChanlun() {
  window._chanlunData = null;
  if (window._chanlunAdded && chart) {
    try {
      chart.removeIndicator('candle_pane', 'CHANLUN');
    } catch(e) {}
    window._chanlunAdded = false;
  }
  console.log('[Chanlun] 已移除缠论分析');
}

function isChanlunActive() {
  return !!window._chanlunAdded;
}

/* ---------- 艾略特波浪数据加载 ---------- */
let _elliottLoading = false;

async function loadElliottWave(symbol, interval, market) {
  if (!chart || _elliottLoading) return;
  _elliottLoading = true;

  if (!symbol) symbol = window.currentSymbol;
  if (!interval) interval = window.currentInterval || '1H';
  if (!market) market = window.currentMarket === 'a' ? 'cn' : (window.currentMarket || 'crypto');

  try {
    const chartData = chart.getDataList();
    if (!chartData || chartData.length < 30) {
      showToast('K线数据不足，至少需要30根K线', 'warning');
      _elliottLoading = false;
      return;
    }

    // 只发送可见区域的K线，bar_offset=from 确保后端返回的索引映射到完整图表坐标
    const visRange = chart.getVisibleRange();
    const from = visRange ? Math.max(0, Math.floor(visRange.from)) : 0;
    const to   = visRange ? Math.min(chartData.length - 1, Math.ceil(visRange.to)) : chartData.length - 1;
    const visibleSlice = chartData.slice(from, to + 1);

    if (visibleSlice.length < 30) {
      showToast('可见K线不足30根，请缩小视图', 'warning');
      _elliottLoading = false;
      return;
    }

    const candles = visibleSlice.map(d => ({
      timestamp: d.timestamp, open: d.open, high: d.high,
      low: d.low, close: d.close, volume: d.volume || 0,
    }));

    const resp = await fetch('/api/chanlun/elliott-wave/from-data', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ candles, bar_offset: from }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    window._elliottData = data;

    if (!window._elliottAdded) {
      chart.createIndicator('ELLIOTT_WAVE', true, { id: 'candle_pane' });
      window._elliottAdded = true;
    }
    chart.resize();

    const cnt = data.patterns ? data.patterns.length : 0;
    if (cnt > 0) {
      const best = data.patterns[0];
      showToast(`艾略特: ${best.pattern_name} 置信度 ${Math.round(best.confidence * 100)}%`, 'success', 3000);
    } else {
      showToast('艾略特波浪: 未识别到有效模式', 'warning', 3000);
    }
  } catch (err) {
    showToast(`艾略特波浪分析失败: ${err.message}`, 'error');
  } finally {
    _elliottLoading = false;
  }
}

function removeElliottWave() {
  window._elliottData = null;
  if (window._elliottAdded && chart) {
    try { chart.removeIndicator('candle_pane', 'ELLIOTT_WAVE'); } catch(e) {}
    window._elliottAdded = false;
  }
  console.log('[Elliott] 已移除艾略特波浪');
}

function isElliottActive() {
  return !!window._elliottAdded;
}

/* ---------- 斐波那契分析数据加载 ---------- */
let _fibLoading = false;

async function loadFibonacci(mode) {
  if (!chart || _fibLoading) return;
  _fibLoading = true;

  const modeLabel = mode === 'extension' ? '扩展' : '回撤';

  try {
    const chartData = chart.getDataList();
    if (!chartData || chartData.length < 30) {
      showToast('K线数据不足（至少需要30根）', 'warning');
      _fibLoading = false;
      return;
    }

    const resp = await fetch('/api/fibonacci/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        candles: chartData.map(function(k) {
          return { timestamp: k.timestamp, open: k.open, high: k.high, low: k.low, close: k.close, volume: k.volume || 0 };
        }),
        mode: mode,
      }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    if (data.error) {
      console.warn('[Fibonacci] ' + data.error);
      showToast(`斐波那契${modeLabel}: ${data.error}`, 'warning', 3000);
      _fibLoading = false;
      return;
    }

    const indicatorName = mode === 'extension' ? 'FIB_EXTENSION' : 'FIB_RETRACEMENT';
    const dataKey = mode === 'extension' ? '_fibExtData' : '_fibRetData';
    const addedKey = mode === 'extension' ? '_fibExtAdded' : '_fibRetAdded';

    window[dataKey] = data;

    if (!window[addedKey]) {
      chart.createIndicator(indicatorName, true, { id: 'candle_pane' });
      window[addedKey] = true;
    }
    chart.resize();

    const lvCount = data.levels ? data.levels.length : 0;
    const trendLabel = data.trend === 'up' ? '上升' : '下降';
    console.log(`[Fibonacci] ${modeLabel} 分析完成 - ${trendLabel}趋势, ${lvCount}个水平`);
    showToast(`斐波那契${modeLabel}: ${trendLabel}趋势, ${lvCount}个水平`, 'success', 3000);
  } catch (err) {
    console.error('[Fibonacci] 加载失败:', err);
    showToast(`斐波那契${modeLabel}失败: ${err.message}`, 'error');
  } finally {
    _fibLoading = false;
  }
}

function removeFibonacci(mode) {
  const indicatorName = mode === 'extension' ? 'FIB_EXTENSION' : 'FIB_RETRACEMENT';
  const dataKey = mode === 'extension' ? '_fibExtData' : '_fibRetData';
  const addedKey = mode === 'extension' ? '_fibExtAdded' : '_fibRetAdded';

  window[dataKey] = null;
  if (window[addedKey] && chart) {
    try {
      chart.removeIndicator('candle_pane', indicatorName);
    } catch(e) {}
    window[addedKey] = false;
  }
  const modeLabel = mode === 'extension' ? '扩展' : '回撤';
  console.log(`[Fibonacci] 已移除斐波那契${modeLabel}`);
}

function isFibActive(name) {
  if (name === 'FIB_RET') return !!window._fibRetAdded;
  if (name === 'FIB_EXT') return !!window._fibExtAdded;
  return false;
}

/* ---------- 窗口大小响应 ---------- */
window.addEventListener('resize', () => {
  if (chart) chart.resize();
});
