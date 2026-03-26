/* ============================================================
   OpenChart Pro - 设置对话框
   ============================================================ */

const Settings = (() => {
  let overlay = null;
  let settingsData = {};

  /* 默认设置 */
  const DEFAULTS = {
    // 通用
    timezone: 'Asia/Shanghai',
    // 图表样式
    upColor: '#00C853',
    downColor: '#FF1744',
    candleType: 'candle_solid',
    showGrid: true,
    // 数据源
    exchange: 'binance',
    // AI/LLM
    llmProvider: 'openai',
    llmApiKey: '',
    llmModel: '',
    llmBaseUrl: '',
    ollamaModel: '',
    ollamaBaseUrl: 'http://localhost:11434',
    // 第三方 API Key
    exchangeApiKey: '',
    exchangeApiSecret: '',
    newsApiKey: '',
    // 通知
    notifyBrowser: true,
    notifySound: true,
    webhookUrl: '',
    // 回测
    btInitialCapital: 100000,
    btCommission: 0.001,
    btSlippage: 0.0005,
  };

  const LLM_PROVIDERS = {
    openai:    { label: 'OpenAI',          fields: ['llmApiKey', 'llmModel'] },
    anthropic: { label: 'Anthropic',       fields: ['llmApiKey', 'llmModel'] },
    deepseek:  { label: 'DeepSeek',        fields: ['llmApiKey', 'llmModel', 'llmBaseUrl'] },
    ollama:    { label: 'Ollama (本地)',    fields: ['ollamaModel', 'ollamaBaseUrl'] },
  };

  function init() {
    overlay = document.getElementById('settings-modal');
    if (!overlay) return;

    // 关闭按钮
    overlay.querySelector('.modal-close')?.addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

    // 保存按钮
    overlay.querySelector('#settings-save')?.addEventListener('click', save);

    // 恢复默认
    overlay.querySelector('#settings-reset')?.addEventListener('click', resetDefaults);

    // LLM Provider 切换
    const providerSelect = overlay.querySelector('#setting-llmProvider');
    if (providerSelect) {
      providerSelect.addEventListener('change', () => {
        toggleLLMFields(providerSelect.value);
      });
    }
  }

  async function open() {
    if (!overlay) return;
    await load();
    populateForm();
    overlay.classList.add('show');
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('show');
  }

  /* ---------- 加载 / 保存 ---------- */
  async function load() {
    try {
      const resp = await fetch('/api/settings');
      if (resp.ok) {
        const data = await resp.json();
        settingsData = { ...DEFAULTS, ...(data.settings || data.data || data) };
      } else {
        settingsData = { ...DEFAULTS };
      }
    } catch (e) {
      console.warn('[Settings] 加载失败，使用默认值:', e);
      settingsData = { ...DEFAULTS };
    }
  }

  async function save() {
    collectForm();

    // 映射前端字段名 → 后端字段名
    const bd = {};
    if (settingsData.timezone) bd.timezone = settingsData.timezone;
    if (settingsData.candleType) bd.candle_type = settingsData.candleType;
    if (settingsData.showGrid !== undefined) bd.show_grid = settingsData.showGrid;
    if (settingsData.exchange) bd.crypto_exchange = settingsData.exchange;
    if (settingsData.llmProvider) bd.llm_provider = settingsData.llmProvider;
    if (settingsData.notifyBrowser !== undefined) bd.enable_browser_notification = settingsData.notifyBrowser;
    if (settingsData.notifySound !== undefined) bd.enable_sound = settingsData.notifySound;
    // LLM配置按provider存到对应字段
    const provider = settingsData.llmProvider || 'deepseek';
    if (provider === 'qwen') {
      if (settingsData.llmApiKey) bd.qwen_api_key = settingsData.llmApiKey;
      if (settingsData.llmModel) bd.qwen_model = settingsData.llmModel;
      if (settingsData.llmBaseUrl) bd.qwen_base_url = settingsData.llmBaseUrl;
    } else {
      if (settingsData.llmApiKey) bd.deepseek_api_key = settingsData.llmApiKey;
      if (settingsData.llmModel) bd.deepseek_model = settingsData.llmModel;
      if (settingsData.llmBaseUrl) bd.deepseek_base_url = settingsData.llmBaseUrl;
    }
    if (settingsData.newsApiKey) bd.finnhub_api_key = settingsData.newsApiKey;

    try {
      const resp = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: bd }),
      });
      if (resp.ok) {
        showToast('设置已保存', 'success');
        applySettings();
        close();
      } else {
        throw new Error(`HTTP ${resp.status}`);
      }
    } catch (e) {
      showToast(`保存设置失败: ${e.message}`, 'error');
    }
  }

  function resetDefaults() {
    if (!confirm('确定恢复默认设置？')) return;
    settingsData = { ...DEFAULTS };
    populateForm();
    showToast('已恢复默认设置（需点击保存生效）', 'info');
  }

  /* ---------- 表单 ↔ 数据 ---------- */
  function populateForm() {
    setVal('setting-timezone', settingsData.timezone);
    setVal('setting-upColor', settingsData.upColor);
    setVal('setting-downColor', settingsData.downColor);
    setVal('setting-candleType', settingsData.candleType);
    setChecked('setting-showGrid', settingsData.showGrid);
    setVal('setting-exchange', settingsData.exchange);
    setVal('setting-llmProvider', settingsData.llmProvider);
    setVal('setting-llmApiKey', settingsData.llmApiKey ? maskKey(settingsData.llmApiKey) : '');
    setVal('setting-llmModel', settingsData.llmModel);
    setVal('setting-llmBaseUrl', settingsData.llmBaseUrl);
    setVal('setting-ollamaModel', settingsData.ollamaModel);
    setVal('setting-ollamaBaseUrl', settingsData.ollamaBaseUrl);
    setVal('setting-exchangeApiKey', settingsData.exchangeApiKey ? maskKey(settingsData.exchangeApiKey) : '');
    setVal('setting-exchangeApiSecret', settingsData.exchangeApiSecret ? maskKey(settingsData.exchangeApiSecret) : '');
    setVal('setting-newsApiKey', settingsData.newsApiKey ? maskKey(settingsData.newsApiKey) : '');
    setChecked('setting-notifyBrowser', settingsData.notifyBrowser);
    setChecked('setting-notifySound', settingsData.notifySound);
    setVal('setting-webhookUrl', settingsData.webhookUrl);
    setVal('setting-btInitialCapital', settingsData.btInitialCapital);
    setVal('setting-btCommission', settingsData.btCommission);
    setVal('setting-btSlippage', settingsData.btSlippage);

    toggleLLMFields(settingsData.llmProvider);
  }

  function collectForm() {
    settingsData.timezone = getVal('setting-timezone');
    settingsData.upColor = getVal('setting-upColor');
    settingsData.downColor = getVal('setting-downColor');
    settingsData.candleType = getVal('setting-candleType');
    settingsData.showGrid = getChecked('setting-showGrid');
    settingsData.exchange = getVal('setting-exchange');
    settingsData.llmProvider = getVal('setting-llmProvider');

    // API Key: 只要有值且不是纯掩码就保存
    const llmKeyInput = getVal('setting-llmApiKey') || '';
    if (llmKeyInput && !llmKeyInput.match(/^[\*•]+$/)) {
      settingsData.llmApiKey = llmKeyInput;
    }

    settingsData.llmModel = getVal('setting-llmModel') || '';
    settingsData.llmBaseUrl = getVal('setting-llmBaseUrl') || '';

    // 如果BaseURL没填，给默认值
    if (!settingsData.llmBaseUrl) {
      const provider = settingsData.llmProvider;
      if (provider === 'deepseek') settingsData.llmBaseUrl = 'https://api.deepseek.com/v1';
      else if (provider === 'qwen') settingsData.llmBaseUrl = 'https://dashscope.aliyuncs.com/compatible-mode/v1';
    }
    // 如果Model没填，给默认值
    if (!settingsData.llmModel) {
      const provider = settingsData.llmProvider;
      if (provider === 'deepseek') settingsData.llmModel = 'deepseek-chat';
      else if (provider === 'qwen') settingsData.llmModel = 'qwen-turbo';
    }
    settingsData.ollamaModel = getVal('setting-ollamaModel');
    settingsData.ollamaBaseUrl = getVal('setting-ollamaBaseUrl');

    const exKeyInput = getVal('setting-exchangeApiKey');
    if (exKeyInput && !exKeyInput.includes('••••')) settingsData.exchangeApiKey = exKeyInput;

    const exSecretInput = getVal('setting-exchangeApiSecret');
    if (exSecretInput && !exSecretInput.includes('••••')) settingsData.exchangeApiSecret = exSecretInput;

    const newsKeyInput = getVal('setting-newsApiKey');
    if (newsKeyInput && !newsKeyInput.includes('••••')) settingsData.newsApiKey = newsKeyInput;

    settingsData.notifyBrowser = getChecked('setting-notifyBrowser');
    settingsData.notifySound = getChecked('setting-notifySound');
    settingsData.webhookUrl = getVal('setting-webhookUrl');
    settingsData.btInitialCapital = parseFloat(getVal('setting-btInitialCapital')) || DEFAULTS.btInitialCapital;
    settingsData.btCommission = parseFloat(getVal('setting-btCommission')) || DEFAULTS.btCommission;
    settingsData.btSlippage = parseFloat(getVal('setting-btSlippage')) || DEFAULTS.btSlippage;
  }

  /* ---------- 应用设置到界面 ---------- */
  function applySettings() {
    // 可在此处更新图表主题色、网格等
    if (chart && settingsData.upColor && settingsData.downColor) {
      chart.setStyles({
        candle: {
          bar: {
            upColor: settingsData.upColor,
            downColor: settingsData.downColor,
            upBorderColor: settingsData.upColor,
            downBorderColor: settingsData.downColor,
            upWickColor: settingsData.upColor,
            downWickColor: settingsData.downColor,
          },
        },
        grid: {
          horizontal: { show: settingsData.showGrid },
          vertical: { show: settingsData.showGrid },
        },
      });
    }
  }

  /* ---------- LLM Provider 字段切换 ---------- */
  function toggleLLMFields(provider) {
    const allFields = ['llmApiKey', 'llmModel', 'llmBaseUrl', 'ollamaModel', 'ollamaBaseUrl'];
    const cfg = LLM_PROVIDERS[provider] || LLM_PROVIDERS.openai;

    for (const f of allFields) {
      const row = document.getElementById(`row-${f}`);
      if (row) {
        row.style.display = cfg.fields.includes(f) ? 'flex' : 'none';
      }
    }
  }

  /* ---------- 工具函数 ---------- */
  function setVal(id, val) {
    const el = document.getElementById(id);
    if (el) el.value = val ?? '';
  }
  function getVal(id) {
    const el = document.getElementById(id);
    return el ? el.value : '';
  }
  function setChecked(id, checked) {
    const el = document.getElementById(id);
    if (el) el.checked = !!checked;
  }
  function getChecked(id) {
    const el = document.getElementById(id);
    return el ? el.checked : false;
  }
  function maskKey(key) {
    if (!key || key.length < 8) return key;
    return key.slice(0, 4) + '••••••••' + key.slice(-4);
  }

  return { init, open, close };
})();
