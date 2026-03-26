/* ============================================================
   OpenChart Pro - Toast 通知组件
   ============================================================ */

const Toast = (() => {
  const MAX_TOASTS = 3;
  let container = null;

  const ICONS = {
    info:    'ℹ',
    success: '✓',
    warning: '⚠',
    error:   '✕',
  };

  function ensureContainer() {
    if (!container) {
      container = document.getElementById('toast-container');
      if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        document.body.appendChild(container);
      }
    }
    return container;
  }

  function removeToast(el) {
    el.classList.add('toast-removing');
    el.addEventListener('animationend', () => {
      el.remove();
    }, { once: true });
  }

  /**
   * 显示一条 Toast 通知
   * @param {string} message  消息文本
   * @param {'info'|'success'|'warning'|'error'} type 类型
   * @param {number} duration 自动消失毫秒数（error 类型忽略此参数，需手动关闭）
   */
  function showToast(message, type = 'info', duration = 3000) {
    const ct = ensureContainer();

    // 限制最多 MAX_TOASTS 条，超过移除最早的
    while (ct.children.length >= MAX_TOASTS) {
      removeToast(ct.firstElementChild);
    }

    const el = document.createElement('div');
    el.className = `toast ${type}`;

    const icon = document.createElement('span');
    icon.className = 'toast-icon';
    icon.textContent = ICONS[type] || ICONS.info;

    const msg = document.createElement('span');
    msg.className = 'toast-message';
    msg.textContent = message;

    const closeBtn = document.createElement('button');
    closeBtn.className = 'toast-close';
    closeBtn.innerHTML = '&times;';
    closeBtn.title = '关闭';
    closeBtn.addEventListener('click', () => removeToast(el));

    el.appendChild(icon);
    el.appendChild(msg);
    el.appendChild(closeBtn);
    ct.appendChild(el);

    // error 类型需要手动关闭，不自动消失
    if (type !== 'error' && duration > 0) {
      setTimeout(() => {
        if (el.parentNode) removeToast(el);
      }, duration);
    }

    return el;
  }

  // 公开 API
  return { showToast };
})();

// 全局快捷引用
function showToast(message, type, duration) {
  return Toast.showToast(message, type, duration);
}
