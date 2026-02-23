const DEFAULT_BACKEND_BASE_URL = 'http://localhost:8001';

function normalizeBackendBaseUrl(rawUrl: string): string {
  return rawUrl.replace(/\/+$/, '').replace(/\/api$/, '');
}

function toWebSocketOrigin(httpUrl: string): string {
  try {
    const parsed = new URL(httpUrl);
    parsed.protocol = parsed.protocol === 'https:' ? 'wss:' : 'ws:';
    parsed.pathname = '';
    parsed.search = '';
    parsed.hash = '';
    return parsed.toString().replace(/\/$/, '');
  } catch {
    return httpUrl
      .replace(/^https:\/\//, 'wss://')
      .replace(/^http:\/\//, 'ws://')
      .replace(/\/+$/, '');
  }
}

export function getClientBackendBaseUrl(): string {
  const explicitBaseUrl = process.env.NEXT_PUBLIC_SERVER_BASE_URL || process.env.SERVER_BASE_URL;
  if (explicitBaseUrl) {
    return normalizeBackendBaseUrl(explicitBaseUrl);
  }

  if (typeof window !== 'undefined') {
    return normalizeBackendBaseUrl(window.location.origin);
  }

  return DEFAULT_BACKEND_BASE_URL;
}

export function getClientWebSocketUrl(): string {
  return `${toWebSocketOrigin(getClientBackendBaseUrl())}/ws/chat`;
}
