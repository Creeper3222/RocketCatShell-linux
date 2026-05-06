const DEFAULT_FORM = {
  name: 'bot',
  enabled: false,
  server_url: '',
  username: '',
  password: '',
  e2ee_password: '',
  onebot_ws_url: '',
  onebot_access_token: '',
  onebot_self_id: 910001,
  reconnect_delay: 5.0,
  max_reconnect_attempts: 10,
  enable_subchannel_session_isolation: true,
  remote_media_max_size: 20971520,
  skip_own_messages: true,
  debug: false,
};

const ROCKETCAT_CONFIG_MARKER_FIELD = 'Is rocketcat config';

const state = {
  editingId: null,
  bots: [],
  status: null,
  currentPage: 'network',
  settings: {
    data: null,
    loaded: false,
  },
  basicInfo: {
    items: [],
    summary: {
      enabled_count: 0,
      online_count: 0,
    },
    loaded: false,
  },
  logs: {
    items: [],
    lastId: 0,
    maxEntries: 5000,
    pollTimer: null,
    generation: 0,
    autoScroll: true,
    activeLevels: new Set(['DEBUG', 'INFO', 'WARN', 'ERROR']),
  },
  plugins: {
    items: [],
    loaded: false,
    current: null,
    pendingUninstall: null,
  },
};

function getSuggestedOnebotSelfId() {
  const suggested = Number(state.status?.suggested_onebot_self_id);
  return Number.isFinite(suggested) && suggested > 0
    ? suggested
    : DEFAULT_FORM.onebot_self_id;
}

function buildCreateDefaults() {
  return {
    ...DEFAULT_FORM,
    onebot_self_id: getSuggestedOnebotSelfId(),
  };
}

const elements = {
  navButtons: Array.from(document.querySelectorAll('[data-page]')),
  networkPage: document.getElementById('networkPage'),
  basicPage: document.getElementById('basicPage'),
  logsPage: document.getElementById('logsPage'),
  settingsPage: document.getElementById('settingsPage'),
  pluginsPage: document.getElementById('pluginsPage'),
  bridgeStatus: document.getElementById('bridgeStatus'),
  mainBotStatus: document.getElementById('mainBotStatus'),
  webuiStatus: document.getElementById('webuiStatus'),
  webuiUrl: document.getElementById('webuiUrl'),
  settingsAuthStatus: document.getElementById('settingsAuthStatus'),
  settingsPasswordMode: document.getElementById('settingsPasswordMode'),
  settingsPasswordHint: document.getElementById('settingsPasswordHint'),
  settingsPortHint: document.getElementById('settingsPortHint'),
  settingsMessageIndexHint: document.getElementById('settingsMessageIndexHint'),
  pluginCount: document.getElementById('pluginCount'),
  pluginEnabledCount: document.getElementById('pluginEnabledCount'),
  basicInfoGrid: document.getElementById('basicInfoGrid'),
  basicEmptyState: document.getElementById('basicEmptyState'),
  basicEnabledCount: document.getElementById('basicEnabledCount'),
  basicOnlineCount: document.getElementById('basicOnlineCount'),
  banner: document.getElementById('statusBanner'),
  botGrid: document.getElementById('botGrid'),
  emptyState: document.getElementById('emptyState'),
  pluginGrid: document.getElementById('pluginGrid'),
  pluginEmptyState: document.getElementById('pluginEmptyState'),
  createButton: document.getElementById('createButton'),
  refreshButton: document.getElementById('refreshButton'),
  basicRefreshButton: document.getElementById('basicRefreshButton'),
  settingsRefreshButton: document.getElementById('settingsRefreshButton'),
  pluginsRefreshButton: document.getElementById('pluginsRefreshButton'),
  modal: document.getElementById('botModal'),
  modalTitle: document.getElementById('modalTitle'),
  form: document.getElementById('botForm'),
  settingsForm: document.getElementById('settingsForm'),
  settingsPasswordHelper: document.getElementById('settingsPasswordHelper'),
  settingsWebuiPasswordInput: document.getElementById('settingsWebuiPasswordInput'),
  settingsWebuiPortInput: document.getElementById('settingsWebuiPortInput'),
  settingsMessageIndexMaxEntriesInput: document.getElementById('settingsMessageIndexMaxEntriesInput'),
  settingsPasswordSaveButton: document.getElementById('settingsPasswordSaveButton'),
  settingsPortSaveButton: document.getElementById('settingsPortSaveButton'),
  settingsMessageIndexSaveButton: document.getElementById('settingsMessageIndexSaveButton'),
  settingsMessageIndexRebuildButton: document.getElementById('settingsMessageIndexRebuildButton'),
  settingsExportConfigButton: document.getElementById('settingsExportConfigButton'),
  settingsImportConfigButton: document.getElementById('settingsImportConfigButton'),
  settingsImportFileInput: document.getElementById('settingsImportFileInput'),
  closeModalButton: document.getElementById('closeModalButton'),
  cancelButton: document.getElementById('cancelButton'),
  submitButton: document.getElementById('submitButton'),
  pluginModal: document.getElementById('pluginModal'),
  pluginModalTitle: document.getElementById('pluginModalTitle'),
  pluginModalMeta: document.getElementById('pluginModalMeta'),
  pluginSettingsForm: document.getElementById('pluginSettingsForm'),
  pluginCloseModalButton: document.getElementById('pluginCloseModalButton'),
  pluginCancelButton: document.getElementById('pluginCancelButton'),
  pluginSaveButton: document.getElementById('pluginSaveButton'),
  pluginUninstallModal: document.getElementById('pluginUninstallModal'),
  pluginUninstallTitle: document.getElementById('pluginUninstallTitle'),
  pluginUninstallMessage: document.getElementById('pluginUninstallMessage'),
  pluginUninstallDeleteConfigInput: document.getElementById('pluginUninstallDeleteConfigInput'),
  pluginUninstallDeleteDataInput: document.getElementById('pluginUninstallDeleteDataInput'),
  pluginUninstallCloseButton: document.getElementById('pluginUninstallCloseButton'),
  pluginUninstallCancelButton: document.getElementById('pluginUninstallCancelButton'),
  pluginUninstallConfirmButton: document.getElementById('pluginUninstallConfirmButton'),
  toast: document.getElementById('toast'),
  logConsole: document.getElementById('logConsole'),
  logAutoScrollToggle: document.getElementById('logAutoScrollToggle'),
  logAutoScrollLabel: document.getElementById('logAutoScrollLabel'),
  logMeta: document.getElementById('logMeta'),
  clearLogsButton: document.getElementById('clearLogsButton'),
  logFilterButtons: Array.from(document.querySelectorAll('[data-log-level]')),
};

function showToast(message, kind = 'default') {
  elements.toast.textContent = message;
  elements.toast.className = `toast ${kind}`;
  window.clearTimeout(showToast._timer);
  showToast._timer = window.setTimeout(() => {
    elements.toast.className = 'toast hidden';
  }, 2600);
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401 && !options.skipAuthRedirect) {
    window.location.replace('/');
    throw new Error(payload.error || payload.detail || '登录已失效，请重新登录');
  }
  if (!response.ok) {
    throw new Error(payload.error || payload.detail || '请求失败');
  }
  return payload;
}

function isAbortError(error) {
  return Boolean(error && (error.name === 'AbortError' || error.code === 20));
}

async function writeTextWithPicker(fileName, text) {
  if (typeof window.showSaveFilePicker === 'function') {
    const handle = await window.showSaveFilePicker({
      suggestedName: fileName,
      types: [
        {
          description: 'RocketCat 配置文件',
          accept: {
            'application/json': ['.json'],
          },
        },
      ],
    });
    const writable = await handle.createWritable();
    await writable.write(text);
    await writable.close();
    return;
  }

  const blob = new Blob([text], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

async function pickJsonTextForImport() {
  if (typeof window.showOpenFilePicker === 'function') {
    const [handle] = await window.showOpenFilePicker({
      multiple: false,
      excludeAcceptAllOption: false,
      types: [
        {
          description: 'RocketCat 配置文件',
          accept: {
            'application/json': ['.json'],
          },
        },
      ],
    });
    const file = await handle.getFile();
    return await file.text();
  }

  const input = elements.settingsImportFileInput;
  if (!input) {
    throw new Error('当前浏览器不支持系统文件选择器');
  }

  return await new Promise((resolve, reject) => {
    const cleanup = () => {
      input.removeEventListener('change', onChange);
      input.value = '';
    };

    const onChange = async () => {
      try {
        const [file] = Array.from(input.files || []);
        if (!file) {
          reject(new DOMException('No file selected', 'AbortError'));
          return;
        }
        resolve(await file.text());
      } catch (error) {
        reject(error);
      } finally {
        cleanup();
      }
    };

    input.addEventListener('change', onChange, { once: true });
    input.click();
  });
}

function setActivePage(page) {
  state.currentPage = page;
  elements.networkPage.classList.toggle('hidden', page !== 'network');
  elements.basicPage.classList.toggle('hidden', page !== 'basic');
  elements.logsPage.classList.toggle('hidden', page !== 'logs');
  elements.settingsPage.classList.toggle('hidden', page !== 'settings');
  elements.pluginsPage.classList.toggle('hidden', page !== 'plugins');

  for (const button of elements.navButtons) {
    const isActive = button.dataset.page === page;
    button.classList.toggle('active', isActive);
    button.classList.toggle('ghost', !isActive);
  }

  if (page === 'logs') {
    renderLogs();
  }
}

function buildBasicInfoFallback() {
  const items = [];

  for (const bot of state.bots.filter((item) => item.enabled)) {
    items.push({
      bot_id: bot.id,
      client_name: bot.name || '未命名 Bot',
      login_username: bot.username || '-',
      nickname: bot.username || '-',
      avatar_url: '',
      status_code: 'pending',
      status_label: '等待基础信息接口',
      server_url: bot.server_url || '-',
      onebot_self_id: bot.onebot_self_id || '-',
      server_display_name: '',
      server_avatar_url: '',
      is_main_bot: false,
      user_id: '',
    });
  }

  const onlineCount = items.filter((item) => item.status_code === 'online').length;
  return {
    items,
    summary: {
      enabled_count: items.length,
      online_count: onlineCount,
    },
  };
}

async function activatePage(page, { forceReload = false } = {}) {
  setActivePage(page);
  if (page === 'basic') {
    await loadBasicInfo({ forceReload, silent: false });
    return;
  }
  if (page === 'settings') {
    await loadSettings({ forceReload, silent: false });
    return;
  }
  if (page === 'plugins') {
    await loadPlugins({ forceReload, silent: false });
  }
}

function getBasicStatusTone(statusCode) {
  if (statusCode === 'online') {
    return 'online';
  }
  if (statusCode === 'blocked') {
    return 'blocked';
  }
  return 'pending';
}

function getAvatarInitial(item) {
  const source = String(item.nickname || item.login_username || item.client_name || '?').trim();
  return escapeHtml(source.charAt(0) || '?');
}

function setBanner(message = '', tone = 'warning') {
  if (!message) {
    elements.banner.className = 'banner hidden';
    elements.banner.textContent = '';
    return;
  }
  elements.banner.className = `banner ${tone}`;
  elements.banner.textContent = message;
}

function renderStatus(status) {
  state.status = status;
  elements.bridgeStatus.textContent = status.bridge_enabled ? 'Shell 已运行' : 'Shell 未运行';
  elements.mainBotStatus.textContent = `${Number(status.enabled_bot_count) || 0} / ${Number(status.bot_count) || 0}`;
  elements.webuiStatus.textContent = status.independent_webui_enabled ? 'WebUI 已就绪' : 'WebUI 未启用';
  elements.webuiUrl.textContent = status.access_url || '-';

  if (!status.bridge_enabled) {
    setBanner('RocketCat Shell 当前未处于可用状态。');
    return;
  }
  if (!status.bot_count) {
    setBanner('当前还没有 bot。点击右上角“新建 Bot”开始添加。');
    return;
  }
  if (!status.enabled_bot_count) {
    setBanner('当前所有 bot 都处于停用状态，启用后才会建立连接。');
    return;
  }
  setBanner('');
}

function renderBasicInfo(payload) {
  const summary = payload?.summary || {};
  const items = Array.isArray(payload?.items) ? payload.items : [];
  state.basicInfo = {
    items,
    summary,
    loaded: state.basicInfo.loaded,
  };

  elements.basicEnabledCount.textContent = String(summary.enabled_count || 0);
  elements.basicOnlineCount.textContent = String(summary.online_count || 0);
  elements.basicEmptyState.classList.toggle('hidden', items.length > 0);
  elements.basicInfoGrid.innerHTML = '';

  for (const item of items) {
    const card = document.createElement('article');
    const statusTone = getBasicStatusTone(item.status_code);
    const serverDisplayName = item.server_display_name || '';
    const serverAvatarUrl = item.server_avatar_url || '';
    card.className = 'basic-info-card';
    card.innerHTML = `
      <div class="basic-info-card-header">
        <div class="basic-avatar-shell">
          <span class="basic-avatar-fallback">${getAvatarInitial(item)}</span>
          ${item.avatar_url ? `<img class="basic-avatar-image" src="${escapeHtml(item.avatar_url)}" alt="${escapeHtml(item.nickname || item.client_name || 'avatar')}" onerror="this.remove()" />` : ''}
        </div>
        <div class="basic-identity-block">
          <div class="basic-identity-top">
            <div>
              <h3>${escapeHtml(item.client_name || '未命名客户端')}</h3>
              <p class="basic-login-name">@${escapeHtml(item.login_username || '-')}</p>
            </div>
            <span class="basic-status-pill ${statusTone}">${escapeHtml(item.status_label || '未接入')}</span>
          </div>
          <p class="basic-display-name">${escapeHtml(item.nickname || item.login_username || '-')}</p>
        </div>
      </div>

      <div class="basic-meta-list">
        <div class="basic-meta-row">
          <span>聊天显示昵称</span>
          <strong>${escapeHtml(item.nickname || '-')}</strong>
        </div>
        <div class="basic-meta-row">
          <span>Rocket.Chat 用户名</span>
          <strong>${escapeHtml(item.login_username || '-')}</strong>
        </div>
        <div class="basic-meta-row">
          <span>OneBot self_id</span>
          <strong>${escapeHtml(String(item.onebot_self_id || '-'))}</strong>
        </div>
        <div class="basic-meta-row wide">
          <span>Rocket.Chat 服务器</span>
          <div class="basic-server-value">
            <code>${escapeHtml(item.server_url || '-')}</code>
          </div>
        </div>
        <div class="basic-meta-row wide basic-target-row">
          <div class="basic-target-summary">
            <div class="basic-room-avatar-shell" title="${escapeHtml(serverDisplayName || '未获取到服务器昵称')}">
              ${serverAvatarUrl ? `<img class="basic-room-avatar-image" src="${escapeHtml(serverAvatarUrl)}" alt="${escapeHtml(serverDisplayName || '服务器标识')}" onerror="this.closest('.basic-target-summary').dataset.avatarMissing = 'true'; this.parentElement.classList.add('is-missing'); this.remove();" />` : ''}
              <span class="basic-room-avatar-fallback ${serverAvatarUrl ? 'hidden' : ''}">${escapeHtml((serverDisplayName || '?').trim().charAt(0) || '?')}</span>
            </div>
            <div class="basic-target-texts">
              <strong class="basic-target-name">${escapeHtml(serverDisplayName || '未获取到服务器昵称')}</strong>
            </div>
          </div>
        </div>
      </div>
    `;
    elements.basicInfoGrid.appendChild(card);
  }
}

function renderSettings(payload) {
  const settings = payload || {};
  state.settings = {
    data: settings,
    loaded: state.settings.loaded,
  };

  const isDefaultPassword = Boolean(settings.webui_access_password_is_default);
  elements.settingsAuthStatus.textContent = settings.webui_auth_enabled ? '已启用' : '未启用';
  elements.settingsPasswordMode.textContent = isDefaultPassword ? '默认密码' : '已自定义';
  elements.settingsPasswordHint.textContent = isDefaultPassword
    ? '当前仍在使用默认密码 123456，请尽快修改。'
    : '当前已使用自定义 WebUI 登录密码。';
  if (elements.settingsPasswordHelper) {
    elements.settingsPasswordHelper.textContent = '保存后立即生效。当前会话会保留，后续重新登录需使用新密码。';
  }
  if (elements.settingsPortHint) {
    elements.settingsPortHint.textContent = settings.webui_port_hint
      || '保存后会写入配置。重启 RocketCat Shell 时会优先尝试该端口；如果端口被占用，仍会自动回退到可用端口。';
  }
  if (elements.settingsMessageIndexHint) {
    elements.settingsMessageIndexHint.textContent = settings.message_index_hint
      || '当前最多保留 1000 条 message 双向索引。当最新 message 编号达到 3000002000 时，会自动把当前窗口 3000001001 ~ 3000002000 重新映射为 3000000001 ~ 3000001000。';
  }

  if (elements.settingsWebuiPasswordInput) {
    elements.settingsWebuiPasswordInput.value = '';
  }
  if (elements.settingsWebuiPortInput) {
    elements.settingsWebuiPortInput.value = String(settings.webui_configured_port || state.status?.independent_webui_port || 5751);
  }
  if (elements.settingsMessageIndexMaxEntriesInput) {
    elements.settingsMessageIndexMaxEntriesInput.value = String(settings.message_index_max_entries || 1000);
  }
}

function isLogConsoleNearBottom() {
  if (!elements.logConsole) {
    return true;
  }
  const distance = elements.logConsole.scrollHeight - elements.logConsole.scrollTop - elements.logConsole.clientHeight;
  return distance < 48;
}

function renderLogAutoScrollState() {
  if (elements.logAutoScrollToggle) {
    elements.logAutoScrollToggle.checked = state.logs.autoScroll;
  }
  if (elements.logAutoScrollLabel) {
    elements.logAutoScrollLabel.textContent = state.logs.autoScroll ? '自动滚动已开启' : '自动滚动已关闭';
  }
}

function renderLogs({ scrollToBottom = false } = {}) {
  const activeLevels = state.logs.activeLevels;
  const visibleItems = state.logs.items.filter((item) => activeLevels.has(item.level));

  if (!visibleItems.length) {
    elements.logConsole.innerHTML = '<div class="log-empty">暂时还没有 Shell 或桥接器实时日志。</div>';
  } else {
    elements.logConsole.innerHTML = visibleItems
      .map((item) => `
        <div class="log-entry log-${item.level.toLowerCase()}">
          <span class="log-entry-level">${escapeHtml(item.level)}</span>
          <span class="log-entry-line">${escapeHtml(item.line)}</span>
        </div>
      `)
      .join('');
  }

  elements.logMeta.textContent = `实时日志 · 缓存 ${state.logs.items.length}/${state.logs.maxEntries} 条`;

  for (const button of elements.logFilterButtons) {
    const level = button.dataset.logLevel;
    button.classList.toggle('active', state.logs.activeLevels.has(level));
  }

  renderLogAutoScrollState();

  if (scrollToBottom && state.logs.autoScroll) {
    elements.logConsole.scrollTop = elements.logConsole.scrollHeight;
  }
}

async function loadLogs({ reset = false } = {}) {
  const afterId = reset ? 0 : state.logs.lastId;
  const requestGeneration = state.logs.generation;
  const payload = await requestJson(`/api/logs?after_id=${afterId}`);

  if (requestGeneration !== state.logs.generation) {
    return;
  }

  if (reset) {
    state.logs.items = [];
    state.logs.lastId = 0;
  }

  const incoming = Array.isArray(payload.items) ? payload.items : [];
  if (incoming.length) {
    state.logs.items.push(...incoming);
    const maxEntries = Number(payload.max_entries) || state.logs.maxEntries;
    state.logs.maxEntries = maxEntries;
    if (state.logs.items.length > maxEntries) {
      state.logs.items = state.logs.items.slice(-maxEntries);
    }
    state.logs.lastId = Number(incoming[incoming.length - 1].id) || state.logs.lastId;
  }

  renderLogs({ scrollToBottom: incoming.length > 0 });
}

function setLogAutoScroll(enabled) {
  state.logs.autoScroll = Boolean(enabled);
  renderLogAutoScrollState();
  if (state.logs.autoScroll && elements.logConsole) {
    elements.logConsole.scrollTop = elements.logConsole.scrollHeight;
  }
}

async function clearLogs() {
  const confirmed = window.confirm('确认清空当前实时日志吗？这会同时重置服务端缓存和当前页面日志视图。');
  if (!confirmed) {
    return;
  }

  const payload = await requestJson('/api/logs/clear', {
    method: 'POST',
  });

  state.logs.generation += 1;
  state.logs.items = [];
  state.logs.lastId = 0;
  state.logs.maxEntries = Number(payload.max_entries) || state.logs.maxEntries;
  renderLogs();
  showToast(`已清空 ${Number(payload.cleared) || 0} 条日志`, 'success');
}

function startLogPolling() {
  if (state.logs.pollTimer) {
    return;
  }

  const poll = async () => {
    try {
      await loadLogs();
    } catch (error) {
      console.error('log polling failed', error);
    } finally {
      state.logs.pollTimer = window.setTimeout(poll, 1000);
    }
  };

  state.logs.pollTimer = window.setTimeout(poll, 1000);
}

function effectiveStatusLabel(bot) {
  if (!bot.enabled) {
    return '已停用';
  }
  return bot.runtime_active ? '运行中' : '等待连接';
}

function renderBots(items) {
  state.bots = items;
  elements.botGrid.innerHTML = '';
  elements.emptyState.classList.toggle('hidden', items.length > 0);

  for (const bot of items) {
    const card = document.createElement('article');
    card.className = 'bot-card';
    card.innerHTML = `
      <div class="bot-card-header">
        <div>
          <span class="card-chip">${escapeHtml(bot.name || '未命名 Bot')}</span>
          <p class="card-type">WS bot 客户端</p>
        </div>
        <label class="field-switch compact-switch">
          <input type="checkbox" ${bot.enabled ? 'checked' : ''} data-role="toggle" data-id="${bot.id}" />
          <i></i>
        </label>
      </div>

      <div class="card-body">
        <div class="card-line">
          <span>状态</span>
          <strong>${escapeHtml(effectiveStatusLabel(bot))}</strong>
        </div>
        <div class="card-line">
          <span>Rocket.Chat</span>
          <code>${escapeHtml(bot.server_url || '-')}</code>
        </div>
        <div class="card-line">
          <span>WS URL</span>
          <code>${escapeHtml(bot.onebot_ws_url || '-')}</code>
        </div>
        <div class="card-line">
          <span>用户名</span>
          <strong>${escapeHtml(bot.username || '-')}</strong>
        </div>
        <div class="card-line">
          <span>self_id</span>
          <strong>${escapeHtml(String(bot.onebot_self_id || '-'))}</strong>
        </div>
      </div>

      <div class="card-actions">
        <button class="action-chip" type="button" data-role="edit" data-id="${bot.id}">编辑</button>
        <button class="action-chip danger" type="button" data-role="delete" data-id="${bot.id}">删除</button>
      </div>
    `;
    elements.botGrid.appendChild(card);
  }
}

function formatPluginBadge(value, fallback = '-') {
  const normalized = String(value || '').trim();
  return normalized || fallback;
}

function renderPlugins(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  state.plugins.items = items;
  elements.pluginCount.textContent = String(items.length);
  elements.pluginEnabledCount.textContent = String(items.filter((item) => item.activated).length);
  elements.pluginGrid.innerHTML = '';
  elements.pluginEmptyState.classList.toggle('hidden', items.length > 0);

  for (const item of items) {
    const card = document.createElement('article');
    card.className = `plugin-card ${item.activated ? '' : 'is-disabled'}`;
    const encodedId = encodeURIComponent(item.id);
    const logoMarkup = item.has_logo
      ? `<img class="plugin-logo-image" src="/api/plugins/${encodedId}/logo" alt="${escapeHtml(item.display_name || item.name || item.id)}" loading="lazy" />`
      : `<span class="plugin-logo-fallback">${escapeHtml((item.display_name || item.name || item.id || '?').trim().charAt(0) || '?')}</span>`;

    card.innerHTML = `
      <div class="plugin-card-header">
        <div class="plugin-card-title-row">
          <div class="plugin-logo-shell">${logoMarkup}</div>
          <div class="plugin-title-block">
            <h3>${escapeHtml(item.display_name || item.name || item.id)}</h3>
            <p class="plugin-subtitle">${escapeHtml(item.name || item.id)}</p>
          </div>
        </div>
        <label class="field-switch compact-switch">
          <input type="checkbox" ${item.activated ? 'checked' : ''} data-plugin-role="toggle" data-id="${escapeHtml(item.id)}" />
          <i></i>
        </label>
      </div>

      <div class="plugin-meta-badges">
        <span class="plugin-badge">版本 ${escapeHtml(formatPluginBadge(item.version, '未标注'))}</span>
        <span class="plugin-badge">作者 ${escapeHtml(formatPluginBadge(item.author, '未知'))}</span>
      </div>

      <p class="plugin-card-description">${escapeHtml(item.desc || '暂无插件描述。')}</p>

      <div class="plugin-card-body">
        <div class="card-line">
          <span>状态</span>
          <strong>${escapeHtml(item.activated ? '已启用' : '已停用')}</strong>
        </div>
        <div class="card-line">
          <span>设置项</span>
          <strong>${item.has_settings ? '可配置' : '无额外配置'}</strong>
        </div>
        <div class="card-line">
          <span>运行时</span>
          <strong>${item.runtime_available ? '可加载' : '无 main.py'}</strong>
        </div>
      </div>

      ${item.load_error ? `<p class="plugin-load-warning">${escapeHtml(item.load_error)}</p>` : ''}

      <div class="card-actions plugin-card-actions">
        <button class="action-chip" type="button" data-plugin-role="settings" data-id="${escapeHtml(item.id)}">设置</button>
        <button class="action-chip" type="button" data-plugin-role="reload" data-id="${escapeHtml(item.id)}">重载</button>
        <button class="action-chip danger" type="button" data-plugin-role="uninstall" data-id="${escapeHtml(item.id)}">卸载</button>
      </div>
    `;
    elements.pluginGrid.appendChild(card);
  }
}

function closePluginModal() {
  state.plugins.current = null;
  elements.pluginModal.classList.add('hidden');
  if (elements.pluginSettingsForm) {
    elements.pluginSettingsForm.innerHTML = '';
  }
}

function closePluginUninstallModal() {
  state.plugins.pendingUninstall = null;
  if (elements.pluginUninstallDeleteConfigInput) {
    elements.pluginUninstallDeleteConfigInput.checked = false;
  }
  if (elements.pluginUninstallDeleteDataInput) {
    elements.pluginUninstallDeleteDataInput.checked = false;
  }
  elements.pluginUninstallModal.classList.add('hidden');
}

function formatPluginFieldValue(type, value) {
  if (type === 'bool') {
    return Boolean(value);
  }
  if (type === 'list' || type === 'dict' || type === 'object' || type === 'template_list') {
    return JSON.stringify(value ?? (type === 'list' || type === 'template_list' ? [] : {}), null, 2);
  }
  if (value === null || value === undefined) {
    return '';
  }
  return String(value);
}

function renderPluginSettingsForm(item) {
  const schema = item?.schema || {};
  const config = item?.config || {};
  const entries = Object.entries(schema).filter(([key]) => key !== 'enabled');
  elements.pluginModalTitle.textContent = `插件设置：${item.display_name || item.name || item.id}`;
  elements.pluginModalMeta.innerHTML = `
    <div class="plugin-modal-summary">
      <span class="plugin-badge">${escapeHtml(item.name || item.id)}</span>
      <span class="plugin-badge">版本 ${escapeHtml(formatPluginBadge(item.version, '未标注'))}</span>
      <span class="plugin-badge">作者 ${escapeHtml(formatPluginBadge(item.author, '未知'))}</span>
    </div>
    <p class="plugin-card-description plugin-modal-description">${escapeHtml(item.desc || '暂无插件描述。')}</p>
  `;

  if (!entries.length) {
    elements.pluginSettingsForm.innerHTML = `
      <section class="form-section">
        <p class="plugin-settings-empty">这个插件目前没有额外的可视化设置项。</p>
      </section>
    `;
    return;
  }

  const fieldsHtml = entries.map(([key, fieldSchema]) => {
    const fieldType = String(fieldSchema.type || 'string');
    const fieldLabel = String(fieldSchema.description || key);
    const fieldHint = String(fieldSchema.hint || '');
    const formattedValue = formatPluginFieldValue(fieldType, config[key]);
    const inputId = `pluginField_${key}`;

    if (fieldType === 'bool') {
      return `
        <label class="field-switch plugin-field-switch">
          <span>${escapeHtml(fieldLabel)}</span>
          <input
            id="${escapeHtml(inputId)}"
            type="checkbox"
            data-plugin-field="${escapeHtml(key)}"
            data-plugin-type="${escapeHtml(fieldType)}"
            data-plugin-label="${escapeHtml(fieldLabel)}"
            ${formattedValue ? 'checked' : ''}
          />
          <i></i>
        </label>
        ${fieldHint ? `<p class="plugin-field-hint">${escapeHtml(fieldHint)}</p>` : ''}
      `;
    }

    if (fieldType === 'text' || fieldType === 'list' || fieldType === 'dict' || fieldType === 'object' || fieldType === 'template_list') {
      return `
        <label class="field-block span-two">
          <span>${escapeHtml(fieldLabel)}</span>
          <textarea
            id="${escapeHtml(inputId)}"
            class="plugin-json-field"
            rows="${fieldType === 'text' ? '5' : '6'}"
            data-plugin-field="${escapeHtml(key)}"
            data-plugin-type="${escapeHtml(fieldType)}"
            data-plugin-label="${escapeHtml(fieldLabel)}"
          >${escapeHtml(formattedValue)}</textarea>
        </label>
        ${fieldHint ? `<p class="plugin-field-hint">${escapeHtml(fieldHint)}</p>` : ''}
      `;
    }

    const inputType = fieldType === 'int' || fieldType === 'float' ? 'number' : 'text';
    const inputStep = fieldType === 'float' ? 'any' : fieldType === 'int' ? '1' : '';
    return `
      <label class="field-block span-two">
        <span>${escapeHtml(fieldLabel)}</span>
        <input
          id="${escapeHtml(inputId)}"
          type="${inputType}"
          ${inputStep ? `step="${inputStep}"` : ''}
          value="${escapeHtml(formattedValue)}"
          data-plugin-field="${escapeHtml(key)}"
          data-plugin-type="${escapeHtml(fieldType)}"
          data-plugin-label="${escapeHtml(fieldLabel)}"
        />
      </label>
      ${fieldHint ? `<p class="plugin-field-hint">${escapeHtml(fieldHint)}</p>` : ''}
    `;
  }).join('');

  elements.pluginSettingsForm.innerHTML = `
    <section class="form-section">
      <h3>主配置</h3>
      <div class="field-grid two-columns plugin-field-grid">${fieldsHtml}</div>
    </section>
  `;
}

function collectPluginSettingsPayload() {
  const payload = {};
  const fields = Array.from(elements.pluginSettingsForm.querySelectorAll('[data-plugin-field]'));
  for (const field of fields) {
    const key = field.dataset.pluginField;
    const type = field.dataset.pluginType || 'string';
    const label = field.dataset.pluginLabel || key;
    if (!key) {
      continue;
    }

    if (type === 'bool') {
      payload[key] = Boolean(field.checked);
      continue;
    }

    const rawValue = String(field.value || '');
    if (type === 'int') {
      const value = Number.parseInt(rawValue, 10);
      if (!Number.isInteger(value)) {
        throw new Error(`${label} 必须是整数`);
      }
      payload[key] = value;
      continue;
    }

    if (type === 'float') {
      const value = Number.parseFloat(rawValue);
      if (!Number.isFinite(value)) {
        throw new Error(`${label} 必须是数字`);
      }
      payload[key] = value;
      continue;
    }

    if (type === 'list' || type === 'dict' || type === 'object' || type === 'template_list') {
      let parsed;
      try {
        parsed = rawValue.trim() ? JSON.parse(rawValue) : (type === 'list' || type === 'template_list' ? [] : {});
      } catch (error) {
        throw new Error(`${label} 必须是合法 JSON`);
      }

      if ((type === 'list' || type === 'template_list') && !Array.isArray(parsed)) {
        throw new Error(`${label} 必须是 JSON 数组`);
      }
      if ((type === 'dict' || type === 'object') && (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object')) {
        throw new Error(`${label} 必须是 JSON 对象`);
      }
      payload[key] = parsed;
      continue;
    }

    payload[key] = rawValue;
  }
  return payload;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function setFormData(data) {
  const merged = { ...DEFAULT_FORM, ...data };
  for (const [key, value] of Object.entries(merged)) {
    const field = elements.form.elements.namedItem(key);
    if (!field) {
      continue;
    }
    if (field.type === 'checkbox') {
      field.checked = Boolean(value);
    } else {
      field.value = value ?? '';
    }
  }
}

function collectFormData() {
  const payload = {};
  for (const [key, defaultValue] of Object.entries(DEFAULT_FORM)) {
    const field = elements.form.elements.namedItem(key);
    if (!field) {
      continue;
    }

    if (field.type === 'checkbox') {
      payload[key] = field.checked;
      continue;
    }

    const rawValue = field.value;
    if (typeof defaultValue === 'number') {
      payload[key] = rawValue === '' ? defaultValue : Number(rawValue);
      continue;
    }
    payload[key] = rawValue;
  }
  return payload;
}

function openModal(bot = null) {
  state.editingId = bot?.id || null;
  elements.modalTitle.textContent = bot ? `编辑 Bot：${bot.name}` : '新建 Bot';
  setFormData(bot || buildCreateDefaults());
  elements.modal.classList.remove('hidden');
}

function closeModal() {
  state.editingId = null;
  elements.modal.classList.add('hidden');
}

async function loadData() {
  const [status, bots] = await Promise.all([
    requestJson('/api/status'),
    requestJson('/api/bots'),
  ]);
  renderStatus(status);
  renderBots(bots.items || []);

  if (state.currentPage === 'basic') {
    await loadBasicInfo({ forceReload: true, silent: true });
    return;
  }
  if (state.currentPage === 'settings') {
    await loadSettings({ forceReload: true, silent: true });
    return;
  }
  if (state.currentPage === 'plugins') {
    await loadPlugins({ forceReload: true, silent: true });
  }
}

async function loadPlugins({ forceReload = false, silent = false } = {}) {
  if (!forceReload && state.plugins.loaded) {
    return;
  }

  try {
    const plugins = await requestJson('/api/plugins');
    state.plugins.loaded = true;
    renderPlugins(plugins);
  } catch (error) {
    state.plugins.loaded = false;
    if (!silent) {
      showToast(error.message || '插件列表加载失败', 'error');
    }
  }
}

async function loadSettings({ forceReload = false, silent = false } = {}) {
  if (!forceReload && state.settings.loaded) {
    return;
  }

  try {
    const settings = await requestJson('/api/settings');
    state.settings.loaded = true;
    renderSettings(settings);
  } catch (error) {
    state.settings.loaded = false;
    if (!silent) {
      showToast(error.message || '设置项加载失败', 'error');
    }
  }
}

async function loadBasicInfo({ forceReload = false, silent = false } = {}) {
  if (!forceReload && state.basicInfo.loaded) {
    return;
  }

  try {
    const basicInfo = await requestJson('/api/basic-info');
    state.basicInfo.loaded = true;
    renderBasicInfo(basicInfo);
  } catch (error) {
    state.basicInfo.loaded = false;
    renderBasicInfo(buildBasicInfoFallback());
    if (!silent) {
      showToast('基础信息接口暂不可用，已显示回退信息；如刚更新 Shell，请重启 RocketCat Shell。', 'error');
    }
  }
}

async function saveBot() {
  const payload = collectFormData();
  const isEditing = Boolean(state.editingId);
  const endpoint = state.editingId ? `/api/bots/${state.editingId}` : '/api/bots';
  const method = state.editingId ? 'PUT' : 'POST';

  await requestJson(endpoint, {
    method,
    body: JSON.stringify(payload),
  });
  closeModal();
  showToast(isEditing ? 'Bot 已更新' : 'Bot 已创建', 'success');
  await loadData();
}

async function savePasswordSettings() {
  const password = String(elements.settingsWebuiPasswordInput?.value || '').trim();
  if (!password) {
    throw new Error('请设置登录密码');
  }

  const payload = await requestJson('/api/settings', {
    method: 'PUT',
    body: JSON.stringify({ webui_access_password: password }),
  });
  state.settings.loaded = true;
  renderSettings(payload);
  showToast('WebUI 登录密码已更新，新的密码已立即生效', 'success');
}

async function savePortSettings() {
  const rawPort = String(elements.settingsWebuiPortInput?.value || '').trim();
  if (!rawPort) {
    throw new Error('请输入新的 WebUI 访问端口');
  }

  const port = Number(rawPort);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('请输入 1 到 65535 之间的整数端口');
  }

  const payload = await requestJson('/api/settings', {
    method: 'PUT',
    body: JSON.stringify({ webui_port: port }),
  });
  state.settings.loaded = true;
  renderSettings(payload);
  await loadData();
  showToast('WebUI 访问端口已写入配置；重启后会优先尝试新端口', 'success');
}

async function saveMessageIndexSettings() {
  const rawValue = String(elements.settingsMessageIndexMaxEntriesInput?.value || '').trim();
  if (!rawValue) {
    throw new Error('请输入最大 message 双向索引储存条数');
  }

  const maxEntries = Number(rawValue);
  if (!Number.isInteger(maxEntries) || maxEntries <= 0) {
    throw new Error('最大 message 双向索引储存条数必须是正整数');
  }

  const payload = await requestJson('/api/settings', {
    method: 'PUT',
    body: JSON.stringify({ message_index_max_entries: maxEntries }),
  });
  state.settings.loaded = true;
  renderSettings(payload);
  showToast('message 双向索引条数上限已保存，现有索引窗口已按新规则整理', 'success');
}

function summarizeMessageIndexResult(result) {
  const botCount = Number(result?.bot_count) || 0;
  const changedBotCount = Number(result?.changed_bot_count) || 0;
  const removedCount = Number(result?.removed_message_mapping_count) || 0;
  if (botCount <= 0) {
    return '当前没有可处理的 Bot 索引';
  }
  return `已处理 ${botCount} 个 Bot，发生重排 ${changedBotCount} 个，清理 ${removedCount} 条旧映射`;
}

async function rebuildMessageIndexes() {
  const confirmed = window.confirm('确认按当前条数上限手动重建所有 Bot 的 message 双向索引吗？');
  if (!confirmed) {
    return;
  }

  const payload = await requestJson('/api/settings/rebuild-message-indexes', {
    method: 'POST',
  });
  await loadSettings({ forceReload: true, silent: true });
  showToast(summarizeMessageIndexResult(payload.result), 'success');
}

async function exportShellConfiguration() {
  const payload = await requestJson('/api/settings/export-config');
  const text = `${JSON.stringify(payload, null, 2)}\n`;
  await writeTextWithPicker('rocketcat_config.json', text);
}

async function importShellConfiguration() {
  const text = await pickJsonTextForImport();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (error) {
    throw new Error('配置导入失败，json 解析失败');
  }

  if (!payload || typeof payload !== 'object' || !(ROCKETCAT_CONFIG_MARKER_FIELD in payload)) {
    throw new Error('配置导入失败，json文件不为rocketcat配置文件');
  }

  await requestJson('/api/settings/import-config', {
    method: 'POST',
    body: JSON.stringify(payload),
  });

  closeModal();
  closePluginModal();
  closePluginUninstallModal();
  state.settings.loaded = false;
  state.basicInfo.loaded = false;
  state.plugins.loaded = false;
  await Promise.all([
    loadData(),
    loadSettings({ forceReload: true, silent: true }),
    loadBasicInfo({ forceReload: true, silent: true }),
    loadPlugins({ forceReload: true, silent: true }),
  ]);
}

async function toggleBot(botId, enabled) {
  const target = state.bots.find((bot) => bot.id === botId);
  if (!target) {
    return;
  }
  await requestJson(`/api/bots/${botId}`, {
    method: 'PUT',
    body: JSON.stringify({ ...target, enabled }),
  });
  showToast(enabled ? 'Bot 已启用' : 'Bot 已停用', 'success');
  await loadData();
}

async function deleteBot(botId) {
  const target = state.bots.find((bot) => bot.id === botId);
  if (!target) {
    return;
  }
  const confirmed = window.confirm(`确认删除 Bot「${target.name}」吗？`);
  if (!confirmed) {
    return;
  }
  await requestJson(`/api/bots/${botId}`, { method: 'DELETE' });
  showToast('Bot 已删除', 'success');
  await loadData();
}

async function openPluginSettings(pluginId) {
  const payload = await requestJson(`/api/plugins/${encodeURIComponent(pluginId)}`);
  const item = payload.item;
  if (!item) {
    throw new Error('读取插件详情失败');
  }
  state.plugins.current = item;
  renderPluginSettingsForm(item);
  elements.pluginModal.classList.remove('hidden');
}

async function savePluginSettings() {
  const current = state.plugins.current;
  if (!current?.id) {
    throw new Error('未找到目标插件');
  }
  const payload = collectPluginSettingsPayload();
  const response = await requestJson(`/api/plugins/${encodeURIComponent(current.id)}/config`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
  state.plugins.current = response.item || null;
  closePluginModal();
  state.plugins.loaded = false;
  await Promise.all([
    loadPlugins({ forceReload: true, silent: true }),
    loadData(),
  ]);
  showToast('插件设置已保存，并已刷新运行时', 'success');
}

async function togglePlugin(pluginId, enabled) {
  await requestJson(`/api/plugins/${encodeURIComponent(pluginId)}/enabled`, {
    method: 'PUT',
    body: JSON.stringify({ enabled }),
  });
  state.plugins.loaded = false;
  await Promise.all([
    loadPlugins({ forceReload: true, silent: true }),
    loadData(),
  ]);
  showToast(enabled ? '插件已启用' : '插件已停用', 'success');
}

async function reloadPlugin(pluginId) {
  await requestJson(`/api/plugins/${encodeURIComponent(pluginId)}/reload`, {
    method: 'POST',
  });
  state.plugins.loaded = false;
  await Promise.all([
    loadPlugins({ forceReload: true, silent: true }),
    loadData(),
  ]);
  showToast('插件已重载，并已刷新运行时', 'success');
}

function promptUninstallPlugin(pluginId) {
  const target = state.plugins.items.find((item) => item.id === pluginId);
  if (!target) {
    throw new Error('未找到目标插件');
  }
  state.plugins.pendingUninstall = target;
  elements.pluginUninstallTitle.textContent = '删除确认';
  elements.pluginUninstallMessage.textContent = `你确定要删除插件“${target.display_name || target.name || target.id}”吗？`;
  if (elements.pluginUninstallDeleteConfigInput) {
    elements.pluginUninstallDeleteConfigInput.checked = false;
  }
  if (elements.pluginUninstallDeleteDataInput) {
    elements.pluginUninstallDeleteDataInput.checked = false;
  }
  elements.pluginUninstallModal.classList.remove('hidden');
}

function buildPluginUninstallToast(deleteConfig, deleteData) {
  if (deleteConfig && deleteData) {
    return '插件已卸载，并已删除主配置和持久化数据';
  }
  if (deleteConfig) {
    return '插件已卸载，并已删除主配置';
  }
  if (deleteData) {
    return '插件已卸载，并已删除持久化数据';
  }
  return '插件已卸载，仅删除插件本体';
}

async function confirmUninstallPlugin() {
  const target = state.plugins.pendingUninstall;
  if (!target?.id) {
    throw new Error('未找到待卸载插件');
  }
  const deleteConfig = Boolean(elements.pluginUninstallDeleteConfigInput?.checked);
  const deleteData = Boolean(elements.pluginUninstallDeleteDataInput?.checked);
  await requestJson(`/api/plugins/${encodeURIComponent(target.id)}`, {
    method: 'DELETE',
    body: JSON.stringify({
      delete_config: deleteConfig,
      delete_data: deleteData,
    }),
  });
  closePluginUninstallModal();
  if (state.plugins.current?.id === target.id) {
    closePluginModal();
  }
  state.plugins.loaded = false;
  await Promise.all([
    loadPlugins({ forceReload: true, silent: true }),
    loadData(),
  ]);
  showToast(buildPluginUninstallToast(deleteConfig, deleteData), 'success');
}

elements.createButton?.addEventListener('click', () => openModal());
elements.refreshButton?.addEventListener('click', async () => {
  await loadData();
  showToast('列表已刷新');
});
elements.clearLogsButton?.addEventListener('click', async () => {
  try {
    await clearLogs();
  } catch (error) {
    showToast(error.message || '清空日志失败', 'error');
  }
});
elements.logAutoScrollToggle?.addEventListener('change', (event) => {
  setLogAutoScroll(event.target.checked);
});
elements.basicRefreshButton?.addEventListener('click', async () => {
  await activatePage('basic', { forceReload: true });
  showToast('基础信息已刷新');
});
elements.settingsRefreshButton?.addEventListener('click', async () => {
  await activatePage('settings', { forceReload: true });
  showToast('设置项已刷新');
});
elements.pluginsRefreshButton?.addEventListener('click', async () => {
  await activatePage('plugins', { forceReload: true });
  showToast('插件列表已刷新');
});
for (const button of elements.navButtons) {
  button.addEventListener('click', async () => {
    try {
      await activatePage(button.dataset.page);
    } catch (error) {
      showToast(error.message || '页面切换失败', 'error');
    }
  });
}
    elements.pluginCloseModalButton?.addEventListener('click', closePluginModal);
    elements.pluginCancelButton?.addEventListener('click', closePluginModal);
    elements.pluginUninstallCloseButton?.addEventListener('click', closePluginUninstallModal);
    elements.pluginUninstallCancelButton?.addEventListener('click', closePluginUninstallModal);
elements.closeModalButton?.addEventListener('click', closeModal);
elements.cancelButton?.addEventListener('click', closeModal);
elements.submitButton?.addEventListener('click', async () => {
  try {
    await saveBot();
  } catch (error) {
    showToast(error.message || '保存失败', 'error');
  }
});
elements.settingsPasswordSaveButton?.addEventListener('click', async () => {
  try {
    await savePasswordSettings();
  } catch (error) {
    showToast(error.message || '设置保存失败', 'error');
  }
});
elements.settingsPortSaveButton?.addEventListener('click', async () => {
  try {
    await savePortSettings();
  } catch (error) {
    showToast(error.message || '设置保存失败', 'error');
  }
});
elements.settingsMessageIndexSaveButton?.addEventListener('click', async () => {
  try {
    await saveMessageIndexSettings();
  } catch (error) {
    showToast(error.message || '设置保存失败', 'error');
  }
});
elements.settingsMessageIndexRebuildButton?.addEventListener('click', async () => {
  try {
    await rebuildMessageIndexes();
  } catch (error) {
    showToast(error.message || '手动重建双向索引失败', 'error');
  }
});
elements.settingsExportConfigButton?.addEventListener('click', async () => {
  try {
    await exportShellConfiguration();
    showToast('配置已导出', 'success');
  } catch (error) {
    if (isAbortError(error)) {
      return;
    }
    showToast(error.message || '配置导出失败', 'error');
  }
});
elements.settingsImportConfigButton?.addEventListener('click', async () => {
  try {
    await importShellConfiguration();
    showToast('配置已导入', 'success');
  } catch (error) {
    if (isAbortError(error)) {
      return;
    }
    showToast(error.message || '配置导入失败', 'error');
  }
});
elements.pluginSaveButton?.addEventListener('click', async () => {
  try {
    await savePluginSettings();
  } catch (error) {
    showToast(error.message || '插件设置保存失败', 'error');
  }
});
elements.pluginUninstallConfirmButton?.addEventListener('click', async () => {
  try {
    await confirmUninstallPlugin();
  } catch (error) {
    showToast(error.message || '插件卸载失败', 'error');
  }
});

elements.modal?.addEventListener('click', (event) => {
  if (event.target === elements.modal) {
    closeModal();
  }
});
elements.pluginModal?.addEventListener('click', (event) => {
  if (event.target === elements.pluginModal) {
    closePluginModal();
  }
});
elements.pluginUninstallModal?.addEventListener('click', (event) => {
  if (event.target === elements.pluginUninstallModal) {
    closePluginUninstallModal();
  }
});

elements.botGrid?.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-role]');
  if (!button) {
    return;
  }
  const { id, role } = button.dataset;
  if (role === 'edit') {
    const target = state.bots.find((bot) => bot.id === id);
    if (target) {
      openModal(target);
    }
    return;
  }
  if (role === 'delete') {
    try {
      await deleteBot(id);
    } catch (error) {
      showToast(error.message || '删除失败', 'error');
    }
  }
});

elements.botGrid?.addEventListener('change', async (event) => {
  const input = event.target.closest('[data-role="toggle"]');
  if (!input) {
    return;
  }
  try {
    await toggleBot(input.dataset.id, input.checked);
  } catch (error) {
    input.checked = !input.checked;
    showToast(error.message || '切换失败', 'error');
  }
});

elements.pluginGrid?.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-plugin-role]');
  if (!button) {
    return;
  }

  const pluginId = button.dataset.id;
  const role = button.dataset.pluginRole;
  try {
    if (role === 'settings') {
      await openPluginSettings(pluginId);
      return;
    }
    if (role === 'reload') {
      await reloadPlugin(pluginId);
      return;
    }
    if (role === 'uninstall') {
      promptUninstallPlugin(pluginId);
    }
  } catch (error) {
    showToast(error.message || '插件操作失败', 'error');
  }
});

elements.pluginGrid?.addEventListener('change', async (event) => {
  const input = event.target.closest('[data-plugin-role="toggle"]');
  if (!input) {
    return;
  }
  try {
    await togglePlugin(input.dataset.id, input.checked);
  } catch (error) {
    input.checked = !input.checked;
    showToast(error.message || '插件切换失败', 'error');
  }
});

for (const button of elements.logFilterButtons) {
  button.addEventListener('click', () => {
    const level = button.dataset.logLevel;
    if (state.logs.activeLevels.has(level)) {
      state.logs.activeLevels.delete(level);
    } else {
      state.logs.activeLevels.add(level);
    }
    renderLogs();
  });
}

window.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeModal();
    closePluginModal();
    closePluginUninstallModal();
  }
});

Promise.all([
  loadData(),
  loadLogs({ reset: true }),
])
  .then(() => {
    setActivePage('network');
    startLogPolling();
  })
  .catch((error) => {
    showToast(error.message || '加载失败', 'error');
  });