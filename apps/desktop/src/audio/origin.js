let apiOrigin = '';

function normalizePath(path) {
  return path.startsWith('/') ? path : `/${path}`;
}

function isTauriRuntime() {
  return typeof window !== 'undefined' && typeof window.__TAURI_INTERNALS__?.invoke === 'function';
}

export async function initApiOrigin() {
  if (import.meta.env.PROD && isTauriRuntime()) {
    apiOrigin = await window.__TAURI_INTERNALS__.invoke('server_api_origin');
  } else {
    apiOrigin = '';
  }
  return apiOrigin;
}

export function apiUrl(path) {
  return `${apiOrigin}${normalizePath(path)}`;
}

export function wsUrl(path) {
  const endpoint = normalizePath(path);
  if (apiOrigin) {
    return `${apiOrigin.replace(/^http/, 'ws')}${endpoint}`;
  }

  if (typeof window === 'undefined') {
    return endpoint;
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}${endpoint}`;
}
