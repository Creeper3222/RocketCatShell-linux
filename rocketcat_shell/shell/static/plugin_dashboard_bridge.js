(() => {
  'use strict';

  const CHANNEL = 'rocketcat-plugin-dashboard';
  const pending = new Map();
  const subscriptions = new Map();
  let sequence = 0;

  function nextId(prefix = 'request') {
    sequence += 1;
    return `${prefix}-${Date.now()}-${sequence}`;
  }

  function send(action, payload = {}) {
    const requestId = nextId();
    return new Promise((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        pending.delete(requestId);
        reject(new Error(`RocketCat Dashboard request timed out: ${action}`));
      }, 30000);
      pending.set(requestId, { resolve, reject, timeout });
      window.parent.postMessage(
        {
          channel: CHANNEL,
          kind: 'request',
          requestId,
          action,
          payload,
        },
        '*',
      );
    });
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window.parent) {
      return;
    }
    const message = event.data;
    if (!message || message.channel !== CHANNEL) {
      return;
    }
    if (message.kind === 'event') {
      const subscription = subscriptions.get(String(message.subscriptionId || ''));
      if (!subscription) {
        return;
      }
      if (message.event === 'error') {
        subscription.onError?.(new Error(String(message.error || 'SSE failed')));
        return;
      }
      subscription.onMessage?.(message.data);
      return;
    }
    if (message.kind !== 'response') {
      return;
    }
    const request = pending.get(String(message.requestId || ''));
    if (!request) {
      return;
    }
    pending.delete(message.requestId);
    window.clearTimeout(request.timeout);
    if (message.ok) {
      request.resolve(message.result);
    } else {
      request.reject(new Error(String(message.error || 'Dashboard request failed')));
    }
  });

  const ready = send('ready');

  window.RocketCatPluginDashboard = Object.freeze({
    version: '1',
    ready,
    getContext() {
      return send('context');
    },
    apiRequest(path, options = {}) {
      return send('api', {
        path,
        method: String(options.method || 'GET').toUpperCase(),
        query: options.query || {},
        body: options.body,
        headers: options.headers || {},
      });
    },
    apiGet(path, query = {}) {
      return send('api', { path, method: 'GET', query });
    },
    apiPost(path, body = null) {
      return send('api', { path, method: 'POST', body });
    },
    upload(path, files, fields = {}) {
      const normalizedFiles = Array.from(files || []).filter(
        (file) => file instanceof File || file instanceof Blob,
      );
      return send('upload', {
        path,
        files: normalizedFiles,
        fields,
      });
    },
    download(path, query = {}) {
      return send('download', { path, query });
    },
    async subscribeSSE(path, handlers = {}, query = {}) {
      const subscriptionId = nextId('sse');
      subscriptions.set(subscriptionId, {
        onMessage: handlers.onMessage,
        onError: handlers.onError,
      });
      try {
        await send('sse-subscribe', { subscriptionId, path, query });
      } catch (error) {
        subscriptions.delete(subscriptionId);
        throw error;
      }
      return subscriptionId;
    },
    async unsubscribeSSE(subscriptionId) {
      const normalizedId = String(subscriptionId || '');
      subscriptions.delete(normalizedId);
      await send('sse-unsubscribe', { subscriptionId: normalizedId });
    },
  });

  window.dispatchEvent(new CustomEvent('rocketcat-dashboard-ready'));
})();
