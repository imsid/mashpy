// Thin client for the Mash host API.
//
// Auth rides the same-origin `mash_api_key` cookie that the /admin index
// response sets, so requests need no explicit Authorization header. Every
// successful response is wrapped as `{ ok, data }` by the server; we unwrap
// `data` here and raise a typed ApiError otherwise.

const API_BASE = '/api/v1';

export class ApiError extends Error {
  constructor(message, { status, code, details } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function buildQuery(params) {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : '';
}

async function request(path, { method = 'GET', params, body, signal } = {}) {
  const init = { method, signal, headers: {} };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(`${API_BASE}${path}${buildQuery(params)}`, init);
  } catch (cause) {
    throw new ApiError('network request failed', { details: String(cause) });
  }

  let payload = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { raw: text };
    }
  }

  if (!response.ok) {
    const err = payload?.error ?? {};
    throw new ApiError(err.message || `request failed (${response.status})`, {
      status: response.status,
      code: err.code,
      details: err.details,
    });
  }

  // Success envelope is `{ ok: true, data: {...} }`; fall back to raw payload.
  return payload && 'data' in payload ? payload.data : payload;
}

// Open a server-sent event stream. Returns the EventSource so callers can close
// it; `onEvent` receives the parsed JSON of each message.
function stream(path, params, onEvent) {
  const source = new EventSource(`${API_BASE}${path}${buildQuery(params)}`);
  source.onmessage = (event) => {
    if (!event.data) return;
    try {
      onEvent(JSON.parse(event.data));
    } catch {
      /* ignore malformed frames */
    }
  };
  return source;
}

export const api = {
  request,
  stream,

  // --- Deployment / pool ---
  health: () => request('/health'),
  listAgents: () => request('/agent'),
  getAgent: (agentId) => request(`/agent/${encodeURIComponent(agentId)}`),

  // --- Hosts ---
  listHosts: () => request('/hosts'),
  getHost: (hostId) => request(`/hosts/${encodeURIComponent(hostId)}`),
  defineHost: (hostId, body) =>
    request(`/hosts/${encodeURIComponent(hostId)}`, { method: 'PUT', body }),
  submitHostRequest: (hostId, body) =>
    request(`/hosts/${encodeURIComponent(hostId)}/request`, { method: 'POST', body }),

  // --- Logs / telemetry ---
  listTraces: (params) => request('/telemetry/traces', { params }),
  traceAnalysis: (params) => request('/telemetry/trace/analysis', { params }),
  listEvents: (params) => request('/telemetry/events', { params }),
  usage: (params) => request('/telemetry/usage', { params }),
  listApiEvents: (params) => request('/telemetry/api/events', { params }),
  streamEvents: (params, onEvent) =>
    stream('/telemetry/events/stream', params, onEvent),

  // --- Sessions ---
  listSessions: (agentId) =>
    request(`/agent/${encodeURIComponent(agentId)}/sessions`),
  sessionHistory: (agentId, sessionId) =>
    request(
      `/agent/${encodeURIComponent(agentId)}/sessions/${encodeURIComponent(sessionId)}/history`,
    ),

  // --- Feedback ---
  listFeedback: (params) => request('/feedback', { params }),
};
