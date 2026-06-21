const DEFAULT_FORM = {
  name: 'bot',
  enabled: false,
  server_url: '',
  username: '',
  password: '',
  e2ee_password: '',
  onebot_ws_url: '',
  onebot_access_token: '',
  reconnect_delay: 5.0,
  max_reconnect_attempts: 10,
  enable_subchannel_session_isolation: true,
  remote_media_max_size: 20971520,
  room_info_cache_ttl_seconds: 300.0,
  perf_trace_enabled: false,
  skip_own_messages: true,
  debug: false,
};

const ROCKETCAT_CONFIG_MARKER_FIELD = 'Is rocketcat config';
const FILE_IMAGE_EXTENSIONS = new Set(['.bmp', '.gif', '.jpeg', '.jpg', '.png', '.webp']);
const SIDEBAR_STORAGE_KEY = 'rocketcat_sidebar_open';

function getStoredSidebarOpen() {
  try {
    const rawValue = window.localStorage.getItem(SIDEBAR_STORAGE_KEY);
    return rawValue === null ? true : rawValue !== 'false';
  } catch (_error) {
    return true;
  }
}

const state = {
  editingId: null,
  bots: [],
  status: null,
  currentPage: 'network',
  ui: {
    sidebarOpen: getStoredSidebarOpen(),
  },
  network: {
    pollTimer: null,
  },
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
  diagnostics: {
    data: null,
    loaded: false,
    pollTimer: null,
  },
  logs: {
    items: [],
    lastId: 0,
    maxEntries: 5000,
    pollTimer: null,
    abortController: null,
    polling: false,
    generation: 0,
    autoScroll: true,
    showPerf: true,
    activeLevels: new Set(['DEBUG', 'INFO', 'WARN', 'ERROR']),
  },
  plugins: {
    items: [],
    loaded: false,
    current: null,
    pendingUninstall: null,
  },
  files: {
    path: '',
    parentPath: '',
    canGoUp: false,
    rootPath: '',
    items: [],
    loaded: false,
    loading: false,
    uploading: false,
    downloading: false,
    uploadVisible: false,
    createType: 'file',
    selectedPaths: new Set(),
    moving: false,
    moveTargetPath: '',
    moveTree: {
      directories: new Map(),
      expanded: new Set(),
      loading: new Set(),
    },
    pendingDeletePaths: null,
    pendingMovePaths: null,
    pendingRenameItem: null,
    pendingAuthItem: null,
    pendingAuthMode: 'edit',
    previewItem: null,
    editingFile: null,
    pendingSave: false,
    imageViewer: {
      visible: false,
      items: [],
      index: 0,
    },
  },
  terminal: {
    items: [],
    activeId: '',
    loaded: false,
    sockets: new Map(),
    terms: new Map(),
    fitAddons: new Map(),
    dragId: '',
  },
  userMappings: {
    botId: '',
    items: [],
    total: 0,
    offset: 0,
    limit: 50,
    search: '',
    ready: false,
  },
};

function buildCreateDefaults() {
  return { ...DEFAULT_FORM };
}

const elements = {
  shellLayout: document.querySelector('.shell-layout'),
  navButtons: Array.from(document.querySelectorAll('[data-page]')),
  sidebarToggleButtons: [],
  networkPage: document.getElementById('networkPage'),
  diagnosticsPage: document.getElementById('diagnosticsPage'),
  basicPage: document.getElementById('basicPage'),
  logsPage: document.getElementById('logsPage'),
  settingsPage: document.getElementById('settingsPage'),
  pluginsPage: document.getElementById('pluginsPage'),
  filesPage: document.getElementById('filesPage'),
  terminalPage: document.getElementById('terminalPage'),
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
  basicRocketCatVersion: document.getElementById('basicRocketCatVersion'),
  diagnosticsRefreshButton: document.getElementById('diagnosticsRefreshButton'),
  diagnosticsCpuSummary: document.getElementById('diagnosticsCpuSummary'),
  diagnosticsCpuCores: document.getElementById('diagnosticsCpuCores'),
  diagnosticsCpuFrequency: document.getElementById('diagnosticsCpuFrequency'),
  diagnosticsProcessCpuUsage: document.getElementById('diagnosticsProcessCpuUsage'),
  diagnosticsCpuRing: document.getElementById('diagnosticsCpuRing'),
  diagnosticsCpuProcessRing: document.getElementById('diagnosticsCpuProcessRing'),
  diagnosticsCpuMeterValue: document.getElementById('diagnosticsCpuMeterValue'),
  diagnosticsCpuMeterDetail: document.getElementById('diagnosticsCpuMeterDetail'),
  diagnosticsCpuMeterSystem: document.getElementById('diagnosticsCpuMeterSystem'),
  diagnosticsCpuMeterProcess: document.getElementById('diagnosticsCpuMeterProcess'),
  diagnosticsMemorySummary: document.getElementById('diagnosticsMemorySummary'),
  diagnosticsMemoryAvailable: document.getElementById('diagnosticsMemoryAvailable'),
  diagnosticsMemoryProcess: document.getElementById('diagnosticsMemoryProcess'),
  diagnosticsMemoryTotal: document.getElementById('diagnosticsMemoryTotal'),
  diagnosticsMemoryRing: document.getElementById('diagnosticsMemoryRing'),
  diagnosticsMemoryProcessRing: document.getElementById('diagnosticsMemoryProcessRing'),
  diagnosticsMemoryMeterValue: document.getElementById('diagnosticsMemoryMeterValue'),
  diagnosticsMemoryMeterDetail: document.getElementById('diagnosticsMemoryMeterDetail'),
  diagnosticsMemoryMeterSystem: document.getElementById('diagnosticsMemoryMeterSystem'),
  diagnosticsMemoryMeterProcess: document.getElementById('diagnosticsMemoryMeterProcess'),
  diagnosticsSnapshotTime: document.getElementById('diagnosticsSnapshotTime'),
  diagnosticsHostNote: document.getElementById('diagnosticsHostNote'),
  diagnosticsCacheNote: document.getElementById('diagnosticsCacheNote'),
  diagnosticsOnlineCount: document.getElementById('diagnosticsOnlineCount'),
  diagnosticsRuntimeStorage: document.getElementById('diagnosticsRuntimeStorage'),
  diagnosticsRocketCatVersion: document.getElementById('diagnosticsRocketCatVersion'),
  diagnosticsGrid: document.getElementById('diagnosticsGrid'),
  diagnosticsEmptyState: document.getElementById('diagnosticsEmptyState'),
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
  fileRefreshButton: document.getElementById('fileRefreshButton'),
  fileUpButton: document.getElementById('fileUpButton'),
  fileCreateButton: document.getElementById('fileCreateButton'),
  fileUploadButton: document.getElementById('fileUploadButton'),
  fileDeleteSelectedButton: document.getElementById('fileDeleteSelectedButton'),
  fileMoveSelectedButton: document.getElementById('fileMoveSelectedButton'),
  fileDownloadSelectedButton: document.getElementById('fileDownloadSelectedButton'),
  fileDeleteSelectedCount: document.getElementById('fileDeleteSelectedCount'),
  fileMoveSelectedCount: document.getElementById('fileMoveSelectedCount'),
  fileDownloadSelectedCount: document.getElementById('fileDownloadSelectedCount'),
  fileCurrentPath: document.getElementById('fileCurrentPath'),
  fileRootPath: document.getElementById('fileRootPath'),
  fileItemCount: document.getElementById('fileItemCount'),
  fileSensitiveCount: document.getElementById('fileSensitiveCount'),
  fileBreadcrumb: document.getElementById('fileBreadcrumb'),
  fileStatus: document.getElementById('fileStatus'),
  fileUploadZone: document.getElementById('fileUploadZone'),
  fileUploadInput: document.getElementById('fileUploadInput'),
  fileUploadPickButton: document.getElementById('fileUploadPickButton'),
  fileUploadStatus: document.getElementById('fileUploadStatus'),
  fileTableBody: document.getElementById('fileTableBody'),
  fileSelectAllInput: document.getElementById('fileSelectAllInput'),
  fileEmptyState: document.getElementById('fileEmptyState'),
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
  openUserMappingsButton: document.getElementById('openUserMappingsButton'),
  userMappingsButtonHint: document.getElementById('userMappingsButtonHint'),
  userMappingsModal: document.getElementById('userMappingsModal'),
  userMappingsModalTitle: document.getElementById('userMappingsModalTitle'),
  userMappingsCloseButton: document.getElementById('userMappingsCloseButton'),
  userMappingsDoneButton: document.getElementById('userMappingsDoneButton'),
  userMappingsSearchInput: document.getElementById('userMappingsSearchInput'),
  userMappingsSearchButton: document.getElementById('userMappingsSearchButton'),
  userMappingsRefreshButton: document.getElementById('userMappingsRefreshButton'),
  userMappingsSummary: document.getElementById('userMappingsSummary'),
  userMappingsNotice: document.getElementById('userMappingsNotice'),
  userMappingsTableBody: document.getElementById('userMappingsTableBody'),
  userMappingsEmpty: document.getElementById('userMappingsEmpty'),
  userMappingsPrevButton: document.getElementById('userMappingsPrevButton'),
  userMappingsNextButton: document.getElementById('userMappingsNextButton'),
  userMappingsPageLabel: document.getElementById('userMappingsPageLabel'),
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
  filePreviewModal: document.getElementById('filePreviewModal'),
  filePreviewTitle: document.getElementById('filePreviewTitle'),
  filePreviewMeta: document.getElementById('filePreviewMeta'),
  filePreviewNotice: document.getElementById('filePreviewNotice'),
  filePreviewContent: document.getElementById('filePreviewContent'),
  filePreviewCloseButton: document.getElementById('filePreviewCloseButton'),
  filePreviewCancelButton: document.getElementById('filePreviewCancelButton'),
  fileEditModal: document.getElementById('fileEditModal'),
  fileEditPathChip: document.getElementById('fileEditPathChip'),
  fileEditNotice: document.getElementById('fileEditNotice'),
  fileEditLineNumbers: document.getElementById('fileEditLineNumbers'),
  fileEditContentInput: document.getElementById('fileEditContentInput'),
  fileEditCloseButton: document.getElementById('fileEditCloseButton'),
  fileEditCancelButton: document.getElementById('fileEditCancelButton'),
  fileEditSaveButton: document.getElementById('fileEditSaveButton'),
  fileSaveConfirmModal: document.getElementById('fileSaveConfirmModal'),
  fileSaveConfirmTitle: document.getElementById('fileSaveConfirmTitle'),
  fileSaveConfirmMessage: document.getElementById('fileSaveConfirmMessage'),
  fileSaveConfirmCloseButton: document.getElementById('fileSaveConfirmCloseButton'),
  fileSaveConfirmCancelButton: document.getElementById('fileSaveConfirmCancelButton'),
  fileSaveConfirmSubmitButton: document.getElementById('fileSaveConfirmSubmitButton'),
  fileImageViewer: document.getElementById('fileImageViewer'),
  fileImageViewerCount: document.getElementById('fileImageViewerCount'),
  fileImageViewerImage: document.getElementById('fileImageViewerImage'),
  fileImageViewerCloseButton: document.getElementById('fileImageViewerCloseButton'),
  fileImageViewerPrevButton: document.getElementById('fileImageViewerPrevButton'),
  fileImageViewerNextButton: document.getElementById('fileImageViewerNextButton'),
  fileCreateModal: document.getElementById('fileCreateModal'),
  fileCreateNameInput: document.getElementById('fileCreateNameInput'),
  fileCreateCloseButton: document.getElementById('fileCreateCloseButton'),
  fileCreateCancelButton: document.getElementById('fileCreateCancelButton'),
  fileCreateSubmitButton: document.getElementById('fileCreateSubmitButton'),
  fileCreateTypeButtons: Array.from(document.querySelectorAll('[data-file-create-type]')),
  fileDeleteModal: document.getElementById('fileDeleteModal'),
  fileDeleteTitle: document.getElementById('fileDeleteTitle'),
  fileDeleteMessage: document.getElementById('fileDeleteMessage'),
  fileDeleteCloseButton: document.getElementById('fileDeleteCloseButton'),
  fileDeleteCancelButton: document.getElementById('fileDeleteCancelButton'),
  fileDeleteConfirmButton: document.getElementById('fileDeleteConfirmButton'),
  fileMoveModal: document.getElementById('fileMoveModal'),
  fileMoveTree: document.getElementById('fileMoveTree'),
  fileMoveSelectedPath: document.getElementById('fileMoveSelectedPath'),
  fileMoveSelectionInfo: document.getElementById('fileMoveSelectionInfo'),
  fileMoveCloseButton: document.getElementById('fileMoveCloseButton'),
  fileMoveCancelButton: document.getElementById('fileMoveCancelButton'),
  fileMoveConfirmButton: document.getElementById('fileMoveConfirmButton'),
  fileRenameModal: document.getElementById('fileRenameModal'),
  fileRenameNameInput: document.getElementById('fileRenameNameInput'),
  fileRenameCloseButton: document.getElementById('fileRenameCloseButton'),
  fileRenameCancelButton: document.getElementById('fileRenameCancelButton'),
  fileRenameSubmitButton: document.getElementById('fileRenameSubmitButton'),
  fileAuthModal: document.getElementById('fileAuthModal'),
  fileAuthMessage: document.getElementById('fileAuthMessage'),
  fileAuthPasswordInput: document.getElementById('fileAuthPasswordInput'),
  fileAuthCloseButton: document.getElementById('fileAuthCloseButton'),
  fileAuthCancelButton: document.getElementById('fileAuthCancelButton'),
  fileAuthSubmitButton: document.getElementById('fileAuthSubmitButton'),
  terminalCreateButton: document.getElementById('terminalCreateButton'),
  terminalTabs: document.getElementById('terminalTabs'),
  terminalEmptyState: document.getElementById('terminalEmptyState'),
  terminalWorkspace: document.getElementById('terminalWorkspace'),
  terminalScreen: document.getElementById('terminalScreen'),
  toast: document.getElementById('toast'),
  logConsole: document.getElementById('logConsole'),
  logAutoScrollToggle: document.getElementById('logAutoScrollToggle'),
  logAutoScrollLabel: document.getElementById('logAutoScrollLabel'),
  logMeta: document.getElementById('logMeta'),
  clearLogsButton: document.getElementById('clearLogsButton'),
  logFilterButtons: Array.from(document.querySelectorAll('[data-log-level]')),
  logPerfButton: document.querySelector('[data-log-perf]'),
};

function showToast(message, kind = 'default') {
  elements.toast.textContent = message;
  elements.toast.className = `toast ${kind}`;
  window.clearTimeout(showToast._timer);
  showToast._timer = window.setTimeout(() => {
    elements.toast.className = 'toast hidden';
  }, 2600);
}

function getSidebarToggleIcon(open) {
  if (open) {
    return `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M3 18h13v-2H3v2Z" />
        <path d="M3 13h10v-2H3v2Z" />
        <path d="M3 6v2h13V6H3Z" />
        <path d="m21 15.59-3.58-3.59L21 8.41 19.59 7l-5 5 5 5L21 15.59Z" />
      </svg>
    `;
  }
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 18h18v-2H3v2Z" />
      <path d="M3 13h18v-2H3v2Z" />
      <path d="M3 6v2h18V6H3Z" />
    </svg>
  `;
}

function setSidebarOpen(open, { persist = true } = {}) {
  state.ui.sidebarOpen = Boolean(open);
  document.body.classList.toggle('sidebar-collapsed', !state.ui.sidebarOpen);
  document.body.classList.toggle('sidebar-expanded', state.ui.sidebarOpen);

  const title = state.ui.sidebarOpen ? '收起左侧栏' : '展开左侧栏';
  for (const button of elements.sidebarToggleButtons) {
    button.innerHTML = getSidebarToggleIcon(state.ui.sidebarOpen);
    button.setAttribute('aria-label', title);
    button.setAttribute('title', title);
    button.setAttribute('aria-pressed', String(state.ui.sidebarOpen));
  }

  if (persist) {
    try {
      window.localStorage.setItem(SIDEBAR_STORAGE_KEY, String(state.ui.sidebarOpen));
    } catch (_error) {
      // Ignore storage failures in restricted browser modes.
    }
  }

  window.setTimeout(() => {
    if (state.currentPage === 'terminal' && state.terminal.activeId) {
      fitTerminal(state.terminal.activeId);
    }
  }, 220);
}

function toggleSidebar() {
  setSidebarOpen(!state.ui.sidebarOpen);
}

function setupSidebarToggleButtons() {
  const headers = Array.from(document.querySelectorAll('.page-header'));
  for (const header of headers) {
    if (header.querySelector('[data-sidebar-toggle]')) {
      continue;
    }
    const titleNode = header.firstElementChild;
    if (!titleNode || titleNode.classList.contains('header-actions')) {
      continue;
    }

    const titleGroup = document.createElement('div');
    titleGroup.className = 'page-header-title-group';

    const button = document.createElement('button');
    button.className = 'sidebar-toggle-button';
    button.type = 'button';
    button.dataset.sidebarToggle = 'true';
    button.addEventListener('click', toggleSidebar);

    header.insertBefore(titleGroup, titleNode);
    titleGroup.append(button, titleNode);
  }

  elements.sidebarToggleButtons = Array.from(document.querySelectorAll('[data-sidebar-toggle]'));
  setSidebarOpen(state.ui.sidebarOpen, { persist: false });
}

async function requestJson(url, options = {}) {
  const { headers: optionHeaders, ...requestOptions } = options;
  const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;
  const response = await fetch(url, {
    headers: isFormData
      ? { ...(optionHeaders || {}) }
      : {
          'Content-Type': 'application/json',
          ...(optionHeaders || {}),
        },
    ...requestOptions,
  });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401 && !options.skipAuthRedirect) {
    stopLogPolling();
    window.location.replace('/');
    throw new Error(payload.error || payload.detail || '登录已失效，请重新登录');
  }
  if (!response.ok) {
    const detail = payload.error || payload.detail;
    const message = typeof detail === 'string'
      ? detail
      : detail?.message
        ? `${detail.message}${detail.occupant?.user_id ? `（占用者：${detail.occupant.user_id} / ${detail.occupant.onebot_id}）` : ''}`
        : '请求失败';
    throw new Error(message);
  }
  return payload;
}

async function requestBlob(url, options = {}) {
  const { headers: optionHeaders, ...requestOptions } = options;
  const response = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...(optionHeaders || {}),
    },
    ...requestOptions,
  });
  if (response.status === 401 && !options.skipAuthRedirect) {
    stopLogPolling();
    window.location.replace('/');
    throw new Error('登录已失效，请重新登录');
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || payload.detail || '请求失败');
  }
  return response.blob();
}

function isAbortError(error) {
  return Boolean(error && (error.name === 'AbortError' || error.code === 20));
}

function buildJsonSavePickerOptions(fileName) {
  return {
    suggestedName: fileName,
    types: [
      {
        description: 'RocketCat 配置文件',
        accept: {
          'application/json': ['.json'],
        },
      },
    ],
  };
}

async function writeTextWithPicker(fileName, text, handle = null) {
  if (handle || typeof window.showSaveFilePicker === 'function') {
    const pickerHandle = handle || await window.showSaveFilePicker(buildJsonSavePickerOptions(fileName));
    const writable = await pickerHandle.createWritable();
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
  elements.diagnosticsPage.classList.toggle('hidden', page !== 'diagnostics');
  elements.basicPage.classList.toggle('hidden', page !== 'basic');
  elements.logsPage.classList.toggle('hidden', page !== 'logs');
  elements.settingsPage.classList.toggle('hidden', page !== 'settings');
  elements.pluginsPage.classList.toggle('hidden', page !== 'plugins');
  elements.filesPage.classList.toggle('hidden', page !== 'files');
  elements.terminalPage.classList.toggle('hidden', page !== 'terminal');

  for (const button of elements.navButtons) {
    const isActive = button.dataset.page === page;
    button.classList.toggle('active', isActive);
    button.classList.toggle('ghost', !isActive);
  }

  if (page === 'logs') {
    renderLogs();
    startLogPolling();
  } else {
    stopLogPolling();
  }

  if (page === 'network') {
    startNetworkPolling();
  } else {
    stopNetworkPolling();
  }

  if (page === 'diagnostics') {
    startDiagnosticsPolling();
  } else {
    stopDiagnosticsPolling();
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
  if (page === 'network') {
    await loadData();
    return;
  }
  if (page === 'diagnostics') {
    await loadDiagnostics({ forceReload, silent: false });
    return;
  }
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
    return;
  }
  if (page === 'files') {
    await loadFiles({ forceReload, silent: false });
    return;
  }
  if (page === 'terminal') {
    await loadTerminals({ forceReload, silent: false });
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

function getDiagnosticStatusTone(statusCode) {
  if (statusCode === 'online') {
    return 'online';
  }
  if (statusCode === 'disabled') {
    return 'blocked';
  }
  return 'pending';
}

function formatDiagnosticBytes(value) {
  const normalized = Number(value);
  if (!Number.isFinite(normalized) || normalized < 0) {
    return '-';
  }
  if (normalized < 1024) {
    return `${Math.trunc(normalized)} B`;
  }
  if (normalized < 1024 ** 2) {
    return `${(normalized / 1024).toFixed(2)} KB`;
  }
  if (normalized < 1024 ** 3) {
    return `${(normalized / (1024 ** 2)).toFixed(2)} MB`;
  }
  return `${(normalized / (1024 ** 3)).toFixed(2)} GB`;
}

function formatDiagnosticTime(value) {
  const normalized = Number(value);
  if (!Number.isFinite(normalized) || normalized <= 0) {
    return '-';
  }
  const date = new Date(normalized * 1000);
  if (Number.isNaN(date.getTime())) {
    return '-';
  }
  const elapsedSeconds = Math.max(0, Math.floor(Date.now() / 1000 - normalized));
  let ageLabel = `${elapsedSeconds}s 前`;
  if (elapsedSeconds >= 86400) {
    ageLabel = `${Math.floor(elapsedSeconds / 86400)}d 前`;
  } else if (elapsedSeconds >= 3600) {
    ageLabel = `${Math.floor(elapsedSeconds / 3600)}h 前`;
  } else if (elapsedSeconds >= 60) {
    ageLabel = `${Math.floor(elapsedSeconds / 60)}m 前`;
  }
  const dateLabel = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`;
  return `${dateLabel} (${ageLabel})`;
}

function formatDiagnosticDuration(value) {
  const normalized = Number(value);
  if (!Number.isFinite(normalized) || normalized < 0) {
    return '-';
  }
  if (normalized < 1) {
    return '<1 秒';
  }
  if (normalized < 10) {
    return `${normalized.toFixed(1)} 秒`;
  }
  if (normalized < 60) {
    return `${Math.round(normalized)} 秒`;
  }
  if (normalized < 3600) {
    const minutes = Math.floor(normalized / 60);
    const seconds = Math.round(normalized % 60);
    return seconds > 0 ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
  }
  const hours = Math.floor(normalized / 3600);
  const minutes = Math.floor((normalized % 3600) / 60);
  return minutes > 0 ? `${hours} 小时 ${minutes} 分` : `${hours} 小时`;
}

function clampDiagnosticPercent(value) {
  const normalized = Number(value);
  if (!Number.isFinite(normalized)) {
    return 0;
  }
  return Math.max(0, Math.min(normalized, 100));
}

function formatDiagnosticPercentLabel(value) {
  const normalized = clampDiagnosticPercent(value);
  if (normalized === 0) {
    return '0%';
  }
  if (normalized < 10) {
    return `${normalized.toFixed(1)}%`;
  }
  return `${normalized.toFixed(0)}%`;
}

function getDiagnosticVisibleInnerPercent(value) {
  const normalized = clampDiagnosticPercent(value);
  if (normalized <= 0) {
    return 0;
  }
  return Math.max(normalized, 2.5);
}

function setDiagnosticsMeter(circleElement, percent) {
  if (!circleElement) {
    return;
  }
  const normalized = clampDiagnosticPercent(percent);
  circleElement.style.strokeDasharray = `${normalized} 100`;
}

function getDiagnosticsCacheStatusLabel(cacheMeta) {
  const status = String(cacheMeta?.cache_status || '').trim().toLowerCase();
  if (status === 'hit') {
    return '缓存命中';
  }
  if (status === 'miss') {
    return '实时采样';
  }
  if (status === 'disabled') {
    return '缓存关闭';
  }
  if (status === 'error') {
    return '采样失败';
  }
  return '状态未知';
}

function getDiagnosticAuthLabel(authState) {
  const normalized = String(authState || '').trim().toLowerCase();
  if (normalized === 'authenticated') {
    return '已认证';
  }
  if (normalized === 'partial') {
    return '部分认证';
  }
  if (normalized === 'disconnected') {
    return '未认证';
  }
  return String(authState || '-').trim() || '-';
}

function getRocketCatVersion(source = null) {
  const payload = source || {};
  return String(
    payload.version
      || payload.product_version
      || payload.host?.product_version
      || state.status?.version
      || '-'
  ).trim() || '-';
}

function stopNetworkPolling() {
  if (state.network.pollTimer) {
    window.clearTimeout(state.network.pollTimer);
    state.network.pollTimer = null;
  }
}

function startNetworkPolling() {
  stopNetworkPolling();
  if (state.currentPage !== 'network') {
    return;
  }
  state.network.pollTimer = window.setTimeout(async () => {
    try {
      await loadData();
    } catch (error) {
      if (!isAbortError(error)) {
        console.warn(error);
      }
    } finally {
      if (state.currentPage === 'network') {
        startNetworkPolling();
      }
    }
  }, 10000);
}

function stopDiagnosticsPolling() {
  if (state.diagnostics.pollTimer) {
    window.clearTimeout(state.diagnostics.pollTimer);
    state.diagnostics.pollTimer = null;
  }
}

function startDiagnosticsPolling() {
  stopDiagnosticsPolling();
  if (state.currentPage !== 'diagnostics') {
    return;
  }
  state.diagnostics.pollTimer = window.setTimeout(async () => {
    try {
      await loadDiagnostics({ forceReload: true, silent: true });
    } catch (error) {
      if (!isAbortError(error)) {
        console.warn(error);
      }
    } finally {
      if (state.currentPage === 'diagnostics') {
        startDiagnosticsPolling();
      }
    }
  }, 10000);
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

function renderDiagnostics(payload) {
  const diagnostics = payload || {};
  const host = diagnostics.host || null;
  const hostCache = diagnostics.host_cache || null;
  const summary = diagnostics.summary || {};
  const items = Array.isArray(diagnostics.items) ? diagnostics.items : [];
  state.diagnostics.data = diagnostics;
  state.diagnostics.loaded = true;

  elements.diagnosticsSnapshotTime.textContent = host
    ? (host.snapshot_timestamp ? formatDiagnosticTime(host.snapshot_timestamp) : (host.snapshot_time || '-'))
    : '主机快照不可用';
  elements.diagnosticsHostNote.textContent = host
    ? `${host.system_label || '-'} · Python ${host.python_version || '-'} · 主机 ${host.hostname || '-'}`
    : (diagnostics.host_error || '当前无法获取主机诊断快照。');
  elements.diagnosticsCacheNote.textContent = host
    ? `采样状态: ${getDiagnosticsCacheStatusLabel(hostCache)} · 快照年龄 ${formatDiagnosticDuration(hostCache?.snapshot_age_seconds)} · TTL ${formatDiagnosticDuration(hostCache?.cache_ttl_seconds)}`
    : `采样状态: ${getDiagnosticsCacheStatusLabel(hostCache)} · TTL ${formatDiagnosticDuration(hostCache?.cache_ttl_seconds)}`;

  const cpuPercent = clampDiagnosticPercent(host?.cpu_usage_percent);
  const cpuProcessPercent = Math.min(cpuPercent, clampDiagnosticPercent(host?.process_cpu_usage_percent));
  const memoryPercent = clampDiagnosticPercent(host?.memory_usage_percent);
  const memoryProcessPercent = Math.min(memoryPercent, clampDiagnosticPercent(host?.process_memory_percent));
  const cpuProcessVisiblePercent = Math.min(cpuPercent, getDiagnosticVisibleInnerPercent(cpuProcessPercent));
  const memoryProcessVisiblePercent = Math.min(memoryPercent, getDiagnosticVisibleInnerPercent(memoryProcessPercent));
  setDiagnosticsMeter(elements.diagnosticsCpuRing, cpuPercent);
  setDiagnosticsMeter(elements.diagnosticsCpuProcessRing, cpuProcessVisiblePercent);
  setDiagnosticsMeter(elements.diagnosticsMemoryRing, memoryPercent);
  setDiagnosticsMeter(elements.diagnosticsMemoryProcessRing, memoryProcessVisiblePercent);

  elements.diagnosticsCpuSummary.textContent = host?.cpu_model || '-';
  elements.diagnosticsCpuCores.textContent = host?.cpu_cores || '-';
  elements.diagnosticsCpuFrequency.textContent = host?.cpu_frequency || '-';
  elements.diagnosticsProcessCpuUsage.textContent = host?.process_cpu_usage || '-';
  elements.diagnosticsCpuMeterValue.textContent = host ? String(Math.round(cpuPercent)) : '-';
  elements.diagnosticsCpuMeterDetail.textContent = '外环系统 · 内环 Shell';
  elements.diagnosticsCpuMeterSystem.textContent = host ? formatDiagnosticPercentLabel(cpuPercent) : '-';
  elements.diagnosticsCpuMeterProcess.textContent = host ? formatDiagnosticPercentLabel(cpuProcessPercent) : '-';

  elements.diagnosticsMemorySummary.textContent = host
    ? `已用 ${host.memory_used || '-'} / 总量 ${host.memory_total || '-'}`
    : '-';
  elements.diagnosticsMemoryAvailable.textContent = host?.memory_available || '-';
  elements.diagnosticsMemoryProcess.textContent = host?.process_memory || '-';
  elements.diagnosticsMemoryTotal.textContent = host?.memory_total || '-';
  elements.diagnosticsMemoryMeterValue.textContent = host ? String(Math.round(memoryPercent)) : '-';
  elements.diagnosticsMemoryMeterDetail.textContent = '外环系统 · 内环 Shell';
  elements.diagnosticsMemoryMeterSystem.textContent = host ? formatDiagnosticPercentLabel(memoryPercent) : '-';
  elements.diagnosticsMemoryMeterProcess.textContent = host ? formatDiagnosticPercentLabel(memoryProcessPercent) : '-';

  elements.diagnosticsOnlineCount.textContent = `${Number(summary.online_bot_count) || 0} / ${Number(summary.enabled_bot_count) || 0}`;
  elements.diagnosticsRuntimeStorage.textContent = `${formatDiagnosticBytes(summary.total_runtime_snapshot_bytes)} / ${formatDiagnosticBytes(summary.total_runtime_journal_bytes)}`;
  elements.diagnosticsRocketCatVersion.textContent = getRocketCatVersion(diagnostics);

  elements.diagnosticsEmptyState.classList.toggle('hidden', items.length > 0);
  elements.diagnosticsGrid.innerHTML = '';

  for (const item of items) {
    const card = document.createElement('article');
    const tone = getDiagnosticStatusTone(item.status_code);
    const disconnectRow = item.last_disconnect_reason
      ? `
        <div class="diagnostics-row">
          <span>最近断开</span>
          <strong>${escapeHtml(item.last_disconnect_reason)}</strong>
        </div>`
      : '';
    card.className = 'diagnostics-card';
    card.innerHTML = `
      <div class="diagnostics-card-head">
        <div>
          <h3 class="diagnostics-card-title">${escapeHtml(item.client_name || '未命名 Bot')}</h3>
          <p class="diagnostics-card-subtitle">${escapeHtml(item.bot_id || '-')}</p>
        </div>
        <span class="basic-status-pill ${tone}">${escapeHtml(item.status_label || '-')}</span>
      </div>

      <div class="diagnostics-card-body">
        <div class="diagnostics-row">
          <span>认证状态</span>
          <strong>${escapeHtml(getDiagnosticAuthLabel(item.auth_state))}</strong>
        </div>
        <div class="diagnostics-row">
          <span>Rocket.Chat</span>
          <strong>${escapeHtml(item.server_url || '-')}</strong>
        </div>
        <div class="diagnostics-row">
          <span>服务端版本</span>
          <strong>${escapeHtml(`${item.server_version || 'unknown'} · ${item.compatibility_status || 'unknown'}`)}</strong>
        </div>
        <div class="diagnostics-row">
          <span>上传端点</span>
          <strong>${escapeHtml(item.upload_endpoint || '-')}</strong>
        </div>
        <div class="diagnostics-row">
          <span>Method 传输</span>
          <strong>${escapeHtml(`${item.method_transport || '-'} · 回退 ${item.method_rest_fallbacks ?? 0} 次`)}</strong>
        </div>
        <div class="diagnostics-row">
          <span>OneBot self_id</span>
          <strong>${escapeHtml(item.onebot_self_id || '-')}</strong>
        </div>
        <div class="diagnostics-row">
          <span>重连失败</span>
          <strong>${escapeHtml(String(item.reconnect_failures ?? 0))}</strong>
        </div>
        <div class="diagnostics-row">
          <span>最近 WebSocket</span>
          <strong>${escapeHtml(formatDiagnosticTime(item.last_websocket_activity_at))}</strong>
        </div>
        <div class="diagnostics-row">
          <span>最近入站</span>
          <strong>${escapeHtml(formatDiagnosticTime(item.last_inbound_message_at))}</strong>
        </div>
        <div class="diagnostics-row">
          <span>最近出站</span>
          <strong>${escapeHtml(formatDiagnosticTime(item.last_outbound_message_at))}</strong>
        </div>
        <div class="diagnostics-row">
          <span>Snapshot</span>
          <strong>${escapeHtml(formatDiagnosticBytes(item.runtime_snapshot_bytes))}</strong>
        </div>
        <div class="diagnostics-row">
          <span>Journal</span>
          <strong>${escapeHtml(formatDiagnosticBytes(item.runtime_journal_bytes))}</strong>
        </div>${disconnectRow}
      </div>
    `;
    elements.diagnosticsGrid.appendChild(card);
  }
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
  elements.basicRocketCatVersion.textContent = getRocketCatVersion(payload);
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
        <div class="basic-meta-row wide">
          <span>服务端版本</span>
          <strong>${escapeHtml(`${item.server_version || 'unknown'} · ${item.compatibility_status || 'unknown'}`)}</strong>
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
    ? '当前仍在使用默认密码 123456，请尽快修改；该密码也用于敏感文件预览鉴权。'
    : '当前已使用自定义 WebUI 登录认证 / 文件管理鉴权密码。';
  if (elements.settingsPasswordHelper) {
    elements.settingsPasswordHelper.textContent = '保存后立即生效。当前会话会保留，后续重新登录和打开敏感持久化数据文件都需使用新密码。';
  }
  if (elements.settingsPortHint) {
    elements.settingsPortHint.textContent = settings.webui_port_hint
      || '保存后会写入配置。重启 RocketCat Shell 时会优先尝试该端口；如果端口被占用，仍会自动回退到可用端口。';
  }
  if (elements.settingsMessageIndexHint) {
    elements.settingsMessageIndexHint.textContent = settings.message_index_hint
      || '当前最多保留 1000 条最近 message 映射。当最新 message 编号达到 3000002000 时，会自动把当前映射窗口 3000001001 ~ 3000002000 重新映射为 3000000001 ~ 3000001000。';
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
  const visibleItems = state.logs.items.filter((item) => {
    if (!activeLevels.has(item.level)) {
      return false;
    }
    if (!state.logs.showPerf && item.is_perf) {
      return false;
    }
    return true;
  });

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
  if (elements.logPerfButton) {
    elements.logPerfButton.classList.toggle('active', state.logs.showPerf);
  }

  renderLogAutoScrollState();

  if (scrollToBottom && state.logs.autoScroll) {
    elements.logConsole.scrollTop = elements.logConsole.scrollHeight;
  }
}

async function loadLogs({ reset = false, waitSeconds = 0, signal = null } = {}) {
  const afterId = reset ? 0 : state.logs.lastId;
  const requestGeneration = state.logs.generation;
  const query = new URLSearchParams({
    after_id: String(afterId),
    wait: String(Math.max(0, Number(waitSeconds) || 0)),
  });
  const payload = await requestJson(`/api/logs?${query.toString()}`, { signal });

  if (requestGeneration !== state.logs.generation) {
    return;
  }

  if (reset || payload.reset) {
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
  if (state.logs.polling) {
    return;
  }
  state.logs.polling = true;

  const poll = async () => {
    if (!state.logs.polling) {
      return;
    }
    const controller = new AbortController();
    state.logs.abortController = controller;
    try {
      await loadLogs({ waitSeconds: 25, signal: controller.signal });
    } catch (error) {
      if (!isAbortError(error)) {
        console.error('log polling failed', error);
      }
    } finally {
      if (state.logs.abortController === controller) {
        state.logs.abortController = null;
      }
      if (state.logs.polling) {
        state.logs.pollTimer = window.setTimeout(poll, 250);
      }
    }
  };

  poll();
}

function stopLogPolling() {
  state.logs.polling = false;
  if (state.logs.pollTimer) {
    window.clearTimeout(state.logs.pollTimer);
    state.logs.pollTimer = null;
  }
  if (state.logs.abortController) {
    state.logs.abortController.abort();
    state.logs.abortController = null;
  }
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

function getTerminalItem(id) {
  return state.terminal.items.find((item) => item.id === id) || null;
}

function getTerminalSocketUrl(id) {
  const url = new URL(window.location.href);
  url.protocol = url.protocol.replace('http', 'ws');
  url.pathname = `/api/ws/terminal/${encodeURIComponent(id)}`;
  url.search = '';
  return url.toString();
}

function createTerminalRenderer(id) {
  if (!id) {
    return null;
  }
  const existing = state.terminal.terms.get(id);
  if (existing) {
    return existing;
  }
  if (typeof window.Terminal !== 'function') {
    showToast('终端渲染组件未加载，请刷新页面', 'error');
    return null;
  }

  const term = new window.Terminal({
    allowTransparency: true,
    convertEol: false,
    cursorBlink: true,
    cursorStyle: 'bar',
    fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
    fontSize: 16,
    lineHeight: 1.25,
    scrollback: 5000,
    theme: {
      background: '#ffffff00',
      foreground: '#111016',
      cursor: '#cf145a',
      selectionBackground: '#cfd3d7',
      black: '#111016',
      red: '#cf145a',
      green: '#208a5b',
      yellow: '#aa7800',
      blue: '#3267d6',
      magenta: '#9f4cc9',
      cyan: '#007f99',
      white: '#7f7f7f',
      brightBlack: '#777284',
      brightRed: '#ef4f8c',
      brightGreen: '#2bad72',
      brightYellow: '#c99826',
      brightBlue: '#4c83f1',
      brightMagenta: '#b96bea',
      brightCyan: '#14a0bd',
      brightWhite: '#111016',
    },
  });

  if (window.FitAddon?.FitAddon) {
    const fitAddon = new window.FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    state.terminal.fitAddons.set(id, fitAddon);
  }

  term.onData((data) => sendTerminalInput(id, data));
  state.terminal.terms.set(id, term);
  return term;
}

function mountActiveTerminal() {
  const id = state.terminal.activeId;
  const screen = elements.terminalScreen;
  if (!id || !screen) {
    return;
  }

  const term = createTerminalRenderer(id);
  if (!term) {
    return;
  }

  if (!term.element) {
    screen.replaceChildren();
    term.open(screen);
  } else if (term.element.parentElement !== screen) {
    screen.replaceChildren();
    screen.appendChild(term.element);
  } else {
    for (const child of Array.from(screen.children)) {
      if (child !== term.element) {
        child.remove();
      }
    }
  }

  fitTerminal(id);
  window.requestAnimationFrame(() => {
    term.focus();
    fitTerminal(id);
  });
}

function fitTerminal(id = state.terminal.activeId) {
  const term = state.terminal.terms.get(id);
  const fitAddon = state.terminal.fitAddons.get(id);
  if (!term || !fitAddon || elements.terminalWorkspace?.classList.contains('hidden')) {
    return;
  }
  try {
    fitAddon.fit();
  } catch (_error) {
    return;
  }
  sendTerminalResize(id);
}

function sendTerminalResize(id) {
  const term = state.terminal.terms.get(id);
  const socket = state.terminal.sockets.get(id);
  if (!term || !socket || socket.readyState !== WebSocket.OPEN) {
    return;
  }
  socket.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
}

function writeTerminalOutput(id, data) {
  const term = createTerminalRenderer(id);
  if (!term || !data) {
    return;
  }
  term.write(data);
}

function handleTerminalMessage(id, event) {
  let payload = { type: 'output', data: String(event.data || '') };
  try {
    payload = JSON.parse(event.data);
  } catch (_error) {
    // Plain text output is accepted for compatibility.
  }

  if (payload.data) {
    writeTerminalOutput(id, payload.data);
  }
  if (payload.type === 'exit') {
    removeTerminalLocally(id);
  }
}

function connectTerminal(id) {
  if (!id || !getTerminalItem(id)) {
    return null;
  }
  const existing = state.terminal.sockets.get(id);
  if (existing && [WebSocket.CONNECTING, WebSocket.OPEN].includes(existing.readyState)) {
    return existing;
  }

  const socket = new WebSocket(getTerminalSocketUrl(id));
  state.terminal.sockets.set(id, socket);
  socket.addEventListener('open', () => {
    fitTerminal(id);
  });
  socket.addEventListener('message', (event) => handleTerminalMessage(id, event));
  socket.addEventListener('close', () => {
    if (state.terminal.sockets.get(id) === socket) {
      state.terminal.sockets.delete(id);
    }
  });
  socket.addEventListener('error', () => {
    if (state.currentPage === 'terminal') {
      showToast('终端连接失败', 'error');
    }
  });
  return socket;
}

function sendTerminalInput(id, data) {
  const socket = connectTerminal(id);
  if (!socket) {
    showToast('请先创建终端', 'error');
    return false;
  }
  if (socket.readyState !== WebSocket.OPEN) {
    showToast('终端正在连接，请稍后再试', 'error');
    return false;
  }
  socket.send(JSON.stringify({ type: 'input', data }));
  return true;
}

function removeTerminalLocally(id) {
  const index = state.terminal.items.findIndex((item) => item.id === id);
  state.terminal.items = state.terminal.items.filter((item) => item.id !== id);
  const socket = state.terminal.sockets.get(id);
  if (socket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) {
    socket.close();
  }
  state.terminal.sockets.delete(id);
  state.terminal.fitAddons.delete(id);
  const term = state.terminal.terms.get(id);
  if (term) {
    term.dispose();
  }
  state.terminal.terms.delete(id);

  if (state.terminal.activeId === id) {
    const fallback = state.terminal.items[Math.max(0, Math.min(index, state.terminal.items.length - 1))];
    state.terminal.activeId = fallback?.id || '';
  }
  renderTerminals();
}

function renderTerminalTabs() {
  if (!elements.terminalTabs) {
    return;
  }
  elements.terminalTabs.innerHTML = state.terminal.items.map((item) => {
    const isActive = item.id === state.terminal.activeId;
    return `
      <button
        class="terminal-tab${isActive ? ' active' : ''}"
        type="button"
        role="tab"
        draggable="true"
        aria-selected="${isActive ? 'true' : 'false'}"
        data-terminal-id="${escapeHtml(item.id)}"
      >
        <span class="terminal-tab-title">${escapeHtml(item.title || item.id)}</span>
        <span class="terminal-tab-close" data-terminal-close="${escapeHtml(item.id)}" aria-label="关闭终端">×</span>
      </button>
    `;
  }).join('');
}

function renderTerminals() {
  const hasTerminals = state.terminal.items.length > 0;
  elements.terminalTabs?.classList.toggle('hidden', !hasTerminals);
  elements.terminalEmptyState?.classList.toggle('hidden', hasTerminals);
  elements.terminalWorkspace?.classList.toggle('hidden', !hasTerminals);

  if (hasTerminals && !getTerminalItem(state.terminal.activeId)) {
    state.terminal.activeId = state.terminal.items[0]?.id || '';
  }

  renderTerminalTabs();
  if (state.terminal.activeId) {
    mountActiveTerminal();
    connectTerminal(state.terminal.activeId);
  }
}

async function loadTerminals({ forceReload = false, silent = false } = {}) {
  if (state.terminal.loaded && !forceReload) {
    renderTerminals();
    return;
  }
  try {
    const payload = await requestJson('/api/terminal/list');
    state.terminal.items = Array.isArray(payload.items) ? payload.items : [];
    if (!getTerminalItem(state.terminal.activeId)) {
      state.terminal.activeId = state.terminal.items[0]?.id || '';
    }
    state.terminal.loaded = true;
    renderTerminals();
  } catch (error) {
    if (!silent) {
      showToast(error.message || '终端列表加载失败', 'error');
    }
    throw error;
  }
}

async function createTerminal() {
  const item = await requestJson('/api/terminal/create', {
    method: 'POST',
    body: JSON.stringify({ cols: 80, rows: 24 }),
  });
  state.terminal.items.push(item);
  state.terminal.activeId = item.id;
  state.terminal.loaded = true;
  renderTerminals();
  showToast('终端已创建', 'success');
}

async function closeTerminal(id) {
  if (!id) {
    return;
  }
  await requestJson(`/api/terminal/${encodeURIComponent(id)}/close`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
  removeTerminalLocally(id);
}

async function saveTerminalOrder() {
  await requestJson('/api/terminal/order', {
    method: 'PUT',
    body: JSON.stringify({
      order: state.terminal.items.map((item) => item.id),
    }),
  });
}

function reorderTerminalTabs(fromId, toId) {
  if (!fromId || !toId || fromId === toId) {
    return;
  }
  const fromIndex = state.terminal.items.findIndex((item) => item.id === fromId);
  const toIndex = state.terminal.items.findIndex((item) => item.id === toId);
  if (fromIndex < 0 || toIndex < 0) {
    return;
  }
  const [moved] = state.terminal.items.splice(fromIndex, 1);
  state.terminal.items.splice(toIndex, 0, moved);
  renderTerminalTabs();
  saveTerminalOrder().catch((error) => {
    showToast(error.message || '终端顺序保存失败', 'error');
  });
}

function normalizeFilePath(value = '') {
  return String(value || '')
    .replaceAll('\\', '/')
    .split('/')
    .filter((part) => part && part !== '.')
    .join('/');
}

function formatFilePath(value = '') {
  const normalized = normalizeFilePath(value);
  return normalized ? `/${normalized}` : '/';
}

function joinFilePath(basePath = '', childPath = '') {
  const base = normalizeFilePath(basePath);
  const child = normalizeFilePath(childPath);
  if (base && child) {
    return `${base}/${child}`;
  }
  return child || base;
}

function getFileExtension(item = {}) {
  const extension = String(item.extension || '').toLowerCase();
  if (extension) {
    return extension;
  }
  const name = String(item.name || item.path || '');
  const dotIndex = name.lastIndexOf('.');
  return dotIndex >= 0 ? name.slice(dotIndex).toLowerCase() : '';
}

function isFileImage(item = {}) {
  return item.preview_type === 'image' || FILE_IMAGE_EXTENSIONS.has(getFileExtension(item));
}

function buildFilePreviewUrl(pathValue = '') {
  return `/api/files/preview?path=${encodeURIComponent(normalizeFilePath(pathValue))}`;
}

function formatFileSize(value, isDirectory = false) {
  if (isDirectory) {
    return '-';
  }
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) {
    return '-';
  }
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 ** 2) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  if (size < 1024 ** 3) {
    return `${(size / (1024 ** 2)).toFixed(1)} MB`;
  }
  return `${(size / (1024 ** 3)).toFixed(2)} GB`;
}

function formatFileTime(value) {
  if (!value) {
    return '-';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '-';
  }
  return date.toLocaleString();
}

function validateRelativeFileName(value = '') {
  const normalized = normalizeFilePath(value);
  if (!normalized) {
    throw new Error('名称不能为空');
  }
  if (String(value || '').startsWith('/') || /^[A-Za-z]:/.test(String(value || ''))) {
    throw new Error('名称必须是相对路径');
  }
  if (normalized.split('/').some((part) => part === '..')) {
    throw new Error('名称不能包含上级目录');
  }
  if (/[<>:"|?*]/.test(normalized)) {
    throw new Error('名称包含非法字符');
  }
  return normalized;
}

function validateFileBaseName(value = '') {
  const name = String(value || '').trim();
  if (!name) {
    throw new Error('名称不能为空');
  }
  if (name === '.' || name === '..' || name.includes('/') || name.includes('\\')) {
    throw new Error('名称不能包含目录层级');
  }
  if (/^[A-Za-z]:/.test(name) || /[<>:"|?*]/.test(name)) {
    throw new Error('名称包含非法字符');
  }
  return name;
}

function getFileIconVariant(item) {
  const extension = getFileExtension(item);
  if (extension === '.txt') {
    return 'text';
  }
  if (extension === '.json' || extension === '.py' || extension === '.md') {
    return 'code';
  }
  if (extension === '.pdf') {
    return 'pdf';
  }
  if (extension === '.doc' || extension === '.docx') {
    return 'word';
  }
  return 'generic';
}

function renderDocumentFileIcon(variant) {
  const icons = {
    generic: `
      <span class="file-icon file-icon--file file-icon--file-generic" aria-hidden="true">
        <svg viewBox="0 0 24 24" focusable="false">
          <path d="M6.8 2.8h7.1l5.3 5.3v13.1H6.8c-1.1 0-2-.9-2-2V4.8c0-1.1.9-2 2-2Z" />
          <path d="M13.8 2.8v5.3c0 .6.5 1.1 1.1 1.1h5.3L13.8 2.8Z" />
        </svg>
      </span>
    `,
    text: `
      <span class="file-icon file-icon--file file-icon--file-text" aria-hidden="true">
        <svg viewBox="0 0 24 24" focusable="false">
          <path fill="#6b7280" d="M6.8 2.8h7.1l5.3 5.3v13.1H6.8c-1.1 0-2-.9-2-2V4.8c0-1.1.9-2 2-2Z" />
          <path fill="rgba(255,255,255,0.42)" d="M13.8 2.8v5.3c0 .6.5 1.1 1.1 1.1h5.3L13.8 2.8Z" />
          <rect x="8.2" y="11" width="7.4" height="1.3" rx=".65" fill="#ffffff" />
          <rect x="8.2" y="13.6" width="7.4" height="1.3" rx=".65" fill="#ffffff" />
          <rect x="8.2" y="16.2" width="6.1" height="1.3" rx=".65" fill="#ffffff" />
        </svg>
      </span>
    `,
    code: `
      <span class="file-icon file-icon--file file-icon--file-code" aria-hidden="true">
        <svg viewBox="0 0 24 24" focusable="false">
          <path fill="#4f8df8" d="M6.8 2.8h7.1l5.3 5.3v13.1H6.8c-1.1 0-2-.9-2-2V4.8c0-1.1.9-2 2-2Z" />
          <path fill="#dbeafe" d="M13.8 2.8v5.3c0 .6.5 1.1 1.1 1.1h5.3L13.8 2.8Z" />
          <path d="m10.2 11.3-2.2 2.2 2.2 2.2" fill="none" stroke="#ffffff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
          <path d="m13.8 11.3 2.2 2.2-2.2 2.2" fill="none" stroke="#ffffff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </span>
    `,
    pdf: `
      <span class="file-icon file-icon--file file-icon--file-pdf" aria-hidden="true">
        <svg viewBox="0 0 24 24" focusable="false">
          <path fill="#f97316" d="M6.8 2.8h7.1l5.3 5.3v13.1H6.8c-1.1 0-2-.9-2-2V4.8c0-1.1.9-2 2-2Z" />
          <path fill="#fdba74" d="M13.8 2.8v5.3c0 .6.5 1.1 1.1 1.1h5.3L13.8 2.8Z" />
          <text x="12" y="17.2" text-anchor="middle" fill="#ffffff" font-size="4.5" font-weight="700" font-family="Segoe UI, Arial, sans-serif">PDF</text>
        </svg>
      </span>
    `,
    word: `
      <span class="file-icon file-icon--file file-icon--file-word" aria-hidden="true">
        <svg viewBox="0 0 24 24" focusable="false">
          <path fill="#4f8df8" d="M6.8 2.8h7.1l5.3 5.3v13.1H6.8c-1.1 0-2-.9-2-2V4.8c0-1.1.9-2 2-2Z" />
          <path fill="#dbeafe" d="M13.8 2.8v5.3c0 .6.5 1.1 1.1 1.1h5.3L13.8 2.8Z" />
          <text x="12" y="17.4" text-anchor="middle" fill="#ffffff" font-size="8.2" font-weight="700" font-family="Segoe UI, Arial, sans-serif">W</text>
        </svg>
      </span>
    `,
  };
  return icons[variant] || icons.generic;
}

function renderFileIcon(item) {
  if (item.is_directory) {
    return `
      <span class="file-icon file-icon--folder" aria-hidden="true">
        <svg viewBox="0 0 24 24" focusable="false">
          <path d="M2.8 7.2c0-1.3 1-2.3 2.3-2.3h5.3c.7 0 1.3.3 1.7.8l1.1 1.3h5.7c1.4 0 2.5 1.1 2.5 2.5v7.9c0 1.4-1.1 2.5-2.5 2.5H5.1c-1.3 0-2.3-1-2.3-2.3V7.2Z" />
        </svg>
      </span>
    `;
  }
  if (isFileImage(item)) {
    return `
      <span class="file-icon file-icon--image" aria-hidden="true">
        <img src="${escapeHtml(buildFilePreviewUrl(item.path))}" alt="" loading="lazy" />
      </span>
    `;
  }
  return renderDocumentFileIcon(getFileIconVariant(item));
}

function renderFileActionIcon(iconName) {
  const icons = {
    rename: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4.6 7.1h6.8" />
        <path d="M4.6 12h5.2" />
        <path d="M4.6 16.9h4.2" />
        <path d="M13.4 17.6 18.7 12.3a1.7 1.7 0 0 0-2.4-2.4l-5.3 5.3-.7 3.1 3.1-.7Z" />
      </svg>
    `,
    move: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="m12 3.8 3.2 3.2-3.2 3.2" />
        <path d="M12 3.8 8.8 7 12 10.2" />
        <path d="m12 13.8 3.2 3.2-3.2 3.2" />
        <path d="M12 13.8 8.8 17 12 20.2" />
        <path d="M3.8 12 7 8.8l3.2 3.2" />
        <path d="M3.8 12 7 15.2l3.2-3.2" />
        <path d="M13.8 12 17 8.8l3.2 3.2" />
        <path d="M13.8 12 17 15.2l3.2-3.2" />
      </svg>
    `,
    copy: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <rect x="8.2" y="8.2" width="10.2" height="10.2" rx="1.7" />
        <path d="M6 14.6H5.7c-1 0-1.7-.8-1.7-1.7V5.7c0-1 .8-1.7 1.7-1.7h7.2c1 0 1.7.8 1.7 1.7V6" />
      </svg>
    `,
    download: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 4.6v10.1" />
        <path d="m7.4 10 4.6 4.6 4.6-4.6" />
        <path d="M5.2 15.7v2.6c0 1 .8 1.9 1.9 1.9h9.8c1 0 1.9-.8 1.9-1.9v-2.6" />
      </svg>
    `,
    trash: `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4.8 7h14.4" />
        <path d="M9.4 7V4.8h5.2V7" />
        <path d="M7 7.2 7.7 20h8.6l.7-12.8" />
        <path d="M10 10.7v5.5" />
        <path d="M14 10.7v5.5" />
      </svg>
    `,
  };
  return icons[iconName] || '';
}

function getFileTypeLabel(item) {
  if (item.is_directory) {
    return '目录';
  }
  if (isFileImage(item)) {
    return '图片文件';
  }
  if (getFileExtension(item) === '.pdf') {
    if (item.is_protected || item.can_edit === false) {
      return item.requires_password ? 'PDF文件 · 需鉴权 · 只读' : 'PDF文件 · 只读';
    }
    return item.requires_password ? 'PDF文件 · 需鉴权' : 'PDF文件';
  }
  if (item.preview_type === 'binary') {
    return '文件 · 当前不可预览';
  }
  if (item.is_protected || item.can_edit === false) {
    return item.requires_password ? '文本文件 · 需鉴权 · 只读' : '文本文件 · 只读';
  }
  return item.requires_password ? '文本文件 · 需鉴权' : '文本文件';
}

function getSelectedFileItems() {
  return state.files.items.filter((item) => state.files.selectedPaths.has(normalizeFilePath(item.path)));
}

function getSelectedFilePaths() {
  return getSelectedFileItems().map((item) => normalizeFilePath(item.path));
}

function pruneFileSelection() {
  const visiblePaths = new Set(state.files.items.map((item) => normalizeFilePath(item.path)));
  for (const selectedPath of Array.from(state.files.selectedPaths)) {
    if (!visiblePaths.has(selectedPath)) {
      state.files.selectedPaths.delete(selectedPath);
    }
  }
}

function renderFileSelectionState() {
  const selectedCount = state.files.selectedPaths.size;
  const visibleCount = state.files.items.length;
  const hasSelection = selectedCount > 0;
  const fileBusy = state.files.loading || state.files.uploading || state.files.moving || state.files.downloading;

  elements.fileDeleteSelectedButton?.classList.toggle('hidden', !hasSelection);
  elements.fileMoveSelectedButton?.classList.toggle('hidden', !hasSelection);
  elements.fileDownloadSelectedButton?.classList.toggle('hidden', !hasSelection);
  if (elements.fileDeleteSelectedCount) {
    elements.fileDeleteSelectedCount.textContent = `(${selectedCount})`;
  }
  if (elements.fileMoveSelectedCount) {
    elements.fileMoveSelectedCount.textContent = `(${selectedCount})`;
  }
  if (elements.fileDownloadSelectedCount) {
    elements.fileDownloadSelectedCount.textContent = `(${selectedCount})`;
  }
  if (elements.fileDeleteSelectedButton) {
    elements.fileDeleteSelectedButton.disabled = fileBusy || !hasSelection;
  }
  if (elements.fileMoveSelectedButton) {
    elements.fileMoveSelectedButton.disabled = fileBusy || !hasSelection;
  }
  if (elements.fileDownloadSelectedButton) {
    elements.fileDownloadSelectedButton.disabled = fileBusy || !hasSelection;
  }
  if (elements.fileSelectAllInput) {
    elements.fileSelectAllInput.checked = visibleCount > 0 && selectedCount === visibleCount;
    elements.fileSelectAllInput.indeterminate = selectedCount > 0 && selectedCount < visibleCount;
    elements.fileSelectAllInput.disabled = fileBusy || visibleCount === 0;
  }
}

function renderFileBreadcrumb(pathValue) {
  if (!elements.fileBreadcrumb) {
    return;
  }
  const normalized = normalizeFilePath(pathValue);
  const parts = normalized ? normalized.split('/') : [];
  const crumbs = [
    '<button class="file-breadcrumb-item" type="button" data-file-path="">根目录</button>',
  ];
  let current = '';
  for (const part of parts) {
    current = current ? `${current}/${part}` : part;
    crumbs.push(
      `<button class="file-breadcrumb-item" type="button" data-file-path="${escapeHtml(current)}">${escapeHtml(part)}</button>`
    );
  }
  elements.fileBreadcrumb.innerHTML = crumbs.join('<span class="file-breadcrumb-separator">/</span>');
}

function renderFileManager(payload = {}) {
  state.files.path = normalizeFilePath(payload.path || '');
  state.files.parentPath = normalizeFilePath(payload.parent_path || '');
  state.files.canGoUp = Boolean(payload.can_go_up);
  state.files.rootPath = String(payload.root_path || '');
  state.files.items = Array.isArray(payload.items) ? payload.items : [];
  state.files.loaded = true;
  pruneFileSelection();

  elements.fileCurrentPath.textContent = formatFilePath(state.files.path);
  elements.fileRootPath.textContent = state.files.rootPath
    ? `边界: ${state.files.rootPath}`
    : '边界: RocketCatShell 根目录';
  elements.fileItemCount.textContent = String(state.files.items.length);
  elements.fileSensitiveCount.textContent = String(
    state.files.items.filter((item) => item.requires_password).length
  );
  const fileBusy = state.files.loading || state.files.uploading || state.files.moving || state.files.downloading;
  elements.fileUpButton.disabled = !state.files.canGoUp || fileBusy;
  elements.fileCreateButton.disabled = fileBusy;
  elements.fileRefreshButton.disabled = fileBusy;
  elements.fileUploadButton.disabled = fileBusy;
  elements.fileUploadButton.classList.toggle('active', state.files.uploadVisible);
  elements.fileUploadZone?.classList.toggle('hidden', !state.files.uploadVisible);
  if (elements.fileUploadStatus) {
    elements.fileUploadStatus.textContent = state.files.uploading
      ? '正在上传文件...'
      : '单次最多上传 20 个文件，单文件不超过 100 MiB。';
  }
  elements.fileStatus.textContent = state.files.loading
    ? '正在读取目录...'
    : '浏览并管理 RocketCatShell 根目录内文件。';
  renderFileBreadcrumb(state.files.path);
  renderFileSelectionState();

  if (!elements.fileTableBody) {
    return;
  }
  if (state.files.loading) {
    elements.fileEmptyState.classList.add('hidden');
    elements.fileTableBody.innerHTML = '<tr><td colspan="6" class="file-table-message">正在读取目录...</td></tr>';
    return;
  }
  elements.fileEmptyState.classList.toggle('hidden', state.files.items.length > 0);
  if (!state.files.items.length) {
    elements.fileTableBody.innerHTML = '';
    return;
  }

  elements.fileTableBody.innerHTML = state.files.items.map((item) => {
    const normalizedPath = normalizeFilePath(item.path);
    const selected = state.files.selectedPaths.has(normalizedPath);
    return `
    <tr class="${selected ? 'file-row-selected' : ''}">
      <td class="file-select-cell">
        <input class="file-checkbox" type="checkbox" aria-label="选择 ${escapeHtml(item.name || item.path || '-')}" data-file-action="select" data-file-path="${escapeHtml(item.path)}" ${selected ? 'checked' : ''} />
      </td>
      <td>
        <button class="file-name-button" type="button" data-file-action="open" data-file-path="${escapeHtml(item.path)}">
          ${renderFileIcon(item)}
          <span class="file-name-text">${escapeHtml(item.name || item.path || '-')}</span>
          ${item.requires_password ? '<span class="file-lock-badge">需鉴权</span>' : ''}
        </button>
      </td>
      <td>${escapeHtml(getFileTypeLabel(item))}</td>
      <td>${escapeHtml(formatFileSize(item.size, item.is_directory))}</td>
      <td>${escapeHtml(formatFileTime(item.mtime))}</td>
      <td class="file-actions-cell">
        <div class="file-row-actions" aria-label="文件操作">
          <button class="file-row-action-button" type="button" data-file-action="rename" data-file-path="${escapeHtml(item.path)}" aria-label="重命名" title="重命名">${renderFileActionIcon('rename')}</button>
          <button class="file-row-action-button" type="button" data-file-action="move" data-file-path="${escapeHtml(item.path)}" aria-label="移动" title="移动">${renderFileActionIcon('move')}</button>
          <button class="file-row-action-button" type="button" data-file-action="copy" data-file-path="${escapeHtml(item.path)}" aria-label="复制相对路径" title="复制相对路径">${renderFileActionIcon('copy')}</button>
          <button class="file-row-action-button" type="button" data-file-action="download" data-file-path="${escapeHtml(item.path)}" aria-label="下载" title="下载">${renderFileActionIcon('download')}</button>
          <button class="file-row-action-button danger" type="button" data-file-action="delete" data-file-path="${escapeHtml(item.path)}" aria-label="删除" title="删除">${renderFileActionIcon('trash')}</button>
        </div>
      </td>
    </tr>
  `;
  }).join('');
  renderFileSelectionState();
}

async function loadFiles({ path = state.files.path, forceReload = false, silent = false } = {}) {
  if (state.files.loading && !forceReload) {
    return;
  }
  state.files.loading = true;
  renderFileManager({
    path: state.files.path,
    parent_path: state.files.parentPath,
    can_go_up: state.files.canGoUp,
    root_path: state.files.rootPath,
    items: state.files.items,
  });
  try {
    const query = new URLSearchParams({ path: normalizeFilePath(path) });
    const payload = await requestJson(`/api/files?${query.toString()}`);
    state.files.loading = false;
    state.files.selectedPaths.clear();
    renderFileManager(payload);
  } catch (error) {
    state.files.loading = false;
    renderFileManager({
      path: state.files.path,
      parent_path: state.files.parentPath,
      can_go_up: state.files.canGoUp,
      root_path: state.files.rootPath,
      items: [],
    });
    if (!silent) {
      showToast(error.message || '文件列表加载失败', 'error');
    }
  }
}

function setFileCreateType(type) {
  state.files.createType = type === 'directory' ? 'directory' : 'file';
  for (const button of elements.fileCreateTypeButtons || []) {
    button.classList.toggle('active', button.dataset.fileCreateType === state.files.createType);
  }
}

function openFileCreateModal(type = 'file') {
  setFileCreateType(type);
  if (elements.fileCreateNameInput) {
    elements.fileCreateNameInput.value = '';
  }
  elements.fileCreateModal?.classList.remove('hidden');
  window.setTimeout(() => elements.fileCreateNameInput?.focus(), 0);
}

function closeFileCreateModal() {
  elements.fileCreateModal?.classList.add('hidden');
  if (elements.fileCreateNameInput) {
    elements.fileCreateNameInput.value = '';
  }
}

async function createFileManagerItem() {
  const name = validateRelativeFileName(elements.fileCreateNameInput?.value || '');
  const targetPath = joinFilePath(state.files.path, name);
  await requestJson('/api/files/create', {
    method: 'POST',
    body: JSON.stringify({
      path: targetPath,
      type: state.files.createType,
    }),
  });
  closeFileCreateModal();
  await loadFiles({ forceReload: true });
  showToast(state.files.createType === 'directory' ? '目录已创建' : '文件已创建', 'success');
}

function setFileUploadVisible(visible) {
  state.files.uploadVisible = Boolean(visible);
  renderFileManager({
    path: state.files.path,
    parent_path: state.files.parentPath,
    can_go_up: state.files.canGoUp,
    root_path: state.files.rootPath,
    items: state.files.items,
  });
}

function setFileUploadDragActive(active) {
  elements.fileUploadZone?.classList.toggle('drag-active', Boolean(active));
}

async function uploadFileManagerFiles(fileList) {
  const selectedFiles = Array.from(fileList || []);
  if (!selectedFiles.length) {
    return;
  }

  const formData = new FormData();
  for (const file of selectedFiles) {
    const fileName = file.webkitRelativePath || file.name;
    formData.append('files', file, fileName);
  }

  state.files.uploading = true;
  renderFileManager({
    path: state.files.path,
    parent_path: state.files.parentPath,
    can_go_up: state.files.canGoUp,
    root_path: state.files.rootPath,
    items: state.files.items,
  });
  try {
    const query = new URLSearchParams({ path: state.files.path });
    const payload = await requestJson(`/api/files/upload?${query.toString()}`, {
      method: 'POST',
      body: formData,
    });
    await loadFiles({ forceReload: true });
    showToast(`已上传 ${payload.uploaded || selectedFiles.length} 个文件`, 'success');
  } finally {
    state.files.uploading = false;
    if (elements.fileUploadInput) {
      elements.fileUploadInput.value = '';
    }
    renderFileManager({
      path: state.files.path,
      parent_path: state.files.parentPath,
      can_go_up: state.files.canGoUp,
      root_path: state.files.rootPath,
      items: state.files.items,
    });
  }
}

function setFileSelection(pathValue, selected) {
  const normalized = normalizeFilePath(pathValue);
  if (!normalized) {
    return;
  }
  if (selected) {
    state.files.selectedPaths.add(normalized);
  } else {
    state.files.selectedPaths.delete(normalized);
  }
  renderFileManager({
    path: state.files.path,
    parent_path: state.files.parentPath,
    can_go_up: state.files.canGoUp,
    root_path: state.files.rootPath,
    items: state.files.items,
  });
}

function setAllFileSelection(selected) {
  state.files.selectedPaths.clear();
  if (selected) {
    for (const item of state.files.items) {
      state.files.selectedPaths.add(normalizeFilePath(item.path));
    }
  }
  renderFileManager({
    path: state.files.path,
    parent_path: state.files.parentPath,
    can_go_up: state.files.canGoUp,
    root_path: state.files.rootPath,
    items: state.files.items,
  });
}

function openFileDeleteModal() {
  state.files.pendingDeletePaths = null;
  const selectedCount = getSelectedFilePaths().length;
  if (!selectedCount) {
    return;
  }
  if (elements.fileDeleteTitle) {
    elements.fileDeleteTitle.textContent = '批量删除';
  }
  elements.fileDeleteMessage.textContent = `确定要删除选中的 ${selectedCount} 个项目吗？`;
  elements.fileDeleteModal?.classList.remove('hidden');
}

function openSingleFileDeleteModal(item) {
  if (!item) {
    return;
  }
  state.files.pendingDeletePaths = [normalizeFilePath(item.path)];
  if (elements.fileDeleteTitle) {
    elements.fileDeleteTitle.textContent = '删除文件';
  }
  elements.fileDeleteMessage.textContent = `确定要删除「${item.name || item.path}」吗？`;
  elements.fileDeleteModal?.classList.remove('hidden');
}

function closeFileDeleteModal() {
  state.files.pendingDeletePaths = null;
  elements.fileDeleteModal?.classList.add('hidden');
}

async function deleteSelectedFileItems() {
  const selectedPaths = state.files.pendingDeletePaths || getSelectedFilePaths();
  if (!selectedPaths.length) {
    return;
  }
  await requestJson('/api/files/delete', {
    method: 'POST',
    body: JSON.stringify({ paths: selectedPaths }),
  });
  closeFileDeleteModal();
  state.files.selectedPaths.clear();
  await loadFiles({ forceReload: true });
  showToast(`已删除 ${selectedPaths.length} 个项目`, 'success');
}

function resetMoveTreeState() {
  state.files.moveTree.directories = new Map();
  state.files.moveTree.expanded = new Set(['']);
  state.files.moveTree.loading = new Set();
}

async function loadMoveDirectories(pathValue = '') {
  const normalized = normalizeFilePath(pathValue);
  if (state.files.moveTree.directories.has(normalized)) {
    return;
  }
  state.files.moveTree.loading.add(normalized);
  renderMoveTree();
  try {
    const query = new URLSearchParams({ path: normalized });
    const payload = await requestJson(`/api/files?${query.toString()}`);
    const directories = (payload.items || [])
      .filter((item) => item.is_directory)
      .map((item) => ({
        name: item.name,
        path: normalizeFilePath(item.path),
      }));
    state.files.moveTree.directories.set(normalized, directories);
  } finally {
    state.files.moveTree.loading.delete(normalized);
    renderMoveTree();
  }
}

function renderMoveTreeNode(pathValue = '', depth = 0) {
  const normalized = normalizeFilePath(pathValue);
  const expanded = state.files.moveTree.expanded.has(normalized);
  const loading = state.files.moveTree.loading.has(normalized);
  const selected = normalizeFilePath(state.files.moveTargetPath) === normalized;
  const children = state.files.moveTree.directories.get(normalized) || [];
  const label = normalized ? normalized.split('/').pop() : '/';
  const rows = [`
    <div class="file-move-tree-row ${selected ? 'selected' : ''}" style="--depth: ${depth}">
      <button class="file-move-node" type="button" data-file-move-path="${escapeHtml(normalized)}">
        <span class="file-move-node-toggle">${expanded ? '-' : '+'}</span>
        <span class="file-move-node-label">${escapeHtml(label || '/')}</span>
      </button>
    </div>
  `];
  if (expanded) {
    if (loading) {
      rows.push(`<div class="file-move-tree-loading" style="--depth: ${depth + 1}">正在读取目录...</div>`);
    } else {
      for (const child of children) {
        rows.push(renderMoveTreeNode(child.path, depth + 1));
      }
      if (!children.length) {
        rows.push(`<div class="file-move-tree-empty" style="--depth: ${depth + 1}">空目录</div>`);
      }
    }
  }
  return rows.join('');
}

function renderMoveTree() {
  if (!elements.fileMoveTree) {
    return;
  }
  const movingPaths = state.files.pendingMovePaths || getSelectedFilePaths();
  elements.fileMoveTree.innerHTML = renderMoveTreeNode('', 0);
  elements.fileMoveSelectedPath.textContent = formatFilePath(state.files.moveTargetPath);
  elements.fileMoveSelectionInfo.textContent = `移动项：${movingPaths.length} 个项目`;
  elements.fileMoveConfirmButton.disabled = state.files.moving || !movingPaths.length;
}

async function openFileMoveModal() {
  if (!state.files.selectedPaths.size) {
    return;
  }
  state.files.pendingMovePaths = null;
  await openFileMoveModalForPaths(getSelectedFilePaths());
}

async function openSingleFileMoveModal(item) {
  if (!item) {
    return;
  }
  await openFileMoveModalForPaths([normalizeFilePath(item.path)]);
}

async function openFileMoveModalForPaths(paths) {
  const normalizedPaths = Array.from(new Set((paths || []).map((pathValue) => normalizeFilePath(pathValue)).filter(Boolean)));
  if (!normalizedPaths.length) {
    return;
  }
  state.files.pendingMovePaths = normalizedPaths;
  state.files.moveTargetPath = '';
  resetMoveTreeState();
  elements.fileMoveModal?.classList.remove('hidden');
  renderMoveTree();
  try {
    await loadMoveDirectories('');
    if (state.files.path) {
      const parts = state.files.path.split('/');
      let current = '';
      for (const part of parts) {
        current = current ? `${current}/${part}` : part;
        state.files.moveTree.expanded.add(current);
        await loadMoveDirectories(current);
      }
    }
  } catch (error) {
    showToast(error.message || '目录树加载失败', 'error');
  }
}

function closeFileMoveModal() {
  elements.fileMoveModal?.classList.add('hidden');
  state.files.moveTargetPath = '';
  state.files.pendingMovePaths = null;
  resetMoveTreeState();
}

async function selectMoveTarget(pathValue) {
  const normalized = normalizeFilePath(pathValue);
  state.files.moveTargetPath = normalized;
  if (state.files.moveTree.expanded.has(normalized)) {
    state.files.moveTree.expanded.delete(normalized);
  } else {
    state.files.moveTree.expanded.add(normalized);
    try {
      await loadMoveDirectories(normalized);
    } catch (error) {
      showToast(error.message || '目录读取失败', 'error');
    }
  }
  renderMoveTree();
}

async function moveSelectedFileItems() {
  const selectedPaths = state.files.pendingMovePaths || getSelectedFilePaths();
  if (!selectedPaths.length) {
    return;
  }
  state.files.moving = true;
  renderMoveTree();
  renderFileSelectionState();
  try {
    await requestJson('/api/files/move', {
      method: 'POST',
      body: JSON.stringify({
        paths: selectedPaths,
        target_path: state.files.moveTargetPath,
      }),
    });
    closeFileMoveModal();
    state.files.selectedPaths.clear();
    await loadFiles({ forceReload: true });
    showToast(`已移动 ${selectedPaths.length} 个项目`, 'success');
  } finally {
    state.files.moving = false;
    renderMoveTree();
    renderFileSelectionState();
  }
}

function openFileRenameModal(item) {
  if (!item) {
    return;
  }
  state.files.pendingRenameItem = item;
  elements.fileRenameNameInput.value = item.name || '';
  elements.fileRenameModal?.classList.remove('hidden');
  window.setTimeout(() => elements.fileRenameNameInput?.focus(), 0);
}

function closeFileRenameModal() {
  state.files.pendingRenameItem = null;
  elements.fileRenameModal?.classList.add('hidden');
  if (elements.fileRenameNameInput) {
    elements.fileRenameNameInput.value = '';
  }
}

async function renameFileManagerItem() {
  const item = state.files.pendingRenameItem;
  if (!item) {
    return;
  }
  const name = validateFileBaseName(elements.fileRenameNameInput?.value || '');
  await requestJson('/api/files/rename', {
    method: 'POST',
    body: JSON.stringify({
      path: item.path,
      name,
    }),
  });
  closeFileRenameModal();
  state.files.selectedPaths.delete(normalizeFilePath(item.path));
  await loadFiles({ forceReload: true });
  showToast('重命名成功', 'success');
}

async function copyFileRelativePath(item) {
  if (!item) {
    return;
  }
  const relativePath = normalizeFilePath(item.path);
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(relativePath);
  } else {
    const helper = document.createElement('textarea');
    helper.value = relativePath;
    helper.setAttribute('readonly', 'readonly');
    helper.style.position = 'fixed';
    helper.style.opacity = '0';
    document.body.appendChild(helper);
    helper.select();
    document.execCommand('copy');
    document.body.removeChild(helper);
  }
  showToast('相对路径已复制', 'success');
}

async function downloadSingleFileItem(item) {
  if (!item) {
    return;
  }
  state.files.downloading = true;
  renderFileSelectionState();
  try {
    const query = new URLSearchParams({ path: normalizeFilePath(item.path) });
    const blob = await requestBlob(`/api/files/download?${query.toString()}`, {
      method: 'GET',
    });
    const objectUrl = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = item.is_directory ? `${item.name}.zip` : item.name;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.setTimeout(() => window.URL.revokeObjectURL(objectUrl), 0);
    showToast('下载已开始', 'success');
  } finally {
    state.files.downloading = false;
    renderFileSelectionState();
  }
}

async function downloadSelectedFileItems() {
  const selectedPaths = getSelectedFilePaths();
  if (!selectedPaths.length) {
    return;
  }
  state.files.downloading = true;
  renderFileSelectionState();
  try {
    const blob = await requestBlob('/api/files/download', {
      method: 'POST',
      body: JSON.stringify({ paths: selectedPaths }),
    });
    const objectUrl = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = 'files.zip';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.setTimeout(() => window.URL.revokeObjectURL(objectUrl), 0);
    showToast(`已打包 ${selectedPaths.length} 个项目`, 'success');
  } finally {
    state.files.downloading = false;
    renderFileSelectionState();
  }
}

function findFileItem(pathValue) {
  const normalized = normalizeFilePath(pathValue);
  return state.files.items.find((item) => normalizeFilePath(item.path) === normalized) || null;
}

function buildFileImageViewerItems() {
  return state.files.items
    .filter((item) => !item.is_directory && isFileImage(item))
    .map((item) => ({
      path: normalizeFilePath(item.path),
      name: item.name || item.path || '图片预览',
      url: buildFilePreviewUrl(item.path),
    }));
}

function renderFileImageViewer() {
  if (!state.files.imageViewer.visible) {
    return;
  }
  const items = state.files.imageViewer.items;
  if (!items.length) {
    closeFileImageViewer();
    return;
  }
  const index = Math.max(0, Math.min(state.files.imageViewer.index, items.length - 1));
  state.files.imageViewer.index = index;
  const current = items[index];
  if (elements.fileImageViewerImage) {
    elements.fileImageViewerImage.src = current.url;
    elements.fileImageViewerImage.alt = current.name;
  }
  if (elements.fileImageViewerCount) {
    elements.fileImageViewerCount.textContent = `${index + 1} / ${items.length}`;
  }
  const showNav = items.length > 1;
  elements.fileImageViewerPrevButton?.classList.toggle('hidden', !showNav);
  elements.fileImageViewerNextButton?.classList.toggle('hidden', !showNav);
}

function openFileImageViewer(item) {
  const items = buildFileImageViewerItems();
  if (!items.length) {
    return;
  }
  const targetPath = normalizeFilePath(item?.path || '');
  const index = Math.max(0, items.findIndex((entry) => entry.path === targetPath));
  state.files.imageViewer.items = items;
  state.files.imageViewer.index = index;
  state.files.imageViewer.visible = true;
  document.body.classList.add('file-image-viewer-open');
  renderFileImageViewer();
  elements.fileImageViewer?.classList.remove('hidden');
}

function moveFileImageViewer(step) {
  const items = state.files.imageViewer.items;
  if (!state.files.imageViewer.visible || items.length <= 1) {
    return;
  }
  const total = items.length;
  state.files.imageViewer.index = (state.files.imageViewer.index + step + total) % total;
  renderFileImageViewer();
}

function closeFileImageViewer() {
  state.files.imageViewer.visible = false;
  state.files.imageViewer.items = [];
  state.files.imageViewer.index = 0;
  elements.fileImageViewer?.classList.add('hidden');
  document.body.classList.remove('file-image-viewer-open');
  if (elements.fileImageViewerImage) {
    elements.fileImageViewerImage.removeAttribute('src');
  }
}

async function openFileItem(item) {
  if (!item) {
    return;
  }
  if (item.is_directory) {
    await loadFiles({ path: item.path, forceReload: true });
    return;
  }
  if (isFileImage(item)) {
    openFileImageViewer(item);
    return;
  }
  if (item.preview_type !== 'text') {
    openFilePreviewModal({
      path: item.path,
      name: item.name,
      size: item.size,
      mtime: item.mtime,
      content: '',
      truncated: false,
      unavailable: true,
    });
    return;
  }
  if (item.requires_password) {
    openFileAuthModal(item, 'edit');
    return;
  }
  await openTextFileForEdit(item);
}

async function readFileContent(item, password = '') {
  return requestJson('/api/files/read', {
    method: 'POST',
    body: JSON.stringify({
      path: item.path,
      password,
    }),
    skipAuthRedirect: Boolean(item.requires_password),
  });
}

async function readFileForPreview(item, password = '') {
  const payload = await readFileContent(item, password);
  openFilePreviewModal(payload);
}

async function openTextFileForEdit(item, password = '') {
  const payload = await readFileContent(item, password);
  if (!payload.can_edit) {
    openFilePreviewModal(payload);
    return;
  }
  openFileEditModal(payload, password);
}

function openFilePreviewModal(payload) {
  state.files.previewItem = payload;
  const fileName = payload.name || payload.path || '文件预览';
  elements.filePreviewTitle.textContent = fileName;
  elements.filePreviewMeta.innerHTML = `
    <span>${escapeHtml(formatFilePath(payload.path || ''))}</span>
    <span>${escapeHtml(formatFileSize(payload.size, false))}</span>
    <span>${escapeHtml(formatFileTime(payload.mtime))}</span>
  `;
  if (payload.unavailable) {
    elements.filePreviewNotice.textContent = '当前阶段仅支持文本文件预览。';
    elements.filePreviewNotice.classList.remove('hidden');
    elements.filePreviewContent.textContent = '';
    elements.filePreviewContent.classList.add('hidden');
  } else {
    let notice = '';
    if (payload.is_protected) {
      notice = '该文件属于 RocketCatShell 核心源码或内置插件源码，只允许查看，不能修改。';
    } else if (payload.truncated) {
      notice = '文件较大，仅显示前 1 MiB 内容，暂不允许在线编辑。';
    } else if (payload.can_edit === false) {
      notice = '该文件当前只允许查看，不能在线编辑。';
    }
    elements.filePreviewNotice.textContent = notice;
    elements.filePreviewNotice.classList.toggle('hidden', !notice);
    elements.filePreviewContent.textContent = payload.content || '';
    elements.filePreviewContent.classList.remove('hidden');
  }
  elements.filePreviewModal.classList.remove('hidden');
}

function closeFilePreviewModal() {
  state.files.previewItem = null;
  elements.filePreviewModal?.classList.add('hidden');
  if (elements.filePreviewContent) {
    elements.filePreviewContent.textContent = '';
  }
}

function updateFileEditLineNumbers() {
  if (!elements.fileEditLineNumbers || !elements.fileEditContentInput) {
    return;
  }
  const lineCount = Math.max(1, elements.fileEditContentInput.value.split('\n').length);
  elements.fileEditLineNumbers.textContent = Array.from({ length: lineCount }, (_, index) => index + 1).join('\n');
}

function openFileEditModal(payload, password = '') {
  state.files.editingFile = {
    path: normalizeFilePath(payload.path),
    name: payload.name || payload.path || '文件',
    content: payload.content || '',
    originalContent: payload.content || '',
    password,
    requiresPassword: Boolean(payload.requires_password),
    isProtected: Boolean(payload.is_protected),
  };
  if (elements.fileEditPathChip) {
    elements.fileEditPathChip.textContent = formatFilePath(payload.path || '');
  }
  if (elements.fileEditContentInput) {
    elements.fileEditContentInput.value = state.files.editingFile.content;
    updateFileEditLineNumbers();
    elements.fileEditContentInput.scrollTop = 0;
  }
  if (elements.fileEditNotice) {
    const notice = payload.requires_password
      ? '该文件需要鉴权，保存前会要求二次确认。'
      : '';
    elements.fileEditNotice.textContent = notice;
    elements.fileEditNotice.classList.toggle('hidden', !notice);
  }
  elements.fileEditModal?.classList.remove('hidden');
  window.setTimeout(() => elements.fileEditContentInput?.focus(), 0);
}

function closeFileEditModal() {
  state.files.editingFile = null;
  elements.fileEditModal?.classList.add('hidden');
  if (elements.fileEditContentInput) {
    elements.fileEditContentInput.value = '';
  }
  if (elements.fileEditLineNumbers) {
    elements.fileEditLineNumbers.textContent = '1';
  }
}

function openFileSaveConfirmModal() {
  const editingFile = state.files.editingFile;
  if (!editingFile) {
    return;
  }
  const nextContent = elements.fileEditContentInput?.value || '';
  if (nextContent === editingFile.originalContent) {
    showToast('文件内容没有变化');
    return;
  }
  if (elements.fileSaveConfirmTitle) {
    elements.fileSaveConfirmTitle.textContent = editingFile.requiresPassword ? '保存鉴权文件' : '保存文件';
  }
  if (elements.fileSaveConfirmMessage) {
    elements.fileSaveConfirmMessage.textContent = editingFile.requiresPassword
      ? `修改鉴权文件可能导致出错，确定要保存「${formatFilePath(editingFile.path)}」吗？`
      : `确定要保存「${formatFilePath(editingFile.path)}」的修改吗？`;
  }
  elements.fileSaveConfirmModal?.classList.remove('hidden');
}

function closeFileSaveConfirmModal() {
  state.files.pendingSave = false;
  elements.fileSaveConfirmModal?.classList.add('hidden');
}

async function saveFileEditContent() {
  const editingFile = state.files.editingFile;
  if (!editingFile || state.files.pendingSave) {
    return;
  }
  state.files.pendingSave = true;
  if (elements.fileSaveConfirmSubmitButton) {
    elements.fileSaveConfirmSubmitButton.disabled = true;
  }
  try {
    await requestJson('/api/files/write', {
      method: 'POST',
      body: JSON.stringify({
        path: editingFile.path,
        content: elements.fileEditContentInput?.value || '',
        password: editingFile.password || '',
      }),
      skipAuthRedirect: editingFile.requiresPassword,
    });
    closeFileSaveConfirmModal();
    closeFileEditModal();
    await loadFiles({ forceReload: true, silent: true });
    showToast('保存成功', 'success');
  } finally {
    state.files.pendingSave = false;
    if (elements.fileSaveConfirmSubmitButton) {
      elements.fileSaveConfirmSubmitButton.disabled = false;
    }
  }
}

function openFileAuthModal(item, mode = 'edit') {
  state.files.pendingAuthItem = item;
  state.files.pendingAuthMode = mode;
  elements.fileAuthMessage.textContent = `文件 ${formatFilePath(item.path)} 包含敏感持久化数据，请输入 WebUI 登录认证 / 文件管理鉴权密码。`;
  elements.fileAuthPasswordInput.value = '';
  elements.fileAuthModal.classList.remove('hidden');
  window.setTimeout(() => elements.fileAuthPasswordInput?.focus(), 0);
}

function closeFileAuthModal() {
  state.files.pendingAuthItem = null;
  state.files.pendingAuthMode = 'edit';
  elements.fileAuthModal?.classList.add('hidden');
  if (elements.fileAuthPasswordInput) {
    elements.fileAuthPasswordInput.value = '';
  }
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
  if (elements.openUserMappingsButton) {
    const mappingReady = Boolean(bot?.user_mapping_ready);
    elements.openUserMappingsButton.disabled = !bot || !mappingReady;
    elements.openUserMappingsButton.dataset.botId = bot?.id || '';
    elements.userMappingsButtonHint.textContent = !bot
      ? '保存并首次成功登录后即可审查映射。'
      : mappingReady
        ? `当前 bot self_id：${bot.onebot_self_id}`
        : '尚未建立映射，请先让该 bot 成功登录 Rocket.Chat。';
  }
  elements.modal.classList.remove('hidden');
}

function closeModal() {
  state.editingId = null;
  elements.modal.classList.add('hidden');
}

async function openUserMappings(botId) {
  if (!botId) {
    return;
  }
  state.userMappings = {
    botId,
    items: [],
    total: 0,
    offset: 0,
    limit: 50,
    search: '',
    ready: false,
  };
  elements.userMappingsSearchInput.value = '';
  elements.userMappingsModal.classList.remove('hidden');
  await loadUserMappings();
}

function closeUserMappings() {
  elements.userMappingsModal.classList.add('hidden');
}

async function loadUserMappings() {
  const mappingState = state.userMappings;
  const query = new URLSearchParams({
    search: mappingState.search,
    offset: String(mappingState.offset),
    limit: String(mappingState.limit),
  });
  const payload = await requestJson(
    `/api/bots/${encodeURIComponent(mappingState.botId)}/user-mappings?${query.toString()}`,
  );
  mappingState.items = payload.items || [];
  mappingState.total = Number(payload.total || 0);
  mappingState.offset = Number(payload.offset || 0);
  mappingState.limit = Number(payload.limit || 50);
  mappingState.ready = Boolean(payload.ready);
  elements.userMappingsModalTitle.textContent = `User 映射：${payload.bot_name || mappingState.botId}`;
  renderUserMappings(payload);
}

function renderUserMappings(payload) {
  const mappingState = state.userMappings;
  const items = mappingState.items;
  elements.userMappingsTableBody.innerHTML = '';
  elements.userMappingsEmpty.classList.toggle('hidden', items.length > 0);
  elements.userMappingsNotice.classList.toggle('hidden', payload.ready !== false);
  elements.userMappingsNotice.textContent = payload.ready === false
    ? '该 bot 尚未成功登录 Rocket.Chat，因此还没有可审查的映射。'
    : '';
  elements.userMappingsSummary.textContent = payload.ready === false
    ? '映射未建立'
    : `共 ${mappingState.total} 条 · ${payload.algorithm || 'sha256-linear-v1'}`;

  for (const item of items) {
    const row = document.createElement('tr');
    if (item.conflict_role === 'incumbent') {
      row.classList.add('identity-conflict-incumbent');
    } else if (item.conflict_role === 'displaced') {
      row.classList.add('identity-conflict-displaced');
    }
    const badges = [];
    if (item.is_bot) badges.push('<span class="identity-badge bot">BOT</span>');
    if (item.manual_override) badges.push('<span class="identity-badge override">自定义</span>');
    if (item.synthetic) badges.push('<span class="identity-badge synthetic">测试</span>');
    if (item.conflict_role === 'incumbent') badges.push('<span class="identity-badge incumbent">先入槽位</span>');
    if (item.conflict_role === 'displaced') badges.push('<span class="identity-badge displaced">后入偏移</span>');
    row.innerHTML = `
      <td><code>${escapeHtml(item.user_id)}</code></td>
      <td>${escapeHtml(item.username || '-')}</td>
      <td>${escapeHtml(item.nickname || '-')}</td>
      <td>
        <input class="identity-onebot-input" type="text" inputmode="numeric"
          value="${escapeHtml(String(item.onebot_id))}"
          data-identity-user-id="${escapeHtml(item.user_id)}"
          data-identity-revision="${escapeHtml(String(item.revision))}" />
      </td>
      <td>
        <code>${escapeHtml(String(item.primary_onebot_id))}</code>
        <small>偏移 ${escapeHtml(String(item.probe_offset))}</small>
      </td>
      <td><div class="identity-badges">${badges.join('') || '<span class="identity-badge normal">正常</span>'}</div></td>
      <td>
        <div class="identity-action-stack">
          <button class="action-button subtle identity-save-button" type="button"
            data-identity-save="${escapeHtml(item.user_id)}">保存</button>
          <button class="action-button danger-button identity-delete-button" type="button"
            data-identity-delete="${escapeHtml(item.user_id)}"
            data-identity-label="${escapeHtml(item.nickname || item.username || item.user_id)}">删除</button>
        </div>
      </td>
    `;
    elements.userMappingsTableBody.appendChild(row);
  }

  const pageCount = Math.max(1, Math.ceil(mappingState.total / mappingState.limit));
  const currentPage = Math.min(pageCount, Math.floor(mappingState.offset / mappingState.limit) + 1);
  elements.userMappingsPageLabel.textContent = `${currentPage} / ${pageCount}`;
  elements.userMappingsPrevButton.disabled = mappingState.offset <= 0;
  elements.userMappingsNextButton.disabled =
    mappingState.offset + mappingState.limit >= mappingState.total;
}

function getUserMappingInput(userId) {
  return Array.from(
    elements.userMappingsTableBody.querySelectorAll('[data-identity-user-id]'),
  ).find((item) => item.dataset.identityUserId === userId);
}

async function saveUserMapping(userId) {
  const input = getUserMappingInput(userId);
  if (!input) {
    return;
  }
  const onebotId = String(input.value || '').trim();
  if (!/^\d{11}$/.test(onebotId)) {
    throw new Error('OneBot ID 必须是 11 位数字');
  }
  const revision = Number(input.dataset.identityRevision || 0);
  const result = await requestJson(
    `/api/bots/${encodeURIComponent(state.userMappings.botId)}/user-mappings/${encodeURIComponent(userId)}`,
    {
      method: 'PUT',
      body: JSON.stringify({
        onebot_id: onebotId,
        revision,
      }),
    },
  );
  if ((result.restart_errors || []).length > 0) {
    showToast('映射已保存，但部分 bot 自动重启失败，请查看猫猫日志。', 'error');
  } else {
    showToast('用户 OneBot ID 已保存，相关运行中 bot 已安全重启。', 'success');
  }
  await loadData();
  await loadUserMappings();
}

async function deleteUserMapping(userId, label = '') {
  const input = getUserMappingInput(userId);
  if (!input) {
    return;
  }
  const revision = Number(input.dataset.identityRevision || 0);
  const displayName = String(label || userId || '').trim();
  const confirmed = window.confirm(
    `确认删除映射「${displayName}」吗？\n\n这会删除该用户在当前 server 范围内的共享映射，并重启相关 bot，方便后续重新建映射测试。`,
  );
  if (!confirmed) {
    return;
  }
  if (state.userMappings.items.length === 1 && state.userMappings.offset > 0) {
    state.userMappings.offset = Math.max(0, state.userMappings.offset - state.userMappings.limit);
  }
  const result = await requestJson(
    `/api/bots/${encodeURIComponent(state.userMappings.botId)}/user-mappings/${encodeURIComponent(userId)}`,
    {
      method: 'DELETE',
      body: JSON.stringify({
        revision,
      }),
    },
  );
  if ((result.restart_errors || []).length > 0) {
    showToast('映射已删除，但部分 bot 自动重启失败，请查看猫猫日志。', 'error');
  } else {
    showToast('用户映射已删除，相关运行中 bot 已安全重启。', 'success');
  }
  await loadData();
  await loadUserMappings();
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

async function loadDiagnostics({ forceReload = false, silent = false } = {}) {
  if (!forceReload && state.diagnostics.loaded) {
    return;
  }

  try {
    const diagnostics = await requestJson('/api/diagnostics');
    renderDiagnostics(diagnostics);
  } catch (error) {
    state.diagnostics.loaded = false;
    if (!silent) {
      showToast(error.message || '诊断数据加载失败', 'error');
    }
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
  if (!Number.isFinite(payload.room_info_cache_ttl_seconds) || payload.room_info_cache_ttl_seconds < 0) {
    throw new Error('房间信息缓存 TTL 必须是大于等于 0 的数字');
  }
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
    throw new Error('请设置 WebUI 登录认证 / 文件管理鉴权密码');
  }

  const payload = await requestJson('/api/settings', {
    method: 'PUT',
    body: JSON.stringify({ webui_access_password: password }),
  });
  state.settings.loaded = true;
  renderSettings(payload);
  showToast('WebUI 登录认证 / 文件管理鉴权密码已更新', 'success');
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
    throw new Error('请输入最大消息映射窗口条数');
  }

  const maxEntries = Number(rawValue);
  if (!Number.isInteger(maxEntries) || maxEntries <= 0) {
    throw new Error('最大消息映射窗口条数必须是正整数');
  }

  const payload = await requestJson('/api/settings', {
    method: 'PUT',
    body: JSON.stringify({ message_index_max_entries: maxEntries }),
  });
  state.settings.loaded = true;
  renderSettings(payload);
  showToast('消息映射窗口条数上限已保存，现有映射窗口已按新规则整理', 'success');
}

function summarizeMessageIndexResult(result) {
  const botCount = Number(result?.bot_count) || 0;
  const changedBotCount = Number(result?.changed_bot_count) || 0;
  const removedCount = Number(result?.removed_message_mapping_count) || 0;
  if (botCount <= 0) {
    return '当前没有可处理的 Bot 消息映射窗口';
  }
  return `已处理 ${botCount} 个 Bot，整理 ${changedBotCount} 个映射窗口，清理 ${removedCount} 条旧映射`;
}

async function rebuildMessageIndexes() {
  const confirmed = window.confirm('确认按当前窗口条数上限手动整理所有 Bot 的消息映射窗口吗？');
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
  const fileName = 'rocketcat_config.json';
  const handle = typeof window.showSaveFilePicker === 'function'
    ? await window.showSaveFilePicker(buildJsonSavePickerOptions(fileName))
    : null;
  const payload = await requestJson('/api/settings/export-config');
  const text = `${JSON.stringify(payload, null, 2)}\n`;
  await writeTextWithPicker(fileName, text, handle);
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
elements.diagnosticsRefreshButton?.addEventListener('click', async () => {
  await activatePage('diagnostics', { forceReload: true });
  showToast('运行诊断已刷新');
});
elements.settingsRefreshButton?.addEventListener('click', async () => {
  await activatePage('settings', { forceReload: true });
  showToast('设置项已刷新');
});
elements.pluginsRefreshButton?.addEventListener('click', async () => {
  await activatePage('plugins', { forceReload: true });
  showToast('插件列表已刷新');
});
elements.fileRefreshButton?.addEventListener('click', async () => {
  await loadFiles({ forceReload: true });
  showToast('文件列表已刷新');
});
elements.fileUpButton?.addEventListener('click', async () => {
  if (!state.files.canGoUp) {
    return;
  }
  await loadFiles({ path: state.files.parentPath, forceReload: true });
});
elements.fileCreateButton?.addEventListener('click', () => {
  openFileCreateModal('file');
});
elements.fileUploadButton?.addEventListener('click', () => {
  setFileUploadVisible(!state.files.uploadVisible);
});
elements.fileDeleteSelectedButton?.addEventListener('click', openFileDeleteModal);
elements.fileMoveSelectedButton?.addEventListener('click', async () => {
  await openFileMoveModal();
});
elements.fileDownloadSelectedButton?.addEventListener('click', async () => {
  try {
    await downloadSelectedFileItems();
  } catch (error) {
    showToast(error.message || '下载失败', 'error');
  }
});
elements.terminalCreateButton?.addEventListener('click', async () => {
  try {
    await createTerminal();
  } catch (error) {
    showToast(error.message || '创建终端失败', 'error');
  }
});
elements.terminalTabs?.addEventListener('click', async (event) => {
  const closeButton = event.target.closest('[data-terminal-close]');
  if (closeButton) {
    event.preventDefault();
    event.stopPropagation();
    try {
      await closeTerminal(closeButton.dataset.terminalClose || '');
    } catch (error) {
      showToast(error.message || '关闭终端失败', 'error');
    }
    return;
  }

  const tab = event.target.closest('[data-terminal-id]');
  if (!tab) {
    return;
  }
  state.terminal.activeId = tab.dataset.terminalId || '';
  renderTerminals();
});
elements.terminalTabs?.addEventListener('dragstart', (event) => {
  const tab = event.target.closest('[data-terminal-id]');
  if (!tab) {
    return;
  }
  state.terminal.dragId = tab.dataset.terminalId || '';
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', state.terminal.dragId);
  tab.classList.add('dragging');
});
elements.terminalTabs?.addEventListener('dragover', (event) => {
  if (!state.terminal.dragId) {
    return;
  }
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
});
elements.terminalTabs?.addEventListener('drop', (event) => {
  event.preventDefault();
  const tab = event.target.closest('[data-terminal-id]');
  if (!tab) {
    return;
  }
  reorderTerminalTabs(state.terminal.dragId, tab.dataset.terminalId || '');
});
elements.terminalTabs?.addEventListener('dragend', (event) => {
  event.target.closest('[data-terminal-id]')?.classList.remove('dragging');
  state.terminal.dragId = '';
});
elements.fileSelectAllInput?.addEventListener('change', (event) => {
  setAllFileSelection(event.target.checked);
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
elements.openUserMappingsButton?.addEventListener('click', async () => {
  try {
    await openUserMappings(elements.openUserMappingsButton.dataset.botId || '');
  } catch (error) {
    showToast(error.message || '用户映射加载失败', 'error');
  }
});
elements.userMappingsCloseButton?.addEventListener('click', closeUserMappings);
elements.userMappingsDoneButton?.addEventListener('click', closeUserMappings);
elements.userMappingsSearchButton?.addEventListener('click', async () => {
  state.userMappings.search = String(elements.userMappingsSearchInput?.value || '').trim();
  state.userMappings.offset = 0;
  try {
    await loadUserMappings();
  } catch (error) {
    showToast(error.message || '用户映射搜索失败', 'error');
  }
});
elements.userMappingsSearchInput?.addEventListener('keydown', async (event) => {
  if (event.key !== 'Enter') {
    return;
  }
  event.preventDefault();
  elements.userMappingsSearchButton?.click();
});
elements.userMappingsRefreshButton?.addEventListener('click', async () => {
  const button = elements.userMappingsRefreshButton;
  button.disabled = true;
  try {
    await loadUserMappings();
    showToast('用户映射列表已刷新', 'success');
  } catch (error) {
    showToast(error.message || '用户映射刷新失败', 'error');
  } finally {
    button.disabled = false;
  }
});
elements.userMappingsPrevButton?.addEventListener('click', async () => {
  state.userMappings.offset = Math.max(
    0,
    state.userMappings.offset - state.userMappings.limit,
  );
  try {
    await loadUserMappings();
  } catch (error) {
    showToast(error.message || '用户映射翻页失败', 'error');
  }
});
elements.userMappingsNextButton?.addEventListener('click', async () => {
  state.userMappings.offset += state.userMappings.limit;
  try {
    await loadUserMappings();
  } catch (error) {
    state.userMappings.offset = Math.max(
      0,
      state.userMappings.offset - state.userMappings.limit,
    );
    showToast(error.message || '用户映射翻页失败', 'error');
  }
});
elements.userMappingsTableBody?.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-identity-save], [data-identity-delete]');
  if (!button) {
    return;
  }
  button.disabled = true;
  try {
    if (button.dataset.identityDelete) {
      await deleteUserMapping(
        button.dataset.identityDelete || '',
        button.dataset.identityLabel || '',
      );
    } else {
      await saveUserMapping(button.dataset.identitySave || '');
    }
  } catch (error) {
    showToast(
      error.message || (button.dataset.identityDelete ? '用户映射删除失败' : '用户映射保存失败'),
      'error',
    );
  } finally {
    button.disabled = false;
  }
});
elements.filePreviewCloseButton?.addEventListener('click', closeFilePreviewModal);
elements.filePreviewCancelButton?.addEventListener('click', closeFilePreviewModal);
elements.fileImageViewerCloseButton?.addEventListener('click', closeFileImageViewer);
elements.fileImageViewerPrevButton?.addEventListener('click', () => {
  moveFileImageViewer(-1);
});
elements.fileImageViewerNextButton?.addEventListener('click', () => {
  moveFileImageViewer(1);
});
elements.fileCreateCloseButton?.addEventListener('click', closeFileCreateModal);
elements.fileCreateCancelButton?.addEventListener('click', closeFileCreateModal);
elements.fileCreateSubmitButton?.addEventListener('click', async () => {
  try {
    await createFileManagerItem();
  } catch (error) {
    showToast(error.message || '新建失败', 'error');
    elements.fileCreateNameInput?.focus();
  }
});
elements.fileCreateNameInput?.addEventListener('keydown', async (event) => {
  if (event.key !== 'Enter') {
    return;
  }
  event.preventDefault();
  elements.fileCreateSubmitButton?.click();
});
for (const button of elements.fileCreateTypeButtons || []) {
  button.addEventListener('click', () => {
    setFileCreateType(button.dataset.fileCreateType);
  });
}
elements.fileDeleteCloseButton?.addEventListener('click', closeFileDeleteModal);
elements.fileDeleteCancelButton?.addEventListener('click', closeFileDeleteModal);
elements.fileDeleteConfirmButton?.addEventListener('click', async () => {
  try {
    await deleteSelectedFileItems();
  } catch (error) {
    showToast(error.message || '删除失败', 'error');
  }
});
elements.fileMoveCloseButton?.addEventListener('click', closeFileMoveModal);
elements.fileMoveCancelButton?.addEventListener('click', closeFileMoveModal);
elements.fileMoveConfirmButton?.addEventListener('click', async () => {
  try {
    await moveSelectedFileItems();
  } catch (error) {
    showToast(error.message || '移动失败', 'error');
  }
});
elements.fileMoveTree?.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-file-move-path]');
  if (!button) {
    return;
  }
  await selectMoveTarget(button.dataset.fileMovePath || '');
});
elements.fileRenameCloseButton?.addEventListener('click', closeFileRenameModal);
elements.fileRenameCancelButton?.addEventListener('click', closeFileRenameModal);
elements.fileRenameSubmitButton?.addEventListener('click', async () => {
  try {
    await renameFileManagerItem();
  } catch (error) {
    showToast(error.message || '重命名失败', 'error');
    elements.fileRenameNameInput?.focus();
  }
});
elements.fileRenameNameInput?.addEventListener('keydown', async (event) => {
  if (event.key !== 'Enter') {
    return;
  }
  event.preventDefault();
  elements.fileRenameSubmitButton?.click();
});
elements.fileUploadPickButton?.addEventListener('click', () => {
  elements.fileUploadInput?.click();
});
elements.fileUploadInput?.addEventListener('change', async (event) => {
  try {
    await uploadFileManagerFiles(event.target.files);
  } catch (error) {
    showToast(error.message || '上传失败', 'error');
  }
});
elements.fileUploadZone?.addEventListener('dragenter', (event) => {
  event.preventDefault();
  setFileUploadDragActive(true);
});
elements.fileUploadZone?.addEventListener('dragover', (event) => {
  event.preventDefault();
  setFileUploadDragActive(true);
});
elements.fileUploadZone?.addEventListener('dragleave', (event) => {
  event.preventDefault();
  if (!elements.fileUploadZone?.contains(event.relatedTarget)) {
    setFileUploadDragActive(false);
  }
});
elements.fileUploadZone?.addEventListener('drop', async (event) => {
  event.preventDefault();
  setFileUploadDragActive(false);
  try {
    await uploadFileManagerFiles(event.dataTransfer?.files);
  } catch (error) {
    showToast(error.message || '上传失败', 'error');
  }
});
elements.fileAuthCloseButton?.addEventListener('click', closeFileAuthModal);
elements.fileAuthCancelButton?.addEventListener('click', closeFileAuthModal);
elements.fileAuthSubmitButton?.addEventListener('click', async () => {
  const item = state.files.pendingAuthItem;
  if (!item) {
    return;
  }
  try {
    const password = String(elements.fileAuthPasswordInput?.value || '');
    if (state.files.pendingAuthMode === 'preview') {
      await readFileForPreview(item, password);
    } else {
      await openTextFileForEdit(item, password);
    }
    closeFileAuthModal();
  } catch (error) {
    showToast(error.message || '文件管理鉴权失败', 'error');
    elements.fileAuthPasswordInput?.focus();
  }
});
elements.fileAuthPasswordInput?.addEventListener('keydown', async (event) => {
  if (event.key !== 'Enter') {
    return;
  }
  event.preventDefault();
  elements.fileAuthSubmitButton?.click();
});
elements.fileEditCloseButton?.addEventListener('click', closeFileEditModal);
elements.fileEditCancelButton?.addEventListener('click', closeFileEditModal);
elements.fileEditSaveButton?.addEventListener('click', openFileSaveConfirmModal);
elements.fileEditContentInput?.addEventListener('input', updateFileEditLineNumbers);
elements.fileEditContentInput?.addEventListener('scroll', () => {
  if (elements.fileEditLineNumbers && elements.fileEditContentInput) {
    elements.fileEditLineNumbers.scrollTop = elements.fileEditContentInput.scrollTop;
  }
});
elements.fileEditContentInput?.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault();
    openFileSaveConfirmModal();
  }
});
elements.fileSaveConfirmCloseButton?.addEventListener('click', closeFileSaveConfirmModal);
elements.fileSaveConfirmCancelButton?.addEventListener('click', closeFileSaveConfirmModal);
elements.fileSaveConfirmSubmitButton?.addEventListener('click', async () => {
  try {
    await saveFileEditContent();
  } catch (error) {
    showToast(error.message || '保存失败', 'error');
  }
});
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
    showToast(error.message || '手动整理消息映射窗口失败', 'error');
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
elements.userMappingsModal?.addEventListener('click', (event) => {
  if (event.target === elements.userMappingsModal) {
    closeUserMappings();
  }
});
elements.filePreviewModal?.addEventListener('click', (event) => {
  if (event.target === elements.filePreviewModal) {
    closeFilePreviewModal();
  }
});
elements.fileImageViewer?.addEventListener('click', (event) => {
  if (event.target === elements.fileImageViewer) {
    closeFileImageViewer();
  }
});
elements.fileCreateModal?.addEventListener('click', (event) => {
  if (event.target === elements.fileCreateModal) {
    closeFileCreateModal();
  }
});
elements.fileDeleteModal?.addEventListener('click', (event) => {
  if (event.target === elements.fileDeleteModal) {
    closeFileDeleteModal();
  }
});
elements.fileMoveModal?.addEventListener('click', (event) => {
  if (event.target === elements.fileMoveModal) {
    closeFileMoveModal();
  }
});
elements.fileRenameModal?.addEventListener('click', (event) => {
  if (event.target === elements.fileRenameModal) {
    closeFileRenameModal();
  }
});
elements.fileAuthModal?.addEventListener('click', (event) => {
  if (event.target === elements.fileAuthModal) {
    closeFileAuthModal();
  }
});

elements.fileBreadcrumb?.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-file-path]');
  if (!button) {
    return;
  }
  try {
    await loadFiles({ path: button.dataset.filePath || '', forceReload: true });
  } catch (error) {
    showToast(error.message || '目录切换失败', 'error');
  }
});

elements.fileTableBody?.addEventListener('click', async (event) => {
  const checkbox = event.target.closest('[data-file-action="select"]');
  if (checkbox) {
    setFileSelection(checkbox.dataset.filePath || '', checkbox.checked);
    return;
  }
  const actionButton = event.target.closest('.file-row-action-button[data-file-action]');
  if (actionButton) {
    const item = findFileItem(actionButton.dataset.filePath || '');
    try {
      if (actionButton.dataset.fileAction === 'rename') {
        openFileRenameModal(item);
        return;
      }
      if (actionButton.dataset.fileAction === 'move') {
        await openSingleFileMoveModal(item);
        return;
      }
      if (actionButton.dataset.fileAction === 'copy') {
        await copyFileRelativePath(item);
        return;
      }
      if (actionButton.dataset.fileAction === 'download') {
        await downloadSingleFileItem(item);
        return;
      }
      if (actionButton.dataset.fileAction === 'delete') {
        openSingleFileDeleteModal(item);
      }
    } catch (error) {
      showToast(error.message || '文件操作失败', 'error');
    }
    return;
  }
  const button = event.target.closest('[data-file-action="open"]');
  if (!button) {
    return;
  }
  try {
    await openFileItem(findFileItem(button.dataset.filePath || ''));
  } catch (error) {
    showToast(error.message || '文件打开失败', 'error');
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

elements.logPerfButton?.addEventListener('click', () => {
  state.logs.showPerf = !state.logs.showPerf;
  renderLogs();
});

window.addEventListener('keydown', (event) => {
  if (state.files.imageViewer.visible) {
    if (event.key === 'Escape') {
      closeFileImageViewer();
      return;
    }
    if (event.key === 'ArrowLeft') {
      moveFileImageViewer(-1);
      return;
    }
    if (event.key === 'ArrowRight') {
      moveFileImageViewer(1);
      return;
    }
  }
  if (event.key === 'Escape') {
    closeModal();
    closeUserMappings();
    closePluginModal();
    closePluginUninstallModal();
    closeFileImageViewer();
    closeFilePreviewModal();
    closeFileCreateModal();
    closeFileDeleteModal();
    closeFileMoveModal();
    closeFileRenameModal();
    closeFileAuthModal();
    closeFileSaveConfirmModal();
    closeFileEditModal();
  }
});

window.addEventListener('resize', () => {
  if (state.currentPage === 'terminal' && state.terminal.activeId) {
    fitTerminal(state.terminal.activeId);
  }
});

setupSidebarToggleButtons();

Promise.all([
  loadData(),
  loadLogs({ reset: true }),
])
  .then(() => {
    setActivePage('network');
  })
  .catch((error) => {
    showToast(error.message || '加载失败', 'error');
  });
