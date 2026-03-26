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
      showToast('实时数据已连接', 'success', 2000);

      // 恢复之前的订阅
      for (const sub of this.subscriptions) {
        this._sendRaw({ type: 'subscribe', ...JSON.parse(sub) });
      }
    };

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        const type = data.type || data.event;
        if (type && this.handlers[type]) {
          for (const cb of this.handlers[type]) {
            cb(data);
          }
        }
        // 通配符处理器
        if (this.handlers['*']) {
          for (const cb of this.handlers['*']) {
            cb(data);
          }
        }
      } catch (e) {
        console.warn('[WS] 解析消息失败:', e, event.data);
      }
    };

    this.ws.onclose = (event) => {
      console.log('[WS] 连接关闭', event.code, event.reason);
      this._setStatus('disconnected');
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
