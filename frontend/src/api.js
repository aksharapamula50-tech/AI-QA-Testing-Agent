export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export async function apiRequest(path, options = {}, config = {}) {
  const base = (config.apiBase || '').replace(/\/$/, '');
  const url = `${base}${path}`;
  const fetchImpl = config.fetchImpl || globalThis.fetch;
  let response;
  try {
    response = await fetchImpl(url, { ...options, credentials: 'include' });
  } catch (error) {
    if (error instanceof TypeError) {
      throw new ApiError(`Could not reach the QA API at ${url}. In development, Vite proxies /api to http://127.0.0.1:8000. Confirm FastAPI is running on port 8000. Browser detail: ${error.message}`);
    }
    throw error;
  }

  if (response.status === 204) return null;
  if (response.status === 401) config.onUnauthorized?.();
  const contentType = response.headers.get('content-type') || '';
  const isJson = contentType.includes('application/json') || contentType.includes('+json');
  const nonJsonError = async () => {
    const text = (await response.text()).trim().slice(0, 180);
    throw new ApiError(`Expected JSON from ${options.method || 'GET'} ${path}, but received ${contentType || 'an unknown content type'} (HTTP ${response.status}). Check that /api is routed to FastAPI and not the Vite page.${text ? ` Response: ${text}` : ''}`, response.status);
  };

  // Inspect status and content type before attempting JSON decoding. Proxy HTML
  // fallbacks and framework error pages therefore become useful API errors.
  if (!response.ok && !isJson) await nonJsonError();
  if (response.ok && !isJson) await nonJsonError();
  let body;
  try {
    body = await response.json();
  } catch {
    throw new ApiError(`The API returned invalid JSON for ${options.method || 'GET'} ${path} (HTTP ${response.status}).`, response.status);
  }
  if (!response.ok) {
    throw new ApiError(body?.detail || `QA API request ${options.method || 'GET'} ${path} failed with HTTP ${response.status}`, response.status);
  }
  return body;
}
