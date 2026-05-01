/* ============================================================
   OpenChart Pro - WebSocket 客户端
   ============================================================ */

class WSClient {
  constructor(url) {
    this.url = url || `ws://${location.host}/ws`;
    this.ws = null;
    this.handlers = {};           // type → [callback, ...]
    this.reconnectDelay = 3000;   // 初始重连延迟
    this.maxDelay = 30000;        // 最大重连延迟
    this.currentDelay = this.reconnectDelay;
    this.reconnectTimer = null;
    this.intentionalClose = false;
    this.subscriptions = new Set();
    this._statusEl = null;
  }

  /* ---------- 连接管理 ---------- */

  connect() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    this.intentionalClose = false;
    this._setStatus('connecting');

    try {
      this.ws = new WebSocket(this.url);
    } catch (e) {
      console.error('[WS] 创建连接失败:', e);
      this._scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      console.log('[WS] 已连接');
      this.currentDelay = this.reconnectDelay; // 重置延迟
      this._setStatus('connected');
      // v11.6: 仅首次或断线 ≥ 30s 后才弹"已连接" toast，避免 flaky 网络洪水
      const now = Date.now();
      const lastDc = this._lastDisconnectAt || 0;
      if (!this._everConnected || (now - lastDc) > 30000) {
        showToast('实时数据已连接', 'success', 2000);
      }
      this._everConnected = true;

      // 恢复之前的订阅
      for (const sub of this.subscriptions) {
        this._sendRaw({ type: 'subscribe', ...JSON.parse(sub) });
      }

      // 心跳：30s 一次 ping，连续 2 次未回 pong 视为僵尸连接，强制重连
      clearInterval(this._heartbeatTimer);
      this._missedPongs = 0;
      this._heartbeatTimer = setInterval(() => {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        this._missedPongs++;
        if (this._missedPongs >= 3) {
          console.warn('[WS] 心跳超时，强制重连');
          try { this.ws.close(4000, 'heartbeat-timeout'); } catch {}
          return;
        }
        this._sendRaw({ type: 'ping', ts: Date.now() });
      }, 30000);
    };

    // 消息处理队列：最多缓存 50 条未处理消息，每帧（rAF）处理 5 条
    // 这是反压机制：消息洪流时不会阻塞主线程，多余消息丢最旧的
    this._msgQueue = [];
    this._msgFlushScheduled = false;
    const flushQueue = () => {
      this._msgFlushScheduled = false;
      if (!this._msgQueue.length) return;
      // 每帧最多处理 5 条，避免单帧任务过长
      const batch = this._msgQueue.splice(0, 5);
      for (const data of batch) {
        const type = data.type || data.event;
        if (type && this.handlers[type]) {
          for (const cb of this.handlers[type]) {
            try { cb(data); } catch (e) { console.warn(`[WS] ${type} handler 异常:`, e); }
          }
        }
        if (this.handlers['*']) {
          for (const cb of this.handlers['*']) {
            try { cb(data); } catch (e) { console.warn('[WS] * handler 异常:', e); }
          }
        }
      }
      // 还有积压继续调度
      if (this._msgQueue.length) {
        this._msgFlushScheduled = true;
        requestAnimationFrame(flushQueue);
      }
    };

    // 按 type 分桶的队列上限（防止 flash_news 洪流冲掉 signal/advice 等用户关心的事件）
    // 优先级高的 type 给更多槽位
    const TYPE_BUCKET_LIMIT = {
      signal: 30,
      position_advice: 20,
      auto_trade: 20,
      crypto_diagnosis: 10,
      pool_update: 30,
      flash_news: 50,
      manual_stock_news: 20,
    };
    const DEFAULT_BUCKET_LIMIT = 10;
    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        const type = data.type || data.event;
        if (type === 'pong') { this._missedPongs = 0; return; }
        this._msgQueue.push(data);
        // 分桶节流：同类消息超过自己的桶上限，按 FIFO 丢最老的同类
        // 其他类消息完全不受影响
        const limit = TYPE_BUCKET_LIMIT[type] ?? DEFAULT_BUCKET_LIMIT;
        // 反向扫描找第一个同类超额
        let sameTypeCount = 0;
        for (let i = this._msgQueue.length - 1; i >= 0; i--) {
          if ((this._msgQueue[i].type || this._msgQueue[i].event) === type) {
            sameTypeCount++;
            if (sameTypeCount > limit) {
              this._msgQueue.splice(i, 1);
              break;
            }
          }
        }
        // 硬上限防暴增（比如连接恢复时积压）
        if (this._msgQueue.length > 200) this._msgQueue.splice(0, this._msgQueue.length - 200);
        if (!this._msgFlushScheduled) {
          this._msgFlushScheduled = true;
          requestAnimationFrame(flushQueue);
        }
      } catch (e) {
        console.warn('[WS] 解析消息失败:', e, event.data);
      }
    };

    this.ws.onclose = (event) => {
      console.log('[WS] 连接关闭', event.code, event.reason);
      this._setStatus('disconnected');
      this._lastDisconnectAt = Date.now();   // v11.6: 记 disconnect 时刻给 onopen 用
      clearInterval(this._heartbeatTimer);
      if (!this.intentionalClose) {
        this._scheduleReconnect();
      }
    };

    this.ws.onerror = (err) => {
      console.error('[WS] 错误:', err);
      this._setStatus('disconnected');
    };
  }

  disconnect() {
    this.intentionalClose = true;
    clearTimeout(this.reconnectTimer);
    if (this.ws) {
      this.ws.close(1000, '用户断开');
      this.ws = null;
    }
    this._setStatus('disconnected');
  }

  /* ---------- 订阅管理 ---------- */

  subscribe(symbol, interval) {
    const key = JSON.stringify({ symbol, interval });
    this.subscriptions.add(key);
    this._sendRaw({ type: 'subscribe', symbol, interval });
  }

  unsubscribe(symbol, interval) {
    const key = JSON.stringify({ symbol, interval });
    this.subscriptions.delete(key);
    this._sendRaw({ type: 'unsubscribe', symbol, interval });
  }

  /**
   * 切换订阅：取消旧的、订阅新的
   */
  switch(oldSymbol, oldInterval, newSymbol, newInterval) {
    if (oldSymbol && oldInterval) {
      this.unsubscribe(oldSymbol, oldInterval);
    }
    this.subscribe(newSymbol, newInterval);
  }

  /* ---------- 事件处理 ---------- */

  /**
   * 注册消息处理器
   * @param {string} type  消息类型（或 '*' 接收全部）
   * @param {Function} callback
   */
  on(type, callback) {
    if (!this.handlers[type]) {
      this.handlers[type] = [];
    }
    // 去重：同一 callback 不重复注册（防模块多次 init 导致一条消息触发 N 次处理）
    if (this.handlers[type].includes(callback)) {
      console.warn(`[WS] handler 已注册过，跳过: ${type}`);
      return;
    }
    this.handlers[type].push(callback);
  }

  /**
   * 移除处理器
   */
  off(type, callback) {
    if (!this.handlers[type]) return;
    this.handlers[type] = this.handlers[type].filter(cb => cb !== callback);
  }

  /**
   * 发送消息
   */
  send(data) {
    this._sendRaw(data);
  }

  /* ---------- 内部方法 ---------- */

  _sendRaw(data) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  _scheduleReconnect() {
    if (this.intentionalClose) return;
    console.log(`[WS] ${this.currentDelay / 1000}s 后重连...`);
    this._setStatus('connecting');
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, this.currentDelay);
    // 指数退避：3→6→12→24→30
    this.currentDelay = Math.min(this.currentDelay * 2, this.maxDelay);
  }

  _setStatus(status) {
    if (!this._statusEl) {
      this._statusEl = document.getElementById('ws-status');
    }
    if (this._statusEl) {
      this._statusEl.className = `ws-status ${status}`;
      this._statusEl.title = {
        connected: '已连接',
        disconnected: '已断开',
        connecting: '连接中...',
      }[status] || status;
    }
  }
}

// 全局 WebSocket 实例
const ws = new WSClient();
