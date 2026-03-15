import React, { useEffect, useMemo, useState } from 'react';

const API_BASE = '/api/v1';
const MAX_EVENTS = 5000;
const DEFAULT_LIMIT = 2000;
const DEFAULT_SEARCH_LIMIT = 10;

const CLASS_STYLES = {
  AgentTraceEvent: 'bg-emerald-100 text-emerald-800 border-emerald-200',
  LLMEvent: 'bg-sky-100 text-sky-800 border-sky-200',
  CommandEvent: 'bg-amber-100 text-amber-800 border-amber-200',
  DebugEvent: 'bg-rose-100 text-rose-800 border-rose-200',
  MCPEvent: 'bg-indigo-100 text-indigo-800 border-indigo-200'
};

export default function App() {
  const [events, setEvents] = useState([]);
  const [selectedTraceId, setSelectedTraceId] = useState(null);
  const [selectedSessionId, setSelectedSessionId] = useState(null);
  const [selectedSearchTurnId, setSelectedSearchTurnId] = useState(null);
  const [expanded, setExpanded] = useState(new Set());
  const [status, setStatus] = useState({ connected: false, error: null });
  const [logPath, setLogPath] = useState('');
  const [health, setHealth] = useState({ memorySearchAvailable: false });
  const [telemetryAgentId, setTelemetryAgentId] = useState(null);

  const [searchText, setSearchText] = useState('');
  const [searchTarget, setSearchTarget] = useState('user');
  const [searchResults, setSearchResults] = useState([]);
  const [searchStatus, setSearchStatus] = useState('idle');
  const [searchError, setSearchError] = useState(null);
  const [activeSearchContext, setActiveSearchContext] = useState(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const healthResponse = await fetch(`${API_BASE}/health`);
        const healthPayload = await healthResponse.json().catch(() => ({}));
        if (!healthResponse.ok) {
          throw new Error(apiErrorMessage(healthPayload, `Health check failed (${healthResponse.status})`));
        }

        const healthData = apiData(healthPayload) || {};
        const resolvedAgentId = normalizeText(healthData?.deployment?.primary_agent_id);
        if (!resolvedAgentId) {
          throw new Error('Could not resolve primary agent id from deployment health.');
        }

        const logsResponse = await fetch(
          `${API_BASE}/telemetry/events?${new URLSearchParams({
            agent_id: resolvedAgentId,
            limit: String(DEFAULT_LIMIT)
          }).toString()}`
        );
        const logsPayload = await logsResponse.json().catch(() => ({}));
        if (!logsResponse.ok) {
          throw new Error(apiErrorMessage(logsPayload, `Logs request failed (${logsResponse.status})`));
        }

        const logsData = apiData(logsPayload) || {};
        if (cancelled) {
          return;
        }

        setTelemetryAgentId(resolvedAgentId);
        setHealth({
          memorySearchAvailable: Boolean(
            healthData?.observability?.memory?.search_available ?? healthData?.memory?.search_available
          )
        });
        setEvents(Array.isArray(logsData.events) ? logsData.events : []);
        setLogPath(logsData.path || '');
      } catch (err) {
        if (cancelled) {
          return;
        }
        setStatus((prev) => ({ ...prev, error: String(err) }));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!telemetryAgentId) {
      return undefined;
    }

    const stream = new EventSource(
      `${API_BASE}/telemetry/events/stream?${new URLSearchParams({ agent_id: telemetryAgentId }).toString()}`
    );
    stream.onopen = () => setStatus({ connected: true, error: null });
    stream.onerror = () => setStatus({ connected: false, error: 'stream disconnected' });
    stream.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data);
        setEvents((prev) => {
          const next = [...prev, event];
          return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next;
        });
      } catch (err) {
        setStatus((prev) => ({ ...prev, error: String(err) }));
      }
    };

    return () => stream.close();
  }, [telemetryAgentId]);

  const sessionMap = useMemo(() => {
    const map = new Map();
    for (const event of events) {
      const sessionId = event.session_id || 'unknown';
      if (!map.has(sessionId)) {
        map.set(sessionId, { events: [], traces: new Map(), appId: null });
      }
      const session = map.get(sessionId);
      session.events.push(event);

      const appId = normalizeAppId(event?.app_id);
      if (appId) {
        session.appId = appId;
      }

      let traceId = event.trace_id;
      if (!traceId && event.event_class === 'CommandEvent') {
        traceId = '__commands__';
      }
      if (!traceId) {
        continue;
      }
      if (!session.traces.has(traceId)) {
        session.traces.set(traceId, []);
      }
      session.traces.get(traceId).push(event);
    }

    for (const session of map.values()) {
      for (const list of session.traces.values()) {
        list.sort((a, b) => (timestamp(a) ?? 0) - (timestamp(b) ?? 0));
      }
    }
    return map;
  }, [events]);

  const sessionList = useMemo(() => {
    return Array.from(sessionMap.entries())
      .map(([sessionId, data]) => {
        const timestamps = data.events.map((event) => timestamp(event)).filter((value) => value != null);
        const start = timestamps.length ? Math.min(...timestamps) : null;
        const end = timestamps.length ? Math.max(...timestamps) : null;
        const traces = Array.from(data.traces.entries())
          .map(([traceId, list]) => {
            const startTrace = timestamp(list[0]);
            const endTrace = timestamp(list[list.length - 1]);
            return {
              id: traceId,
              label: traceId === '__commands__' ? 'commands' : traceId,
              count: list.length,
              start: startTrace,
              end: endTrace
            };
          })
          .sort((a, b) => (b.end ?? 0) - (a.end ?? 0));
        return { id: sessionId, start, end, traces, appId: data.appId };
      })
      .sort((a, b) => (b.end ?? 0) - (a.end ?? 0));
  }, [sessionMap]);

  useEffect(() => {
    if (!selectedSessionId && sessionList.length > 0) {
      setSelectedSessionId(sessionList[0].id);
    }
  }, [selectedSessionId, sessionList]);

  useEffect(() => {
    if (!selectedSessionId) {
      return;
    }
    const session = sessionMap.get(selectedSessionId);
    const available = session ? session.traces : null;
    if (!available || available.size === 0) {
      return;
    }
    if (!selectedTraceId) {
      const firstTrace = Array.from(available.keys())[0];
      setSelectedTraceId(firstTrace);
      return;
    }
    if (!available.has(selectedTraceId)) {
      const firstTrace = Array.from(available.keys())[0];
      setSelectedTraceId(firstTrace);
    }
  }, [selectedSessionId, selectedTraceId, selectedSearchTurnId, sessionMap]);

  useEffect(() => {
    if (!activeSearchContext) {
      return;
    }
    if (activeSearchContext.sessionId === selectedSessionId) {
      return;
    }
    setSearchResults([]);
    setSearchStatus('idle');
    setSearchError(null);
    setActiveSearchContext(null);
    setSelectedSearchTurnId(null);
  }, [activeSearchContext, selectedSessionId]);

  const selectedSession = selectedSessionId ? sessionMap.get(selectedSessionId) : null;
  const selectedSessionAppId = selectedSession?.appId || null;
  const selectedEvents =
    selectedSession && selectedTraceId ? selectedSession.traces.get(selectedTraceId) || [] : [];
  const selectedTraceExists = Boolean(
    selectedSession && selectedTraceId && selectedSession.traces.has(selectedTraceId)
  );
  const baseTs = selectedEvents.length ? timestamp(selectedEvents[0]) : null;
  const canSearch = Boolean(selectedSessionId && selectedSessionAppId && health.memorySearchAvailable);

  const toggleExpand = (key) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const expandAll = () => {
    const next = new Set();
    selectedEvents.forEach((event, idx) => next.add(eventKey(event, idx)));
    setExpanded(next);
  };

  const collapseAll = () => setExpanded(new Set());

  const handleSessionSelect = (sessionId) => {
    setSelectedSearchTurnId(null);
    setSelectedSessionId(sessionId);
  };

  const handleTraceSelect = (traceId) => {
    setSelectedSearchTurnId(null);
    setSelectedTraceId(traceId);
  };

  const handleSearchSubmit = async (event) => {
    event.preventDefault();
    const trimmed = searchText.trim();
    if (!trimmed) {
      setSearchStatus('error');
      setSearchError('Enter a search query.');
      return;
    }
    if (!selectedSessionAppId) {
      setSearchStatus('error');
      setSearchError('Select a session with an app_id before searching.');
      return;
    }

    const query = buildSearchQuery(searchTarget, trimmed);
    const params = new URLSearchParams({
      q: query,
      app_id: selectedSessionAppId,
      session_id: selectedSessionId,
      limit: String(DEFAULT_SEARCH_LIMIT)
    });

    setSearchStatus('loading');
    setSearchError(null);
    setSelectedSearchTurnId(null);

    try {
      const response = await fetch(`${API_BASE}/telemetry/memory/search?${params.toString()}`);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(apiErrorMessage(payload, `Search failed (${response.status})`));
      }

      const data = apiData(payload) || {};
      const results = Array.isArray(data.results) ? data.results : [];
      setSearchResults(results);
      setSearchStatus('success');
      setSearchError(null);
      setActiveSearchContext({
        appId: selectedSessionAppId,
        sessionId: selectedSessionId,
        target: searchTarget,
        query,
        text: trimmed
      });
    } catch (err) {
      setSearchStatus('error');
      setSearchError(String(err));
    }
  };

  const handleSearchResultClick = (result) => {
    const turnId = result.turn_id || null;
    if (!turnId) {
      return;
    }
    if (result.session_id && result.session_id !== selectedSessionId) {
      return;
    }

    setSearchError(null);
    setSelectedSearchTurnId(turnId);

    if (selectedSession?.traces.has(turnId)) {
      setSelectedTraceId(turnId);
      return;
    }

    setSearchError('Selected memory hit is not present in the loaded telemetry trace list for this session.');
  };

  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_top,_#f8fafc,_#fef3c7_45%,_#f1f5f9)] text-slate-900">
      <header className="border-b border-slate-200/70 bg-white/70 backdrop-blur">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-6 py-5">
          <div className="flex items-center gap-3">
            <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-900 text-base font-semibold text-amber-50">
              M
            </div>
            <div>
              <p className="font-[600] tracking-tight text-lg font-display">Mash Telemetry</p>
              <p className="text-xs text-slate-500">Live trace waterfall - {logPath || 'loading log path'}</p>
            </div>
          </div>
          <div className="flex items-center gap-3 text-sm">
            <span
              className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 ${
                status.connected
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                  : 'border-slate-200 bg-slate-100 text-slate-500'
              }`}
            >
              <span className={`h-2 w-2 rounded-full ${status.connected ? 'bg-emerald-500' : 'bg-slate-400'}`} />
              {status.connected ? 'Live' : 'Paused'}
            </span>
          </div>
        </div>
      </header>

      <main className="mx-auto grid w-full max-w-6xl grid-cols-1 gap-6 px-6 py-6 lg:grid-cols-[320px_1fr]">
        <aside className="rounded-3xl border border-slate-200/70 bg-white/80 p-4 shadow-[0_20px_60px_-40px_rgba(15,23,42,0.4)]">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-500">
              Sessions
            </h2>
            <span className="text-xs text-slate-400">{sessionList.length} total</span>
          </div>
          <div className="mt-4 space-y-2">
            {sessionList.length === 0 && (
              <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-4 py-6 text-center text-sm text-slate-500">
                No trace events yet.
              </div>
            )}
            {sessionList.map((session) => (
              <div
                key={session.id}
                className={`w-full rounded-2xl border px-4 py-3 text-left transition ${
                  session.id === selectedSessionId
                    ? 'border-slate-900 bg-slate-900 text-amber-50 shadow-lg'
                    : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                }`}
              >
                <button className="w-full text-left" onClick={() => handleSessionSelect(session.id)}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-xs">{session.id}</span>
                    <span className="rounded-full bg-white/20 px-2 py-0.5 text-xs font-medium">
                      {session.traces.length}
                    </span>
                  </div>
                  <div className="mt-2 text-xs opacity-80">
                    {formatDateTime(session.end)} - {formatDuration(session.start, session.end)}
                  </div>
                  {session.appId && (
                    <div className="mt-1 truncate font-mono text-[10px] opacity-75">app {session.appId}</div>
                  )}
                </button>
                {session.id === selectedSessionId && (
                  <div className="mt-3 space-y-2 border-t border-white/10 pt-3">
                    {session.traces.map((trace) => {
                      const isSelectedTrace = trace.id === selectedTraceId;
                      const isSearchMatch = trace.id === selectedSearchTurnId;
                      return (
                        <button
                          key={`${session.id}-${trace.id}`}
                          className={`w-full rounded-2xl border px-3 py-2 text-left text-xs transition ${
                            isSelectedTrace
                              ? 'border-amber-200 bg-amber-50 text-slate-900'
                              : isSearchMatch
                                ? 'border-sky-200 bg-sky-50 text-slate-900 hover:border-sky-300'
                                : 'border-white/20 bg-white/10 text-slate-100 hover:border-white/40'
                          } ${isSearchMatch ? 'ring-1 ring-sky-200/80' : ''}`}
                          onClick={() => handleTraceSelect(trace.id)}
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="truncate font-mono">{trace.label}</span>
                            <span className="rounded-full border border-white/30 px-2 py-0.5 text-[10px]">
                              {trace.count}
                            </span>
                          </div>
                          <div className="mt-1 text-[10px] opacity-80">
                            {formatDateTime(trace.end)} - {formatDuration(trace.start, trace.end)}
                          </div>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            ))}
          </div>
        </aside>

        <section className="rounded-3xl border border-slate-200/70 bg-white/80 p-6 shadow-[0_20px_60px_-40px_rgba(15,23,42,0.4)]">
          <div className="rounded-2xl border border-slate-200/70 bg-white/70 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-500">
                  Memory Search
                </h2>
                <p className="mt-2 text-xs text-slate-500">
                  {!health.memorySearchAvailable
                    ? 'Memory search unavailable (start mash host serve with --memory-db)'
                    : selectedSessionAppId
                    ? `App ${selectedSessionAppId}`
                    : 'Select a session with app_id to search memory'}
                  {selectedSessionId ? ` - Session ${selectedSessionId}` : ''}
                </p>
                {activeSearchContext && (
                  <p className="mt-1 text-xs text-slate-400">
                    Last search: {activeSearchContext.target} / "{activeSearchContext.text}"
                  </p>
                )}
              </div>
              <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs text-slate-600">
                {searchResults.length} result{searchResults.length === 1 ? '' : 's'}
              </span>
            </div>

            <form className="mt-4 space-y-3" onSubmit={handleSearchSubmit}>
              <div className="flex flex-wrap gap-2">
                <div className="inline-flex rounded-full border border-slate-200 bg-white p-1">
                  {['user', 'agent'].map((target) => (
                    <button
                      key={target}
                      type="button"
                      className={`rounded-full px-3 py-1 text-xs font-medium transition ${
                        searchTarget === target
                          ? 'bg-slate-900 text-amber-50'
                          : 'text-slate-600 hover:bg-slate-100'
                      }`}
                      onClick={() => setSearchTarget(target)}
                      disabled={!canSearch}
                    >
                      {target === 'user' ? 'User' : 'Agent'}
                    </button>
                  ))}
                </div>
              </div>

              <div className="flex flex-col gap-2 sm:flex-row">
                <input
                  type="text"
                  value={searchText}
                  onChange={(e) => setSearchText(e.target.value)}
                  placeholder={
                    searchTarget === 'user'
                      ? 'Search user messages in memory...'
                      : 'Search agent responses in memory...'
                  }
                  className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-2.5 text-sm text-slate-900 outline-none ring-0 placeholder:text-slate-400 focus:border-slate-400"
                  disabled={!canSearch || searchStatus === 'loading'}
                />
                <button
                  type="submit"
                  className="rounded-2xl border border-slate-900 bg-slate-900 px-4 py-2.5 text-sm font-medium text-amber-50 shadow-sm transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:border-slate-200 disabled:bg-slate-100 disabled:text-slate-400"
                  disabled={!canSearch || searchStatus === 'loading'}
                >
                  {searchStatus === 'loading' ? 'Searching...' : 'Search'}
                </button>
              </div>
            </form>

            {searchError && (
              <div className="mt-3 rounded-2xl border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                {searchError}
              </div>
            )}

            {searchStatus === 'success' && searchResults.length === 0 && (
              <div className="mt-3 rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-3 py-3 text-sm text-slate-500">
                No memory results found for this query.
              </div>
            )}

            {searchResults.length > 0 && (
              <div className="mt-4 space-y-2">
                {searchResults.map((result) => {
                  const isSelected = selectedSearchTurnId === result.turn_id;
                  return (
                    <button
                      key={`${result.session_id}-${result.turn_id}`}
                      type="button"
                      className={`w-full rounded-2xl border px-3 py-3 text-left transition ${
                        isSelected
                          ? 'border-sky-200 bg-sky-50 shadow-sm'
                          : 'border-slate-200 bg-white hover:border-slate-300'
                      }`}
                      onClick={() => handleSearchResultClick(result)}
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="truncate font-mono text-xs text-slate-700">{result.turn_id}</span>
                        <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] text-slate-600">
                          score {formatSearchScore(result.similarity_score)}
                        </span>
                      </div>
                      <div className="mt-2 line-clamp-3 text-sm text-slate-700">
                        {result.preview || 'No preview available.'}
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <div className="mt-6 flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/70 pb-4">
            <div>
              <h2 className="text-xl font-semibold tracking-tight font-display">Trace Timeline</h2>
              <p className="text-sm text-slate-500">
                {selectedTraceId ? `Trace ${selectedTraceId}` : 'Select a trace to inspect'}
              </p>
              {selectedSearchTurnId && selectedTraceId === selectedSearchTurnId && (
                <p className="mt-1 text-xs text-sky-700">
                  Viewing trace selected from memory search.
                </p>
              )}
            </div>
            <div className="flex items-center gap-2 text-sm">
              <button
                className="rounded-full border border-slate-200 bg-white px-3 py-1 text-slate-700 shadow-sm hover:border-slate-300 disabled:cursor-not-allowed disabled:text-slate-400"
                onClick={expandAll}
                disabled={!selectedTraceId || !selectedTraceExists}
              >
                Expand all
              </button>
              <button
                className="rounded-full border border-slate-200 bg-white px-3 py-1 text-slate-700 shadow-sm hover:border-slate-300 disabled:cursor-not-allowed disabled:text-slate-400"
                onClick={collapseAll}
                disabled={!selectedTraceId || !selectedTraceExists}
              >
                Collapse all
              </button>
            </div>
          </div>

          {selectedTraceId && selectedEvents.length === 0 && (
            <div className="mt-6 rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-4 py-6 text-center text-sm text-slate-500">
              No events yet for this trace.
            </div>
          )}

          <div className="mt-6 space-y-4">
            {selectedEvents.map((event, idx) => {
              const key = eventKey(event, idx);
              const isOpen = expanded.has(key);
              return (
                <div key={key} className="relative">
                  <div className="absolute left-2 top-1 h-full w-px bg-slate-200" />
                  <div className="flex items-start gap-3">
                    <div className="mt-2 h-2.5 w-2.5 rounded-full border border-slate-200 bg-white" />
                    <div className="w-full">
                      <button
                        className="flex w-full flex-wrap items-center justify-between gap-3 rounded-2xl border border-slate-200 bg-white px-4 py-3 text-left shadow-sm hover:border-slate-300"
                        onClick={() => toggleExpand(key)}
                      >
                        <div className="flex items-center gap-3">
                          <span
                            className={`rounded-full border px-2 py-0.5 text-xs font-medium ${
                              CLASS_STYLES[event.event_class] || 'border-slate-200 bg-slate-100 text-slate-600'
                            }`}
                          >
                            {event.event_class || 'Event'}
                          </span>
                          <span className="text-sm font-medium text-slate-800">
                            {event.event_type || 'unknown'}
                          </span>
                        </div>
                        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                          <span>{formatDelta(baseTs, timestamp(event))}</span>
                          <span>-</span>
                          <span>{eventSummary(event)}</span>
                          <span className="rounded-full border border-slate-200 px-2 py-0.5">
                            {isOpen ? 'Hide' : 'Expand'}
                          </span>
                        </div>
                      </button>
                      {isOpen && (
                        <pre className="mt-2 overflow-x-auto rounded-2xl border border-slate-200 bg-slate-950/95 p-4 text-xs text-slate-100">
                          {JSON.stringify(event, null, 2)}
                        </pre>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      </main>
      {status.error && (
        <div className="mx-auto max-w-6xl px-6 pb-6 text-xs text-rose-600">
          {status.error}
        </div>
      )}
    </div>
  );
}

function normalizeAppId(value) {
  if (value == null) return null;
  const normalized = String(value).trim();
  return normalized || null;
}

function normalizeText(value) {
  if (value == null) return null;
  const normalized = String(value).trim();
  return normalized || null;
}

function apiData(payload) {
  if (!payload || typeof payload !== 'object') return null;
  return payload.data && typeof payload.data === 'object' ? payload.data : null;
}

function apiErrorMessage(payload, fallback) {
  if (!payload || typeof payload !== 'object') return fallback;
  const message = payload?.error?.message;
  return typeof message === 'string' && message ? message : fallback;
}

function buildSearchQuery(target, text) {
  const prefix = target === 'agent' ? '@agent:' : '@user:';
  return `${prefix} ${text}`.trim();
}

function formatSearchScore(value) {
  const score = Number(value);
  if (!Number.isFinite(score)) return '0.000';
  return score.toFixed(3);
}

function timestamp(event) {
  const value = event?.ts ?? event?.timestamp ?? event?.time;
  if (value == null) return null;
  const ts = Number(value);
  return Number.isFinite(ts) ? ts : null;
}

function formatTime(value) {
  if (value == null) return 'unknown';
  const date = new Date(value * 1000);
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatDateTime(value) {
  if (value == null) return 'unknown';
  const date = new Date(value * 1000);
  return date.toLocaleString([], {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  });
}

function formatDuration(start, end) {
  if (start == null || end == null) return 'unknown';
  const delta = end - start;
  return `${delta.toFixed(2)}s`;
}

function formatDelta(base, current) {
  if (base == null || current == null) return '+0.00s';
  return `+${(current - base).toFixed(2)}s`;
}

function eventKey(event, idx) {
  return `${event.trace_id || 'trace'}-${event.event_type || 'event'}-${event.ts || idx}`;
}

function eventSummary(event) {
  const parts = [];
  if (event.step_id != null) parts.push(`step ${event.step_id}`);
  if (event.duration_ms != null) parts.push(`${event.duration_ms}ms`);
  if (event.action_type) parts.push(event.action_type);
  if (event.model) parts.push(event.model);
  if (event.total_tokens != null) parts.push(`${event.total_tokens} tokens`);
  if (event.finish_reason) parts.push(`finish ${event.finish_reason}`);
  if (event.command_name) parts.push(event.command_name);

  const payloadTool = event?.payload?.tool || event?.payload?.tool_name || event?.payload?.name;
  if (payloadTool) parts.push(`tool ${payloadTool}`);

  const toolCalls = Array.isArray(event.tool_calls) ? event.tool_calls : null;
  if (toolCalls && toolCalls.length) parts.push(`${toolCalls.length} tool(s)`);

  if (event.error) parts.push('error');

  return parts.length ? parts.join(' - ') : 'event';
}
