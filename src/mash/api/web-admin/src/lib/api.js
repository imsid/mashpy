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

// The OpenAPI schema lives at the app root, outside the /api/v1 envelope.
async function openapi() {
  const response = await fetch('/openapi.json');
  if (!response.ok) {
    throw new ApiError(`failed to load OpenAPI schema (${response.status})`, {
      status: response.status,
    });
  }
  return response.json();
}

export const api = {
  request,
  openapi,

  // --- Deployment / pool ---
  health: () => request('/health'),
  listAgents: () => request('/agent'),
  getAgent: (agentId) => request(`/agent/${encodeURIComponent(agentId)}`),
  listTools: () => request('/tools'),
  listSkills: () => request('/skills'),
  listToolInvocations: () => request('/telemetry/tool-invocations'),
  listSkillInvocations: () => request('/telemetry/skill-invocations'),

  // --- Hosts ---
  listHosts: () => request('/hosts'),
  getHost: (hostId) => request(`/hosts/${encodeURIComponent(hostId)}`),
  defineHost: (hostId, body) =>
    request(`/hosts/${encodeURIComponent(hostId)}`, { method: 'PUT', body }),
  submitHostRequest: (hostId, body) =>
    request(`/hosts/${encodeURIComponent(hostId)}/request`, { method: 'POST', body }),

  // --- Logs / telemetry ---
  listSessionRollups: (params) => request('/telemetry/sessions', { params }),
  listTraces: (params) => request('/telemetry/traces', { params }),
  traceAnalysis: (params) => request('/telemetry/trace/analysis', { params }),
  listEvents: (params) => request('/telemetry/events', { params }),
  usage: (params) => request('/telemetry/usage', { params }),
  listApiEvents: (params) => request('/telemetry/api/events', { params }),
  listCommandEvents: (params) => request('/telemetry/command-events', { params }),

  // --- Sessions ---
  listSessions: (agentId) =>
    request(`/agent/${encodeURIComponent(agentId)}/sessions`),
  sessionHistory: (agentId, sessionId) =>
    request(
      `/agent/${encodeURIComponent(agentId)}/sessions/${encodeURIComponent(sessionId)}/history`,
    ),
  sessionSignals: (agentId, sessionId) =>
    request(
      `/agent/${encodeURIComponent(agentId)}/sessions/${encodeURIComponent(sessionId)}/signals`,
    ),

  // --- Workflows ---
  listWorkflows: () => request('/workflow'),

  // --- Feedback ---
  listFeedback: (params) => request('/feedback', { params }),
};
