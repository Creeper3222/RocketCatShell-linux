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
const UPDATE_TRANSACTION_STORAGE_KEY = 'rocketcat_update_transaction';
const UPDATE_OUTCOME_STORAGE_KEY = 'rocketcat_update_outcome';
const CORE_PAGE_IDS = new Set(['network', 'basic', 'diagnostics', 'logs', 'plugins', 'files', 'terminal', 'settings']);
const MOBILE_NAVIGATION_QUERY = window.matchMedia('(max-width: 1120px)');
const MOBILE_SHEET_QUERY = window.matchMedia('(max-width: 720px)');
const REDUCED_MOTION_QUERY = window.matchMedia('(prefers-reduced-motion: reduce)');
const DRAWER_SPRING = Object.freeze({ dampingRatio: 0.8, response: 0.3 });
const SETTLE_SPRING = Object.freeze({ dampingRatio: 1.0, response: 0.4 });
const MOTION_SETTLE_POSITION = 0.5;
const MOTION_SETTLE_VELOCITY = 5;
const GESTURE_VELOCITY_WINDOW_MS = 100;
const MOTION_DECELERATION_RATE = 0.99;
const CARD_ORDER_DRAG_THRESHOLD = 8;
const CARD_ORDER_AUTO_SCROLL_EDGE = 40;
const CARD_ORDER_AUTO_SCROLL_MAX = 12;
const CARD_ORDER_FLIP_DURATION = 180;
const CARD_ORDER_FLIP_EASING = 'cubic-bezier(0.77, 0, 0.175, 1)';
const CARD_ORDER_POINTER_BLOCK_SELECTOR = [
  'button',
  'input',
  'select',
  'textarea',
  'label',
  'a',
  '[contenteditable]:not([contenteditable="false"])',
  '[role="button"]',
  '[role="link"]',
  '[data-card-order-no-drag]',
].join(',');
const motionAnimations = new WeakMap();
const cardOrderMotionAnimations = new WeakMap();
let inputModality = 'programmatic';

function setInputModality(modality) {
  inputModality = modality;
  document.body.dataset.inputModality = modality;
}

function resolveMotion(motion = 'auto') {
  if (motion === 'instant' || REDUCED_MOTION_QUERY.matches) {
    return 'instant';
  }
  if (motion === 'standard') {
    return 'standard';
  }
  return inputModality === 'keyboard' ? 'instant' : 'standard';
}

function clampMotionValue(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function rubberband(overshoot, dimension, constant = 0.55) {
  const safeDimension = Math.max(1, dimension);
  return (overshoot * safeDimension * constant)
    / (safeDimension + constant * Math.abs(overshoot));
}

function projectGesture(initialVelocity, decelerationRate = MOTION_DECELERATION_RATE) {
  return (initialVelocity / 1000) * decelerationRate / (1 - decelerationRate);
}

function addVelocitySample(samples, position, timestamp = performance.now()) {
  samples.push({ position, timestamp });
  const cutoff = timestamp - GESTURE_VELOCITY_WINDOW_MS;
  while (samples.length > 2 && samples[0].timestamp < cutoff) {
    samples.shift();
  }
}

function getGestureVelocity(samples) {
  if (!samples || samples.length < 2) {
    return 0;
  }
  const first = samples[0];
  const last = samples[samples.length - 1];
  const elapsed = Math.max(1, last.timestamp - first.timestamp);
  return ((last.position - first.position) / elapsed) * 1000;
}

function cancelMotionAnimation(element) {
  const running = element ? motionAnimations.get(element) : null;
  if (!running) {
    return null;
  }
  window.cancelAnimationFrame(running.frame);
  motionAnimations.delete(element);
  return { value: running.value, velocity: running.velocity };
}

function getTransformTranslate(element, axis = 'x') {
  if (!element) {
    return 0;
  }
  const transform = window.getComputedStyle(element).transform;
  if (!transform || transform === 'none') {
    return 0;
  }
  try {
    const matrix = new DOMMatrixReadOnly(transform);
    return axis === 'y' ? matrix.m42 : matrix.m41;
  } catch (_error) {
    return 0;
  }
}

function springTo(element, {
  from,
  target,
  velocity,
  dampingRatio = 1,
  response = 0.4,
  apply,
  complete,
} = {}) {
  if (!element || typeof apply !== 'function') {
    complete?.();
    return null;
  }
  const interrupted = cancelMotionAnimation(element);
  let value = Number.isFinite(from) ? from : (interrupted?.value ?? target);
  let currentVelocity = Number.isFinite(velocity) ? velocity : (interrupted?.velocity ?? 0);
  if (REDUCED_MOTION_QUERY.matches) {
    apply(target);
    complete?.();
    return null;
  }

  const omega0 = (2 * Math.PI) / response;
  const stiffness = omega0 * omega0;
  const damping = 2 * dampingRatio * omega0;
  let previousTime = performance.now();
  const animation = { frame: 0, value, velocity: currentVelocity };
  motionAnimations.set(element, animation);
  apply(value);

  const step = (timestamp) => {
    if (motionAnimations.get(element) !== animation) {
      return;
    }
    const deltaSeconds = Math.min(1 / 30, Math.max(1 / 240, (timestamp - previousTime) / 1000));
    previousTime = timestamp;
    const acceleration = (-stiffness * (animation.value - target)) - (damping * animation.velocity);
    animation.velocity += acceleration * deltaSeconds;
    animation.value += animation.velocity * deltaSeconds;
    apply(animation.value);
    if (
      Math.abs(animation.value - target) < MOTION_SETTLE_POSITION
      && Math.abs(animation.velocity) < MOTION_SETTLE_VELOCITY
    ) {
      animation.value = target;
      animation.velocity = 0;
      apply(target);
      motionAnimations.delete(element);
      complete?.();
      return;
    }
    animation.frame = window.requestAnimationFrame(step);
  };
  animation.frame = window.requestAnimationFrame(step);
  return animation;
}

window.addEventListener('pointerdown', () => setInputModality('pointer'), { capture: true, passive: true });
window.addEventListener('keydown', () => setInputModality('keyboard'), { capture: true });
setInputModality('programmatic');

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
    mobileNavigationOpen: false,
    navigationGesture: null,
  },
  network: {
    pollTimer: null,
    abortController: null,
    renderSignature: '',
  },
  settings: {
    data: null,
    loaded: false,
  },
  updates: {
    status: null,
    releases: [],
    loaded: false,
    loading: false,
    pendingRelease: null,
    transactionId: getStoredUpdateTransaction(),
    pollTimer: null,
    overlayStage: '',
    overlayTransitionToken: 0,
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
    metersInitialized: false,
    pollTimer: null,
    abortController: null,
    renderSignature: '',
    performanceSignature: '',
  },
  cardOrder: {
    loaded: false,
    bots: [],
    plugins: [],
    pointer: null,
    keyboard: null,
    autoScrollFrame: 0,
    savingScopes: new Set(),
    deferred: {
      network: null,
      diagnostics: null,
      basic: null,
      plugins: null,
    },
  },
  logs: {
    items: [],
    lastId: 0,
    maxEntries: 2000,
    pollTimer: null,
    abortController: null,
    polling: false,
    generation: 0,
    autoScroll: true,
    unreadCount: 0,
    renderedIds: new Set(),
    showPerf: false,
    activeLevels: new Set(['DEBUG', 'INFO', 'WARN', 'ERROR']),
  },
  plugins: {
    items: [],
    loaded: false,
    current: null,
    pendingUninstall: null,
    listEditor: null,
  },
  pluginDashboard: {
    plugin: null,
    page: '',
    token: '',
    sessionUrl: '',
    sseControllers: new Map(),
    opening: false,
    ready: false,
    readyTimer: null,
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
      focusPath: '',
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
    pointerDrag: null,
    autoScrollFrame: 0,
    suppressClickUntil: 0,
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
  sidebar: document.getElementById('appSidebar'),
  mobileMenuButton: document.getElementById('mobileMenuButton'),
  navigationScrim: document.getElementById('navigationScrim'),
  navigationEdgeGesture: document.getElementById('navigationEdgeGesture'),
  sidebarDragHandle: document.getElementById('sidebarDragHandle'),
  mobilePageTitle: document.getElementById('mobilePageTitle'),
  mobileRuntimeStatus: document.getElementById('mobileRuntimeStatus'),
  sidebarRuntimeDot: document.getElementById('sidebarRuntimeDot'),
  sidebarRuntimeText: document.getElementById('sidebarRuntimeText'),
  sidebarVersion: document.getElementById('sidebarVersion'),
  logoutButton: document.getElementById('logoutButton'),
  mainContent: document.getElementById('mainContent'),
  botListSummary: document.getElementById('botListSummary'),
  navButtons: Array.from(document.querySelectorAll('[data-page]')),
  sidebarToggleButtons: [],
  networkPage: document.getElementById('networkPage'),
  diagnosticsPage: document.getElementById('diagnosticsPage'),
  basicPage: document.getElementById('basicPage'),
  logsPage: document.getElementById('logsPage'),
  settingsPage: document.getElementById('settingsPage'),
  pluginsPage: document.getElementById('pluginsPage'),
  pluginDashboardPage: document.getElementById('pluginDashboardPage'),
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
  settingsPasswordResult: document.getElementById('settingsPasswordResult'),
  settingsPortResult: document.getElementById('settingsPortResult'),
  settingsPerformanceResult: document.getElementById('settingsPerformanceResult'),
  settingsConfigResult: document.getElementById('settingsConfigResult'),
  updateCurrentVersion: document.getElementById('updateCurrentVersion'),
  updateLatestVersion: document.getElementById('updateLatestVersion'),
  updateCheckedAt: document.getElementById('updateCheckedAt'),
  updateStatusMessage: document.getElementById('updateStatusMessage'),
  updateAvailabilityBadge: document.getElementById('updateAvailabilityBadge'),
  updateCheckButton: document.getElementById('updateCheckButton'),
  updateSelectButton: document.getElementById('updateSelectButton'),
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
  performanceOverallBadge: document.getElementById('performanceOverallBadge'),
  performanceEventLoop: document.getElementById('performanceEventLoop'),
  performanceEventLoopStatus: document.getElementById('performanceEventLoopStatus'),
  performanceLoggingQueue: document.getElementById('performanceLoggingQueue'),
  performanceLoggingStatus: document.getElementById('performanceLoggingStatus'),
  performanceBotGrid: document.getElementById('performanceBotGrid'),
  diagnosticsGrid: document.getElementById('diagnosticsGrid'),
  diagnosticsEmptyState: document.getElementById('diagnosticsEmptyState'),
  banner: document.getElementById('statusBanner'),
  botGrid: document.getElementById('botGrid'),
  emptyState: document.getElementById('emptyState'),
  pluginGrid: document.getElementById('pluginGrid'),
  cardOrderInstructions: document.getElementById('cardOrderInstructions'),
  cardOrderLiveRegion: document.getElementById('cardOrderLiveRegion'),
  pluginEmptyState: document.getElementById('pluginEmptyState'),
  pluginDashboardBackButton: document.getElementById('pluginDashboardBackButton'),
  pluginDashboardCloseButton: document.getElementById('pluginDashboardCloseButton'),
  pluginDashboardRefreshButton: document.getElementById('pluginDashboardRefreshButton'),
  pluginDashboardPageSelect: document.getElementById('pluginDashboardPageSelect'),
  pluginDashboardTitle: document.getElementById('pluginDashboardTitle'),
  pluginDashboardLogo: document.getElementById('pluginDashboardLogo'),
  pluginDashboardFrame: document.getElementById('pluginDashboardFrame'),
  pluginDashboardFrameShell: document.getElementById('pluginDashboardFrameShell'),
  pluginDashboardLoading: document.getElementById('pluginDashboardLoading'),
  pluginDashboardError: document.getElementById('pluginDashboardError'),
  pluginDashboardRetryButton: document.getElementById('pluginDashboardRetryButton'),
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
  botFormStatus: document.getElementById('botFormStatus'),
  settingsForm: document.getElementById('settingsForm'),
  settingsPasswordHelper: document.getElementById('settingsPasswordHelper'),
  settingsWebuiPasswordInput: document.getElementById('settingsWebuiPasswordInput'),
  settingsWebuiPortInput: document.getElementById('settingsWebuiPortInput'),
  settingsMessageIndexMaxEntriesInput: document.getElementById('settingsMessageIndexMaxEntriesInput'),
  settingsPerformanceProfileInput: document.getElementById('settingsPerformanceProfileInput'),
  settingsInboundWorkerCountInput: document.getElementById('settingsInboundWorkerCountInput'),
  settingsOnebotQueueMaxInput: document.getElementById('settingsOnebotQueueMaxInput'),
  settingsIdentityCacheMaxInput: document.getElementById('settingsIdentityCacheMaxInput'),
  settingsMediaCacheMaxBytesInput: document.getElementById('settingsMediaCacheMaxBytesInput'),
  settingsMediaCacheMaxAgeInput: document.getElementById('settingsMediaCacheMaxAgeInput'),
  settingsLogFileMaxBytesInput: document.getElementById('settingsLogFileMaxBytesInput'),
  settingsLogFileBackupCountInput: document.getElementById('settingsLogFileBackupCountInput'),
  settingsTerminalMaxSessionsInput: document.getElementById('settingsTerminalMaxSessionsInput'),
  settingsTerminalIdleTimeoutInput: document.getElementById('settingsTerminalIdleTimeoutInput'),
  settingsPasswordSaveButton: document.getElementById('settingsPasswordSaveButton'),
  settingsPortSaveButton: document.getElementById('settingsPortSaveButton'),
  settingsMessageIndexRebuildButton: document.getElementById('settingsMessageIndexRebuildButton'),
  settingsPerformanceSaveButton: document.getElementById('settingsPerformanceSaveButton'),
  settingsExportConfigButton: document.getElementById('settingsExportConfigButton'),
  settingsImportConfigButton: document.getElementById('settingsImportConfigButton'),
  settingsImportFileInput: document.getElementById('settingsImportFileInput'),
  updateReleaseModal: document.getElementById('updateReleaseModal'),
  updateReleaseList: document.getElementById('updateReleaseList'),
  updateReleaseCloseButton: document.getElementById('updateReleaseCloseButton'),
  updateReleaseCancelButton: document.getElementById('updateReleaseCancelButton'),
  updateConfirmModal: document.getElementById('updateConfirmModal'),
  updateConfirmAction: document.getElementById('updateConfirmAction'),
  updateConfirmVersion: document.getElementById('updateConfirmVersion'),
  updateConfirmMessage: document.getElementById('updateConfirmMessage'),
  updateConfirmCloseButton: document.getElementById('updateConfirmCloseButton'),
  updateConfirmCancelButton: document.getElementById('updateConfirmCancelButton'),
  updateConfirmSubmitButton: document.getElementById('updateConfirmSubmitButton'),
  updateRestartOverlay: document.getElementById('updateRestartOverlay'),
  updateRestartSpinner: document.getElementById('updateRestartSpinner'),
  updateRestartTitle: document.getElementById('updateRestartTitle'),
  updateRestartMessage: document.getElementById('updateRestartMessage'),
  updateRestartProgress: document.getElementById('updateRestartProgress'),
  updateRestartTransaction: document.getElementById('updateRestartTransaction'),
  updateRestartRetryButton: document.getElementById('updateRestartRetryButton'),
  confirmModal: document.getElementById('confirmModal'),
  confirmModalKicker: document.getElementById('confirmModalKicker'),
  confirmModalTitle: document.getElementById('confirmModalTitle'),
  confirmModalMessage: document.getElementById('confirmModalMessage'),
  confirmModalCloseButton: document.getElementById('confirmModalCloseButton'),
  confirmModalCancelButton: document.getElementById('confirmModalCancelButton'),
  confirmModalSubmitButton: document.getElementById('confirmModalSubmitButton'),
  closeModalButton: document.getElementById('closeModalButton'),
  cancelButton: document.getElementById('cancelButton'),
  submitButton: document.getElementById('submitButton'),
  openUserMappingsButton: document.getElementById('openUserMappingsButton'),
  userMappingsButtonHint: document.getElementById('userMappingsButtonHint'),
  userMappingsModal: document.getElementById('userMappingsModal'),
  userMappingsModalTitle: document.getElementById('userMappingsModalTitle'),
  userMappingsBotName: document.getElementById('userMappingsBotName'),
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
  pluginFormStatus: document.getElementById('pluginFormStatus'),
  pluginCloseModalButton: document.getElementById('pluginCloseModalButton'),
  pluginCancelButton: document.getElementById('pluginCancelButton'),
  pluginSaveButton: document.getElementById('pluginSaveButton'),
  pluginListEditorModal: document.getElementById('pluginListEditorModal'),
  pluginListEditorTitle: document.getElementById('pluginListEditorTitle'),
  pluginListEditorDescription: document.getElementById('pluginListEditorDescription'),
  pluginListEditorForm: document.getElementById('pluginListEditorForm'),
  pluginListEditorInput: document.getElementById('pluginListEditorInput'),
  pluginListEditorStatus: document.getElementById('pluginListEditorStatus'),
  pluginListEditorItems: document.getElementById('pluginListEditorItems'),
  pluginListEditorEmpty: document.getElementById('pluginListEditorEmpty'),
  pluginListEditorCloseButton: document.getElementById('pluginListEditorCloseButton'),
  pluginListEditorCancelButton: document.getElementById('pluginListEditorCancelButton'),
  pluginListEditorConfirmButton: document.getElementById('pluginListEditorConfirmButton'),
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
  logBackToBottomButton: document.getElementById('logBackToBottomButton'),
  logUnreadCount: document.getElementById('logUnreadCount'),
  logFilterButtons: Array.from(document.querySelectorAll('[data-log-level]')),
  logPerfButton: document.querySelector('[data-log-perf]'),
};

const dialogTriggers = new WeakMap();
const dialogSnapshots = new WeakMap();
const dialogCloseTimers = new WeakMap();
const dialogSheetGestures = new WeakMap();
const dialogStack = [];
const toastRecords = new WeakMap();
const pendingToasts = [];
let confirmationResolver = null;
let toastQueueExitPending = false;

function getVisibleToasts() {
  return Array.from(elements.toast?.querySelectorAll('.toast-notification') || []);
}

function setToastPresentation(notification, position) {
  const record = toastRecords.get(notification);
  if (!record) {
    return;
  }
  record.position = position;
  const width = Math.max(1, record.width || notification.getBoundingClientRect().width);
  notification.style.transform = `translateX(${position}px)`;
  notification.style.opacity = String(clampMotionValue(1 - ((Math.abs(position) / width) * 0.46), 0.42, 1));
}

function animateToastLayout(previousPositions, motion = 'auto') {
  if (resolveMotion(motion) === 'instant') {
    return;
  }
  window.requestAnimationFrame(() => {
    for (const notification of getVisibleToasts()) {
      if (
        notification.dataset.swiping === 'true'
        || notification.dataset.closing === 'true'
        || motionAnimations.has(notification)
      ) {
        continue;
      }
      const previous = previousPositions.get(notification);
      if (!previous) {
        continue;
      }
      const current = notification.getBoundingClientRect();
      const deltaY = previous.top - current.top;
      if (Math.abs(deltaY) < 0.5) {
        continue;
      }
      notification.getAnimations().forEach((animation) => animation.cancel());
      notification.animate(
        [
          { transform: `translateY(${deltaY}px)` },
          { transform: 'translateY(0)' },
        ],
        { duration: 180, easing: 'cubic-bezier(0.77, 0, 0.175, 1)' },
      );
    }
  });
}

function removeToast(notification, motion = 'auto') {
  const previousPositions = new Map(
    getVisibleToasts().map((item) => [item, item.getBoundingClientRect()]),
  );
  cancelMotionAnimation(notification);
  notification.remove();
  toastRecords.delete(notification);
  toastQueueExitPending = false;
  flushToastQueue();
  animateToastLayout(previousPositions, motion);
}

function dismissToast(notification, {
  motion = 'auto',
  direction = 0,
  velocity = 0,
  gesture = false,
} = {}) {
  if (!notification || notification.dataset.closing === 'true') {
    return;
  }
  const record = toastRecords.get(notification);
  if (record?.timer) {
    window.clearTimeout(record.timer);
    record.timer = 0;
  }
  notification.dataset.closing = 'true';
  delete notification.dataset.swiping;
  if (gesture && resolveMotion(motion) === 'standard' && direction) {
    const width = Math.max(1, notification.getBoundingClientRect().width);
    if (record) {
      record.width = width;
    }
    const target = direction * (width + 40);
    springTo(notification, {
      from: record?.position ?? getTransformTranslate(notification),
      target,
      velocity,
      ...SETTLE_SPRING,
      apply: (value) => setToastPresentation(notification, value),
      complete: () => removeToast(notification, motion),
    });
    return;
  }
  notification.style.transform = '';
  notification.style.opacity = '';
  const duration = resolveMotion(motion) === 'instant' ? 0 : 120;
  window.setTimeout(() => removeToast(notification, motion), duration);
}

function scheduleToast(notification, duration = null) {
  const record = toastRecords.get(notification);
  if (!record || record.pauseReasons.size || notification.dataset.closing === 'true') {
    return;
  }
  record.remaining = duration ?? record.remaining;
  record.startedAt = performance.now();
  record.timer = window.setTimeout(() => dismissToast(notification, { motion: 'standard' }), record.remaining);
}

function pauseToast(notification, reason = 'manual') {
  const record = toastRecords.get(notification);
  if (!record) {
    return;
  }
  record.pauseReasons.add(reason);
  if (!record.timer) {
    return;
  }
  window.clearTimeout(record.timer);
  record.timer = 0;
  const elapsed = performance.now() - record.startedAt;
  record.remaining = Math.max(500, record.remaining - elapsed);
}

function resumeToast(notification, reason = 'manual') {
  const record = toastRecords.get(notification);
  if (!record || notification.dataset.closing === 'true') {
    return;
  }
  record.pauseReasons.delete(reason);
  if (!record.pauseReasons.size && !record.timer) {
    scheduleToast(notification);
  }
}

function finishToastPointerGesture(notification, event, cancelled = false) {
  const record = toastRecords.get(notification);
  const gesture = record?.gesture;
  if (!record || !gesture || gesture.pointerId !== event.pointerId) {
    return;
  }
  record.gesture = null;
  notification.releasePointerCapture?.(event.pointerId);
  if (!gesture.active) {
    resumeToast(notification, 'drag');
    return;
  }
  const velocity = getGestureVelocity(gesture.samples);
  const width = Math.max(1, record.width || notification.getBoundingClientRect().width);
  const shouldDismiss = !cancelled && (
    Math.abs(record.position) >= width * 0.35
    || Math.abs(velocity) >= 110
  );
  if (shouldDismiss) {
    const direction = Math.sign(Math.abs(velocity) >= 110 ? velocity : record.position) || 1;
    dismissToast(notification, { direction, velocity, gesture: true, motion: 'standard' });
    return;
  }
  delete notification.dataset.swiping;
  springTo(notification, {
    from: record.position,
    target: 0,
    velocity: cancelled ? 0 : velocity,
    ...SETTLE_SPRING,
    apply: (value) => setToastPresentation(notification, value),
    complete: () => {
      notification.style.transform = '';
      notification.style.opacity = '';
      record.position = 0;
      resumeToast(notification, 'drag');
    },
  });
}

function setupToastGesture(notification) {
  notification.addEventListener('pointerdown', (event) => {
    if (
      REDUCED_MOTION_QUERY.matches
      || event.button !== 0
      || event.target.closest('.toast-close')
    ) {
      return;
    }
    const record = toastRecords.get(notification);
    if (!record || record.gesture || notification.dataset.closing === 'true') {
      return;
    }
    const interrupted = cancelMotionAnimation(notification);
    record.width = Math.max(1, notification.getBoundingClientRect().width);
    record.position = interrupted?.value ?? getTransformTranslate(notification);
    record.gesture = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startPosition: record.position,
      active: false,
      samples: [],
    };
    addVelocitySample(record.gesture.samples, event.clientX, event.timeStamp);
    pauseToast(notification, 'drag');
    notification.setPointerCapture?.(event.pointerId);
  });
  notification.addEventListener('pointermove', (event) => {
    const record = toastRecords.get(notification);
    const gesture = record?.gesture;
    if (!gesture || gesture.pointerId !== event.pointerId) {
      return;
    }
    const deltaX = event.clientX - gesture.startX;
    const deltaY = event.clientY - gesture.startY;
    if (!gesture.active) {
      if (Math.hypot(deltaX, deltaY) < 8) {
        return;
      }
      if (Math.abs(deltaX) <= Math.abs(deltaY) * 1.2) {
        record.gesture = null;
        notification.releasePointerCapture?.(event.pointerId);
        resumeToast(notification, 'drag');
        return;
      }
      gesture.active = true;
      notification.dataset.swiping = 'true';
    }
    event.preventDefault();
    addVelocitySample(gesture.samples, event.clientX, event.timeStamp);
    setToastPresentation(notification, gesture.startPosition + deltaX);
  });
  notification.addEventListener('pointerup', (event) => finishToastPointerGesture(notification, event));
  notification.addEventListener('pointercancel', (event) => finishToastPointerGesture(notification, event, true));
  notification.addEventListener('lostpointercapture', (event) => finishToastPointerGesture(notification, event, true));
}

function mountToast({ message, tone }) {
  const notification = document.createElement('div');
  notification.className = `toast-notification ${tone}`;
  notification.setAttribute('role', tone === 'error' ? 'alert' : 'status');
  notification.setAttribute('aria-atomic', 'true');
  notification.tabIndex = 0;
  notification.innerHTML = `
    <span class="toast-icon" aria-hidden="true">${tone === 'error' ? '!' : tone === 'success' ? '✓' : 'i'}</span>
    <span class="toast-message">${escapeHtml(message)}</span>
    <button class="toast-close" type="button" aria-label="关闭通知">×</button>
  `;
  notification.querySelector('.toast-close')?.addEventListener('click', () => dismissToast(notification));
  notification.addEventListener('mouseenter', () => pauseToast(notification, 'hover'));
  notification.addEventListener('mouseleave', () => resumeToast(notification, 'hover'));
  notification.addEventListener('focusin', () => pauseToast(notification, 'focus'));
  notification.addEventListener('focusout', () => resumeToast(notification, 'focus'));
  toastRecords.set(notification, {
    remaining: tone === 'error' ? 8000 : 4000,
    startedAt: 0,
    timer: 0,
    pauseReasons: new Set(document.hidden ? ['hidden'] : []),
    position: 0,
    width: 0,
    gesture: null,
  });
  setupToastGesture(notification);
  elements.toast.appendChild(notification);
  scheduleToast(notification);
}

function flushToastQueue() {
  if (!elements.toast) {
    return;
  }
  while (pendingToasts.length && getVisibleToasts().length < 3) {
    mountToast(pendingToasts.shift());
  }
  if (pendingToasts.length && !toastQueueExitPending) {
    const visibleToasts = getVisibleToasts();
    if (visibleToasts.some((notification) => notification.dataset.closing === 'true')) {
      toastQueueExitPending = true;
      return;
    }
    const oldest = visibleToasts[0];
    if (oldest) {
      toastQueueExitPending = true;
      dismissToast(oldest, { motion: 'standard' });
    }
  }
}

function showToast(message, kind = 'default') {
  if (!elements.toast) {
    return;
  }
  const tone = kind === 'error' ? 'error' : kind === 'success' ? 'success' : 'info';
  pendingToasts.push({ message, tone });
  flushToastQueue();
}

document.addEventListener('visibilitychange', () => {
  for (const notification of getVisibleToasts()) {
    if (document.hidden) {
      pauseToast(notification, 'hidden');
    } else {
      resumeToast(notification, 'hidden');
    }
  }
  if (document.hidden) {
    stopNetworkPolling();
    stopDiagnosticsPolling();
    stopLogPolling();
    return;
  }
  if (state.currentPage === 'network') {
    loadData().catch((error) => {
      if (!isAbortError(error)) console.warn(error);
    }).finally(startNetworkPolling);
  } else if (state.currentPage === 'diagnostics') {
    loadDiagnostics({ forceReload: true, silent: true }).finally(startDiagnosticsPolling);
  } else if (state.currentPage === 'logs') {
    startLogPolling();
  }
});

function setFormResult(element, message = '', kind = 'default') {
  if (!element) {
    return;
  }
  element.textContent = message;
  element.className = `form-result ${kind}`.trim();
  element.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  if (message && kind === 'error') {
    element.tabIndex = -1;
    element.focus({ preventScroll: true });
  }
}

function controlSignature(dialog) {
  if (!dialog) {
    return '';
  }
  const controls = Array.from(dialog.querySelectorAll('input, select, textarea'));
  return JSON.stringify(controls.map((control) => ({
    id: control.id || control.name || control.type,
    value: control.type === 'checkbox' || control.type === 'radio' ? control.checked : control.value,
  })));
}

function markDialogPristine(dialog) {
  if (dialog?.hasAttribute('data-dirty-guard')) {
    dialogSnapshots.set(dialog, controlSignature(dialog));
  }
}

function isDialogDirty(dialog) {
  if (!dialog?.hasAttribute('data-dirty-guard')) {
    return false;
  }
  return dialogSnapshots.get(dialog) !== controlSignature(dialog);
}

function getTopDialog() {
  for (let index = dialogStack.length - 1; index >= 0; index -= 1) {
    if (dialogStack[index]?.open) {
      return dialogStack[index];
    }
  }
  return null;
}

function setDialogMotionFrame(dialogs, motion = 'auto') {
  const motionType = (
    motion === 'auto'
    && REDUCED_MOTION_QUERY.matches
    && inputModality !== 'keyboard'
  ) ? 'reduced' : resolveMotion(motion);
  for (const dialog of dialogs) {
    dialog.dataset.motion = motionType;
  }
  if (motionType === 'instant') {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        for (const dialog of dialogs) {
          if (dialog.dataset.motion === 'instant') {
            dialog.dataset.motion = 'standard';
          }
        }
      });
    });
  }
  return motionType;
}

function refreshDialogBodyState({ motion = 'auto' } = {}) {
  for (let index = dialogStack.length - 1; index >= 0; index -= 1) {
    if (!dialogStack[index]?.open) {
      dialogStack.splice(index, 1);
    }
  }
  for (const dialog of document.querySelectorAll('dialog[open]')) {
    if (!dialogStack.includes(dialog)) {
      dialogStack.push(dialog);
    }
  }
  const openDialogs = dialogStack.filter((dialog) => dialog.open);
  setDialogMotionFrame(openDialogs, motion);
  openDialogs.forEach((dialog, index) => {
    dialog.dataset.dialogDepth = String(openDialogs.length - index - 1);
  });
  for (const dialog of document.querySelectorAll('dialog:not([open])')) {
    delete dialog.dataset.dialogDepth;
  }
  document.body.classList.toggle('dialog-open', openDialogs.length > 0);
}

function canDragDialogSheet(dialog) {
  return Boolean(
    MOBILE_SHEET_QUERY.matches
    && !REDUCED_MOTION_QUERY.matches
    && dialog?.matches('dialog.modal')
    && dialog.dataset.backdropDismiss === 'true'
    && dialog.dataset.blocking !== 'true'
    && !dialog.hasAttribute('data-dirty-guard')
    && !dialog.classList.contains('file-edit-modal'),
  );
}

function clearDialogSheetPresentation(dialog) {
  const panel = dialog?.querySelector('.modal-panel');
  cancelMotionAnimation(panel);
  if (panel) {
    panel.style.transform = '';
    panel.style.opacity = '';
  }
  if (dialog) {
    delete dialog.dataset.sheetDragging;
  }
  dialogSheetGestures.delete(dialog);
}

function setDialogSheetPresentation(dialog, position, height = null) {
  const panel = dialog?.querySelector('.modal-panel');
  if (!panel) {
    return;
  }
  const resolvedHeight = Math.max(1, height || panel.getBoundingClientRect().height);
  panel.style.transform = `translateY(${position}px)`;
  panel.style.opacity = String(clampMotionValue(1 - ((Math.max(0, position) / resolvedHeight) * 0.18), 0.78, 1));
}

function settleDialogSheet(dialog, gesture, { dismiss = false, velocity = 0 } = {}) {
  const panel = dialog?.querySelector('.modal-panel');
  if (!panel) {
    return;
  }
  const height = gesture.height;
  const target = dismiss ? height + 16 : 0;
  springTo(panel, {
    from: gesture.position,
    target,
    velocity,
    ...DRAWER_SPRING,
    apply: (value) => {
      gesture.position = value;
      setDialogSheetPresentation(dialog, value, height);
    },
    complete: () => {
      if (dismiss) {
        if (dialog === elements.confirmModal) {
          resolveConfirmation(false, { motion: 'instant' });
        } else {
          dismissDialogThroughCancelAction(dialog, { motion: 'instant' });
        }
        window.setTimeout(() => clearDialogSheetPresentation(dialog), 0);
        return;
      }
      clearDialogSheetPresentation(dialog);
    },
  });
}

function finishDialogSheetGesture(dialog, event, cancelled = false) {
  const gesture = dialogSheetGestures.get(dialog);
  if (!gesture || gesture.pointerId !== event.pointerId) {
    return;
  }
  dialogSheetGestures.delete(dialog);
  gesture.handle.releasePointerCapture?.(event.pointerId);
  if (!gesture.active) {
    clearDialogSheetPresentation(dialog);
    return;
  }
  const velocity = getGestureVelocity(gesture.samples);
  const height = gesture.height;
  const projectedPosition = gesture.position + projectGesture(velocity);
  const dismiss = !cancelled && velocity >= -40 && (
    projectedPosition >= height * 0.35
    || velocity > 110
  );
  settleDialogSheet(dialog, gesture, { dismiss, velocity: cancelled ? 0 : velocity });
}

function setupDialogSheetHandle(dialog) {
  const panel = dialog?.querySelector('.modal-panel');
  if (!panel) {
    return;
  }
  let handle = panel.querySelector(':scope > .dialog-sheet-handle');
  if (!handle) {
    handle = document.createElement('div');
    handle.className = 'dialog-sheet-handle';
    handle.setAttribute('aria-hidden', 'true');
    handle.innerHTML = '<span></span>';
    panel.prepend(handle);
    handle.addEventListener('pointerdown', (event) => {
      if (
        event.button !== 0
        || !canDragDialogSheet(dialog)
        || getTopDialog() !== dialog
        || dialogSheetGestures.has(dialog)
      ) {
        return;
      }
      event.stopPropagation();
      const interrupted = cancelMotionAnimation(panel);
      const position = interrupted?.value ?? getTransformTranslate(panel, 'y');
      const height = Math.max(1, panel.getBoundingClientRect().height);
      const gesture = {
        pointerId: event.pointerId,
        handle,
        panel,
        startY: event.clientY,
        startPosition: position,
        position,
        height,
        active: false,
        samples: [],
      };
      addVelocitySample(gesture.samples, event.clientY, event.timeStamp);
      dialogSheetGestures.set(dialog, gesture);
      handle.setPointerCapture?.(event.pointerId);
    });
    handle.addEventListener('pointermove', (event) => {
      const gesture = dialogSheetGestures.get(dialog);
      if (!gesture || gesture.pointerId !== event.pointerId) {
        return;
      }
      const deltaY = event.clientY - gesture.startY;
      if (!gesture.active && Math.abs(deltaY) < 10) {
        return;
      }
      gesture.active = true;
      dialog.dataset.sheetDragging = 'true';
      event.preventDefault();
      addVelocitySample(gesture.samples, event.clientY, event.timeStamp);
      const rawPosition = gesture.startPosition + deltaY;
      gesture.position = rawPosition < 0
        ? rubberband(rawPosition, gesture.height)
        : rawPosition;
      setDialogSheetPresentation(dialog, gesture.position, gesture.height);
    });
    handle.addEventListener('pointerup', (event) => finishDialogSheetGesture(dialog, event));
    handle.addEventListener('pointercancel', (event) => finishDialogSheetGesture(dialog, event, true));
    handle.addEventListener('lostpointercapture', (event) => finishDialogSheetGesture(dialog, event, true));
  }
  dialog.dataset.sheetDismissible = String(canDragDialogSheet(dialog));
}

function openDialog(dialog, {
  initialFocus = null,
  trigger = document.activeElement,
  backdropDismiss = !dialog?.hasAttribute('data-dirty-guard'),
  blocking = false,
  motion = 'auto',
} = {}) {
  if (typeof HTMLDialogElement === 'undefined' || !(dialog instanceof HTMLDialogElement)) {
    return;
  }
  const pendingClose = dialogCloseTimers.get(dialog);
  if (pendingClose) {
    window.clearTimeout(pendingClose);
    dialogCloseTimers.delete(dialog);
  }
  if (!dialog.open) {
    dialogTriggers.set(dialog, trigger instanceof HTMLElement ? trigger : null);
    dialog.dataset.backdropDismiss = String(Boolean(backdropDismiss));
    dialog.dataset.blocking = String(Boolean(blocking));
    dialog.showModal();
    dialogStack.push(dialog);
  }
  delete dialog.dataset.closing;
  markDialogPristine(dialog);
  setupDialogSheetHandle(dialog);
  refreshDialogBodyState({ motion });
  window.requestAnimationFrame(() => {
    const focusTarget = initialFocus
      || dialog.querySelector('[autofocus], input:not([type="hidden"]), select, textarea, button:not([disabled])');
    focusTarget?.focus({ preventScroll: true });
  });
}

function closeDialog(dialog, { restoreFocus = true, motion = 'auto' } = {}) {
  if (
    typeof HTMLDialogElement === 'undefined'
    || !(dialog instanceof HTMLDialogElement)
    || !dialog.open
    || dialog.dataset.closing === 'true'
  ) {
    return;
  }
  const motionType = setDialogMotionFrame(dialogStack.filter((item) => item.open), motion);
  cancelMotionAnimation(dialog.querySelector?.('.modal-panel'));
  dialog.dataset.closing = 'true';
  const finish = () => {
    dialogCloseTimers.delete(dialog);
    if (dialog.open) {
      dialog.close();
    }
    delete dialog.dataset.closing;
    dialogSnapshots.delete(dialog);
    const stackIndex = dialogStack.lastIndexOf(dialog);
    if (stackIndex >= 0) {
      dialogStack.splice(stackIndex, 1);
    }
    clearDialogSheetPresentation(dialog);
    refreshDialogBodyState({ motion });
    if (restoreFocus) {
      dialogTriggers.get(dialog)?.focus?.({ preventScroll: true });
    }
    dialogTriggers.delete(dialog);
  };
  const timer = window.setTimeout(finish, motionType === 'instant' ? 0 : 120);
  dialogCloseTimers.set(dialog, timer);
}

async function requestDialogClose(dialog, { force = false, motion = 'auto' } = {}) {
  if (typeof HTMLDialogElement === 'undefined' || !(dialog instanceof HTMLDialogElement) || !dialog.open) {
    return true;
  }
  const requestedMotion = dialog.dataset.dismissMotion || motion;
  delete dialog.dataset.dismissMotion;
  if (!force && dialog.dataset.blocking === 'true') {
    return false;
  }
  if (!force && isDialogDirty(dialog)) {
    const discard = await askForConfirmation({
      title: '放弃未保存的修改？',
      message: '当前内容尚未保存。关闭后，这些修改将无法恢复。',
      confirmLabel: '放弃修改',
      kind: 'danger',
    });
    if (!discard) {
      return false;
    }
  }
  closeDialog(dialog, { motion: requestedMotion });
  return true;
}

const DIALOG_CANCEL_ACTIONS = Object.freeze({
  botModal: 'cancelButton',
  userMappingsModal: 'userMappingsDoneButton',
  pluginModal: 'pluginCancelButton',
  pluginListEditorModal: 'pluginListEditorCancelButton',
  pluginUninstallModal: 'pluginUninstallCancelButton',
  filePreviewModal: 'filePreviewCancelButton',
  fileEditModal: 'fileEditCancelButton',
  fileSaveConfirmModal: 'fileSaveConfirmCancelButton',
  fileImageViewer: 'fileImageViewerCloseButton',
  fileCreateModal: 'fileCreateCancelButton',
  fileDeleteModal: 'fileDeleteCancelButton',
  fileMoveModal: 'fileMoveCancelButton',
  fileRenameModal: 'fileRenameCancelButton',
  fileAuthModal: 'fileAuthCancelButton',
  updateReleaseModal: 'updateReleaseCancelButton',
  updateConfirmModal: 'updateConfirmCancelButton',
});

function dismissDialogThroughCancelAction(dialog, { motion = 'auto' } = {}) {
  if (!dialog?.open) {
    return;
  }
  if (dialog === elements.confirmModal) {
    resolveConfirmation(false, { motion });
    return;
  }
  const actionButton = document.getElementById(DIALOG_CANCEL_ACTIONS[dialog.id] || '');
  if (actionButton && !actionButton.disabled) {
    dialog.dataset.dismissMotion = resolveMotion(motion);
    actionButton.click();
    return;
  }
  requestDialogClose(dialog, { motion });
}

function askForConfirmation({
  title = '确认操作',
  message = '',
  confirmLabel = '确认',
  cancelLabel = '取消',
  kind = 'default',
  kicker = 'CONFIRM ACTION',
} = {}) {
  if (!elements.confirmModal) {
    return Promise.resolve(false);
  }
  if (confirmationResolver) {
    confirmationResolver(false);
    confirmationResolver = null;
  }
  elements.confirmModalKicker.textContent = kicker;
  elements.confirmModalTitle.textContent = title;
  elements.confirmModalMessage.textContent = message;
  elements.confirmModalSubmitButton.textContent = confirmLabel;
  elements.confirmModalCancelButton.textContent = cancelLabel;
  elements.confirmModalSubmitButton.classList.toggle('danger-button', kind === 'danger');
  openDialog(elements.confirmModal, {
    initialFocus: kind === 'danger' ? elements.confirmModalCancelButton : elements.confirmModalSubmitButton,
    backdropDismiss: true,
  });
  return new Promise((resolve) => {
    confirmationResolver = resolve;
  });
}

function resolveConfirmation(value, { motion = 'auto' } = {}) {
  const resolver = confirmationResolver;
  confirmationResolver = null;
  closeDialog(elements.confirmModal, { motion });
  resolver?.(Boolean(value));
}

async function runBusy(button, busyLabel, operation) {
  if (!button || button.getAttribute('aria-busy') === 'true') {
    return undefined;
  }
  const previousLabel = button.textContent;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  if (busyLabel) {
    button.textContent = busyLabel;
  }
  try {
    return await operation();
  } finally {
    button.disabled = false;
    button.setAttribute('aria-busy', 'false');
    if (busyLabel) {
      button.textContent = previousLabel;
    }
  }
}

function setupDialogControllers() {
  for (const dialog of document.querySelectorAll('dialog')) {
    dialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      dismissDialogThroughCancelAction(dialog, { motion: 'instant' });
    });
    dialog.addEventListener('pointerdown', (event) => {
      if (event.target !== dialog || dialog.dataset.backdropDismiss !== 'true') {
        return;
      }
      dismissDialogThroughCancelAction(dialog);
    });
  }
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

  window.requestAnimationFrame(() => {
    if (state.currentPage === 'terminal' && state.terminal.activeId) {
      fitTerminal(state.terminal.activeId);
    }
  });
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

function setMobileNavigationAccessibility(open) {
  elements.mobileMenuButton?.setAttribute('aria-expanded', String(open));
  elements.mobileMenuButton?.setAttribute('aria-label', open ? '关闭主导航' : '打开主导航');
  if (!elements.sidebar) {
    return;
  }
  const mobileLayout = MOBILE_NAVIGATION_QUERY.matches;
  elements.sidebar.inert = mobileLayout && !open;
  if (mobileLayout && !open) {
    elements.sidebar.setAttribute('aria-hidden', 'true');
  } else {
    elements.sidebar.removeAttribute('aria-hidden');
  }
}

function focusAfterMobileNavigation(open, restoreFocus = true) {
  if (open) {
    const activeItem = elements.sidebar?.querySelector('[aria-current="page"]')
      || elements.sidebar?.querySelector('.nav-item');
    window.requestAnimationFrame(() => activeItem?.focus({ preventScroll: true }));
  } else if (restoreFocus) {
    window.requestAnimationFrame(() => elements.mobileMenuButton?.focus({ preventScroll: true }));
  }
}

function getMobileNavigationWidth() {
  return Math.max(1, elements.sidebar?.getBoundingClientRect().width || 320);
}

function setMobileNavigationPresentation(position, width = null) {
  if (!elements.sidebar || !elements.navigationScrim) {
    return;
  }
  const resolvedWidth = Math.max(1, width || getMobileNavigationWidth());
  const progress = clampMotionValue((position + resolvedWidth) / resolvedWidth, 0, 1);
  elements.sidebar.style.transform = `translateX(${position}px)`;
  elements.navigationScrim.style.opacity = String(progress);
}

function clearMobileNavigationPresentation() {
  if (elements.sidebar) {
    elements.sidebar.style.transform = '';
  }
  if (elements.navigationScrim) {
    elements.navigationScrim.style.opacity = '';
  }
  document.body.classList.remove('navigation-gesturing');
}

function finalizeMobileNavigation(open, { restoreFocus = true } = {}) {
  state.ui.mobileNavigationOpen = Boolean(open);
  document.body.classList.toggle('mobile-navigation-open', state.ui.mobileNavigationOpen);
  setMobileNavigationAccessibility(state.ui.mobileNavigationOpen);
  clearMobileNavigationPresentation();
  focusAfterMobileNavigation(state.ui.mobileNavigationOpen, restoreFocus);
}

function animateMobileNavigation(open, {
  restoreFocus = true,
  motion = 'auto',
  from,
  velocity,
} = {}) {
  const mobileLayout = MOBILE_NAVIGATION_QUERY.matches;
  const nextOpen = Boolean(open && mobileLayout);
  if (!elements.sidebar || !mobileLayout || resolveMotion(motion) === 'instant') {
    cancelMotionAnimation(elements.sidebar);
    finalizeMobileNavigation(nextOpen, { restoreFocus });
    return;
  }

  const interrupted = cancelMotionAnimation(elements.sidebar);
  const width = getMobileNavigationWidth();
  const currentPosition = Number.isFinite(from)
    ? from
    : (interrupted?.value ?? getTransformTranslate(elements.sidebar));
  state.ui.mobileNavigationOpen = nextOpen;
  setMobileNavigationAccessibility(true);
  if (nextOpen) {
    document.body.classList.add('mobile-navigation-open');
  }
  document.body.classList.add('navigation-gesturing');
  springTo(elements.sidebar, {
    from: currentPosition,
    target: nextOpen ? 0 : -width,
    velocity: Number.isFinite(velocity) ? velocity : interrupted?.velocity,
    ...DRAWER_SPRING,
    apply: (value) => setMobileNavigationPresentation(value, width),
    complete: () => finalizeMobileNavigation(nextOpen, { restoreFocus }),
  });
}

function setMobileNavigationOpen(open, options = {}) {
  animateMobileNavigation(open, options);
}

function isNavigationGestureBlocked() {
  return Boolean(getTopDialog() || elements.updateRestartOverlay?.dataset.blocking === 'true');
}

function cancelNavigationGesture({ resume = true } = {}) {
  const gesture = state.ui.navigationGesture;
  if (!gesture) {
    return;
  }
  state.ui.navigationGesture = null;
  gesture.source.releasePointerCapture?.(gesture.pointerId);
  if (resume) {
    animateMobileNavigation(gesture.originalOpen, {
      restoreFocus: false,
      motion: 'standard',
      from: gesture.position,
      velocity: 0,
    });
  }
}

function beginNavigationGesture(event, sourceKind) {
  if (
    event.button !== 0
    || !MOBILE_NAVIGATION_QUERY.matches
    || REDUCED_MOTION_QUERY.matches
    || isNavigationGestureBlocked()
    || state.ui.navigationGesture
  ) {
    return;
  }
  if (sourceKind === 'edge' && state.ui.mobileNavigationOpen) {
    return;
  }
  if (sourceKind === 'handle' && !state.ui.mobileNavigationOpen) {
    return;
  }
  const interrupted = cancelMotionAnimation(elements.sidebar);
  const position = interrupted?.value ?? getTransformTranslate(elements.sidebar);
  const width = getMobileNavigationWidth();
  const source = event.currentTarget;
  state.ui.navigationGesture = {
    pointerId: event.pointerId,
    source,
    sourceKind,
    originalOpen: state.ui.mobileNavigationOpen,
    startX: event.clientX,
    startY: event.clientY,
    startPosition: position,
    position,
    width,
    active: false,
    samples: [],
  };
  addVelocitySample(state.ui.navigationGesture.samples, event.clientX, event.timeStamp);
  source.setPointerCapture?.(event.pointerId);
}

function moveNavigationGesture(event) {
  const gesture = state.ui.navigationGesture;
  if (!gesture || gesture.pointerId !== event.pointerId) {
    return;
  }
  const deltaX = event.clientX - gesture.startX;
  const deltaY = event.clientY - gesture.startY;
  if (!gesture.active) {
    if (Math.hypot(deltaX, deltaY) < 10) {
      return;
    }
    if (Math.abs(deltaX) <= Math.abs(deltaY) * 1.2) {
      cancelNavigationGesture();
      return;
    }
    gesture.active = true;
    document.body.classList.add('mobile-navigation-open', 'navigation-gesturing');
    setMobileNavigationAccessibility(true);
  }
  event.preventDefault();
  addVelocitySample(gesture.samples, event.clientX, event.timeStamp);
  const width = gesture.width;
  const rawPosition = gesture.startPosition + deltaX;
  if (rawPosition < -width) {
    gesture.position = -width + rubberband(rawPosition + width, width);
  } else if (rawPosition > 0) {
    gesture.position = rubberband(rawPosition, width);
  } else {
    gesture.position = rawPosition;
  }
  setMobileNavigationPresentation(gesture.position, width);
}

function finishNavigationGesture(event, cancelled = false) {
  const gesture = state.ui.navigationGesture;
  if (!gesture || gesture.pointerId !== event.pointerId) {
    return;
  }
  state.ui.navigationGesture = null;
  gesture.source.releasePointerCapture?.(event.pointerId);
  if (!gesture.active) {
    animateMobileNavigation(gesture.originalOpen, { restoreFocus: false, motion: 'standard', from: gesture.position });
    return;
  }
  const velocity = getGestureVelocity(gesture.samples);
  const width = gesture.width;
  const projectedPosition = gesture.position + projectGesture(velocity);
  const nextOpen = cancelled ? gesture.originalOpen : projectedPosition > (-width / 2);
  animateMobileNavigation(nextOpen, {
    restoreFocus: true,
    motion: 'standard',
    from: gesture.position,
    velocity: cancelled ? 0 : velocity,
  });
}

function setupMobileNavigationGestures() {
  for (const [source, kind] of [
    [elements.navigationEdgeGesture, 'edge'],
    [elements.sidebarDragHandle, 'handle'],
  ]) {
    source?.addEventListener('pointerdown', (event) => beginNavigationGesture(event, kind));
    source?.addEventListener('pointermove', moveNavigationGesture);
    source?.addEventListener('pointerup', (event) => finishNavigationGesture(event));
    source?.addEventListener('pointercancel', (event) => finishNavigationGesture(event, true));
    source?.addEventListener('lostpointercapture', (event) => finishNavigationGesture(event, true));
  }
}

function pageDisplayName(page) {
  const labels = {
    network: '网络配置',
    basic: '基础信息',
    diagnostics: '运行诊断',
    logs: '猫猫日志',
    plugins: '插件管理',
    files: '文件管理',
    terminal: '系统终端',
    settings: '基础设置',
    'plugin-dashboard': '插件 Dashboard',
  };
  return labels[page] || 'RocketCatShell';
}

function parseHashRoute() {
  const raw = window.location.hash.replace(/^#/, '');
  if (raw.startsWith('plugin-dashboard/')) {
    const [, pluginId = '', page = ''] = raw.split('/');
    return {
      page: 'plugin-dashboard',
      pluginId: decodeURIComponent(pluginId),
      dashboardPage: decodeURIComponent(page),
    };
  }
  return { page: CORE_PAGE_IDS.has(raw) ? raw : 'network' };
}

async function navigateToPage(page, { replace = false, focusMain = true } = {}) {
  const target = CORE_PAGE_IDS.has(page) ? page : 'network';
  if (state.currentPage === 'plugin-dashboard') {
    await cleanupPluginDashboardSession();
    state.pluginDashboard.plugin = null;
    state.pluginDashboard.page = '';
  }
  const method = replace ? 'replaceState' : 'pushState';
  window.history[method]({ rocketcatPage: target }, '', `#${target}`);
  await activatePage(target);
  if (focusMain) {
    elements.mainContent?.focus({ preventScroll: true });
  }
}

async function restoreHashRoute({ replaceInvalid = false } = {}) {
  const route = parseHashRoute();
  if (route.page === 'plugin-dashboard' && route.pluginId) {
    await loadPlugins({ forceReload: false, silent: false });
    await openPluginDashboard(route.pluginId, route.dashboardPage, { pushHistory: false });
    return;
  }
  if (replaceInvalid && window.location.hash !== `#${route.page}`) {
    window.history.replaceState({ rocketcatPage: route.page }, '', `#${route.page}`);
  }
  if (state.currentPage === 'plugin-dashboard') {
    await cleanupPluginDashboardSession();
    state.pluginDashboard.plugin = null;
    state.pluginDashboard.page = '';
  }
  await activatePage(route.page);
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
    const requestError = new Error(message);
    requestError.status = response.status;
    throw requestError;
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
  if (page !== state.currentPage) {
    cancelActiveCardOrderInteraction({ announce: false });
  }
  state.currentPage = page;
  elements.networkPage.classList.toggle('hidden', page !== 'network');
  elements.diagnosticsPage.classList.toggle('hidden', page !== 'diagnostics');
  elements.basicPage.classList.toggle('hidden', page !== 'basic');
  elements.logsPage.classList.toggle('hidden', page !== 'logs');
  elements.settingsPage.classList.toggle('hidden', page !== 'settings');
  elements.pluginsPage.classList.toggle('hidden', page !== 'plugins');
  elements.pluginDashboardPage.classList.toggle('hidden', page !== 'plugin-dashboard');
  elements.filesPage.classList.toggle('hidden', page !== 'files');
  elements.terminalPage.classList.toggle('hidden', page !== 'terminal');
  document.body.classList.toggle('plugin-dashboard-open', page === 'plugin-dashboard');
  if (elements.mobilePageTitle) {
    elements.mobilePageTitle.textContent = pageDisplayName(page);
  }
  setMobileNavigationOpen(false, { restoreFocus: false });

  for (const button of elements.navButtons) {
    const isActive = button.dataset.page === page;
    button.classList.toggle('active', isActive);
    button.classList.toggle('ghost', !isActive);
    if (isActive) {
      button.setAttribute('aria-current', 'page');
    } else {
      button.removeAttribute('aria-current');
    }
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
  const pageElement = {
    network: elements.networkPage,
    diagnostics: elements.diagnosticsPage,
    basic: elements.basicPage,
    settings: elements.settingsPage,
    plugins: elements.pluginsPage,
    files: elements.filesPage,
    terminal: elements.terminalPage,
    logs: elements.logsPage,
  }[page];
  pageElement?.setAttribute('aria-busy', 'true');
  try {
    if (page === 'network') {
      await loadData();
    } else if (page === 'diagnostics') {
      await loadDiagnostics({ forceReload, silent: false });
    } else if (page === 'basic') {
      await loadBasicInfo({ forceReload, silent: false });
    } else if (page === 'settings') {
      await Promise.all([
        loadSettings({ forceReload, silent: false }),
        loadUpdateStatus({ refresh: false, silent: false }),
      ]);
    } else if (page === 'plugins') {
      await loadPlugins({ forceReload, silent: false });
    } else if (page === 'files') {
      await loadFiles({ forceReload, silent: false });
    } else if (page === 'terminal') {
      await loadTerminals({ forceReload, silent: false });
    }
  } finally {
    pageElement?.setAttribute('aria-busy', 'false');
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
  if (state.network.abortController) {
    state.network.abortController.abort();
    state.network.abortController = null;
  }
}

function startNetworkPolling() {
  stopNetworkPolling();
  if (state.currentPage !== 'network' || document.hidden) {
    return;
  }
  state.network.pollTimer = window.setTimeout(async () => {
    const controller = new AbortController();
    state.network.abortController = controller;
    try {
      await loadData({ signal: controller.signal });
    } catch (error) {
      if (!isAbortError(error)) {
        console.warn(error);
      }
    } finally {
      if (state.network.abortController === controller) {
        state.network.abortController = null;
      }
      if (state.currentPage === 'network' && !document.hidden) {
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
  if (state.diagnostics.abortController) {
    state.diagnostics.abortController.abort();
    state.diagnostics.abortController = null;
  }
}

function startDiagnosticsPolling() {
  stopDiagnosticsPolling();
  if (state.currentPage !== 'diagnostics' || document.hidden) {
    return;
  }
  state.diagnostics.pollTimer = window.setTimeout(async () => {
    const controller = new AbortController();
    state.diagnostics.abortController = controller;
    try {
      await loadDiagnostics({ forceReload: true, silent: true, signal: controller.signal });
    } catch (error) {
      if (!isAbortError(error)) {
        console.warn(error);
      }
    } finally {
      if (state.diagnostics.abortController === controller) {
        state.diagnostics.abortController = null;
      }
      if (state.currentPage === 'diagnostics' && !document.hidden) {
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
  const shellOnline = Boolean(status.bridge_enabled && status.independent_webui_enabled);
  const runtimeState = shellOnline ? 'online' : 'error';
  const runtimeLabel = shellOnline ? 'Shell 正常运行' : 'Shell 状态异常';
  for (const dot of [elements.sidebarRuntimeDot, elements.mobileRuntimeStatus]) {
    if (!dot) continue;
    dot.dataset.state = runtimeState;
    dot.setAttribute('aria-label', runtimeLabel);
  }
  if (elements.sidebarRuntimeText) {
    elements.sidebarRuntimeText.textContent = runtimeLabel;
  }
  if (elements.sidebarVersion) {
    elements.sidebarVersion.textContent = getRocketCatVersion(status);
  }

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

const CARD_ORDER_GRIDS = [
  { grid: elements.botGrid, scope: 'bots', page: 'network' },
  { grid: elements.basicInfoGrid, scope: 'bots', page: 'basic' },
  { grid: elements.diagnosticsGrid, scope: 'bots', page: 'diagnostics' },
  { grid: elements.pluginGrid, scope: 'plugins', page: 'plugins' },
].filter((entry) => entry.grid);

function normalizeClientCardOrder(value) {
  const seen = new Set();
  const normalized = [];
  for (const rawItem of Array.isArray(value) ? value : []) {
    if (typeof rawItem !== 'string') {
      continue;
    }
    const item = rawItem.trim();
    if (!item || seen.has(item)) {
      continue;
    }
    seen.add(item);
    normalized.push(item);
  }
  return normalized;
}

function reconcileClientCardOrder(scope, entityIds, { authoritative = false } = {}) {
  const ids = normalizeClientCardOrder(entityIds);
  const available = new Set(ids);
  const current = normalizeClientCardOrder(state.cardOrder[scope]);
  const next = authoritative
    ? current.filter((item) => available.has(item))
    : current.slice();
  const seen = new Set(next);
  for (const item of ids) {
    if (!seen.has(item)) {
      seen.add(item);
      next.push(item);
    }
  }
  state.cardOrder[scope] = next;
  return next;
}

function orderItemsForCards(items, scope, getId) {
  const ranks = new Map(
    normalizeClientCardOrder(state.cardOrder[scope]).map((item, index) => [item, index]),
  );
  return items
    .map((item, index) => ({ item, index, rank: ranks.get(String(getId(item) || '')) }))
    .sort((left, right) => {
      const leftRank = left.rank ?? Number.MAX_SAFE_INTEGER;
      const rightRank = right.rank ?? Number.MAX_SAFE_INTEGER;
      return leftRank - rightRank || left.index - right.index;
    })
    .map((entry) => entry.item);
}

function buildCardOrderDragSurface(label) {
  return `
    <div
      class="card-order-drag-surface"
      data-card-order-drag-surface
      aria-hidden="true"
      title="拖动 ${escapeHtml(label || '卡片')} 的空白区域调整顺序"
    ></div>
  `;
}

function configureCardOrderCard(card, scope, id, label) {
  const normalizedId = String(id || '');
  const normalizedLabel = String(label || normalizedId || '卡片');
  card.dataset.cardOrderId = normalizedId;
  card.dataset.cardOrderName = normalizedLabel;
  card.dataset.cardOrderScope = scope;
  card.tabIndex = 0;
  card.setAttribute('aria-label', `${normalizedLabel}，可排序卡片`);
  card.setAttribute('aria-roledescription', '可排序卡片');
  card.setAttribute('aria-describedby', 'cardOrderInstructions');
  card.setAttribute(
    'aria-keyshortcuts',
    'Space Enter ArrowLeft ArrowRight ArrowUp ArrowDown Home End Escape',
  );
}

function getCardOrderGridConfig(grid) {
  return CARD_ORDER_GRIDS.find((entry) => entry.grid === grid) || null;
}

function getCardOrderCards(grid) {
  return Array.from(grid?.querySelectorAll(':scope > [data-card-order-id]') || []);
}

function getGridCardOrder(grid) {
  return getCardOrderCards(grid).map((card) => card.dataset.cardOrderId || '');
}

function mergeVisibleCardOrder(scope, visibleOrder, baseOrder = state.cardOrder[scope]) {
  const visible = normalizeClientCardOrder(visibleOrder);
  const visibleSet = new Set(visible);
  const base = normalizeClientCardOrder(baseOrder);
  const merged = base.slice();
  const slots = [];
  for (let index = 0; index < merged.length; index += 1) {
    if (visibleSet.has(merged[index])) {
      slots.push(index);
    }
  }
  if (slots.length === visible.length) {
    slots.forEach((slot, index) => {
      merged[slot] = visible[index];
    });
    return merged;
  }
  return reconcileClientCardOrder(scope, visible, { authoritative: false }).slice();
}

function reorderCardGridInstantly(grid, orderedIds) {
  const cards = new Map(
    getCardOrderCards(grid).map((card) => [card.dataset.cardOrderId || '', card]),
  );
  for (const id of orderedIds) {
    const card = cards.get(id);
    if (card) {
      grid.appendChild(card);
      cards.delete(id);
    }
  }
  for (const card of cards.values()) {
    grid.appendChild(card);
  }
}

function applyCardOrderToRenderedGrids(scope) {
  const ranks = new Map(
    normalizeClientCardOrder(state.cardOrder[scope]).map((item, index) => [item, index]),
  );
  for (const { grid, scope: gridScope } of CARD_ORDER_GRIDS) {
    if (gridScope !== scope) {
      continue;
    }
    const ids = getGridCardOrder(grid).sort((left, right) => (
      (ranks.get(left) ?? Number.MAX_SAFE_INTEGER)
      - (ranks.get(right) ?? Number.MAX_SAFE_INTEGER)
    ));
    reorderCardGridInstantly(grid, ids);
  }
}

function announceCardOrder(message) {
  if (!elements.cardOrderLiveRegion) {
    return;
  }
  elements.cardOrderLiveRegion.textContent = '';
  window.requestAnimationFrame(() => {
    elements.cardOrderLiveRegion.textContent = message;
  });
}

function setCardOrderScopeBusy(scope, busy) {
  if (busy) {
    state.cardOrder.savingScopes.add(scope);
  } else {
    state.cardOrder.savingScopes.delete(scope);
  }
  syncCardOrderScopeBusy(scope);
}

function syncCardOrderScopeBusy(scope) {
  const busy = state.cardOrder.savingScopes.has(scope);
  for (const { grid, scope: gridScope } of CARD_ORDER_GRIDS) {
    if (gridScope !== scope) {
      continue;
    }
    grid.setAttribute('aria-busy', String(busy));
    for (const card of getCardOrderCards(grid)) {
      card.setAttribute('aria-disabled', String(busy));
    }
  }
}

async function loadCardOrder({ forceReload = false, silent = true } = {}) {
  if (state.cardOrder.loaded && !forceReload) {
    return;
  }
  try {
    const payload = await requestJson('/api/settings/card-order');
    state.cardOrder.bots = normalizeClientCardOrder(payload.bots);
    state.cardOrder.plugins = normalizeClientCardOrder(payload.plugins);
    state.cardOrder.loaded = true;
    applyCardOrderToRenderedGrids('bots');
    applyCardOrderToRenderedGrids('plugins');
  } catch (error) {
    state.cardOrder.loaded = true;
    if (!silent) {
      showToast(error.message || '卡片顺序加载失败，已使用当前列表顺序', 'error');
    }
  }
}

function focusCardOrderCard(scope, id, preferredGrid = null) {
  const grids = CARD_ORDER_GRIDS.slice().sort((left, right) => (
    Number(right.grid === preferredGrid) - Number(left.grid === preferredGrid)
  ));
  for (const { grid, scope: gridScope } of grids) {
    if (gridScope !== scope) {
      continue;
    }
    const card = getCardOrderCards(grid)
      .find((item) => item.dataset.cardOrderId === id);
    if (card) {
      card.focus({ preventScroll: true, focusVisible: inputModality === 'keyboard' });
      return;
    }
  }
}

async function saveCardOrder(scope, order, { fallbackOrder, focusId, focusGrid } = {}) {
  const nextOrder = normalizeClientCardOrder(order);
  setCardOrderScopeBusy(scope, true);
  try {
    const payload = await requestJson('/api/settings/card-order', {
      method: 'PUT',
      body: JSON.stringify({ [scope]: nextOrder }),
    });
    state.cardOrder.bots = normalizeClientCardOrder(payload.bots);
    state.cardOrder.plugins = normalizeClientCardOrder(payload.plugins);
    applyCardOrderToRenderedGrids('bots');
    applyCardOrderToRenderedGrids('plugins');
    announceCardOrder('卡片顺序已保存');
  } catch (error) {
    let restored = normalizeClientCardOrder(fallbackOrder);
    try {
      const serverOrder = await requestJson('/api/settings/card-order');
      state.cardOrder.bots = normalizeClientCardOrder(serverOrder.bots);
      state.cardOrder.plugins = normalizeClientCardOrder(serverOrder.plugins);
      restored = state.cardOrder[scope];
    } catch (_refreshError) {
      state.cardOrder[scope] = restored;
    }
    state.cardOrder[scope] = restored;
    if (error.status === 409 && focusGrid) {
      const page = getCardOrderGridConfig(focusGrid)?.page;
      try {
        await refreshCardOrderPage(page);
      } catch (_pageRefreshError) {
        // The canonical order is restored; normal polling can retry page data.
      }
    }
    applyCardOrderToRenderedGrids('bots');
    applyCardOrderToRenderedGrids('plugins');
    showToast(error.message || '卡片顺序保存失败，已恢复原顺序', 'error');
    announceCardOrder('卡片顺序保存失败，已恢复原顺序');
  } finally {
    setCardOrderScopeBusy(scope, false);
    if (focusId) {
      focusCardOrderCard(scope, focusId, focusGrid);
    }
  }
}

function cancelCardOrderSpring(card) {
  const running = card ? cardOrderMotionAnimations.get(card) : null;
  if (!running) {
    return null;
  }
  if (typeof Animation !== 'undefined' && running instanceof Animation) {
    running.cancel();
    cardOrderMotionAnimations.delete(card);
    return running;
  }
  window.cancelAnimationFrame(running.frame);
  cardOrderMotionAnimations.delete(card);
  running.resolve?.();
  return running;
}

function springCardOrderToOrigin(card, { x = 0, y = 0, velocityX = 0, velocityY = 0 } = {}) {
  cancelCardOrderSpring(card);
  if (!card || REDUCED_MOTION_QUERY.matches) {
    if (card) {
      card.style.transform = '';
    }
    return Promise.resolve();
  }
  const omega0 = (2 * Math.PI) / SETTLE_SPRING.response;
  const stiffness = omega0 * omega0;
  const damping = 2 * SETTLE_SPRING.dampingRatio * omega0;
  const animation = {
    frame: 0,
    x,
    y,
    velocityX,
    velocityY,
    previousTime: performance.now(),
  };
  cardOrderMotionAnimations.set(card, animation);
  return new Promise((resolve) => {
    animation.resolve = resolve;
    const step = (timestamp) => {
      if (cardOrderMotionAnimations.get(card) !== animation) {
        resolve();
        return;
      }
      const deltaSeconds = Math.min(
        1 / 30,
        Math.max(1 / 240, (timestamp - animation.previousTime) / 1000),
      );
      animation.previousTime = timestamp;
      const accelerationX = (-stiffness * animation.x) - (damping * animation.velocityX);
      const accelerationY = (-stiffness * animation.y) - (damping * animation.velocityY);
      animation.velocityX += accelerationX * deltaSeconds;
      animation.velocityY += accelerationY * deltaSeconds;
      animation.x += animation.velocityX * deltaSeconds;
      animation.y += animation.velocityY * deltaSeconds;
      card.style.transform = `translate(${animation.x}px, ${animation.y}px)`;
      if (
        Math.abs(animation.x) < MOTION_SETTLE_POSITION
        && Math.abs(animation.y) < MOTION_SETTLE_POSITION
        && Math.abs(animation.velocityX) < MOTION_SETTLE_VELOCITY
        && Math.abs(animation.velocityY) < MOTION_SETTLE_VELOCITY
      ) {
        card.style.transform = '';
        cardOrderMotionAnimations.delete(card);
        resolve();
        return;
      }
      animation.frame = window.requestAnimationFrame(step);
    };
    animation.frame = window.requestAnimationFrame(step);
  });
}

function cancelCardOrderFlip(card) {
  const animation = cardOrderMotionAnimations.get(card);
  if (typeof Animation !== 'undefined' && animation instanceof Animation) {
    animation.cancel();
    cardOrderMotionAnimations.delete(card);
  }
}

function moveCardWithFlip(grid, card, reference) {
  const siblings = getCardOrderCards(grid).filter((item) => item !== card);
  for (const sibling of siblings) {
    cancelCardOrderFlip(sibling);
  }
  const before = new Map(siblings.map((item) => [item, item.getBoundingClientRect()]));
  if (reference) {
    grid.insertBefore(card, reference);
  } else {
    grid.appendChild(card);
  }
  if (REDUCED_MOTION_QUERY.matches) {
    return;
  }
  for (const sibling of siblings) {
    const previous = before.get(sibling);
    const current = sibling.getBoundingClientRect();
    const deltaX = previous.left - current.left;
    const deltaY = previous.top - current.top;
    if (Math.abs(deltaX) < 0.5 && Math.abs(deltaY) < 0.5) {
      continue;
    }
    const animation = sibling.animate(
      [
        { transform: `translate(${deltaX}px, ${deltaY}px)` },
        { transform: 'translate(0, 0)' },
      ],
      {
        duration: CARD_ORDER_FLIP_DURATION,
        easing: CARD_ORDER_FLIP_EASING,
      },
    );
    cardOrderMotionAnimations.set(sibling, animation);
    animation.finished.catch(() => null).finally(() => {
      if (cardOrderMotionAnimations.get(sibling) === animation) {
        cardOrderMotionAnimations.delete(sibling);
      }
    });
  }
}

function maybeReorderCardAtPointer(drag, clientX, clientY) {
  const cards = getCardOrderCards(drag.grid);
  const candidates = cards.filter((item) => item !== drag.card);
  if (!candidates.length) {
    return;
  }
  let target = candidates[0];
  let targetRect = target.getBoundingClientRect();
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const candidate of candidates) {
    const rect = candidate.getBoundingClientRect();
    const dx = (clientX - (rect.left + rect.width / 2)) / Math.max(1, rect.width);
    const dy = (clientY - (rect.top + rect.height / 2)) / Math.max(1, rect.height);
    const distance = (dx * dx) + (dy * dy);
    if (distance < bestDistance) {
      bestDistance = distance;
      target = candidate;
      targetRect = rect;
    }
  }
  const sameVisualRow = clientY >= targetRect.top && clientY <= targetRect.bottom;
  const beforeTarget = sameVisualRow
    ? clientX < targetRect.left + targetRect.width / 2
    : clientY < targetRect.top + targetRect.height / 2;
  const withoutDragged = cards.filter((item) => item !== drag.card);
  const targetIndex = withoutDragged.indexOf(target);
  const insertIndex = targetIndex + (beforeTarget ? 0 : 1);
  const nextCards = withoutDragged.slice();
  nextCards.splice(insertIndex, 0, drag.card);
  if (nextCards.every((item, index) => item === cards[index])) {
    return;
  }
  const reference = insertIndex < withoutDragged.length
    ? withoutDragged[insertIndex]
    : null;
  moveCardWithFlip(drag.grid, drag.card, reference);
}

function setDraggedCardPresentation(drag, clientX, clientY, { reorder = true } = {}) {
  drag.clientX = clientX;
  drag.clientY = clientY;
  if (reorder) {
    maybeReorderCardAtPointer(drag, clientX, clientY);
  }
  const presented = drag.card.getBoundingClientRect();
  const naturalLeft = presented.left - drag.translateX;
  const naturalTop = presented.top - drag.translateY;
  drag.translateX = (clientX - drag.pointerOffsetX) - naturalLeft;
  drag.translateY = (clientY - drag.pointerOffsetY) - naturalTop;
  drag.card.style.transform = `translate(${drag.translateX}px, ${drag.translateY}px)`;
}

function stopCardOrderAutoScroll() {
  if (state.cardOrder.autoScrollFrame) {
    window.cancelAnimationFrame(state.cardOrder.autoScrollFrame);
    state.cardOrder.autoScrollFrame = 0;
  }
}

function startCardOrderAutoScroll() {
  stopCardOrderAutoScroll();
  const step = () => {
    const drag = state.cardOrder.pointer;
    if (!drag?.active || drag.finishing) {
      state.cardOrder.autoScrollFrame = 0;
      return;
    }
    let delta = 0;
    if (drag.clientY < CARD_ORDER_AUTO_SCROLL_EDGE) {
      delta = -CARD_ORDER_AUTO_SCROLL_MAX
        * (1 - clampMotionValue(drag.clientY / CARD_ORDER_AUTO_SCROLL_EDGE, 0, 1));
    } else if (drag.clientY > window.innerHeight - CARD_ORDER_AUTO_SCROLL_EDGE) {
      delta = CARD_ORDER_AUTO_SCROLL_MAX
        * (1 - clampMotionValue((window.innerHeight - drag.clientY) / CARD_ORDER_AUTO_SCROLL_EDGE, 0, 1));
    }
    if (Math.abs(delta) > 0.2) {
      window.scrollBy(0, delta);
      setDraggedCardPresentation(drag, drag.clientX, drag.clientY);
    }
    state.cardOrder.autoScrollFrame = window.requestAnimationFrame(step);
  };
  state.cardOrder.autoScrollFrame = window.requestAnimationFrame(step);
}

function pausePollingForCardOrder(page) {
  if (page === 'network') {
    stopNetworkPolling();
  } else if (page === 'diagnostics') {
    stopDiagnosticsPolling();
  }
}

function resumePollingAfterCardOrder() {
  if (state.currentPage === 'network') {
    startNetworkPolling();
  } else if (state.currentPage === 'diagnostics') {
    startDiagnosticsPolling();
  }
}

async function refreshCardOrderPage(page) {
  if (page === 'network') {
    await loadData();
  } else if (page === 'basic') {
    await loadBasicInfo({ forceReload: true, silent: true });
  } else if (page === 'diagnostics') {
    await loadDiagnostics({ forceReload: true, silent: true });
  } else if (page === 'plugins') {
    await loadPlugins({ forceReload: true, silent: true });
  }
}

function applyDeferredCardOrderRenders() {
  const deferred = state.cardOrder.deferred;
  state.cardOrder.deferred = {
    network: null,
    diagnostics: null,
    basic: null,
    plugins: null,
  };
  if (deferred.network) renderBots(deferred.network);
  if (deferred.diagnostics) renderDiagnostics(deferred.diagnostics);
  if (deferred.basic) renderBasicInfo(deferred.basic);
  if (deferred.plugins) renderPlugins(deferred.plugins);
}

function shouldDeferCardOrderRender(page, payload) {
  const interaction = state.cardOrder.pointer || state.cardOrder.keyboard;
  if (!interaction || interaction.page !== page) {
    return false;
  }
  state.cardOrder.deferred[page] = payload;
  return true;
}

function finishCardOrderInteraction() {
  applyDeferredCardOrderRenders();
  resumePollingAfterCardOrder();
}

function clearCardOrderPointerPresentation(drag) {
  stopCardOrderAutoScroll();
  cancelCardOrderSpring(drag.card);
  drag.card.style.transform = '';
  drag.card.classList.remove('is-card-order-dragging');
  document.body.classList.remove('card-order-drag-active');
  delete drag.grid.dataset.cardOrderInteraction;
  for (const card of getCardOrderCards(drag.grid)) {
    cancelCardOrderFlip(card);
  }
}

function isPointOverCardText(card, clientX, clientY) {
  const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let node = walker.nextNode();
  while (node) {
    if (String(node.nodeValue || '').trim()) {
      const parent = node.parentElement;
      if (
        parent
        && !parent.closest('[hidden], .hidden, .visually-hidden, [aria-hidden="true"]')
        && getComputedStyle(parent).visibility !== 'hidden'
      ) {
        range.selectNodeContents(node);
        for (const rect of range.getClientRects()) {
          if (
            clientX >= rect.left - 1
            && clientX <= rect.right + 1
            && clientY >= rect.top - 1
            && clientY <= rect.bottom + 1
          ) {
            range.detach?.();
            return true;
          }
        }
      }
    }
    node = walker.nextNode();
  }
  range.detach?.();
  return false;
}

function isCardOrderPointerBlocked(event, card) {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) {
    return true;
  }
  if (target.closest(CARD_ORDER_POINTER_BLOCK_SELECTOR)) {
    return true;
  }
  if (isPointOverCardText(card, event.clientX, event.clientY)) {
    return true;
  }
  return false;
}

function cancelCardOrderPointerDrag({ restore = true, announce = true } = {}) {
  const drag = state.cardOrder.pointer;
  if (!drag) {
    return;
  }
  drag.cancelled = true;
  try {
    if (drag.grid.hasPointerCapture?.(drag.pointerId)) {
      drag.grid.releasePointerCapture(drag.pointerId);
    }
  } catch (_error) {
    // Pointer capture may already have been released by the browser.
  }
  clearCardOrderPointerPresentation(drag);
  if (restore && drag.active) {
    state.cardOrder[drag.scope] = drag.originalFullOrder.slice();
    applyCardOrderToRenderedGrids(drag.scope);
  }
  state.cardOrder.pointer = null;
  if (announce && drag.active) {
    announceCardOrder('已取消排序并恢复原顺序');
  }
  finishCardOrderInteraction();
}

function beginCardOrderPointer(event) {
  const target = event.target instanceof Element ? event.target : null;
  const card = target?.closest('[data-card-order-id]');
  if (!card || state.cardOrder.pointer || state.cardOrder.keyboard) {
    return;
  }
  if (!event.isPrimary || (event.pointerType === 'mouse' && event.button !== 0)) {
    return;
  }
  const grid = card?.parentElement;
  const config = getCardOrderGridConfig(grid);
  if (
    !config
    || state.cardOrder.savingScopes.has(config.scope)
    || isCardOrderPointerBlocked(event, card)
  ) {
    return;
  }
  event.preventDefault();
  const rect = card.getBoundingClientRect();
  card.focus({ preventScroll: true, focusVisible: false });
  state.cardOrder.pointer = {
    pointerId: event.pointerId,
    pointerType: event.pointerType,
    card,
    grid,
    scope: config.scope,
    page: config.page,
    id: card.dataset.cardOrderId || '',
    label: card.dataset.cardOrderName || card.dataset.cardOrderId || '卡片',
    startX: event.clientX,
    startY: event.clientY,
    clientX: event.clientX,
    clientY: event.clientY,
    pointerOffsetX: event.clientX - rect.left,
    pointerOffsetY: event.clientY - rect.top,
    translateX: 0,
    translateY: 0,
    samplesX: [{ position: event.clientX, timestamp: performance.now() }],
    samplesY: [{ position: event.clientY, timestamp: performance.now() }],
    originalFullOrder: normalizeClientCardOrder(state.cardOrder[config.scope]),
    active: false,
    finishing: false,
    cancelled: false,
  };
  grid.dataset.cardOrderInteraction = 'pending';
}

function moveCardOrderPointer(event) {
  const drag = state.cardOrder.pointer;
  if (!drag || drag.pointerId !== event.pointerId || drag.finishing) {
    return;
  }
  const deltaX = event.clientX - drag.startX;
  const deltaY = event.clientY - drag.startY;
  if (!drag.active) {
    if (Math.hypot(deltaX, deltaY) < CARD_ORDER_DRAG_THRESHOLD) {
      return;
    }
    drag.active = true;
    drag.grid.dataset.cardOrderInteraction = 'dragging';
    drag.card.classList.add('is-card-order-dragging');
    document.body.classList.add('card-order-drag-active');
    try {
      drag.grid.setPointerCapture(event.pointerId);
    } catch (_error) {
      // Window listeners still keep the drag coherent if capture is unavailable.
    }
    pausePollingForCardOrder(drag.page);
    startCardOrderAutoScroll();
    announceCardOrder(`正在调整 ${drag.label}`);
  }
  event.preventDefault();
  const timestamp = performance.now();
  addVelocitySample(drag.samplesX, event.clientX, timestamp);
  addVelocitySample(drag.samplesY, event.clientY, timestamp);
  setDraggedCardPresentation(drag, event.clientX, event.clientY);
}

async function commitCardOrderPointer(event) {
  const drag = state.cardOrder.pointer;
  if (!drag || drag.pointerId !== event.pointerId || drag.finishing) {
    return;
  }
  if (!drag.active) {
    delete drag.grid.dataset.cardOrderInteraction;
    state.cardOrder.pointer = null;
    finishCardOrderInteraction();
    return;
  }
  event.preventDefault();
  drag.finishing = true;
  stopCardOrderAutoScroll();
  const timestamp = performance.now();
  addVelocitySample(drag.samplesX, event.clientX, timestamp);
  addVelocitySample(drag.samplesY, event.clientY, timestamp);
  const visibleOrder = getGridCardOrder(drag.grid);
  const nextFullOrder = mergeVisibleCardOrder(
    drag.scope,
    visibleOrder,
    drag.originalFullOrder,
  );
  state.cardOrder[drag.scope] = nextFullOrder;
  applyCardOrderToRenderedGrids(drag.scope);
  const velocityX = getGestureVelocity(drag.samplesX);
  const velocityY = getGestureVelocity(drag.samplesY);
  await springCardOrderToOrigin(drag.card, {
    x: drag.translateX,
    y: drag.translateY,
    velocityX,
    velocityY,
  });
  if (drag.cancelled) {
    return;
  }
  clearCardOrderPointerPresentation(drag);
  state.cardOrder.pointer = null;
  await saveCardOrder(drag.scope, nextFullOrder, {
    fallbackOrder: drag.originalFullOrder,
    focusId: drag.id,
    focusGrid: drag.grid,
  });
  finishCardOrderInteraction();
}

function getCardOrderPositionLabel(grid, id) {
  const ids = getGridCardOrder(grid);
  const index = ids.indexOf(id);
  return `第 ${Math.max(0, index) + 1} / ${ids.length} 位`;
}

function startKeyboardCardOrder(card) {
  const grid = card?.parentElement;
  const config = getCardOrderGridConfig(grid);
  if (!card || !config || state.cardOrder.savingScopes.has(config.scope)) {
    return;
  }
  state.cardOrder.keyboard = {
    card,
    grid,
    scope: config.scope,
    page: config.page,
    id: card.dataset.cardOrderId || '',
    label: card.dataset.cardOrderName || card.dataset.cardOrderId || '卡片',
    originalFullOrder: normalizeClientCardOrder(state.cardOrder[config.scope]),
  };
  card.classList.add('is-card-order-keyboard-selected');
  pausePollingForCardOrder(config.page);
  announceCardOrder(
    `已选择 ${state.cardOrder.keyboard.label}，${getCardOrderPositionLabel(grid, state.cardOrder.keyboard.id)}`,
  );
}

function clearKeyboardCardOrderPresentation(interaction) {
  interaction.card.classList.remove('is-card-order-keyboard-selected');
}

function cancelKeyboardCardOrder({ announce = true } = {}) {
  const interaction = state.cardOrder.keyboard;
  if (!interaction) {
    return;
  }
  state.cardOrder[interaction.scope] = interaction.originalFullOrder.slice();
  applyCardOrderToRenderedGrids(interaction.scope);
  clearKeyboardCardOrderPresentation(interaction);
  state.cardOrder.keyboard = null;
  focusCardOrderCard(interaction.scope, interaction.id, interaction.grid);
  if (announce) {
    announceCardOrder('已取消排序并恢复原顺序');
  }
  finishCardOrderInteraction();
}

function findVerticalKeyboardTarget(cards, currentIndex, direction) {
  const current = cards[currentIndex];
  const currentRect = current.getBoundingClientRect();
  const currentCenterX = currentRect.left + currentRect.width / 2;
  const rows = [];
  for (const card of cards) {
    const rect = card.getBoundingClientRect();
    let row = rows.find((item) => Math.abs(item.top - rect.top) <= 8);
    if (!row) {
      row = { top: rect.top, cards: [] };
      rows.push(row);
    }
    row.cards.push({ card, rect });
  }
  rows.sort((left, right) => left.top - right.top);
  const rowIndex = rows.findIndex((row) => row.cards.some((item) => item.card === current));
  const targetRow = rows[rowIndex + direction];
  if (!targetRow) {
    return currentIndex;
  }
  const target = targetRow.cards.reduce((best, item) => {
    const distance = Math.abs((item.rect.left + item.rect.width / 2) - currentCenterX);
    return !best || distance < best.distance ? { card: item.card, distance } : best;
  }, null);
  return Math.max(0, cards.indexOf(target?.card));
}

function moveKeyboardCardOrder(event, interaction) {
  const cards = getCardOrderCards(interaction.grid);
  const currentIndex = cards.indexOf(interaction.card);
  if (currentIndex < 0) {
    return;
  }
  let targetIndex = currentIndex;
  if (event.key === 'ArrowLeft') targetIndex = Math.max(0, currentIndex - 1);
  if (event.key === 'ArrowRight') targetIndex = Math.min(cards.length - 1, currentIndex + 1);
  if (event.key === 'ArrowUp') targetIndex = findVerticalKeyboardTarget(cards, currentIndex, -1);
  if (event.key === 'ArrowDown') targetIndex = findVerticalKeyboardTarget(cards, currentIndex, 1);
  if (event.key === 'Home') targetIndex = 0;
  if (event.key === 'End') targetIndex = cards.length - 1;
  if (targetIndex === currentIndex) {
    announceCardOrder(`${interaction.label} 已在该方向的边界`);
    return;
  }
  const reordered = cards.slice();
  reordered.splice(currentIndex, 1);
  reordered.splice(targetIndex, 0, interaction.card);
  reorderCardGridInstantly(
    interaction.grid,
    reordered.map((card) => card.dataset.cardOrderId || ''),
  );
  state.cardOrder[interaction.scope] = mergeVisibleCardOrder(
    interaction.scope,
    getGridCardOrder(interaction.grid),
    state.cardOrder[interaction.scope],
  );
  applyCardOrderToRenderedGrids(interaction.scope);
  interaction.card.focus({ preventScroll: true });
  announceCardOrder(
    `${interaction.label} 已移动到 ${getCardOrderPositionLabel(interaction.grid, interaction.id)}`,
  );
}

async function handleCardOrderKeydown(event) {
  const card = event.target instanceof Element
    ? event.target.closest('[data-card-order-id]')
    : null;
  if (!card || event.target !== card) {
    return;
  }
  const interaction = state.cardOrder.keyboard;
  if (event.key === ' ' || event.key === 'Enter') {
    event.preventDefault();
    if (!interaction) {
      startKeyboardCardOrder(card);
      return;
    }
    if (interaction.card !== card) {
      return;
    }
    const nextOrder = normalizeClientCardOrder(state.cardOrder[interaction.scope]);
    clearKeyboardCardOrderPresentation(interaction);
    state.cardOrder.keyboard = null;
    await saveCardOrder(interaction.scope, nextOrder, {
      fallbackOrder: interaction.originalFullOrder,
      focusId: interaction.id,
      focusGrid: interaction.grid,
    });
    finishCardOrderInteraction();
    return;
  }
  if (!interaction || interaction.card !== card) {
    return;
  }
  if (event.key === 'Escape') {
    event.preventDefault();
    cancelKeyboardCardOrder();
    return;
  }
  if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    moveKeyboardCardOrder(event, interaction);
  }
}

function cancelActiveCardOrderInteraction({ announce = false } = {}) {
  if (state.cardOrder.pointer) {
    cancelCardOrderPointerDrag({ restore: true, announce });
  }
  if (state.cardOrder.keyboard) {
    cancelKeyboardCardOrder({ announce });
  }
}

function setupCardOrderInteractions() {
  for (const { grid, scope, page } of CARD_ORDER_GRIDS) {
    grid.dataset.cardOrderGrid = 'true';
    grid.dataset.cardOrderScope = scope;
    grid.dataset.cardOrderPage = page;
    grid.addEventListener('pointerdown', beginCardOrderPointer);
    grid.addEventListener('keydown', (event) => {
      handleCardOrderKeydown(event).catch((error) => {
        showToast(error.message || '键盘排序失败', 'error');
      });
    });
  }
  window.addEventListener('pointermove', moveCardOrderPointer, { passive: false });
  window.addEventListener('pointerup', (event) => {
    commitCardOrderPointer(event).catch((error) => {
      showToast(error.message || '卡片排序失败', 'error');
      cancelCardOrderPointerDrag({ restore: true });
    });
  });
  window.addEventListener('pointercancel', (event) => {
    if (state.cardOrder.pointer?.pointerId === event.pointerId) {
      cancelCardOrderPointerDrag({ restore: true });
    }
  });
  window.addEventListener('lostpointercapture', (event) => {
    const drag = state.cardOrder.pointer;
    if (drag && drag.pointerId === event.pointerId && !drag.finishing) {
      cancelCardOrderPointerDrag({ restore: true });
    }
  }, true);
}

function renderPerformanceBackpressure(diagnostics) {
  const hostPerformance = diagnostics?.performance || {};
  const eventLoop = hostPerformance.event_loop_lag_ms || {};
  const logging = hostPerformance.logging || {};
  const loopP99 = Number(eventLoop.p99) || 0;
  const loopMax = Number(eventLoop.max) || 0;
  const logDepth = (Number(logging.normal_depth) || 0) + (Number(logging.critical_depth) || 0);
  const logCapacity = (Number(logging.normal_capacity) || 0) + (Number(logging.critical_capacity) || 0);
  const logHighWater = (Number(logging.normal_high_water) || 0) + (Number(logging.critical_high_water) || 0);
  const droppedLogs = Object.values(logging.dropped || {}).reduce((total, value) => total + (Number(value) || 0), 0);
  const items = Array.isArray(diagnostics?.items) ? diagnostics.items : [];
  let warningCount = 0;

  elements.performanceEventLoop.textContent = `${loopP99.toFixed(1)} ms / ${loopMax.toFixed(1)} ms`;
  const loopHealthy = loopP99 <= 25 && loopMax <= 100;
  elements.performanceEventLoopStatus.textContent = loopHealthy ? '延迟在目标范围内' : '延迟超过目标，请检查阻塞任务';
  elements.performanceEventLoopStatus.dataset.tone = loopHealthy ? 'healthy' : 'warning';
  if (!loopHealthy) warningCount += 1;

  elements.performanceLoggingQueue.textContent = `${logDepth} / ${logCapacity} · ${logHighWater}`;
  const loggingHealthy = Boolean(logging.listener_alive) && droppedLogs === 0 && (!logCapacity || logDepth / logCapacity < 0.75);
  elements.performanceLoggingStatus.textContent = loggingHealthy
    ? '监听线程正常，未发生降级'
    : `需要关注 · 丢弃 ${droppedLogs} 条${logging.last_error ? ' · 写入异常' : ''}`;
  elements.performanceLoggingStatus.dataset.tone = loggingHealthy ? 'healthy' : 'warning';
  if (!loggingHealthy) warningCount += 1;

  const signature = JSON.stringify(items.map((item) => ({
    id: item.bot_id,
    name: item.client_name,
    performance: item.performance,
  })));
  const itemIsOverloaded = (item) => {
    const performance = item.performance || {};
    const ingress = performance.ingress || {};
    const actions = performance.onebot_actions || {};
    const persistence = performance.persistence || {};
    const plugins = performance.plugins || {};
    const ingressCapacity = Number(ingress.capacity) || 0;
    const actionCapacity = Number(actions.capacity) || 0;
    const persistenceCapacity = Number(persistence.capacity) || 0;
    return (Number(ingress.overload_dropped) || 0) > 0
      || (ingressCapacity > 0 && (Number(ingress.depth) || 0) / ingressCapacity >= 0.75)
      || (actionCapacity > 0 && (Number(actions.depth) || 0) / actionCapacity >= 0.75)
      || (persistenceCapacity > 0 && (Number(persistence.backlog) || 0) / persistenceCapacity >= 0.75)
      || persistence.writer_alive === false
      || Boolean(persistence.last_error)
      || (Number(plugins.open_circuits) || 0) > 0;
  };
  warningCount += items.filter(itemIsOverloaded).length;
  if (state.diagnostics.performanceSignature !== signature) {
    state.diagnostics.performanceSignature = signature;
    const fragment = document.createDocumentFragment();
    for (const item of items) {
      const performance = item.performance || {};
      const ingress = performance.ingress || {};
      const actions = performance.onebot_actions || {};
      const persistence = performance.persistence || {};
      const plugins = performance.plugins || {};
      const ingressCapacity = Number(ingress.capacity) || 0;
      const actionCapacity = Number(actions.capacity) || 0;
      const persistenceCapacity = Number(persistence.capacity) || 0;
      const overloaded = itemIsOverloaded(item);
      const card = document.createElement('article');
      card.className = 'performance-bot-card';
      card.dataset.tone = overloaded ? 'warning' : 'healthy';
      card.innerHTML = `
        <header>
          <div><strong>${escapeHtml(item.client_name || item.bot_id || 'Bot')}</strong><small>${escapeHtml(item.bot_id || '-')}</small></div>
          <span>${overloaded ? '需要关注' : '运行正常'}</span>
        </header>
        <dl>
          <div><dt>入站队列</dt><dd>${Number(ingress.depth) || 0} / ${ingressCapacity} · p99 ${Number(ingress.wait_p99_ms || 0).toFixed(1)} ms</dd></div>
          <div><dt>过载丢弃</dt><dd>${Number(ingress.overload_dropped) || 0}</dd></div>
          <div><dt>OneBot action</dt><dd>${Number(actions.depth) || 0} / ${actionCapacity} · 拒绝 ${Number(actions.busy_rejected) || 0}</dd></div>
          <div><dt>持久化</dt><dd>${Number(persistence.backlog) || 0} / ${persistenceCapacity} · p99 ${Number(persistence.enqueue_wait_p99_ms || 0).toFixed(1)} ms</dd></div>
          <div><dt>插件</dt><dd>p99 ${Number(plugins.dispatch_p99_ms || 0).toFixed(1)} ms · 熔断 ${Number(plugins.open_circuits) || 0}</dd></div>
        </dl>
      `;
      fragment.appendChild(card);
    }
    elements.performanceBotGrid.replaceChildren(fragment);
  }

  elements.performanceOverallBadge.textContent = warningCount ? `${warningCount} 项需要关注` : '运行正常';
  elements.performanceOverallBadge.dataset.tone = warningCount ? 'warning' : 'healthy';
}

function renderDiagnostics(payload) {
  if (shouldDeferCardOrderRender('diagnostics', payload)) {
    return;
  }
  const diagnostics = payload || {};
  const host = diagnostics.host || null;
  const hostCache = diagnostics.host_cache || null;
  const resourceBuffers = diagnostics.resource_buffers || {};
  const summary = diagnostics.summary || {};
  const items = Array.isArray(diagnostics.items) ? diagnostics.items : [];
  reconcileClientCardOrder('bots', items.map((item) => item.bot_id), { authoritative: false });
  const orderedItems = orderItemsForCards(items, 'bots', (item) => item.bot_id);
  state.diagnostics.data = diagnostics;
  state.diagnostics.loaded = true;

  elements.diagnosticsSnapshotTime.textContent = host
    ? (host.snapshot_timestamp ? formatDiagnosticTime(host.snapshot_timestamp) : (host.snapshot_time || '-'))
    : '主机快照不可用';
  elements.diagnosticsHostNote.textContent = host
    ? `${host.system_label || '-'} · Python ${host.python_version || '-'} · 主机 ${host.hostname || '-'}`
    : (diagnostics.host_error || '当前无法获取主机诊断快照。');
  const logBuffer = resourceBuffers.logs || {};
  const terminalBuffer = resourceBuffers.terminals || {};
  const resourceSuffix = ` · 日志 ${formatDiagnosticBytes(logBuffer.bytes)} / ${formatDiagnosticBytes(logBuffer.max_bytes)} · 终端 ${terminalBuffer.active_sessions ?? 0}/${terminalBuffer.max_sessions ?? 0}`;
  elements.diagnosticsCacheNote.textContent = (host
    ? `采样状态: ${getDiagnosticsCacheStatusLabel(hostCache)} · 快照年龄 ${formatDiagnosticDuration(hostCache?.snapshot_age_seconds)} · TTL ${formatDiagnosticDuration(hostCache?.cache_ttl_seconds)}`
    : `采样状态: ${getDiagnosticsCacheStatusLabel(hostCache)} · TTL ${formatDiagnosticDuration(hostCache?.cache_ttl_seconds)}`) + resourceSuffix;

  const cpuPercent = clampDiagnosticPercent(host?.cpu_usage_percent);
  const cpuProcessPercent = Math.min(cpuPercent, clampDiagnosticPercent(host?.process_cpu_usage_percent));
  const memoryPercent = clampDiagnosticPercent(host?.memory_usage_percent);
  const memoryProcessPercent = Math.min(memoryPercent, clampDiagnosticPercent(host?.process_memory_percent));
  const cpuProcessVisiblePercent = Math.min(cpuPercent, getDiagnosticVisibleInnerPercent(cpuProcessPercent));
  const memoryProcessVisiblePercent = Math.min(memoryPercent, getDiagnosticVisibleInnerPercent(memoryProcessPercent));
  const initializeMetersInstantly = !state.diagnostics.metersInitialized && resolveMotion('auto') === 'instant';
  if (initializeMetersInstantly) {
    for (const card of document.querySelectorAll('.diagnostics-meter-card')) {
      card.dataset.initialized = 'true';
    }
  }
  setDiagnosticsMeter(elements.diagnosticsCpuRing, cpuPercent);
  setDiagnosticsMeter(elements.diagnosticsCpuProcessRing, cpuProcessVisiblePercent);
  setDiagnosticsMeter(elements.diagnosticsMemoryRing, memoryPercent);
  setDiagnosticsMeter(elements.diagnosticsMemoryProcessRing, memoryProcessVisiblePercent);
  if (!state.diagnostics.metersInitialized) {
    state.diagnostics.metersInitialized = true;
    window.setTimeout(() => {
      for (const card of document.querySelectorAll('.diagnostics-meter-card')) {
        card.dataset.initialized = 'true';
      }
    }, initializeMetersInstantly ? 0 : 180);
  }

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

  renderPerformanceBackpressure(diagnostics);
  const renderSignature = JSON.stringify(orderedItems);
  if (state.diagnostics.renderSignature === renderSignature) {
    return;
  }
  state.diagnostics.renderSignature = renderSignature;

  elements.diagnosticsEmptyState.classList.toggle('hidden', items.length > 0);
  elements.diagnosticsGrid.innerHTML = '';

  for (const item of orderedItems) {
    const card = document.createElement('article');
    const tone = getDiagnosticStatusTone(item.status_code);
    const onebotStatus = item.onebot_connected === true
      ? '已连接'
      : item.onebot_waiting_for_upstream === true
        ? `等待上游 · 每 ${Number(item.onebot_retry_delay_seconds) || 5} 秒重试`
        : '未连接';
    const disconnectRow = item.last_disconnect_reason
      ? `
        <div class="diagnostics-row">
          <span>最近断开</span>
          <strong>${escapeHtml(item.last_disconnect_reason)}</strong>
        </div>`
      : '';
    card.className = 'diagnostics-card';
    configureCardOrderCard(
      card,
      'bots',
      item.bot_id,
      item.client_name || item.bot_id || 'Bot',
    );
    card.innerHTML = `
      ${buildCardOrderDragSurface(item.client_name || item.bot_id || 'Bot')}
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
          <span>Rocket.Chat 重连失败</span>
          <strong>${escapeHtml(String(item.reconnect_failures ?? 0))}</strong>
        </div>
        <div class="diagnostics-row">
          <span>OneBot 上游</span>
          <strong>${escapeHtml(onebotStatus)}</strong>
        </div>
        <div class="diagnostics-row">
          <span>入站队列</span>
          <strong>${escapeHtml(`${item.inbound_queue_depth ?? 0} / ${item.inbound_queue_capacity ?? 0} · ${item.inbound_worker_count ?? 0} workers`)}</strong>
        </div>
        <div class="diagnostics-row">
          <span>OneBot 出站队列</span>
          <strong>${escapeHtml(`${item.outgoing_queue_depth ?? 0} / ${item.outgoing_queue_max_entries ?? 0}`)}</strong>
        </div>
        <div class="diagnostics-row">
          <span>OneBot 丢弃事件</span>
          <strong>${escapeHtml(String(item.onebot_dropped_event_count ?? 0))}</strong>
        </div>
        <div class="diagnostics-row">
          <span>用户 / 房间缓存</span>
          <strong>${escapeHtml(`${item.user_cache_entries ?? 0}/${item.user_cache_capacity ?? 0} · ${item.room_cache_entries ?? 0}/${item.room_cache_capacity ?? 0}`)}</strong>
        </div>
        <div class="diagnostics-row">
          <span>身份缓存</span>
          <strong>${escapeHtml(`${item.identity_cache?.by_user_entries ?? 0}/${item.identity_cache?.max_entries ?? 0} · hit ${item.identity_cache?.hits ?? 0} / miss ${item.identity_cache?.misses ?? 0}`)}</strong>
        </div>
        <div class="diagnostics-row">
          <span>媒体缓存</span>
          <strong>${escapeHtml(`${item.media_cache?.file_count ?? 0} 个 · ${formatDiagnosticBytes(item.media_cache?.total_bytes)}`)}</strong>
        </div>
        <div class="diagnostics-row">
          <span>Runtime 重载</span>
          <strong>${escapeHtml(String(item.runtime_restart_count ?? 0))}</strong>
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
  syncCardOrderScopeBusy('bots');
}

function renderBasicInfo(payload) {
  if (shouldDeferCardOrderRender('basic', payload)) {
    return;
  }
  const summary = payload?.summary || {};
  const items = Array.isArray(payload?.items) ? payload.items : [];
  reconcileClientCardOrder('bots', items.map((item) => item.bot_id), { authoritative: false });
  const orderedItems = orderItemsForCards(items, 'bots', (item) => item.bot_id);
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

  for (const item of orderedItems) {
    const card = document.createElement('article');
    const statusTone = getBasicStatusTone(item.status_code);
    const serverDisplayName = item.server_display_name || '';
    const serverAvatarUrl = item.server_avatar_url || '';
    card.className = 'basic-info-card';
    configureCardOrderCard(
      card,
      'bots',
      item.bot_id,
      item.client_name || item.bot_id || 'Bot',
    );
    card.innerHTML = `
      ${buildCardOrderDragSurface(item.client_name || item.bot_id || 'Bot')}
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
  syncCardOrderScopeBusy('bots');
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
  const performanceFields = [
    ['settingsPerformanceProfileInput', settings.performance_profile || 'balanced'],
    ['settingsInboundWorkerCountInput', settings.inbound_worker_count ?? 0],
    ['settingsOnebotQueueMaxInput', settings.onebot_outgoing_queue_max_entries ?? 512],
    ['settingsIdentityCacheMaxInput', settings.identity_cache_max_entries ?? 4096],
    ['settingsMediaCacheMaxBytesInput', settings.media_cache_max_bytes ?? 1073741824],
    ['settingsMediaCacheMaxAgeInput', settings.media_cache_max_age_hours ?? 168],
    ['settingsLogFileMaxBytesInput', settings.log_file_max_bytes ?? 10485760],
    ['settingsLogFileBackupCountInput', settings.log_file_backup_count ?? 3],
    ['settingsTerminalMaxSessionsInput', settings.terminal_max_sessions ?? 6],
    ['settingsTerminalIdleTimeoutInput', settings.terminal_idle_timeout_seconds ?? 0],
  ];
  for (const [fieldName, value] of performanceFields) {
    if (elements[fieldName]) {
      elements[fieldName].value = String(value);
    }
  }
}

function getStoredUpdateTransaction() {
  try {
    return String(window.localStorage.getItem(UPDATE_TRANSACTION_STORAGE_KEY) || '').trim();
  } catch (_error) {
    return '';
  }
}

function relocateMessageIndexSettingsSection() {
  const messageIndexInput = elements.settingsMessageIndexMaxEntriesInput;
  const messageIndexHint = elements.settingsMessageIndexHint;
  const rebuildButton = elements.settingsMessageIndexRebuildButton;
  const performanceSaveButton = elements.settingsPerformanceSaveButton;
  if (!messageIndexInput || !messageIndexHint || !rebuildButton || !performanceSaveButton) {
    return;
  }

  const legacySection = messageIndexInput.closest('.form-section');
  const advancedSection = performanceSaveButton.closest('.form-section');
  if (!legacySection || !advancedSection || legacySection === advancedSection) {
    return;
  }

  if (legacySection.parentElement === advancedSection) {
    return;
  }

  const advancedActions = performanceSaveButton.parentElement;
  const messageIndexField = messageIndexInput.closest('.field-block');
  const legacySaveButton = document.getElementById('settingsMessageIndexSaveButton');
  const terminalIdleLabel = elements.settingsTerminalIdleTimeoutInput
    ?.closest('.field-block')
    ?.querySelector('span');
  if (!advancedActions || !messageIndexField) {
    return;
  }

  if (messageIndexField.parentElement !== advancedSection) {
    advancedSection.insertBefore(messageIndexField, messageIndexHint);
  }

  if (messageIndexHint.parentElement !== advancedSection) {
    advancedSection.insertBefore(messageIndexHint, advancedActions);
  }

  advancedSection.insertBefore(messageIndexField, messageIndexHint);

  let terminalIdleHint = advancedSection.querySelector('#settingsTerminalIdleHint');
  if (!terminalIdleHint) {
    terminalIdleHint = document.createElement('p');
    terminalIdleHint.id = 'settingsTerminalIdleHint';
    terminalIdleHint.className = 'settings-helper settings-helper-compact';
  }
  terminalIdleHint.textContent = '“终端空闲关闭”只作用于 WebUI 的系统终端会话，不会关闭 RocketCatShell 进程；仅在终端没有任何连接时才会自动回收。设置为 0 等于不限制时间。';
  advancedSection.insertBefore(terminalIdleHint, advancedActions);

  if (rebuildButton.parentElement !== advancedActions) {
    advancedActions.classList.add('settings-actions-spacious');
    advancedActions.insertBefore(rebuildButton, performanceSaveButton);
  }

  if (terminalIdleLabel) {
    terminalIdleLabel.textContent = '终端空闲关闭（秒，0=不限制）';
  }
  elements.settingsTerminalIdleTimeoutInput?.setAttribute('min', '0');
  elements.settingsTerminalIdleTimeoutInput?.setAttribute('step', '60');
  elements.settingsTerminalIdleTimeoutInput?.setAttribute(
    'placeholder',
    '0 等于不限制时间，仅影响 WebUI 终端会话',
  );

  legacySaveButton?.remove();
  legacySection.remove();
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

function createLogEntry(item) {
  const entry = document.createElement('div');
  entry.className = `log-entry log-${String(item.level || 'INFO').toLowerCase()}`;
  entry.dataset.logId = String(item.id || '');
  entry.innerHTML = `
    <span class="log-entry-level">${escapeHtml(item.level)}</span>
    <span class="log-entry-line">${escapeHtml(item.line)}</span>
  `;
  return entry;
}

function renderLogNavigationState() {
  const nearBottom = isLogConsoleNearBottom();
  if (nearBottom) {
    state.logs.unreadCount = 0;
  }
  if (elements.logUnreadCount) {
    elements.logUnreadCount.textContent = String(state.logs.unreadCount);
  }
  elements.logBackToBottomButton?.classList.toggle(
    'hidden',
    nearBottom && state.logs.unreadCount === 0,
  );
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

  const visibleIds = visibleItems.map((item) => String(item.id || ''));
  const existingIds = Array.from(elements.logConsole.querySelectorAll('[data-log-id]'))
    .map((entry) => entry.dataset.logId || '');
  const canAppend = existingIds.length <= visibleIds.length
    && existingIds.every((id, index) => id === visibleIds[index]);
  const previousScrollTop = elements.logConsole.scrollTop;

  if (!visibleItems.length) {
    elements.logConsole.innerHTML = '<div class="log-empty">暂时还没有 Shell 或桥接器实时日志。</div>';
  } else if (canAppend && existingIds.length > 0) {
    const fragment = document.createDocumentFragment();
    for (const item of visibleItems.slice(existingIds.length)) {
      fragment.appendChild(createLogEntry(item));
    }
    elements.logConsole.appendChild(fragment);
  } else {
    const fragment = document.createDocumentFragment();
    for (const item of visibleItems) {
      fragment.appendChild(createLogEntry(item));
    }
    elements.logConsole.replaceChildren(fragment);
    if (!scrollToBottom || !state.logs.autoScroll) {
      elements.logConsole.scrollTop = previousScrollTop;
    }
  }

  elements.logMeta.textContent = `实时日志 · 缓存 ${state.logs.items.length}/${state.logs.maxEntries} 条`;

  for (const button of elements.logFilterButtons) {
    const level = button.dataset.logLevel;
    const active = state.logs.activeLevels.has(level);
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  }
  if (elements.logPerfButton) {
    elements.logPerfButton.classList.toggle('active', state.logs.showPerf);
    elements.logPerfButton.setAttribute('aria-pressed', String(state.logs.showPerf));
  }

  renderLogAutoScrollState();

  if (scrollToBottom && state.logs.autoScroll) {
    elements.logConsole.scrollTop = elements.logConsole.scrollHeight;
    state.logs.unreadCount = 0;
  }
  renderLogNavigationState();
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

  if (incoming.length && !state.logs.autoScroll) {
    state.logs.unreadCount += incoming.length;
  }
  renderLogs({ scrollToBottom: incoming.length > 0 });
}

function setLogAutoScroll(enabled) {
  state.logs.autoScroll = Boolean(enabled);
  renderLogAutoScrollState();
  if (state.logs.autoScroll && elements.logConsole) {
    elements.logConsole.scrollTop = elements.logConsole.scrollHeight;
    state.logs.unreadCount = 0;
  }
  renderLogNavigationState();
}

async function clearLogs() {
  const confirmed = await askForConfirmation({
    title: '清空实时日志？',
    message: '这会同时重置服务端缓存和当前页面日志视图。',
    confirmLabel: '清空日志',
    kind: 'danger',
  });
  if (!confirmed) {
    return;
  }

  const payload = await requestJson('/api/logs/clear', {
    method: 'POST',
  });

  state.logs.generation += 1;
  state.logs.items = [];
  state.logs.lastId = 0;
  state.logs.unreadCount = 0;
  state.logs.maxEntries = Number(payload.max_entries) || state.logs.maxEntries;
  renderLogs();
  showToast(`已清空 ${Number(payload.cleared) || 0} 条日志`, 'success');
}

function startLogPolling() {
  if (state.logs.polling || document.hidden || state.currentPage !== 'logs') {
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
      if (state.logs.polling && !document.hidden && state.currentPage === 'logs') {
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
  if (shouldDeferCardOrderRender('network', items)) {
    return;
  }
  state.bots = items;
  reconcileClientCardOrder('bots', items.map((item) => item.id), { authoritative: true });
  const orderedItems = orderItemsForCards(items, 'bots', (item) => item.id);
  const renderSignature = JSON.stringify(orderedItems);
  if (state.network.renderSignature === renderSignature) {
    return;
  }
  state.network.renderSignature = renderSignature;
  elements.botGrid.innerHTML = '';
  elements.emptyState.classList.toggle('hidden', items.length > 0);
  if (elements.botListSummary) {
    const enabledCount = items.filter((item) => item.enabled).length;
    elements.botListSummary.textContent = `${items.length} 个 Bot · ${enabledCount} 个启用`;
  }

  for (const bot of orderedItems) {
    const card = document.createElement('article');
    card.className = 'bot-card';
    configureCardOrderCard(card, 'bots', bot.id, bot.name || bot.id || 'Bot');
    card.innerHTML = `
      ${buildCardOrderDragSurface(bot.name || bot.id || 'Bot')}
      <div class="bot-card-header">
        <div>
          <span class="card-chip">${escapeHtml(bot.name || '未命名 Bot')}</span>
          <p class="card-type">WS bot 客户端</p>
        </div>
        <label class="field-switch compact-switch">
          <span class="visually-hidden">${escapeHtml(bot.enabled ? `停用 ${bot.name || '未命名 Bot'}` : `启用 ${bot.name || '未命名 Bot'}`)}</span>
          <input type="checkbox" aria-label="${escapeHtml(bot.enabled ? `停用 ${bot.name || '未命名 Bot'}` : `启用 ${bot.name || '未命名 Bot'}`)}" ${bot.enabled ? 'checked' : ''} data-role="toggle" data-id="${bot.id}" />
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
  syncCardOrderScopeBusy('bots');
}

function formatPluginBadge(value, fallback = '-') {
  const normalized = String(value || '').trim();
  return normalized || fallback;
}

function renderPlugins(payload) {
  if (shouldDeferCardOrderRender('plugins', payload)) {
    return;
  }
  const items = Array.isArray(payload?.items) ? payload.items : [];
  reconcileClientCardOrder('plugins', items.map((item) => item.id), { authoritative: true });
  const orderedItems = orderItemsForCards(items, 'plugins', (item) => item.id);
  state.plugins.items = items;
  elements.pluginCount.textContent = String(items.length);
  elements.pluginEnabledCount.textContent = String(items.filter((item) => item.activated).length);
  elements.pluginGrid.innerHTML = '';
  elements.pluginEmptyState.classList.toggle('hidden', items.length > 0);

  for (const item of orderedItems) {
    const card = document.createElement('article');
    card.className = `plugin-card ${item.activated ? '' : 'is-disabled'}`;
    configureCardOrderCard(
      card,
      'plugins',
      item.id,
      item.display_name || item.name || item.id || '插件',
    );
    const encodedId = encodeURIComponent(item.id);
    const logoMarkup = item.has_logo
      ? `<img class="plugin-logo-image" src="/api/plugins/${encodedId}/logo" alt="${escapeHtml(item.display_name || item.name || item.id)}" loading="lazy" />`
      : `<span class="plugin-logo-fallback">${escapeHtml((item.display_name || item.name || item.id || '?').trim().charAt(0) || '?')}</span>`;

    card.innerHTML = `
      ${buildCardOrderDragSurface(item.display_name || item.name || item.id || '插件')}
      <div class="plugin-card-header">
        <div class="plugin-card-title-row">
          <div class="plugin-logo-shell">${logoMarkup}</div>
          <div class="plugin-title-block">
            <h3>${escapeHtml(item.display_name || item.name || item.id)}</h3>
            <p class="plugin-subtitle">${escapeHtml(item.name || item.id)}</p>
          </div>
        </div>
        <div class="plugin-switch-shell">
          <label class="field-switch compact-switch">
            <span class="visually-hidden">${escapeHtml(item.activated ? `停用 ${item.display_name || item.name || item.id}` : `启用 ${item.display_name || item.name || item.id}`)}</span>
            <input type="checkbox" aria-label="${escapeHtml(item.activated ? `停用 ${item.display_name || item.name || item.id}` : `启用 ${item.display_name || item.name || item.id}`)}" ${item.activated ? 'checked' : ''} data-plugin-role="toggle" data-id="${escapeHtml(item.id)}" />
            <i></i>
          </label>
        </div>
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
        ${item.has_dashboard ? `
          <button
            class="plugin-dashboard-launch"
            type="button"
            data-plugin-role="dashboard"
            data-id="${escapeHtml(item.id)}"
            aria-label="打开插件 UI 界面"
            title="${item.activated ? '打开插件 UI 界面' : '启用插件后可打开 UI 界面'}"
            ${item.activated ? '' : 'disabled'}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M21 16V4H3V16H21M21 2C22.1 2 23 2.9 23 4V16C23 17.1 22.1 18 21 18H14V20H16V22H8V20H10V18H3C1.89 18 1 17.1 1 16V4C1 2.89 1.89 2 3 2H21M5 6V12H8V6H5M10 6V9H13V6H10M15 6V12H18V6H15M10 11V12H13V11H10Z" />
            </svg>
          </button>
        ` : ''}
        <button class="action-chip" type="button" data-plugin-role="settings" data-id="${escapeHtml(item.id)}">设置</button>
        <button class="action-chip" type="button" data-plugin-role="reload" data-id="${escapeHtml(item.id)}">重载</button>
        <button class="action-chip danger" type="button" data-plugin-role="uninstall" data-id="${escapeHtml(item.id)}">卸载</button>
      </div>
    `;
    elements.pluginGrid.appendChild(card);
  }
  syncCardOrderScopeBusy('plugins');
}

const PLUGIN_DASHBOARD_CHANNEL = 'rocketcat-plugin-dashboard';

function normalizePluginDashboardPath(path) {
  const raw = String(path || '').trim().replaceAll('\\', '/');
  if (!raw || raw.startsWith('/') || /^[a-z][a-z0-9+.-]*:/i.test(raw)) {
    throw new Error('Dashboard API 必须使用相对路径');
  }
  const parts = raw.split('/').filter(Boolean);
  if (!parts.length || parts.some((part) => part === '.' || part === '..')) {
    throw new Error('Dashboard API 路径无效');
  }
  return parts.join('/');
}

function appendPluginDashboardQuery(url, query = {}) {
  const target = new URL(url, window.location.origin);
  for (const [key, value] of Object.entries(query || {})) {
    const values = Array.isArray(value) ? value : [value];
    for (const item of values) {
      if (item !== undefined && item !== null) {
        target.searchParams.append(key, String(item));
      }
    }
  }
  return `${target.pathname}${target.search}`;
}

function buildPluginDashboardApiUrl(kind, path, query = {}) {
  const pluginId = state.pluginDashboard.plugin?.id;
  if (!pluginId) {
    throw new Error('Dashboard 会话尚未建立');
  }
  const normalizedPath = normalizePluginDashboardPath(path);
  return appendPluginDashboardQuery(
    `/api/plugins/${encodeURIComponent(pluginId)}/dashboard/${kind}/${normalizedPath
      .split('/')
      .map((part) => encodeURIComponent(part))
      .join('/')}`,
    query,
  );
}

function postPluginDashboardMessage(message) {
  const target = elements.pluginDashboardFrame?.contentWindow;
  if (!target) {
    return;
  }
  target.postMessage(
    {
      channel: PLUGIN_DASHBOARD_CHANNEL,
      ...message,
    },
    '*',
  );
}

function buildPluginDashboardContext() {
  const plugin = state.pluginDashboard.plugin || {};
  return {
    product: 'RocketCatShell',
    version: state.status?.version || '-',
    plugin: {
      id: plugin.id || '',
      name: plugin.name || '',
      display_name: plugin.display_name || plugin.name || plugin.id || '',
      version: plugin.version || '',
    },
    page: state.pluginDashboard.page,
    pages: Array.isArray(plugin.pages) ? plugin.pages : [],
    locale: document.documentElement.lang || 'zh-CN',
    theme: 'light',
  };
}

async function parsePluginDashboardResponse(response) {
  if (response.status === 401) {
    window.location.replace('/');
    throw new Error('登录已失效，请重新登录');
  }
  const contentType = String(response.headers.get('content-type') || '').toLowerCase();
  const payload = contentType.includes('application/json')
    ? await response.json().catch(() => ({}))
    : await response.text();
  if (!response.ok) {
    const detail = typeof payload === 'object'
      ? payload.detail || payload.error
      : payload;
    throw new Error(
      typeof detail === 'string'
        ? detail
        : detail?.message || `Dashboard 请求失败 (${response.status})`,
    );
  }
  return payload;
}

function formatUpdateTimestamp(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return '尚未检查';
  }
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(seconds * 1000));
}

function formatReleaseDate(value) {
  if (!value) {
    return '发布时间未知';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '发布时间未知';
  }
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(date);
}

function updateActionLabel(action) {
  if (action === 'update') {
    return '升级';
  }
  if (action === 'rollback') {
    return '回滚';
  }
  return '同版本重装';
}

function persistUpdateTransaction(transactionId) {
  const normalized = String(transactionId || '').trim();
  state.updates.transactionId = normalized;
  try {
    if (normalized) {
      window.localStorage.setItem(UPDATE_TRANSACTION_STORAGE_KEY, normalized);
    } else {
      window.localStorage.removeItem(UPDATE_TRANSACTION_STORAGE_KEY);
    }
  } catch (_error) {
    // Restricted browser storage must not interrupt a prepared update.
  }
}

function renderUpdateStatus(payload) {
  const status = payload || {};
  state.updates.status = status;
  state.updates.loaded = true;
  elements.updateCurrentVersion.textContent = status.current_version || '-';
  elements.updateLatestVersion.textContent = status.latest_version || status.current_version || '-';
  elements.updateCheckedAt.textContent = formatUpdateTimestamp(status.checked_at);

  const active = status.active_transaction;
  let badgeText = '已是最新';
  let badgeTone = 'current';
  let message = '已使用缓存完成检查；只有手动点击“检查更新”才会绕过缓存。';
  let messageTone = '';
  if (active) {
    badgeText = '事务进行中';
    badgeTone = 'available';
    message = `版本事务 ${active.transaction_id}：${active.stage || active.status || '处理中'}`;
    if (!state.updates.transactionId) {
      persistUpdateTransaction(active.transaction_id);
      showUpdateRestartOverlay(active.transaction_id);
      pollUpdateTransaction(active.transaction_id);
    }
  } else if (status.error) {
    badgeText = status.stale ? '离线缓存' : '检查失败';
    badgeTone = 'error';
    message = status.stale
      ? 'GitHub 暂不可用，当前显示最近一次缓存结果。'
      : '暂时无法连接官方更新源，请稍后手动重试。';
    messageTone = 'error';
  } else if (status.update_available) {
    badgeText = '发现新版本';
    badgeTone = 'available';
    message = `可升级至 ${status.latest_version}，选择版本后仍需二次确认。`;
    messageTone = 'success';
  }
  if (status.refresh_limited) {
    message = '手动检查间隔为 60 秒，本次继续显示最近结果。';
  }
  elements.updateAvailabilityBadge.textContent = badgeText;
  elements.updateAvailabilityBadge.className = `version-state-badge ${badgeTone}`;
  elements.updateStatusMessage.textContent = message;
  elements.updateStatusMessage.className = `version-status-message ${messageTone}`.trim();
}

async function loadUpdateStatus({ refresh = false, silent = false } = {}) {
  if (state.updates.loading) {
    return;
  }
  state.updates.loading = true;
  elements.updateCheckButton.disabled = true;
  elements.updateCheckButton.setAttribute('aria-busy', 'true');
  try {
    const payload = await requestJson(`/api/updates/status?refresh=${refresh ? 'true' : 'false'}`);
    renderUpdateStatus(payload);
  } catch (error) {
    state.updates.loaded = false;
    elements.updateAvailabilityBadge.textContent = '检查失败';
    elements.updateAvailabilityBadge.className = 'version-state-badge error';
    elements.updateStatusMessage.textContent = error.message || '更新状态加载失败';
    elements.updateStatusMessage.className = 'version-status-message error';
    if (!silent) {
      showToast(error.message || '更新状态加载失败', 'error');
    }
  } finally {
    state.updates.loading = false;
    elements.updateCheckButton.disabled = false;
    elements.updateCheckButton.setAttribute('aria-busy', 'false');
  }
}

function renderUpdateReleases(payload) {
  state.updates.releases = Array.isArray(payload?.releases) ? payload.releases : [];
  if (!state.updates.releases.length) {
    elements.updateReleaseList.innerHTML = '<div class="update-release-empty">暂无可切换的兼容版本。v0.2.1 及更早版本已按安全策略排除。</div>';
    return;
  }
  const currentVersion = state.updates.status?.current_version || '';
  elements.updateReleaseList.innerHTML = state.updates.releases.map((release) => {
    const action = release.action || 'reinstall';
    const channel = release.prerelease ? '预发布' : '稳定版';
    const notes = String(release.notes || '').trim() || '该版本未提供更新说明。';
    const size = Number(release.asset?.size || 0);
    const sizeLabel = size > 0 ? formatDiagnosticBytes(size) : '大小以下载时为准';
    const isCurrent = release.tag_name === currentVersion;
    return `
      <article class="update-release-card ${isCurrent ? 'current' : ''}">
        <div>
          <div class="update-release-heading">
            <strong>${escapeHtml(release.tag_name || '-')}</strong>
            <span class="release-channel-badge ${release.prerelease ? 'prerelease' : 'stable'}">${channel}</span>
            <span class="release-action-label ${escapeHtml(action)}">${updateActionLabel(action)}</span>
          </div>
          <p class="update-release-meta">${escapeHtml(formatReleaseDate(release.published_at))} · ${escapeHtml(sizeLabel)}</p>
          <p class="update-release-notes">${escapeHtml(notes)}</p>
        </div>
        <button class="action-button ${action === 'update' ? 'primary' : 'subtle'}" type="button" data-update-tag="${escapeHtml(release.tag_name || '')}">选择此版本</button>
      </article>
    `;
  }).join('');
}

async function openUpdateReleaseModal() {
  openDialog(elements.updateReleaseModal, { initialFocus: elements.updateReleaseCloseButton });
  elements.updateReleaseList.innerHTML = '<div class="update-release-empty">正在读取兼容版本…</div>';
  try {
    const payload = await requestJson('/api/updates/releases?refresh=false');
    renderUpdateReleases(payload);
  } catch (error) {
    elements.updateReleaseList.innerHTML = `<div class="update-release-empty">${escapeHtml(error.message || '版本列表加载失败')}</div>`;
  }
}

function closeUpdateReleaseModal() {
  requestDialogClose(elements.updateReleaseModal);
}

function promptUpdateSwitch(release) {
  if (!release) {
    return;
  }
  state.updates.pendingRelease = release;
  const action = release.action || 'reinstall';
  elements.updateConfirmAction.textContent = updateActionLabel(action);
  elements.updateConfirmAction.className = `release-action-label ${action}`;
  elements.updateConfirmVersion.textContent = release.tag_name || '-';
  elements.updateConfirmMessage.textContent = action === 'rollback'
    ? '当前容器会重启并回滚应用层。配置、Bot 数据、用户插件、日志和数据库不会被替换；目标启动失败时会自动恢复。容器重建仍会回到镜像版本。'
    : '当前容器会在下载和安全校验后重启。普通容器重启保留此版本，删除或重建容器会恢复镜像版本；持久数据和用户插件不会被替换。';
  openDialog(elements.updateConfirmModal, { initialFocus: elements.updateConfirmCancelButton });
}

function closeUpdateConfirmModal() {
  requestDialogClose(elements.updateConfirmModal);
  state.updates.pendingRelease = null;
}

function showUpdateRestartOverlay(transactionId) {
  openDialog(elements.updateRestartOverlay, {
    initialFocus: elements.updateRestartOverlay?.querySelector('.update-restart-panel'),
    backdropDismiss: false,
    blocking: true,
  });
  state.updates.overlayTransitionToken += 1;
  state.updates.overlayStage = 'preparing';
  elements.updateRestartSpinner.className = 'update-restart-spinner';
  elements.updateRestartTitle.textContent = '正在安全切换版本';
  elements.updateRestartMessage.textContent = '事务已建立，服务会在校验和备份完成后自动恢复连接。';
  elements.updateRestartTransaction.textContent = `事务 ${transactionId}`;
  elements.updateRestartRetryButton.classList.add('hidden');
  elements.updateRestartRetryButton.textContent = '重新检查';
  elements.updateRestartRetryButton.dataset.mode = 'poll';
  elements.updateRestartProgress.style.width = '';
  elements.updateRestartProgress.style.animation = '';
}

function setUpdateRestartVisual(stageKey, {
  title = null,
  message = '',
  tone = 'pending',
  motion = 'standard',
} = {}) {
  const sameStage = state.updates.overlayStage === stageKey;
  if (sameStage) {
    if (title && elements.updateRestartTitle.textContent !== title) {
      elements.updateRestartTitle.textContent = title;
    }
    if (message && elements.updateRestartMessage.textContent !== message) {
      elements.updateRestartMessage.textContent = message;
    }
    return;
  }
  state.updates.overlayStage = stageKey;
  const transitionToken = state.updates.overlayTransitionToken + 1;
  state.updates.overlayTransitionToken = transitionToken;
  const liveRegion = elements.updateRestartMessage.closest('.update-transaction-live')
    || elements.updateRestartMessage;
  const animatedNodes = [liveRegion];
  if (title && title !== elements.updateRestartTitle.textContent) {
    animatedNodes.push(elements.updateRestartTitle);
  }
  for (const node of animatedNodes) {
    node.getAnimations?.().forEach((animation) => animation.cancel());
  }

  const commit = () => {
    if (state.updates.overlayTransitionToken !== transitionToken) {
      return false;
    }
    if (title) {
      elements.updateRestartTitle.textContent = title;
    }
    if (message) {
      elements.updateRestartMessage.textContent = message;
    }
    elements.updateRestartSpinner.className = `update-restart-spinner${tone === 'pending' ? '' : ` ${tone}`}`;
    return true;
  };

  if (resolveMotion(motion) === 'instant' || !elements.updateRestartOverlay.open) {
    commit();
    return;
  }
  const exits = animatedNodes.map((node) => node.animate(
    [
      { opacity: 1, transform: 'translateY(0)' },
      { opacity: 0, transform: 'translateY(-4px)' },
    ],
    { duration: 120, easing: 'cubic-bezier(0.23, 1, 0.32, 1)', fill: 'forwards' },
  ));
  Promise.all(exits.map((animation) => animation.finished.catch(() => null))).then(() => {
    if (!commit()) {
      return;
    }
    for (const node of animatedNodes) {
      node.animate(
        [
          { opacity: 0, transform: 'translateY(8px)' },
          { opacity: 1, transform: 'translateY(0)' },
        ],
        { duration: 180, easing: 'cubic-bezier(0.23, 1, 0.32, 1)' },
      );
    }
  });
}

function updateRestartStage(transaction) {
  const stageMessages = {
    prepared: '更新包已通过校验，等待服务安全退出。',
    helper_started: '独立更新助手已启动。',
    waiting_for_shutdown: '正在优雅关闭 Bot、插件、终端和 WebUI。',
    forcing_shutdown: '优雅退出超时，正在核对进程身份后结束进程树。',
    backing_up: '正在备份本版本受管文件。',
    backup_complete: '事务备份已完成。',
    replacing: '正在替换 RocketCatShell 受管文件。',
    starting_target: '正在启动目标版本。',
    checking_target: '目标版本已启动，正在执行健康检查。',
    rolling_back: '目标版本未通过健康检查，正在自动回滚。',
  };
  const stage = transaction.stage || transaction.status || 'processing';
  setUpdateRestartVisual(`transaction:${stage}`, {
    message: stageMessages[transaction.stage] || `事务阶段：${transaction.stage || transaction.status || '处理中'}`,
  });
}

function saveUpdateOutcome(kind, message) {
  try {
    window.localStorage.setItem(
      UPDATE_OUTCOME_STORAGE_KEY,
      JSON.stringify({ kind, message }),
    );
  } catch (_error) {
    // The overlay still presents the outcome when storage is unavailable.
  }
}

function finishUpdatePolling(transaction) {
  const status = transaction.status;
  window.clearTimeout(state.updates.pollTimer);
  state.updates.pollTimer = null;
  if (status === 'completed') {
    persistUpdateTransaction('');
    const message = `RocketCatShell 已成功切换到 ${transaction.target_version}。`;
    saveUpdateOutcome('success', message);
    setUpdateRestartVisual('completed', {
      title: '版本切换完成',
      message,
      tone: 'complete',
    });
    elements.updateRestartProgress.style.width = '100%';
    elements.updateRestartProgress.style.animation = 'none';
    window.setTimeout(() => window.location.reload(), 900);
    return true;
  }
  if (status === 'rolled_back') {
    persistUpdateTransaction('');
    const message = `目标版本启动失败，已自动回滚到 ${transaction.current_version}。`;
    saveUpdateOutcome('error', message);
    setUpdateRestartVisual('rolled-back', {
      title: '已自动回滚',
      message,
      tone: 'error',
    });
    elements.updateRestartProgress.style.width = '100%';
    elements.updateRestartProgress.style.animation = 'none';
    window.setTimeout(() => window.location.reload(), 1400);
    return true;
  }
  if (status === 'recovery_required' || status === 'failed') {
    if (status === 'failed') {
      persistUpdateTransaction('');
      elements.updateRestartRetryButton.textContent = '返回版本管理';
      elements.updateRestartRetryButton.dataset.mode = 'dismiss';
    }
    setUpdateRestartVisual(status, {
      title: status === 'recovery_required' ? '需要人工恢复' : '版本事务未完成',
      message: transaction.error || transaction.rollback_error || '请查看日志并重新检查事务状态。',
      tone: 'error',
    });
    elements.updateRestartProgress.style.width = '100%';
    elements.updateRestartProgress.style.animation = 'none';
    elements.updateRestartRetryButton.classList.remove('hidden');
    elements.updateRestartOverlay.dataset.blocking = 'false';
    window.requestAnimationFrame(() => elements.updateRestartRetryButton.focus({ preventScroll: true }));
    return true;
  }
  return false;
}

async function pollUpdateTransaction(transactionId) {
  window.clearTimeout(state.updates.pollTimer);
  const poll = async () => {
    try {
      const health = await requestJson('/api/health', {
        cache: 'no-store',
        skipAuthRedirect: true,
      });
      if (health.status === 'ok') {
        const transaction = await requestJson(`/api/updates/transactions/${encodeURIComponent(transactionId)}`);
        updateRestartStage(transaction);
        if (finishUpdatePolling(transaction)) {
          return;
        }
        if (health.update_transaction !== transactionId) {
          setUpdateRestartVisual('waiting-for-target', {
            message: '旧服务正在退出，等待目标版本恢复连接。',
          });
        }
      } else {
        setUpdateRestartVisual('waiting-for-target', {
          message: '旧服务正在退出，等待目标版本恢复连接。',
        });
      }
    } catch (_error) {
      setUpdateRestartVisual('reconnecting', {
        message: '服务正在重启，浏览器会自动重新连接。',
      });
    }
    state.updates.pollTimer = window.setTimeout(poll, 1200);
  };
  await poll();
}

async function submitUpdateSwitch() {
  const release = state.updates.pendingRelease;
  if (!release) {
    return;
  }
  elements.updateConfirmSubmitButton.disabled = true;
  elements.updateConfirmSubmitButton.setAttribute('aria-busy', 'true');
  const previousLabel = elements.updateConfirmSubmitButton.textContent;
  elements.updateConfirmSubmitButton.textContent = '正在校验更新包…';
  try {
    const transaction = await requestJson('/api/updates/switch', {
      method: 'POST',
      body: JSON.stringify({ tag_name: release.tag_name }),
    });
    persistUpdateTransaction(transaction.transaction_id);
    closeDialog(elements.updateConfirmModal, { restoreFocus: false });
    closeDialog(elements.updateReleaseModal, { restoreFocus: false });
    showUpdateRestartOverlay(transaction.transaction_id);
    await pollUpdateTransaction(transaction.transaction_id);
  } finally {
    elements.updateConfirmSubmitButton.disabled = false;
    elements.updateConfirmSubmitButton.setAttribute('aria-busy', 'false');
    elements.updateConfirmSubmitButton.textContent = previousLabel;
  }
}

function consumeStoredUpdateOutcome() {
  try {
    const raw = window.localStorage.getItem(UPDATE_OUTCOME_STORAGE_KEY);
    if (!raw) {
      return;
    }
    window.localStorage.removeItem(UPDATE_OUTCOME_STORAGE_KEY);
    const outcome = JSON.parse(raw);
    window.setTimeout(() => {
      showToast(outcome.message || '版本事务已结束', outcome.kind || 'default');
    }, 400);
  } catch (_error) {
    // Ignore malformed or unavailable local storage.
  }
}

async function executePluginDashboardApi(payload) {
  const method = String(payload?.method || 'GET').toUpperCase();
  const headers = { ...(payload?.headers || {}) };
  const options = { method, headers };
  if (!['GET', 'HEAD'].includes(method) && payload?.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(payload.body);
  }
  const response = await fetch(
    buildPluginDashboardApiUrl('api', payload?.path, payload?.query),
    options,
  );
  return await parsePluginDashboardResponse(response);
}

async function executePluginDashboardUpload(payload) {
  const form = new FormData();
  for (const [key, value] of Object.entries(payload?.fields || {})) {
    const values = Array.isArray(value) ? value : [value];
    for (const item of values) {
      form.append(key, String(item ?? ''));
    }
  }
  for (const file of Array.from(payload?.files || [])) {
    if (file instanceof File) {
      form.append('files', file, file.name);
    } else if (file instanceof Blob) {
      form.append('files', file, 'upload.bin');
    }
  }
  const response = await fetch(
    buildPluginDashboardApiUrl('api', payload?.path),
    { method: 'POST', body: form },
  );
  return await parsePluginDashboardResponse(response);
}

async function executePluginDashboardDownload(payload) {
  const response = await fetch(
    buildPluginDashboardApiUrl('api', payload?.path, payload?.query),
  );
  if (!response.ok) {
    await parsePluginDashboardResponse(response);
    return;
  }
  const blob = await response.blob();
  const disposition = String(response.headers.get('content-disposition') || '');
  const fileNameMatch = disposition.match(/filename\*?=(?:UTF-8'')?["']?([^"';]+)["']?/i);
  const fileName = fileNameMatch
    ? decodeURIComponent(fileNameMatch[1])
    : 'rocketcat-plugin-download';
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  return { ok: true, filename: fileName, size: blob.size };
}

async function startPluginDashboardSSE(payload) {
  const subscriptionId = String(payload?.subscriptionId || '');
  if (!subscriptionId) {
    throw new Error('SSE subscriptionId 缺失');
  }
  state.pluginDashboard.sseControllers.get(subscriptionId)?.abort();
  const controller = new AbortController();
  state.pluginDashboard.sseControllers.set(subscriptionId, controller);
  const response = await fetch(
    buildPluginDashboardApiUrl('sse', payload?.path, payload?.query),
    {
      headers: { Accept: 'text/event-stream' },
      signal: controller.signal,
    },
  );
  if (!response.ok || !response.body) {
    state.pluginDashboard.sseControllers.delete(subscriptionId);
    await parsePluginDashboardResponse(response);
    return;
  }

  (async () => {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        buffer = buffer.replaceAll('\r\n', '\n');
        let boundary = buffer.indexOf('\n\n');
        while (boundary >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const data = block
            .split(/\r?\n/)
            .filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trimStart())
            .join('\n');
          postPluginDashboardMessage({
            kind: 'event',
            subscriptionId,
            event: 'message',
            data,
          });
          boundary = buffer.indexOf('\n\n');
        }
      }
    } catch (error) {
      if (error?.name !== 'AbortError') {
        postPluginDashboardMessage({
          kind: 'event',
          subscriptionId,
          event: 'error',
          error: error?.message || 'SSE connection failed',
        });
      }
    } finally {
      state.pluginDashboard.sseControllers.delete(subscriptionId);
      reader.releaseLock();
    }
  })();
  return { ok: true, subscriptionId };
}

function stopPluginDashboardSSE(subscriptionId) {
  const normalizedId = String(subscriptionId || '');
  state.pluginDashboard.sseControllers.get(normalizedId)?.abort();
  state.pluginDashboard.sseControllers.delete(normalizedId);
  return { ok: true };
}

async function handlePluginDashboardBridgeMessage(event) {
  if (
    state.currentPage !== 'plugin-dashboard'
    || event.source !== elements.pluginDashboardFrame?.contentWindow
  ) {
    return;
  }
  const message = event.data;
  if (
    !message
    || message.channel !== PLUGIN_DASHBOARD_CHANNEL
    || message.kind !== 'request'
    || typeof message.requestId !== 'string'
    || typeof message.action !== 'string'
  ) {
    return;
  }

  try {
    let result;
    if (message.action === 'ready' || message.action === 'context') {
      result = buildPluginDashboardContext();
      if (message.action === 'ready') {
        markPluginDashboardReady();
      }
    } else if (message.action === 'api') {
      result = await executePluginDashboardApi(message.payload);
    } else if (message.action === 'upload') {
      result = await executePluginDashboardUpload(message.payload);
    } else if (message.action === 'download') {
      result = await executePluginDashboardDownload(message.payload);
    } else if (message.action === 'sse-subscribe') {
      result = await startPluginDashboardSSE(message.payload);
    } else if (message.action === 'sse-unsubscribe') {
      result = stopPluginDashboardSSE(message.payload?.subscriptionId);
    } else {
      throw new Error(`未知 Dashboard Bridge 操作: ${message.action}`);
    }
    postPluginDashboardMessage({
      kind: 'response',
      requestId: message.requestId,
      ok: true,
      result,
    });
  } catch (error) {
    postPluginDashboardMessage({
      kind: 'response',
      requestId: message.requestId,
      ok: false,
      error: error?.message || 'Dashboard Bridge 请求失败',
    });
  }
}

async function cleanupPluginDashboardSession() {
  window.clearTimeout(state.pluginDashboard.readyTimer);
  state.pluginDashboard.readyTimer = null;
  state.pluginDashboard.ready = false;
  for (const controller of state.pluginDashboard.sseControllers.values()) {
    controller.abort();
  }
  state.pluginDashboard.sseControllers.clear();
  const pluginId = state.pluginDashboard.plugin?.id;
  const token = state.pluginDashboard.token;
  if (pluginId && token) {
    await fetch(
      `/api/plugins/${encodeURIComponent(pluginId)}/dashboard/session/${encodeURIComponent(token)}`,
      { method: 'DELETE' },
    ).catch(() => {});
  }
  state.pluginDashboard.token = '';
  state.pluginDashboard.sessionUrl = '';
  if (elements.pluginDashboardFrame) {
    elements.pluginDashboardFrame.src = 'about:blank';
  }
}

function setPluginDashboardPhase(phase, { motion = 'auto' } = {}) {
  const shell = elements.pluginDashboardFrameShell;
  if (!shell || shell.dataset.dashboardPhase === phase) {
    return;
  }
  shell.dataset.motion = resolveMotion(motion);
  shell.dataset.dashboardPhase = phase;
  if (shell.dataset.motion === 'instant') {
    window.requestAnimationFrame(() => {
      if (shell.dataset.motion === 'instant') {
        shell.dataset.motion = 'standard';
      }
    });
  }
}

function markPluginDashboardReady() {
  window.clearTimeout(state.pluginDashboard.readyTimer);
  state.pluginDashboard.readyTimer = null;
  state.pluginDashboard.ready = true;
  setPluginDashboardPhase('ready', { motion: 'standard' });
}

function startPluginDashboardReadyTimeout() {
  window.clearTimeout(state.pluginDashboard.readyTimer);
  state.pluginDashboard.ready = false;
  if (elements.pluginDashboardLoading) {
    elements.pluginDashboardLoading.textContent = '正在建立安全 Dashboard 会话…';
  }
  elements.pluginDashboardLoading?.classList.remove('hidden');
  elements.pluginDashboardError?.classList.remove('hidden');
  setPluginDashboardPhase('loading', { motion: 'standard' });
  state.pluginDashboard.readyTimer = window.setTimeout(() => {
    if (state.pluginDashboard.ready || state.currentPage !== 'plugin-dashboard') {
      return;
    }
    setPluginDashboardPhase('error', { motion: 'standard' });
    window.requestAnimationFrame(() => elements.pluginDashboardRetryButton?.focus({ preventScroll: true }));
  }, 20000);
}

function renderPluginDashboardHeader(plugin, selectedPage) {
  elements.pluginDashboardTitle.textContent = plugin.display_name || plugin.name || plugin.id;
  const pages = Array.isArray(plugin.pages) ? plugin.pages : [];
  elements.pluginDashboardPageSelect.innerHTML = pages
    .map((page) => (
      `<option value="${escapeHtml(page.name)}" ${page.name === selectedPage ? 'selected' : ''}>`
      + `${escapeHtml(page.title || page.name)}</option>`
    ))
    .join('');
  elements.pluginDashboardPageSelect.disabled = pages.length <= 1;
  elements.pluginDashboardLogo.innerHTML = plugin.has_logo
    ? `<img src="/api/plugins/${encodeURIComponent(plugin.id)}/logo" alt="" />`
    : `<span>${escapeHtml((plugin.display_name || plugin.name || plugin.id || '?').charAt(0))}</span>`;
}

async function openPluginDashboard(pluginId, pageName = '', { pushHistory = true } = {}) {
  if (state.pluginDashboard.opening) {
    return;
  }
  state.pluginDashboard.opening = true;
  try {
    const plugin = state.plugins.items.find((item) => item.id === pluginId);
    if (!plugin?.has_dashboard) {
      throw new Error('该插件没有 Dashboard 页面');
    }
    if (!plugin.activated) {
      throw new Error('请先启用插件');
    }
    await cleanupPluginDashboardSession();
    const selectedPage = pageName || plugin.default_page || plugin.pages?.[0]?.name;
    const session = await requestJson(
      `/api/plugins/${encodeURIComponent(plugin.id)}/dashboard/session`,
      {
        method: 'POST',
        body: JSON.stringify({ page: selectedPage }),
      },
    );
    state.pluginDashboard.plugin = plugin;
    state.pluginDashboard.page = session.page;
    state.pluginDashboard.token = session.token;
    state.pluginDashboard.sessionUrl = session.url;
    renderPluginDashboardHeader(plugin, session.page);
    startPluginDashboardReadyTimeout();
    elements.pluginDashboardFrame.onload = () => {
      elements.pluginDashboardLoading.textContent = '页面已载入，正在等待安全 Bridge 握手…';
    };
    elements.pluginDashboardFrame.src = session.url;
    setActivePage('plugin-dashboard');
    if (pushHistory) {
      window.history.pushState(
        { rocketcatPage: 'plugin-dashboard', pluginId: plugin.id, page: session.page },
        '',
        `#plugin-dashboard/${encodeURIComponent(plugin.id)}/${encodeURIComponent(session.page)}`,
      );
    } else if (window.history.state?.rocketcatPage === 'plugin-dashboard') {
      window.history.replaceState(
        { rocketcatPage: 'plugin-dashboard', pluginId: plugin.id, page: session.page },
        '',
        `#plugin-dashboard/${encodeURIComponent(plugin.id)}/${encodeURIComponent(session.page)}`,
      );
    }
  } finally {
    state.pluginDashboard.opening = false;
  }
}

async function leavePluginDashboard({ fromHistory = false } = {}) {
  await cleanupPluginDashboardSession();
  state.pluginDashboard.plugin = null;
  state.pluginDashboard.page = '';
  setActivePage('plugins');
  if (!fromHistory) {
    window.history.replaceState({ rocketcatPage: 'plugins' }, '', '#plugins');
  }
  await loadPlugins({ forceReload: true, silent: true });
}

async function closePluginModal({ force = false } = {}) {
  const closed = await requestDialogClose(elements.pluginModal, { force });
  if (!closed) {
    return;
  }
  state.plugins.current = null;
  window.setTimeout(() => {
    if (!elements.pluginModal.open && elements.pluginSettingsForm) {
      elements.pluginSettingsForm.innerHTML = '';
    }
  }, REDUCED_MOTION_QUERY.matches ? 0 : 120);
}

function closePluginUninstallModal() {
  state.plugins.pendingUninstall = null;
  if (elements.pluginUninstallDeleteConfigInput) {
    elements.pluginUninstallDeleteConfigInput.checked = false;
  }
  if (elements.pluginUninstallDeleteDataInput) {
    elements.pluginUninstallDeleteDataInput.checked = false;
  }
  requestDialogClose(elements.pluginUninstallModal);
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

function normalizePluginIntegerList(value, label = '表情 ID 列表', { strict = true } = {}) {
  if (!Array.isArray(value)) {
    if (strict) {
      throw new Error(`${label} 必须是数字 ID 列表`);
    }
    return [];
  }
  const normalized = [];
  const seen = new Set();
  for (const item of value) {
    const rawValue = typeof item === 'string' ? item.trim() : item;
    const parsed = typeof rawValue === 'number'
      ? rawValue
      : (/^\d+$/.test(String(rawValue)) ? Number(rawValue) : Number.NaN);
    if (!Number.isSafeInteger(parsed) || parsed < 0) {
      if (strict) {
        throw new Error(`${label} 只能包含非负整数表情 ID`);
      }
      continue;
    }
    if (!seen.has(parsed)) {
      normalized.push(parsed);
      seen.add(parsed);
    }
  }
  return normalized;
}

function renderPluginIdChips(values) {
  if (!values.length) {
    return '<span class="plugin-id-list-empty">尚未添加 ID</span>';
  }
  return values
    .map((value) => `<code class="plugin-id-chip">${escapeHtml(value)}</code>`)
    .join('');
}

function renderPluginRegularField([key, fieldSchema], config) {
  const fieldType = String(fieldSchema.type || 'string');
  const fieldLabel = String(fieldSchema.description || key);
  const fieldHint = String(fieldSchema.hint || '');
  const formattedValue = formatPluginFieldValue(fieldType, config[key]);
  const inputId = `pluginField_${key}`;

  if (fieldType === 'bool') {
    return `
      <div class="plugin-regular-field">
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
      </div>
    `;
  }

  if (fieldType === 'text' || fieldType === 'list' || fieldType === 'dict' || fieldType === 'object' || fieldType === 'template_list') {
    return `
      <div class="plugin-regular-field span-two">
        <label class="field-block">
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
      </div>
    `;
  }

  const inputType = fieldType === 'int' || fieldType === 'float' ? 'number' : 'text';
  const inputStep = fieldType === 'float' ? 'any' : fieldType === 'int' ? '1' : '';
  return `
    <div class="plugin-regular-field span-two">
      <label class="field-block">
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
    </div>
  `;
}

function renderPluginStateMappingCard([key, fieldSchema], pairedSchema, config) {
  const groupLabel = String(fieldSchema.ui_group_label || fieldSchema.description || key);
  const fieldLabel = String(fieldSchema.description || key);
  const fieldHint = String(fieldSchema.hint || '');
  const pairedKey = String(fieldSchema.ui_pair || '');
  const pairedLabel = String(pairedSchema.description || pairedKey);
  const pairedHint = String(pairedSchema.hint || '');
  const values = normalizePluginIntegerList(config[key], fieldLabel, { strict: false });
  const inputId = `pluginField_${key}`;
  const pairInputId = `pluginField_${pairedKey}`;
  const summaryId = `pluginListSummary_${key}`;
  const pairValue = formatPluginFieldValue(String(pairedSchema.type || 'string'), config[pairedKey]);

  return `
    <section class="plugin-state-mapping-card" data-plugin-state="${escapeHtml(fieldSchema.ui_group || key)}" aria-labelledby="pluginStateTitle_${escapeHtml(key)}">
      <div class="plugin-state-mapping-heading">
        <div>
          <p class="plugin-state-kicker">STATE MAPPING</p>
          <h4 id="pluginStateTitle_${escapeHtml(key)}">${escapeHtml(groupLabel)}</h4>
        </div>
        <span class="plugin-state-index" aria-hidden="true">${escapeHtml(values.length)}</span>
      </div>

      <div class="plugin-id-list-field">
        <div class="plugin-id-list-heading">
          <span id="pluginListLabel_${escapeHtml(key)}">${escapeHtml(fieldLabel)}</span>
          <button class="action-button subtle plugin-list-edit-button" type="button" data-plugin-list-edit="${escapeHtml(key)}" aria-haspopup="dialog">添加更多</button>
        </div>
        <input
          id="${escapeHtml(inputId)}"
          type="hidden"
          value="${escapeHtml(JSON.stringify(values))}"
          data-plugin-field="${escapeHtml(key)}"
          data-plugin-type="list"
          data-plugin-label="${escapeHtml(fieldLabel)}"
          data-plugin-integer-list="true"
          data-plugin-group-label="${escapeHtml(groupLabel)}"
        />
        <div id="${escapeHtml(summaryId)}" class="plugin-id-chip-list" data-plugin-list-summary="${escapeHtml(key)}" aria-labelledby="pluginListLabel_${escapeHtml(key)}">
          ${renderPluginIdChips(values)}
        </div>
        ${fieldHint ? `<p class="plugin-field-hint">${escapeHtml(fieldHint)}</p>` : ''}
      </div>

      <label class="field-block plugin-shortcode-field">
        <span>${escapeHtml(pairedLabel)}</span>
        <input
          id="${escapeHtml(pairInputId)}"
          type="text"
          value="${escapeHtml(pairValue)}"
          data-plugin-field="${escapeHtml(pairedKey)}"
          data-plugin-type="${escapeHtml(pairedSchema.type || 'string')}"
          data-plugin-label="${escapeHtml(pairedLabel)}"
          autocomplete="off"
          spellcheck="false"
        />
        ${pairedHint ? `<small class="plugin-field-hint">${escapeHtml(pairedHint)}</small>` : ''}
      </label>
    </section>
  `;
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

  const entryMap = new Map(entries);
  const mappingEntries = entries.filter(([, fieldSchema]) => (
    String(fieldSchema.type || '') === 'list'
    && fieldSchema.ui_component === 'integer_list'
    && typeof fieldSchema.ui_pair === 'string'
    && String(entryMap.get(fieldSchema.ui_pair)?.type || '') === 'string'
  ));
  const pairedKeys = new Set(mappingEntries.map(([, fieldSchema]) => fieldSchema.ui_pair));
  const mappingKeys = new Set(mappingEntries.map(([key]) => key));
  const regularEntries = entries.filter(([key]) => !mappingKeys.has(key) && !pairedKeys.has(key));
  const fieldsHtml = regularEntries.map((entry) => renderPluginRegularField(entry, config)).join('');
  const mappingCardsHtml = mappingEntries.map(([key, fieldSchema]) => (
    renderPluginStateMappingCard([key, fieldSchema], entryMap.get(fieldSchema.ui_pair), config)
  )).join('');

  elements.pluginSettingsForm.innerHTML = `
    <section class="form-section">
      <h3>主配置</h3>
      <div class="field-grid two-columns plugin-field-grid">${fieldsHtml}</div>
    </section>
    ${mappingCardsHtml ? `
      <section class="form-section plugin-state-mapping-section">
        <div class="plugin-state-mapping-section-heading">
          <div>
            <p class="plugin-state-kicker">REACTION MAPPINGS</p>
            <h3>四态映射</h3>
          </div>
          <p>每个状态的上游数字 ID 与 Rocket.Chat shortcode 在同一模块中配置。</p>
        </div>
        <div class="plugin-state-mapping-grid">${mappingCardsHtml}</div>
      </section>
    ` : ''}
  `;
}

function getPluginIntegerListField(fieldKey) {
  return Array.from(elements.pluginSettingsForm?.querySelectorAll('[data-plugin-integer-list="true"]') || [])
    .find((field) => field.dataset.pluginField === fieldKey) || null;
}

function getPluginListConflict(value, currentFieldKey) {
  for (const field of elements.pluginSettingsForm?.querySelectorAll('[data-plugin-integer-list="true"]') || []) {
    if (field.dataset.pluginField === currentFieldKey) {
      continue;
    }
    const values = normalizePluginIntegerList(JSON.parse(field.value || '[]'), field.dataset.pluginLabel);
    if (values.includes(value)) {
      return field.dataset.pluginGroupLabel || field.dataset.pluginLabel || field.dataset.pluginField;
    }
  }
  return '';
}

function renderPluginListEditor() {
  const editor = state.plugins.listEditor;
  if (!editor) {
    elements.pluginListEditorItems.innerHTML = '';
    elements.pluginListEditorEmpty.classList.remove('hidden');
    return;
  }
  elements.pluginListEditorItems.innerHTML = editor.values.map((value) => `
    <div class="plugin-list-editor-item" role="listitem">
      <code>${escapeHtml(value)}</code>
      <button class="icon-button plugin-list-editor-remove" type="button" data-plugin-list-remove="${escapeHtml(value)}" aria-label="删除表情 ID ${escapeHtml(value)}">×</button>
    </div>
  `).join('');
  elements.pluginListEditorEmpty.classList.toggle('hidden', editor.values.length > 0);
}

function openPluginListEditor(fieldKey, trigger) {
  const field = getPluginIntegerListField(fieldKey);
  if (!field) {
    throw new Error('未找到对应的表情 ID 配置项');
  }
  const groupLabel = field.dataset.pluginGroupLabel || field.dataset.pluginLabel || '当前状态';
  state.plugins.listEditor = {
    fieldKey,
    groupLabel,
    values: normalizePluginIntegerList(JSON.parse(field.value || '[]'), field.dataset.pluginLabel),
  };
  elements.pluginListEditorTitle.textContent = `修改${groupLabel}表情 ID`;
  elements.pluginListEditorDescription.textContent = '逐个添加 AstrBot 上游 I Am Thinking 插件使用的数字表情 ID；同一 ID 不能跨状态重复。';
  elements.pluginListEditorInput.value = '';
  setFormResult(elements.pluginListEditorStatus);
  renderPluginListEditor();
  openDialog(elements.pluginListEditorModal, {
    trigger,
    initialFocus: elements.pluginListEditorInput,
    backdropDismiss: true,
  });
}

function closePluginListEditor() {
  state.plugins.listEditor = null;
  setFormResult(elements.pluginListEditorStatus);
  requestDialogClose(elements.pluginListEditorModal);
}

function addPluginListEditorValue() {
  const editor = state.plugins.listEditor;
  if (!editor) {
    return;
  }
  const rawValue = String(elements.pluginListEditorInput.value || '').trim();
  if (!/^\d+$/.test(rawValue)) {
    setFormResult(elements.pluginListEditorStatus, '请输入非负整数表情 ID', 'error');
    elements.pluginListEditorInput.focus();
    return;
  }
  const value = Number(rawValue);
  if (!Number.isSafeInteger(value)) {
    setFormResult(elements.pluginListEditorStatus, '表情 ID 超出可安全保存的整数范围', 'error');
    elements.pluginListEditorInput.focus();
    return;
  }
  if (editor.values.includes(value)) {
    setFormResult(elements.pluginListEditorStatus, `表情 ID ${value} 已在当前列表中`, 'error');
    elements.pluginListEditorInput.select();
    return;
  }
  const conflictingGroup = getPluginListConflict(value, editor.fieldKey);
  if (conflictingGroup) {
    setFormResult(elements.pluginListEditorStatus, `表情 ID ${value} 已用于「${conflictingGroup}」，不能跨状态重复`, 'error');
    elements.pluginListEditorInput.select();
    return;
  }
  editor.values.push(value);
  elements.pluginListEditorInput.value = '';
  setFormResult(elements.pluginListEditorStatus);
  renderPluginListEditor();
  elements.pluginListEditorInput.focus();
}

function applyPluginListEditor() {
  const editor = state.plugins.listEditor;
  if (!editor) {
    return;
  }
  const field = getPluginIntegerListField(editor.fieldKey);
  if (!field) {
    setFormResult(elements.pluginListEditorStatus, '原配置项已不可用，请关闭后重试', 'error');
    return;
  }
  field.value = JSON.stringify(editor.values);
  field.dispatchEvent(new Event('input', { bubbles: true }));
  field.dispatchEvent(new Event('change', { bubbles: true }));
  const summary = Array.from(elements.pluginSettingsForm.querySelectorAll('[data-plugin-list-summary]'))
    .find((item) => item.dataset.pluginListSummary === editor.fieldKey);
  if (summary) {
    summary.innerHTML = renderPluginIdChips(editor.values);
  }
  const card = field.closest('.plugin-state-mapping-card');
  const count = card?.querySelector('.plugin-state-index');
  if (count) {
    count.textContent = String(editor.values.length);
  }
  state.plugins.listEditor = null;
  requestDialogClose(elements.pluginListEditorModal);
}

function validatePluginIntegerListMappings(payload) {
  const ownerById = new Map();
  for (const field of elements.pluginSettingsForm?.querySelectorAll('[data-plugin-integer-list="true"]') || []) {
    const fieldKey = field.dataset.pluginField;
    const groupLabel = field.dataset.pluginGroupLabel || field.dataset.pluginLabel || fieldKey;
    const values = normalizePluginIntegerList(payload[fieldKey], field.dataset.pluginLabel || fieldKey);
    payload[fieldKey] = values;
    for (const value of values) {
      const previousOwner = ownerById.get(value);
      if (previousOwner) {
        throw new Error(`表情 ID ${value} 同时出现在「${previousOwner}」和「${groupLabel}」，请保留在一个状态中`);
      }
      ownerById.set(value, groupLabel);
    }
  }
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
      payload[key] = field.dataset.pluginIntegerList === 'true'
        ? normalizePluginIntegerList(parsed, label)
        : parsed;
      continue;
    }

    payload[key] = rawValue;
  }
  validatePluginIntegerListMappings(payload);
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
  url.hash = '';
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
  const existingTabs = new Map(
    Array.from(elements.terminalTabs.querySelectorAll('[data-terminal-id]'))
      .map((tab) => [tab.dataset.terminalId, tab]),
  );
  const nextTabs = state.terminal.items.map((item) => {
    const isActive = item.id === state.terminal.activeId;
    let tab = existingTabs.get(item.id);
    if (!tab) {
      const template = document.createElement('template');
      template.innerHTML = `
      <div
        class="terminal-tab${isActive ? ' active' : ''}"
        role="presentation"
        data-terminal-id="${escapeHtml(item.id)}"
      >
        <button
          id="terminal-tab-${escapeHtml(item.id)}"
          class="terminal-tab-trigger"
          type="button"
          role="tab"
          aria-selected="${isActive ? 'true' : 'false'}"
          aria-controls="terminalScreen"
          tabindex="${isActive ? '0' : '-1'}"
          data-terminal-activate="${escapeHtml(item.id)}"
        >
          <span class="terminal-tab-title">${escapeHtml(item.title || item.id)}</span>
        </button>
        <span
          class="terminal-tab-drag-handle"
          data-terminal-drag-handle="${escapeHtml(item.id)}"
          aria-hidden="true"
          title="拖动调整顺序"
        ><i></i></span>
        <button class="terminal-tab-close" type="button" data-terminal-close="${escapeHtml(item.id)}" aria-label="关闭终端 ${escapeHtml(item.title || item.id)}">×</button>
      </div>
      `;
      tab = template.content.firstElementChild;
    } else {
      existingTabs.delete(item.id);
      tab.classList.toggle('active', isActive);
      const trigger = tab.querySelector('[data-terminal-activate]');
      if (trigger) {
        trigger.setAttribute('aria-selected', isActive ? 'true' : 'false');
        trigger.tabIndex = isActive ? 0 : -1;
      }
      const title = tab.querySelector('.terminal-tab-title');
      if (title) title.textContent = item.title || item.id;
      const close = tab.querySelector('[data-terminal-close]');
      if (close) close.setAttribute('aria-label', `关闭终端 ${item.title || item.id}`);
    }
    return tab;
  });
  existingTabs.forEach((tab) => tab.remove());
  elements.terminalTabs.replaceChildren(...nextTabs);
}

function syncTerminalTabPanelAccessibility() {
  if (!elements.terminalScreen) {
    return;
  }
  elements.terminalScreen.setAttribute('role', 'tabpanel');
  elements.terminalScreen.setAttribute('tabindex', '0');
  if (state.terminal.activeId) {
    elements.terminalScreen.setAttribute('aria-labelledby', `terminal-tab-${state.terminal.activeId}`);
  } else {
    elements.terminalScreen.removeAttribute('aria-labelledby');
  }
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
  syncTerminalTabPanelAccessibility();
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

function animateTerminalTabLayout(previousPositions, {
  excludeId = '',
  motion = 'standard',
} = {}) {
  if (resolveMotion(motion) === 'instant') {
    return;
  }
  for (const tab of elements.terminalTabs?.querySelectorAll('[data-terminal-id]') || []) {
    if (tab.dataset.terminalId === excludeId) {
      continue;
    }
    const previous = previousPositions.get(tab.dataset.terminalId);
    const current = tab.getBoundingClientRect();
    if (!previous) continue;
    const deltaX = previous.left - current.left;
    const deltaY = previous.top - current.top;
    if (Math.abs(deltaX) < 1 && Math.abs(deltaY) < 1) continue;
    tab.getAnimations().forEach((animation) => animation.cancel());
    tab.animate(
      [
        { transform: `translate(${deltaX}px, ${deltaY}px)` },
        { transform: 'translate(0, 0)' },
      ],
      { duration: 180, easing: 'cubic-bezier(0.77, 0, 0.175, 1)' },
    );
  }
}

function getTerminalTabPositions() {
  return new Map(
    Array.from(elements.terminalTabs?.querySelectorAll('[data-terminal-id]') || [])
      .map((tab) => [tab.dataset.terminalId, tab.getBoundingClientRect()]),
  );
}

function reorderTerminalTabs(fromId, toId, { animate = true, persist = true } = {}) {
  if (!fromId || !toId || fromId === toId) {
    return;
  }
  const fromIndex = state.terminal.items.findIndex((item) => item.id === fromId);
  const toIndex = state.terminal.items.findIndex((item) => item.id === toId);
  if (fromIndex < 0 || toIndex < 0) {
    return;
  }
  const originalOrder = state.terminal.items.map((item) => item.id);
  const previousPositions = getTerminalTabPositions();
  const [moved] = state.terminal.items.splice(fromIndex, 1);
  state.terminal.items.splice(toIndex, 0, moved);
  renderTerminalTabs();
  syncTerminalTabPanelAccessibility();
  if (animate) {
    animateTerminalTabLayout(previousPositions);
  }
  if (persist) {
    const committedOrder = state.terminal.items.map((item) => item.id);
    saveTerminalOrder().catch((error) => {
      if (state.terminal.items.map((item) => item.id).join('\0') === committedOrder.join('\0')) {
        restoreTerminalOrder(originalOrder, { animate });
      }
      showToast(error.message || '终端顺序保存失败，已恢复原顺序', 'error');
    });
  }
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
  elements.fileUploadZone?.setAttribute('aria-busy', String(state.files.uploading));
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
    elements.fileTableBody.innerHTML = '<tr><td colspan="6" data-label="状态" class="file-table-message">正在读取目录...</td></tr>';
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
      <td class="file-select-cell" data-label="选择">
        <input class="file-checkbox" type="checkbox" aria-label="选择 ${escapeHtml(item.name || item.path || '-')}" data-file-action="select" data-file-path="${escapeHtml(item.path)}" ${selected ? 'checked' : ''} />
      </td>
      <td data-label="名称">
        <button class="file-name-button" type="button" data-file-action="open" data-file-path="${escapeHtml(item.path)}">
          ${renderFileIcon(item)}
          <span class="file-name-text">${escapeHtml(item.name || item.path || '-')}</span>
          ${item.requires_password ? '<span class="file-lock-badge">需鉴权</span>' : ''}
        </button>
      </td>
      <td data-label="类型">${escapeHtml(getFileTypeLabel(item))}</td>
      <td data-label="大小">${escapeHtml(formatFileSize(item.size, item.is_directory))}</td>
      <td data-label="修改时间">${escapeHtml(formatFileTime(item.mtime))}</td>
      <td class="file-actions-cell" data-label="操作">
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
    const active = button.dataset.fileCreateType === state.files.createType;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
    button.tabIndex = active ? 0 : -1;
  }
}

function openFileCreateModal(type = 'file') {
  setFileCreateType(type);
  if (elements.fileCreateNameInput) {
    elements.fileCreateNameInput.value = '';
  }
  openDialog(elements.fileCreateModal, { initialFocus: elements.fileCreateNameInput });
}

function closeFileCreateModal() {
  requestDialogClose(elements.fileCreateModal);
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
  openDialog(elements.fileDeleteModal, { initialFocus: elements.fileDeleteCancelButton });
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
  openDialog(elements.fileDeleteModal, { initialFocus: elements.fileDeleteCancelButton });
}

function closeFileDeleteModal() {
  state.files.pendingDeletePaths = null;
  requestDialogClose(elements.fileDeleteModal);
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
  state.files.moveTree.focusPath = '';
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
  const focusable = normalizeFilePath(state.files.moveTree.focusPath) === normalized;
  const rows = [`
    <div
      class="file-move-tree-item"
      role="treeitem"
      aria-level="${depth + 1}"
      aria-expanded="${String(expanded)}"
      aria-selected="${String(selected)}"
      tabindex="${focusable ? '0' : '-1'}"
      data-file-move-path="${escapeHtml(normalized)}"
    >
      <div class="file-move-tree-row ${selected ? 'selected' : ''}" style="--depth: ${depth}">
        <span class="file-move-node">
        <span class="file-move-node-toggle">${expanded ? '-' : '+'}</span>
        <span class="file-move-node-label">${escapeHtml(label || '/')}</span>
        </span>
      </div>
  `];
  if (expanded) {
    rows.push('<div role="group" class="file-move-tree-group">');
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
    rows.push('</div>');
  }
  rows.push('</div>');
  return rows.join('');
}

function renderMoveTree() {
  if (!elements.fileMoveTree) {
    return;
  }
  const movingPaths = state.files.pendingMovePaths || getSelectedFilePaths();
  const restoreFocus = elements.fileMoveTree.contains(document.activeElement);
  elements.fileMoveTree.innerHTML = renderMoveTreeNode('', 0);
  elements.fileMoveSelectedPath.textContent = formatFilePath(state.files.moveTargetPath);
  elements.fileMoveSelectionInfo.textContent = `移动项：${movingPaths.length} 个项目`;
  elements.fileMoveConfirmButton.disabled = state.files.moving || !movingPaths.length;
  if (restoreFocus) {
    const focused = Array.from(elements.fileMoveTree.querySelectorAll('[role="treeitem"]'))
      .find((item) => normalizeFilePath(item.dataset.fileMovePath || '') === normalizeFilePath(state.files.moveTree.focusPath));
    focused?.focus({ preventScroll: true });
  }
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
  openDialog(elements.fileMoveModal, { initialFocus: elements.fileMoveTree });
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
  requestDialogClose(elements.fileMoveModal);
  state.files.moveTargetPath = '';
  state.files.pendingMovePaths = null;
  resetMoveTreeState();
}

async function selectMoveTarget(pathValue) {
  const normalized = normalizeFilePath(pathValue);
  state.files.moveTree.focusPath = normalized;
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
  openDialog(elements.fileRenameModal, { initialFocus: elements.fileRenameNameInput });
}

function closeFileRenameModal() {
  state.files.pendingRenameItem = null;
  requestDialogClose(elements.fileRenameModal);
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
  openDialog(elements.fileImageViewer, { initialFocus: elements.fileImageViewerCloseButton });
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
  closeDialog(elements.fileImageViewer);
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
  openDialog(elements.filePreviewModal, { initialFocus: elements.filePreviewCloseButton });
}

function closeFilePreviewModal() {
  state.files.previewItem = null;
  requestDialogClose(elements.filePreviewModal);
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
  openDialog(elements.fileEditModal, {
    initialFocus: elements.fileEditContentInput,
    backdropDismiss: false,
  });
}

function focusMoveTreeItem(item) {
  if (!item || !elements.fileMoveTree) {
    return;
  }
  for (const treeItem of elements.fileMoveTree.querySelectorAll('[role="treeitem"]')) {
    treeItem.tabIndex = treeItem === item ? 0 : -1;
  }
  state.files.moveTree.focusPath = normalizeFilePath(item.dataset.fileMovePath || '');
  item.focus({ preventScroll: true });
}

async function setMoveTreeExpanded(pathValue, expanded) {
  const normalized = normalizeFilePath(pathValue);
  state.files.moveTree.focusPath = normalized;
  if (expanded) {
    state.files.moveTree.expanded.add(normalized);
    await loadMoveDirectories(normalized);
  } else {
    state.files.moveTree.expanded.delete(normalized);
    renderMoveTree();
  }
}

async function closeFileEditModal({ force = false } = {}) {
  const closed = await requestDialogClose(elements.fileEditModal, { force });
  if (!closed) {
    return;
  }
  state.files.editingFile = null;
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
  openDialog(elements.fileSaveConfirmModal, { initialFocus: elements.fileSaveConfirmCancelButton });
}

function closeFileSaveConfirmModal() {
  state.files.pendingSave = false;
  requestDialogClose(elements.fileSaveConfirmModal);
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
    closeDialog(elements.fileSaveConfirmModal, { restoreFocus: false });
    await closeFileEditModal({ force: true });
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
  openDialog(elements.fileAuthModal, { initialFocus: elements.fileAuthPasswordInput });
}

function closeFileAuthModal() {
  state.files.pendingAuthItem = null;
  state.files.pendingAuthMode = 'edit';
  requestDialogClose(elements.fileAuthModal);
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
  setFormResult(elements.botFormStatus);
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
  openDialog(elements.modal, { initialFocus: elements.form?.elements?.namedItem('name') });
}

async function closeModal({ force = false } = {}) {
  const closed = await requestDialogClose(elements.modal, { force });
  if (closed) {
    state.editingId = null;
  }
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
  openDialog(elements.userMappingsModal, { initialFocus: elements.userMappingsSearchInput });
  await loadUserMappings();
}

function closeUserMappings() {
  requestDialogClose(elements.userMappingsModal);
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
  elements.userMappingsModalTitle.textContent = 'User 映射';
  if (elements.userMappingsBotName) {
    elements.userMappingsBotName.textContent = payload.bot_name || mappingState.botId;
  }
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
      <td data-label="userId"><code>${escapeHtml(item.user_id)}</code></td>
      <td data-label="用户名">${escapeHtml(item.username || '-')}</td>
      <td data-label="昵称">${escapeHtml(item.nickname || '-')}</td>
      <td data-label="OneBot ID">
        <input class="identity-onebot-input" type="text" inputmode="numeric"
          value="${escapeHtml(String(item.onebot_id))}"
          data-identity-user-id="${escapeHtml(item.user_id)}"
          data-identity-revision="${escapeHtml(String(item.revision))}" />
      </td>
      <td data-label="主槽 / 偏移">
        <code>${escapeHtml(String(item.primary_onebot_id))}</code>
        <small>偏移 ${escapeHtml(String(item.probe_offset))}</small>
      </td>
      <td data-label="状态"><div class="identity-badges">${badges.join('') || '<span class="identity-badge normal">正常</span>'}</div></td>
      <td data-label="操作">
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
  const confirmed = await askForConfirmation({
    title: '删除 User 映射？',
    message: `确认删除映射「${displayName}」吗？这会删除该用户在当前 server 范围内的共享映射，并重启相关 Bot。`,
    confirmLabel: '删除映射',
    kind: 'danger',
  });
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

async function loadData({ signal = null } = {}) {
  const [status, bots] = await Promise.all([
    requestJson('/api/status?compact=true', { signal }),
    requestJson('/api/bots', { signal }),
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

async function loadDiagnostics({ forceReload = false, silent = false, signal = null } = {}) {
  if (!forceReload && state.diagnostics.loaded) {
    return;
  }

  try {
    const diagnostics = await requestJson('/api/diagnostics', { signal });
    renderDiagnostics(diagnostics);
  } catch (error) {
    if (isAbortError(error)) {
      return;
    }
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
  await closeModal({ force: true });
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
  setFormResult(elements.settingsPasswordResult, '密码已保存并立即生效。', 'success');
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
  setFormResult(elements.settingsPortResult, '端口已写入配置，将在下次启动时优先使用。', 'success');
  showToast('WebUI 访问端口已写入配置；重启后会优先尝试新端口', 'success');
}

async function savePerformanceSettings() {
  const readInteger = (element, label) => {
    const value = Number(String(element?.value || '').trim());
    if (!Number.isInteger(value)) {
      throw new Error(`${label} 必须是整数`);
    }
    return value;
  };
  const payload = {
    performance_profile: String(elements.settingsPerformanceProfileInput?.value || 'balanced'),
    message_index_max_entries: readInteger(elements.settingsMessageIndexMaxEntriesInput, 'message mapping window size'),
    inbound_worker_count: readInteger(elements.settingsInboundWorkerCountInput, '入站 Worker 数量'),
    onebot_outgoing_queue_max_entries: readInteger(elements.settingsOnebotQueueMaxInput, 'OneBot 队列上限'),
    identity_cache_max_entries: readInteger(elements.settingsIdentityCacheMaxInput, '身份缓存上限'),
    media_cache_max_bytes: readInteger(elements.settingsMediaCacheMaxBytesInput, '媒体缓存上限'),
    media_cache_max_age_hours: readInteger(elements.settingsMediaCacheMaxAgeInput, '媒体缓存保留时间'),
    log_file_max_bytes: readInteger(elements.settingsLogFileMaxBytesInput, '日志文件上限'),
    log_file_backup_count: readInteger(elements.settingsLogFileBackupCountInput, '日志备份数量'),
    terminal_max_sessions: readInteger(elements.settingsTerminalMaxSessionsInput, '终端会话上限'),
    terminal_idle_timeout_seconds: readInteger(elements.settingsTerminalIdleTimeoutInput, '终端空闲关闭时间'),
  };
  const settings = await requestJson('/api/settings', {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
  state.settings.loaded = true;
  renderSettings(settings);
  setFormResult(elements.settingsPerformanceResult, '性能与资源设置已保存。', 'success');
  showToast('性能与资源设置已保存', 'success');
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
  const confirmed = await askForConfirmation({
    title: '整理消息映射窗口？',
    message: '将按当前窗口条数上限整理所有 Bot 的消息映射，运行中的相关状态可能会短暂刷新。',
    confirmLabel: '开始整理',
  });
  if (!confirmed) {
    return;
  }

  const payload = await requestJson('/api/settings/rebuild-message-indexes', {
    method: 'POST',
  });
  await loadSettings({ forceReload: true, silent: true });
  setFormResult(elements.settingsPerformanceResult, summarizeMessageIndexResult(payload.result), 'success');
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

  await closeModal({ force: true });
  await closePluginModal({ force: true });
  closePluginUninstallModal();
  state.settings.loaded = false;
  state.basicInfo.loaded = false;
  state.plugins.loaded = false;
  await loadCardOrder({ forceReload: true, silent: true });
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
  const confirmed = await askForConfirmation({
    title: '删除 Bot？',
    message: `确认删除 Bot「${target.name}」吗？该操作无法撤销。`,
    confirmLabel: '删除 Bot',
    kind: 'danger',
  });
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
  setFormResult(elements.pluginFormStatus);
  openDialog(elements.pluginModal, { initialFocus: elements.pluginSettingsForm?.querySelector('input, select, textarea') });
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
  await closePluginModal({ force: true });
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
  openDialog(elements.pluginUninstallModal, { initialFocus: elements.pluginUninstallCancelButton });
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
    await closePluginModal({ force: true });
  }
  state.plugins.loaded = false;
  await Promise.all([
    loadPlugins({ forceReload: true, silent: true }),
    loadData(),
  ]);
  showToast(buildPluginUninstallToast(deleteConfig, deleteData), 'success');
}

elements.mobileMenuButton?.addEventListener('click', () => {
  setMobileNavigationOpen(!state.ui.mobileNavigationOpen);
});
elements.navigationScrim?.addEventListener('click', () => setMobileNavigationOpen(false));
elements.logoutButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.logoutButton, '正在退出…', async () => {
      await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' });
      window.location.replace('/');
    });
  } catch (error) {
    if (isAbortError(error)) {
      return;
    }
    showToast(error.message || '退出登录失败', 'error');
  }
});
elements.confirmModalCloseButton?.addEventListener('click', () => resolveConfirmation(false));
elements.confirmModalCancelButton?.addEventListener('click', () => resolveConfirmation(false));
elements.confirmModalSubmitButton?.addEventListener('click', () => resolveConfirmation(true));

elements.createButton?.addEventListener('click', () => openModal());
elements.refreshButton?.addEventListener('click', async () => {
  await runBusy(elements.refreshButton, '刷新中…', async () => {
    await loadData();
    showToast('列表已刷新');
  });
});
elements.clearLogsButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.clearLogsButton, '清理中…', clearLogs);
  } catch (error) {
    showToast(error.message || '清空日志失败', 'error');
  }
});
elements.logAutoScrollToggle?.addEventListener('change', (event) => {
  setLogAutoScroll(event.target.checked);
});
elements.logBackToBottomButton?.addEventListener('click', () => {
  state.logs.unreadCount = 0;
  elements.logConsole?.scrollTo({ top: elements.logConsole.scrollHeight, behavior: 'auto' });
  renderLogNavigationState();
});
elements.logConsole?.addEventListener('scroll', renderLogNavigationState, { passive: true });
elements.basicRefreshButton?.addEventListener('click', async () => {
  await runBusy(elements.basicRefreshButton, '刷新中…', async () => {
    await activatePage('basic', { forceReload: true });
    showToast('基础信息已刷新');
  });
});
elements.diagnosticsRefreshButton?.addEventListener('click', async () => {
  await runBusy(elements.diagnosticsRefreshButton, '刷新中…', async () => {
    await activatePage('diagnostics', { forceReload: true });
    showToast('运行诊断已刷新');
  });
});
elements.settingsRefreshButton?.addEventListener('click', async () => {
  await runBusy(elements.settingsRefreshButton, '刷新中…', async () => {
    await activatePage('settings', { forceReload: true });
    showToast('设置项已刷新');
  });
});
elements.updateCheckButton?.addEventListener('click', async () => {
  await loadUpdateStatus({ refresh: true, silent: false });
  if (state.updates.status?.refresh_limited) {
    showToast('检查更新操作过于频繁，已显示最近结果');
  } else if (state.updates.status?.update_available) {
    showToast(`发现新版本 ${state.updates.status.latest_version}`, 'success');
  } else if (state.updates.status && !state.updates.status.error) {
    showToast('当前已是最新兼容版本', 'success');
  }
});
elements.updateSelectButton?.addEventListener('click', () => {
  openUpdateReleaseModal().catch((error) => {
    showToast(error.message || '版本列表加载失败', 'error');
  });
});
elements.updateReleaseCloseButton?.addEventListener('click', closeUpdateReleaseModal);
elements.updateReleaseCancelButton?.addEventListener('click', closeUpdateReleaseModal);
elements.updateReleaseList?.addEventListener('click', (event) => {
  const button = event.target.closest('[data-update-tag]');
  if (!button) {
    return;
  }
  const release = state.updates.releases.find((item) => item.tag_name === button.dataset.updateTag);
  promptUpdateSwitch(release);
});
elements.updateConfirmCloseButton?.addEventListener('click', closeUpdateConfirmModal);
elements.updateConfirmCancelButton?.addEventListener('click', closeUpdateConfirmModal);
elements.updateConfirmSubmitButton?.addEventListener('click', async () => {
  try {
    await submitUpdateSwitch();
  } catch (error) {
    showToast(error.message || '版本切换准备失败', 'error');
  }
});
elements.updateRestartRetryButton?.addEventListener('click', () => {
  if (elements.updateRestartRetryButton.dataset.mode === 'dismiss') {
    elements.updateRestartOverlay.dataset.blocking = 'false';
    closeDialog(elements.updateRestartOverlay);
    loadUpdateStatus({ refresh: false, silent: true });
    return;
  }
  const transactionId = state.updates.transactionId;
  if (!transactionId) {
    window.location.reload();
    return;
  }
  showUpdateRestartOverlay(transactionId);
  pollUpdateTransaction(transactionId);
});
elements.pluginsRefreshButton?.addEventListener('click', async () => {
  await runBusy(elements.pluginsRefreshButton, '刷新中…', async () => {
    await activatePage('plugins', { forceReload: true });
    showToast('插件列表已刷新');
  });
});
elements.pluginDashboardBackButton?.addEventListener('click', () => {
  leavePluginDashboard().catch((error) => {
    showToast(error.message || '关闭 Dashboard 失败', 'error');
  });
});
elements.pluginDashboardCloseButton?.addEventListener('click', () => {
  leavePluginDashboard().catch((error) => {
    showToast(error.message || '关闭 Dashboard 失败', 'error');
  });
});
elements.pluginDashboardRefreshButton?.addEventListener('click', () => {
  const pluginId = state.pluginDashboard.plugin?.id;
  if (!pluginId) {
    return;
  }
  runBusy(elements.pluginDashboardRefreshButton, '连接中…', () => (
    openPluginDashboard(pluginId, state.pluginDashboard.page, { pushHistory: false })
  )).catch((error) => showToast(error.message || '刷新 Dashboard 失败', 'error'));
});
elements.pluginDashboardRetryButton?.addEventListener('click', () => {
  elements.pluginDashboardRefreshButton?.click();
});
elements.pluginDashboardPageSelect?.addEventListener('change', (event) => {
  const pluginId = state.pluginDashboard.plugin?.id;
  if (!pluginId) {
    return;
  }
  const select = event.target;
  select.disabled = true;
  select.setAttribute('aria-busy', 'true');
  openPluginDashboard(pluginId, select.value, { pushHistory: false })
    .catch((error) => showToast(error.message || '切换 Dashboard 页面失败', 'error'))
    .finally(() => {
      const pageCount = state.pluginDashboard.plugin?.pages?.length || 0;
      select.disabled = pageCount <= 1;
      select.setAttribute('aria-busy', 'false');
    });
});
elements.fileRefreshButton?.addEventListener('click', async () => {
  await runBusy(elements.fileRefreshButton, null, async () => {
    await loadFiles({ forceReload: true });
    showToast('文件列表已刷新');
  });
});
elements.fileUpButton?.addEventListener('click', async () => {
  if (!state.files.canGoUp) {
    return;
  }
  const button = elements.fileUpButton;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    await loadFiles({ path: state.files.parentPath, forceReload: true });
  } finally {
    button.setAttribute('aria-busy', 'false');
    button.disabled = !state.files.canGoUp
      || state.files.loading
      || state.files.uploading
      || state.files.moving
      || state.files.downloading;
  }
});
elements.fileCreateButton?.addEventListener('click', () => {
  openFileCreateModal('file');
});
elements.fileUploadButton?.addEventListener('click', () => {
  setFileUploadVisible(!state.files.uploadVisible);
});
elements.fileDeleteSelectedButton?.addEventListener('click', openFileDeleteModal);
elements.fileMoveSelectedButton?.addEventListener('click', async () => {
  await runBusy(elements.fileMoveSelectedButton, null, openFileMoveModal);
});
elements.fileDownloadSelectedButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.fileDownloadSelectedButton, null, downloadSelectedFileItems);
  } catch (error) {
    showToast(error.message || '下载失败', 'error');
  }
});
elements.terminalCreateButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.terminalCreateButton, null, createTerminal);
  } catch (error) {
    showToast(error.message || '创建终端失败', 'error');
  }
});
elements.terminalTabs?.addEventListener('click', async (event) => {
  if (Date.now() < state.terminal.suppressClickUntil) {
    event.preventDefault();
    return;
  }
  const closeButton = event.target.closest('[data-terminal-close]');
  if (closeButton) {
    event.preventDefault();
    event.stopPropagation();
    try {
      await runBusy(closeButton, null, () => closeTerminal(closeButton.dataset.terminalClose || ''));
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
elements.terminalTabs?.addEventListener('keydown', (event) => {
  const trigger = event.target.closest('[role="tab"][data-terminal-activate]');
  if (!trigger) {
    return;
  }
  const tabs = state.terminal.items;
  const currentIndex = tabs.findIndex((item) => item.id === trigger.dataset.terminalActivate);
  if (currentIndex < 0) {
    return;
  }
  const isReorder = event.altKey && event.shiftKey && ['ArrowLeft', 'ArrowRight'].includes(event.key);
  if (isReorder) {
    event.preventDefault();
    const targetIndex = Math.max(0, Math.min(tabs.length - 1, currentIndex + (event.key === 'ArrowLeft' ? -1 : 1)));
    if (targetIndex !== currentIndex) {
      const activeId = tabs[currentIndex].id;
      reorderTerminalTabs(activeId, tabs[targetIndex].id, { animate: false });
      elements.terminalTabs.querySelector(`[data-terminal-activate="${CSS.escape(activeId)}"]`)?.focus();
    }
    return;
  }
  const navigationKeys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
  if (!navigationKeys.includes(event.key)) {
    return;
  }
  event.preventDefault();
  let targetIndex = currentIndex;
  if (event.key === 'Home') targetIndex = 0;
  if (event.key === 'End') targetIndex = tabs.length - 1;
  if (event.key === 'ArrowLeft') targetIndex = (currentIndex - 1 + tabs.length) % tabs.length;
  if (event.key === 'ArrowRight') targetIndex = (currentIndex + 1) % tabs.length;
  state.terminal.activeId = tabs[targetIndex].id;
  renderTerminals();
  elements.terminalTabs.querySelector(`[data-terminal-activate="${CSS.escape(state.terminal.activeId)}"]`)?.focus();
});

function syncTerminalItemsFromDom() {
  const itemById = new Map(state.terminal.items.map((item) => [item.id, item]));
  state.terminal.items = Array.from(elements.terminalTabs?.querySelectorAll('[data-terminal-id]') || [])
    .map((tab) => itemById.get(tab.dataset.terminalId))
    .filter(Boolean);
}

function restoreTerminalOrder(order, { animate = true, excludeId = '' } = {}) {
  const previousPositions = getTerminalTabPositions();
  const itemById = new Map(state.terminal.items.map((item) => [item.id, item]));
  const tabById = new Map(
    Array.from(elements.terminalTabs?.querySelectorAll('[data-terminal-id]') || [])
      .map((tab) => [tab.dataset.terminalId, tab]),
  );
  state.terminal.items = order.map((id) => itemById.get(id)).filter(Boolean);
  for (const id of order) {
    const tab = tabById.get(id);
    if (tab) {
      elements.terminalTabs.appendChild(tab);
    }
  }
  if (animate) {
    animateTerminalTabLayout(previousPositions, { excludeId });
  }
}

function positionDraggedTerminal(drag, clientX) {
  const rect = drag.element.getBoundingClientRect();
  const baseLeft = rect.left - drag.position;
  const desiredLeft = clientX - drag.grabOffsetX;
  drag.position = desiredLeft - baseLeft;
  drag.element.style.transform = `translateX(${drag.position}px)`;
}

function moveTerminalPlaceholder(drag, clientX) {
  let moved = true;
  while (moved) {
    moved = false;
    const tabs = Array.from(elements.terminalTabs?.querySelectorAll('[data-terminal-id]') || []);
    const index = tabs.indexOf(drag.element);
    const previous = tabs[index - 1];
    const next = tabs[index + 1];
    if (previous && clientX < previous.getBoundingClientRect().left + (previous.getBoundingClientRect().width / 2)) {
      const positions = getTerminalTabPositions();
      elements.terminalTabs.insertBefore(drag.element, previous);
      syncTerminalItemsFromDom();
      animateTerminalTabLayout(positions, { excludeId: drag.fromId });
      positionDraggedTerminal(drag, clientX);
      moved = true;
      continue;
    }
    if (next && clientX > next.getBoundingClientRect().left + (next.getBoundingClientRect().width / 2)) {
      const positions = getTerminalTabPositions();
      elements.terminalTabs.insertBefore(drag.element, next.nextSibling);
      syncTerminalItemsFromDom();
      animateTerminalTabLayout(positions, { excludeId: drag.fromId });
      positionDraggedTerminal(drag, clientX);
      moved = true;
    }
  }
}

function stopTerminalAutoScroll() {
  if (state.terminal.autoScrollFrame) {
    window.cancelAnimationFrame(state.terminal.autoScrollFrame);
    state.terminal.autoScrollFrame = 0;
  }
}

function startTerminalAutoScroll() {
  stopTerminalAutoScroll();
  const step = () => {
    const drag = state.terminal.pointerDrag;
    if (!drag?.active || !elements.terminalTabs) {
      state.terminal.autoScrollFrame = 0;
      return;
    }
    const rect = elements.terminalTabs.getBoundingClientRect();
    let scrollDelta = 0;
    if (drag.lastX < rect.left + 40) {
      scrollDelta = -12 * clampMotionValue((rect.left + 40 - drag.lastX) / 40, 0, 1);
    } else if (drag.lastX > rect.right - 40) {
      scrollDelta = 12 * clampMotionValue((drag.lastX - (rect.right - 40)) / 40, 0, 1);
    }
    if (Math.abs(scrollDelta) > 0.1) {
      const previousScroll = elements.terminalTabs.scrollLeft;
      elements.terminalTabs.scrollLeft += scrollDelta;
      if (elements.terminalTabs.scrollLeft !== previousScroll) {
        positionDraggedTerminal(drag, drag.lastX);
        moveTerminalPlaceholder(drag, drag.lastX);
      }
    }
    state.terminal.autoScrollFrame = window.requestAnimationFrame(step);
  };
  state.terminal.autoScrollFrame = window.requestAnimationFrame(step);
}

elements.terminalTabs?.addEventListener('pointerdown', (event) => {
  const handle = event.target.closest('[data-terminal-drag-handle]');
  if (event.button !== 0 || !handle || state.terminal.pointerDrag) {
    return;
  }
  const tab = handle.closest('[data-terminal-id]');
  if (!tab) {
    return;
  }
  const rect = tab.getBoundingClientRect();
  const presentationPosition = getTransformTranslate(tab);
  tab.getAnimations().forEach((animation) => animation.cancel());
  const interrupted = cancelMotionAnimation(tab);
  const position = interrupted?.value ?? presentationPosition;
  state.terminal.pointerDrag = {
    pointerId: event.pointerId,
    fromId: tab.dataset.terminalId || '',
    originalOrder: state.terminal.items.map((item) => item.id),
    startX: event.clientX,
    startY: event.clientY,
    lastX: event.clientX,
    grabOffsetX: event.clientX - rect.left,
    position,
    active: false,
    element: tab,
    handle,
    samples: [],
  };
  addVelocitySample(state.terminal.pointerDrag.samples, event.clientX, event.timeStamp);
  elements.terminalTabs.setPointerCapture?.(event.pointerId);
});

elements.terminalTabs?.addEventListener('pointermove', (event) => {
  const drag = state.terminal.pointerDrag;
  if (!drag || drag.pointerId !== event.pointerId) {
    return;
  }
  const deltaX = event.clientX - drag.startX;
  const deltaY = event.clientY - drag.startY;
  if (!drag.active && Math.hypot(deltaX, deltaY) < 8) {
    return;
  }
  drag.active = true;
  drag.lastX = event.clientX;
  event.preventDefault();
  drag.element.classList.add('dragging');
  addVelocitySample(drag.samples, event.clientX, event.timeStamp);
  positionDraggedTerminal(drag, event.clientX);
  moveTerminalPlaceholder(drag, event.clientX);
  if (!state.terminal.autoScrollFrame) {
    startTerminalAutoScroll();
  }
});

function finishTerminalPointerDrag(event, cancelled = false) {
  const drag = state.terminal.pointerDrag;
  if (!drag || drag.pointerId !== event.pointerId) {
    return;
  }
  state.terminal.pointerDrag = null;
  stopTerminalAutoScroll();
  elements.terminalTabs?.releasePointerCapture?.(event.pointerId);
  if (!drag.active) {
    return;
  }
  state.terminal.suppressClickUntil = Date.now() + 160;
  const visualLeft = drag.element.getBoundingClientRect().left;
  if (cancelled) {
    restoreTerminalOrder(drag.originalOrder, {
      animate: !REDUCED_MOTION_QUERY.matches,
      excludeId: drag.fromId,
    });
    const baseLeft = drag.element.getBoundingClientRect().left - drag.position;
    drag.position = visualLeft - baseLeft;
    drag.element.style.transform = `translateX(${drag.position}px)`;
  }
  const velocity = cancelled ? 0 : getGestureVelocity(drag.samples);
  const settle = () => {
    drag.element.classList.remove('dragging');
    drag.element.style.transform = '';
  };
  if (REDUCED_MOTION_QUERY.matches) {
    settle();
  } else {
    springTo(drag.element, {
      from: drag.position,
      target: 0,
      velocity,
      ...SETTLE_SPRING,
      apply: (value) => {
        drag.position = value;
        drag.element.style.transform = `translateX(${value}px)`;
      },
      complete: settle,
    });
  }
  if (!cancelled) {
    const committedOrder = state.terminal.items.map((item) => item.id);
    saveTerminalOrder().catch((error) => {
      if (state.terminal.items.map((item) => item.id).join('\0') === committedOrder.join('\0')) {
        cancelMotionAnimation(drag.element);
        drag.element.classList.remove('dragging');
        drag.element.style.transform = '';
        restoreTerminalOrder(drag.originalOrder, { animate: true });
      }
      showToast(error.message || '终端顺序保存失败，已恢复原顺序', 'error');
    });
  }
}

elements.terminalTabs?.addEventListener('pointerup', (event) => finishTerminalPointerDrag(event));
elements.terminalTabs?.addEventListener('pointercancel', (event) => finishTerminalPointerDrag(event, true));
elements.terminalTabs?.addEventListener('lostpointercapture', (event) => finishTerminalPointerDrag(event, true));
elements.fileSelectAllInput?.addEventListener('change', (event) => {
  setAllFileSelection(event.target.checked);
});
for (const button of elements.navButtons) {
  button.addEventListener('click', async () => {
    try {
      await navigateToPage(button.dataset.page);
    } catch (error) {
      showToast(error.message || '页面切换失败', 'error');
    }
  });
}
    elements.pluginCloseModalButton?.addEventListener('click', closePluginModal);
    elements.pluginCancelButton?.addEventListener('click', closePluginModal);
    elements.pluginListEditorCloseButton?.addEventListener('click', closePluginListEditor);
    elements.pluginListEditorCancelButton?.addEventListener('click', closePluginListEditor);
    elements.pluginListEditorConfirmButton?.addEventListener('click', applyPluginListEditor);
    elements.pluginListEditorForm?.addEventListener('submit', (event) => {
      event.preventDefault();
      addPluginListEditorValue();
    });
    elements.pluginListEditorItems?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-plugin-list-remove]');
      const editor = state.plugins.listEditor;
      if (!button || !editor) {
        return;
      }
      const value = Number(button.dataset.pluginListRemove);
      editor.values = editor.values.filter((item) => item !== value);
      setFormResult(elements.pluginListEditorStatus);
      renderPluginListEditor();
      elements.pluginListEditorInput?.focus();
    });
    elements.pluginSettingsForm?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-plugin-list-edit]');
      if (!button) {
        return;
      }
      try {
        openPluginListEditor(button.dataset.pluginListEdit || '', button);
      } catch (error) {
        setFormResult(elements.pluginFormStatus, error.message || '表情 ID 列表打开失败', 'error');
        showToast(error.message || '表情 ID 列表打开失败', 'error');
      }
    });
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
    await runBusy(elements.userMappingsSearchButton, '搜索中…', loadUserMappings);
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
  try {
    await runBusy(button, '刷新中…', async () => {
      await loadUserMappings();
      showToast('用户映射列表已刷新', 'success');
    });
  } catch (error) {
    showToast(error.message || '用户映射刷新失败', 'error');
  }
});
elements.userMappingsPrevButton?.addEventListener('click', async () => {
  state.userMappings.offset = Math.max(
    0,
    state.userMappings.offset - state.userMappings.limit,
  );
  const button = elements.userMappingsPrevButton;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    await loadUserMappings();
  } catch (error) {
    showToast(error.message || '用户映射翻页失败', 'error');
  } finally {
    button.setAttribute('aria-busy', 'false');
    button.disabled = state.userMappings.offset <= 0;
  }
});
elements.userMappingsNextButton?.addEventListener('click', async () => {
  state.userMappings.offset += state.userMappings.limit;
  const button = elements.userMappingsNextButton;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    await loadUserMappings();
  } catch (error) {
    state.userMappings.offset = Math.max(
      0,
      state.userMappings.offset - state.userMappings.limit,
    );
    showToast(error.message || '用户映射翻页失败', 'error');
  } finally {
    button.setAttribute('aria-busy', 'false');
    button.disabled = state.userMappings.offset + state.userMappings.limit >= state.userMappings.total;
  }
});
elements.userMappingsTableBody?.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-identity-save], [data-identity-delete]');
  if (!button) {
    return;
  }
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
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
    button.setAttribute('aria-busy', 'false');
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
    await runBusy(elements.fileCreateSubmitButton, '创建中…', createFileManagerItem);
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
  button.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      return;
    }
    event.preventDefault();
    const buttons = elements.fileCreateTypeButtons;
    const currentIndex = buttons.indexOf(button);
    let targetIndex = currentIndex;
    if (event.key === 'ArrowLeft') targetIndex = (currentIndex - 1 + buttons.length) % buttons.length;
    if (event.key === 'ArrowRight') targetIndex = (currentIndex + 1) % buttons.length;
    if (event.key === 'Home') targetIndex = 0;
    if (event.key === 'End') targetIndex = buttons.length - 1;
    const target = buttons[targetIndex];
    setFileCreateType(target.dataset.fileCreateType);
    target.focus();
  });
}
elements.fileDeleteCloseButton?.addEventListener('click', closeFileDeleteModal);
elements.fileDeleteCancelButton?.addEventListener('click', closeFileDeleteModal);
elements.fileDeleteConfirmButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.fileDeleteConfirmButton, '删除中…', deleteSelectedFileItems);
  } catch (error) {
    showToast(error.message || '删除失败', 'error');
  }
});
elements.fileMoveCloseButton?.addEventListener('click', closeFileMoveModal);
elements.fileMoveCancelButton?.addEventListener('click', closeFileMoveModal);
elements.fileMoveConfirmButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.fileMoveConfirmButton, '移动中…', moveSelectedFileItems);
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
elements.fileMoveTree?.addEventListener('keydown', async (event) => {
  const current = event.target.closest('[role="treeitem"]');
  if (!current) {
    return;
  }
  const visibleItems = Array.from(elements.fileMoveTree.querySelectorAll('[role="treeitem"]'));
  const currentIndex = visibleItems.indexOf(current);
  if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    let targetIndex = currentIndex;
    if (event.key === 'ArrowDown') targetIndex = Math.min(visibleItems.length - 1, currentIndex + 1);
    if (event.key === 'ArrowUp') targetIndex = Math.max(0, currentIndex - 1);
    if (event.key === 'Home') targetIndex = 0;
    if (event.key === 'End') targetIndex = visibleItems.length - 1;
    focusMoveTreeItem(visibleItems[targetIndex]);
    return;
  }
  const pathValue = normalizeFilePath(current.dataset.fileMovePath || '');
  if (event.key === 'ArrowRight') {
    event.preventDefault();
    if (current.getAttribute('aria-expanded') !== 'true') {
      await setMoveTreeExpanded(pathValue, true);
    } else {
      const child = current.querySelector('[role="group"] > [role="treeitem"]');
      focusMoveTreeItem(child || current);
    }
    return;
  }
  if (event.key === 'ArrowLeft') {
    event.preventDefault();
    if (current.getAttribute('aria-expanded') === 'true') {
      await setMoveTreeExpanded(pathValue, false);
    } else {
      const parentPath = pathValue.split('/').slice(0, -1).join('/');
      const parent = visibleItems.find((item) => normalizeFilePath(item.dataset.fileMovePath || '') === parentPath);
      focusMoveTreeItem(parent || current);
    }
    return;
  }
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    await selectMoveTarget(pathValue);
  }
});
elements.fileRenameCloseButton?.addEventListener('click', closeFileRenameModal);
elements.fileRenameCancelButton?.addEventListener('click', closeFileRenameModal);
elements.fileRenameSubmitButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.fileRenameSubmitButton, '重命名中…', renameFileManagerItem);
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
    await runBusy(elements.fileAuthSubmitButton, '验证中…', async () => {
      const password = String(elements.fileAuthPasswordInput?.value || '');
      if (state.files.pendingAuthMode === 'preview') {
        await readFileForPreview(item, password);
      } else {
        await openTextFileForEdit(item, password);
      }
      closeFileAuthModal();
    });
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
    await runBusy(elements.fileSaveConfirmSubmitButton, '保存中…', saveFileEditContent);
  } catch (error) {
    showToast(error.message || '保存失败', 'error');
  }
});
elements.form?.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!elements.form.reportValidity()) {
    return;
  }
  try {
    await runBusy(elements.submitButton, '保存中…', saveBot);
  } catch (error) {
    setFormResult(elements.botFormStatus, error.message || '保存失败', 'error');
    showToast(error.message || '保存失败', 'error');
  }
});
elements.settingsPasswordSaveButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.settingsPasswordSaveButton, '保存中…', savePasswordSettings);
  } catch (error) {
    setFormResult(elements.settingsPasswordResult, error.message || '设置保存失败', 'error');
    showToast(error.message || '设置保存失败', 'error');
  }
});
elements.settingsPortSaveButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.settingsPortSaveButton, '保存中…', savePortSettings);
  } catch (error) {
    setFormResult(elements.settingsPortResult, error.message || '设置保存失败', 'error');
    showToast(error.message || '设置保存失败', 'error');
  }
});
elements.settingsMessageIndexRebuildButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.settingsMessageIndexRebuildButton, '整理中…', rebuildMessageIndexes);
  } catch (error) {
    setFormResult(elements.settingsPerformanceResult, error.message || '手动整理消息映射窗口失败', 'error');
    showToast(error.message || '手动整理消息映射窗口失败', 'error');
  }
});
elements.settingsPerformanceSaveButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.settingsPerformanceSaveButton, '保存中…', savePerformanceSettings);
  } catch (error) {
    setFormResult(elements.settingsPerformanceResult, error.message || '性能与资源设置保存失败', 'error');
    showToast(error.message || '性能与资源设置保存失败', 'error');
  }
});
elements.settingsExportConfigButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.settingsExportConfigButton, '导出中…', exportShellConfiguration);
    setFormResult(elements.settingsConfigResult, '配置已导出。', 'success');
    showToast('配置已导出', 'success');
  } catch (error) {
    if (isAbortError(error)) {
      return;
    }
    setFormResult(elements.settingsConfigResult, error.message || '配置导出失败', 'error');
    showToast(error.message || '配置导出失败', 'error');
  }
});
elements.settingsImportConfigButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.settingsImportConfigButton, '导入中…', importShellConfiguration);
    setFormResult(elements.settingsConfigResult, '配置已导入。', 'success');
    showToast('配置已导入', 'success');
  } catch (error) {
    if (isAbortError(error)) {
      return;
    }
    setFormResult(elements.settingsConfigResult, error.message || '配置导入失败', 'error');
    showToast(error.message || '配置导入失败', 'error');
  }
});
elements.pluginSaveButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.pluginSaveButton, '保存中…', savePluginSettings);
  } catch (error) {
    setFormResult(elements.pluginFormStatus, error.message || '插件设置保存失败', 'error');
    showToast(error.message || '插件设置保存失败', 'error');
  }
});
elements.pluginUninstallConfirmButton?.addEventListener('click', async () => {
  try {
    await runBusy(elements.pluginUninstallConfirmButton, '卸载中…', confirmUninstallPlugin);
  } catch (error) {
    showToast(error.message || '插件卸载失败', 'error');
  }
});

elements.fileBreadcrumb?.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-file-path]');
  if (!button) {
    return;
  }
  try {
    await runBusy(button, null, () => (
      loadFiles({ path: button.dataset.filePath || '', forceReload: true })
    ));
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
        await runBusy(actionButton, null, () => openSingleFileMoveModal(item));
        return;
      }
      if (actionButton.dataset.fileAction === 'copy') {
        await runBusy(actionButton, null, () => copyFileRelativePath(item));
        return;
      }
      if (actionButton.dataset.fileAction === 'download') {
        await runBusy(actionButton, null, () => downloadSingleFileItem(item));
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
    await runBusy(button, null, () => openFileItem(findFileItem(button.dataset.filePath || '')));
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
      await runBusy(button, null, () => deleteBot(id));
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
  input.disabled = true;
  input.setAttribute('aria-busy', 'true');
  try {
    await toggleBot(input.dataset.id, input.checked);
  } catch (error) {
    input.checked = !input.checked;
    showToast(error.message || '切换失败', 'error');
  } finally {
    input.disabled = false;
    input.setAttribute('aria-busy', 'false');
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
      await runBusy(button, null, () => openPluginSettings(pluginId));
      return;
    }
    if (role === 'dashboard') {
      await runBusy(button, null, () => openPluginDashboard(pluginId));
      return;
    }
    if (role === 'reload') {
      await runBusy(button, null, () => reloadPlugin(pluginId));
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
  input.disabled = true;
  input.setAttribute('aria-busy', 'true');
  try {
    await togglePlugin(input.dataset.id, input.checked);
  } catch (error) {
    input.checked = !input.checked;
    showToast(error.message || '插件切换失败', 'error');
  } finally {
    input.disabled = false;
    input.setAttribute('aria-busy', 'false');
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
  if (
    event.key === 'Tab'
    && state.ui.mobileNavigationOpen
    && !document.querySelector('dialog[open]')
    && elements.sidebar
  ) {
    const focusable = Array.from(elements.sidebar.querySelectorAll(
      'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )).filter((element) => !element.hidden && element.getClientRects().length > 0);
    if (focusable.length) {
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  }
  if (state.files.imageViewer.visible) {
    if (event.key === 'ArrowLeft') {
      moveFileImageViewer(-1);
      return;
    }
    if (event.key === 'ArrowRight') {
      moveFileImageViewer(1);
      return;
    }
  }
  if (event.key === 'Escape' && state.ui.mobileNavigationOpen && !document.querySelector('dialog[open]')) {
    setMobileNavigationOpen(false, { motion: 'instant' });
  }
});

window.addEventListener('resize', () => {
  cancelActiveCardOrderInteraction({ announce: false });
  if (state.currentPage === 'terminal' && state.terminal.activeId) {
    fitTerminal(state.terminal.activeId);
  }
});

window.addEventListener('message', (event) => {
  handlePluginDashboardBridgeMessage(event).catch((error) => {
    showToast(error.message || 'Dashboard Bridge 处理失败', 'error');
  });
});

window.addEventListener('popstate', () => {
  restoreHashRoute().catch((error) => {
    showToast(error.message || '恢复页面失败', 'error');
  });
});

MOBILE_NAVIGATION_QUERY.addEventListener?.('change', () => {
  setMobileNavigationOpen(false, { restoreFocus: false, motion: 'instant' });
});

MOBILE_SHEET_QUERY.addEventListener?.('change', () => {
  for (const dialog of document.querySelectorAll('dialog[open]')) {
    setupDialogSheetHandle(dialog);
  }
});

REDUCED_MOTION_QUERY.addEventListener?.('change', () => {
  if (!REDUCED_MOTION_QUERY.matches) {
    return;
  }
  cancelNavigationGesture({ resume: false });
  cancelActiveCardOrderInteraction({ announce: false });
  setMobileNavigationOpen(state.ui.mobileNavigationOpen, { restoreFocus: false, motion: 'instant' });
  for (const dialog of document.querySelectorAll('dialog[open]')) {
    clearDialogSheetPresentation(dialog);
    setupDialogSheetHandle(dialog);
  }
  for (const notification of getVisibleToasts()) {
    const record = toastRecords.get(notification);
    if (record?.gesture) {
      record.gesture = null;
      cancelMotionAnimation(notification);
      notification.style.transform = '';
      notification.style.opacity = '';
      delete notification.dataset.swiping;
      record.position = 0;
      resumeToast(notification, 'drag');
    }
  }
});

relocateMessageIndexSettingsSection();
document.body.classList.add('motion-runtime-ready');
setupSidebarToggleButtons();
setupDialogControllers();
setupMobileNavigationGestures();
setupCardOrderInteractions();
setMobileNavigationOpen(false, { restoreFocus: false, motion: 'instant' });
consumeStoredUpdateOutcome();

if (state.updates.transactionId) {
  showUpdateRestartOverlay(state.updates.transactionId);
  pollUpdateTransaction(state.updates.transactionId);
}

Promise.all([
  loadCardOrder({ silent: true }),
  loadLogs({ reset: true }),
])
  .then(async () => {
    await loadData();
    await restoreHashRoute({ replaceInvalid: true });
  })
  .catch((error) => {
    showToast(error.message || '加载失败', 'error');
  });
