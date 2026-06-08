import React, { useEffect, useMemo, useState } from 'react';

const API_BASE = '/api/v1';
const MAX_EVENTS = 5000;
const DEFAULT_LIMIT = 2000;
const DEFAULT_SEARCH_LIMIT = 10;

const EVENT_STYLES = {
  runtime: 'bg-emerald-100 text-emerald-800 border-emerald-200',
  llm: 'bg-sky-100 text-sky-800 border-sky-200',
  command: 'bg-amber-100 text-amber-800 border-amber-200',
  debug: 'bg-rose-100 text-rose-800 border-rose-200',
  mcp: 'bg-indigo-100 text-indigo-800 border-indigo-200',
  memory: 'bg-cyan-100 text-cyan-800 border-cyan-200',
  api: 'bg-violet-100 text-violet-800 border-violet-200',
  default: 'border-slate-200 bg-slate-100 text-slate-600'
};

export default function App() {
  const [activeTab, setActiveTab] = useState('traces');
  const [events, setEvents] = useState([]);
  const [selectedTraceId, setSelectedTraceId] = useState(null);
  const [selectedSessionId, setSelectedSessionId] = useState(null);
  const [selectedSearchTurnId, setSelectedSearchTurnId] = useState(null);
  const [expanded, setExpanded] = useState(new Set());
  const [status, setStatus] = useState({ connected: false, error: null });
  const [eventSource, setEventSource] = useState('');
  const [health, setHealth] = useState({ memorySearchAvailable: false });
  const [telemetryAgentId, setTelemetryAgentId] = useState(null);
  const [apiEvents, setApiEvents] = useState([]);
  const [apiStatus, setApiStatus] = useState({ connected: false, error: null });
  const [apiFilters, setApiFilters] = useState({
    method: '',
    statusFamily: '',
    path: ''
  });
  const [selectedApiEventId, setSelectedApiEventId] = useState(null);
  const [apiLive, setApiLive] = useState(true);

  const [traceAnalysis, setTraceAnalysis] = useState(null);
  const [analysisLoading, setAnalysisLoading] = useState(false);

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

        const eventsResponse = await fetch(
          `${API_BASE}/telemetry/events?${new URLSearchParams({
            agent_id: resolvedAgentId,
            limit: String(DEFAULT_LIMIT)
          }).toString()}`
        );
        const eventsPayload = await eventsResponse.json().catch(() => ({}));
        if (!eventsResponse.ok) {
          throw new Error(apiErrorMessage(eventsPayload, `Events request failed (${eventsResponse.status})`));
        }

        const eventsData = apiData(eventsPayload) || {};
        const apiEventsResponse = await fetch(
          `${API_BASE}/telemetry/api/events?${new URLSearchParams({
            limit: String(DEFAULT_LIMIT)
          }).toString()}`
        );
        const apiEventsPayload = await apiEventsResponse.json().catch(() => ({}));
        if (!apiEventsResponse.ok) {
          throw new Error(apiErrorMessage(apiEventsPayload, `API events request failed (${apiEventsResponse.status})`));
        }
        const apiEventsData = apiData(apiEventsPayload) || {};
        if (cancelled) {
          return;
        }

        setTelemetryAgentId(resolvedAgentId);
        setHealth({
          memorySearchAvailable: Boolean(
            healthData?.observability?.memory?.search_available ?? healthData?.memory?.search_available
          )
        });
        setEvents(Array.isArray(eventsData.events) ? eventsData.events : []);
        setApiEvents(Array.isArray(apiEventsData.events) ? apiEventsData.events : []);
        setEventSource(eventsData.source || '');
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

  useEffect(() => {
    if (!apiLive) {
      setApiStatus((prev) => ({ ...prev, connected: false }));
      return undefined;
    }

    const stream = new EventSource(`${API_BASE}/telemetry/api/events/stream`);
    stream.onopen = () => setApiStatus({ connected: true, error: null });
    stream.onerror = () => setApiStatus({ connected: false, error: 'API stream disconnected' });
    stream.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data);
        setApiEvents((prev) => {
          const next = upsertById(prev, event, 'api_event_id');
          return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next;
        });
      } catch (err) {
        setApiStatus((prev) => ({ ...prev, error: String(err) }));
      }
    };

    return () => stream.close();
  }, [apiLive]);

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

      const traceId = event.trace_id;
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
              label: traceId,
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

  useEffect(() => {
    if (!telemetryAgentId || !selectedSessionId || !selectedTraceId) {
      setTraceAnalysis(null);
      return;
    }
    let cancelled = false;
    setAnalysisLoading(true);
    (async () => {
      try {
        const params = new URLSearchParams({
          agent_id: telemetryAgentId,
          session_id: selectedSessionId,
          trace_id: selectedTraceId
        });
        const response = await fetch(`${API_BASE}/telemetry/trace/analysis?${params.toString()}`);
        const payload = await response.json().catch(() => ({}));
        if (cancelled) return;
        if (!response.ok) {
          setTraceAnalysis(null);
          return;
        }
        setTraceAnalysis(apiData(payload));
      } catch {
        if (!cancelled) setTraceAnalysis(null);
      } finally {
        if (!cancelled) setAnalysisLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [telemetryAgentId, selectedSessionId, selectedTraceId, events.length]);

  const selectedSession = selectedSessionId ? sessionMap.get(selectedSessionId) : null;
  const selectedSessionAppId = selectedSession?.appId || null;
  const selectedEvents =
    selectedSession && selectedTraceId ? selectedSession.traces.get(selectedTraceId) || [] : [];
  const selectedTraceExists = Boolean(
    selectedSession && selectedTraceId && selectedSession.traces.has(selectedTraceId)
  );
  const baseTs = selectedEvents.length ? timestamp(selectedEvents[0]) : null;
  const canSearch = Boolean(selectedSessionId && selectedSessionAppId && health.memorySearchAvailable);
  const filteredApiEvents = useMemo(
    () => filterApiEvents(apiEvents, apiFilters),
    [apiEvents, apiFilters]
  );
  const selectedApiEvent =
    filteredApiEvents.find((event) => String(event.api_event_id) === String(selectedApiEventId)) ||
    filteredApiEvents[0] ||
    null;

  useEffect(() => {
    if (!selectedApiEventId && filteredApiEvents.length > 0) {
      setSelectedApiEventId(filteredApiEvents[0].api_event_id);
    }
    if (
      selectedApiEventId &&
      filteredApiEvents.length > 0 &&
      !filteredApiEvents.some((event) => String(event.api_event_id) === String(selectedApiEventId))
    ) {
      setSelectedApiEventId(filteredApiEvents[0].api_event_id);
    }
  }, [filteredApiEvents, selectedApiEventId]);

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

  const handleApiFilterChange = (key, value) => {
    setApiFilters((prev) => ({ ...prev, [key]: value }));
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
              <p className="text-xs text-slate-500">Trace debugger - {eventSource || 'loading event source'}</p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <div className="inline-flex rounded-full border border-slate-200 bg-white p-1">
              {['traces', 'api'].map((tab) => (
                <button
                  key={tab}
                  type="button"
                  className={`rounded-full px-3 py-1 text-xs font-medium transition ${
                    activeTab === tab
                      ? 'bg-slate-900 text-amber-50'
                      : 'text-slate-600 hover:bg-slate-100'
                  }`}
                  onClick={() => setActiveTab(tab)}
                >
                  {tab === 'traces' ? 'Traces' : 'API'}
                </button>
              ))}
            </div>
            <span
              className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 ${
                activeTab === 'api'
                  ? apiStatus.connected
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                    : 'border-slate-200 bg-slate-100 text-slate-500'
                  : status.connected
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                  : 'border-slate-200 bg-slate-100 text-slate-500'
              }`}
            >
              <span
                className={`h-2 w-2 rounded-full ${
                  (activeTab === 'api' ? apiStatus.connected : status.connected) ? 'bg-emerald-500' : 'bg-slate-400'
                }`}
              />
              {(activeTab === 'api' ? apiStatus.connected : status.connected) ? 'Live' : 'Paused'}
            </span>
          </div>
        </div>
      </header>

      <main className="mx-auto grid w-full max-w-6xl grid-cols-1 gap-6 px-6 py-6 lg:grid-cols-[320px_1fr]">
        <aside
          className={`rounded-3xl border border-slate-200/70 bg-white/80 p-4 shadow-[0_20px_60px_-40px_rgba(15,23,42,0.4)] ${
            activeTab === 'traces' ? '' : 'hidden'
          }`}
        >
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

        <section
          className={`rounded-3xl border border-slate-200/70 bg-white/80 p-6 shadow-[0_20px_60px_-40px_rgba(15,23,42,0.4)] ${
            activeTab === 'traces' ? '' : 'hidden'
          }`}
        >
          <div className="rounded-2xl border border-slate-200/70 bg-white/70 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-500">
                  Memory Search
                </h2>
                <p className="mt-2 text-xs text-slate-500">
                  {!health.memorySearchAvailable
                    ? 'Memory search unavailable for the selected agent'
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

          {analysisLoading && (
            <div className="mt-6 text-center text-sm text-slate-500">Loading trace analysis...</div>
          )}

          {traceAnalysis && !analysisLoading && (
            <div className="mt-6 space-y-6">
              <TraceSummaryBar analysis={traceAnalysis} />
              <SpanWaterfall
                spanTree={traceAnalysis.span_tree}
                totalDuration={traceAnalysis.total_duration_ms}
                expanded={expanded}
                toggleExpand={toggleExpand}
              />
              <TraceStatsPanel analysis={traceAnalysis} />
            </div>
          )}

          {!traceAnalysis && !analysisLoading && selectedEvents.length > 0 && (
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
                                styleForEvent(event.event_type)
                              }`}
                            >
                              {eventFamilyLabel(event.event_type)}
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
          )}
        </section>
        <section
          className={`lg:col-span-2 rounded-3xl border border-slate-200/70 bg-white/80 p-6 shadow-[0_20px_60px_-40px_rgba(15,23,42,0.4)] ${
            activeTab === 'api' ? '' : 'hidden'
          }`}
        >
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-200/70 pb-4">
            <div>
              <h2 className="text-xl font-semibold tracking-tight font-display">API Requests</h2>
              <p className="text-sm text-slate-500">
                {filteredApiEvents.length} shown from {apiEvents.length} logged requests
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <button
                type="button"
                className={`rounded-full border px-3 py-1 shadow-sm transition ${
                  apiLive
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                    : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                }`}
                onClick={() => setApiLive((value) => !value)}
              >
                {apiLive ? 'Live' : 'Paused'}
              </button>
              <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs text-slate-600">
                {apiStatus.connected ? 'stream connected' : 'stream idle'}
              </span>
            </div>
          </div>

          <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
            <select
              value={apiFilters.method}
              onChange={(event) => handleApiFilterChange('method', event.target.value)}
              className="rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 outline-none focus:border-slate-400"
            >
              <option value="">All methods</option>
              <option value="GET">GET</option>
              <option value="POST">POST</option>
              <option value="PUT">PUT</option>
              <option value="PATCH">PATCH</option>
              <option value="DELETE">DELETE</option>
            </select>
            <select
              value={apiFilters.statusFamily}
              onChange={(event) => handleApiFilterChange('statusFamily', event.target.value)}
              className="rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 outline-none focus:border-slate-400"
            >
              <option value="">All statuses</option>
              <option value="2">2xx</option>
              <option value="3">3xx</option>
              <option value="4">4xx</option>
              <option value="5">5xx</option>
            </select>
            <input
              value={apiFilters.path}
              onChange={(event) => handleApiFilterChange('path', event.target.value)}
              placeholder="Path contains"
              className="rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 outline-none placeholder:text-slate-400 focus:border-slate-400"
            />
          </div>

          <div className="mt-6 grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(360px,0.9fr)]">
            <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-[0.2em] text-slate-500">
                  <tr>
                    <th className="px-3 py-3 font-semibold">Method</th>
                    <th className="px-3 py-3 font-semibold">Path</th>
                    <th className="px-3 py-3 font-semibold">Status</th>
                    <th className="px-3 py-3 font-semibold">Duration</th>
                    <th className="px-3 py-3 font-semibold">Event</th>
                    <th className="px-3 py-3 font-semibold">Time</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {filteredApiEvents.length === 0 && (
                    <tr>
                      <td className="px-3 py-8 text-center text-sm text-slate-500" colSpan={6}>
                        No API requests match the current filters.
                      </td>
                    </tr>
                  )}
                  {filteredApiEvents.map((event) => {
                    const selected = String(event.api_event_id) === String(selectedApiEvent?.api_event_id);
                    return (
                      <tr
                        key={event.api_event_id}
                        className={`cursor-pointer transition ${
                          selected ? 'bg-sky-50' : 'hover:bg-slate-50'
                        }`}
                        onClick={() => setSelectedApiEventId(event.api_event_id)}
                      >
                        <td className="px-3 py-3 font-mono text-xs text-slate-700">{event.method}</td>
                        <td className="max-w-[280px] truncate px-3 py-3 font-mono text-xs text-slate-800">
                          {event.path}
                        </td>
                        <td className="px-3 py-3">
                          <span className={`rounded-full border px-2 py-0.5 text-xs ${statusClass(event.status_code)}`}>
                            {event.status_code}
                          </span>
                        </td>
                        <td className="px-3 py-3 text-xs text-slate-600">{event.duration_ms}ms</td>
                        <td className="max-w-[160px] truncate px-3 py-3 font-mono text-[11px] text-slate-500">
                          {event.api_event_id}
                        </td>
                        <td className="px-3 py-3 text-xs text-slate-500">{formatTime(timestamp(event))}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="rounded-2xl border border-slate-200 bg-white p-4">
              {selectedApiEvent ? (
                <div>
                  <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-200 pb-3">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="rounded-full border border-violet-200 bg-violet-100 px-2 py-0.5 text-xs font-medium text-violet-800">
                          {selectedApiEvent.method}
                        </span>
                        <span className={`rounded-full border px-2 py-0.5 text-xs ${statusClass(selectedApiEvent.status_code)}`}>
                          {selectedApiEvent.status_code}
                        </span>
                      </div>
                      <p className="mt-2 break-all font-mono text-sm text-slate-800">{selectedApiEvent.path}</p>
                    </div>
                    <span className="rounded-full border border-slate-200 px-2 py-0.5 text-xs text-slate-600">
                      {selectedApiEvent.duration_ms}ms
                    </span>
                  </div>
                  <div className="mt-4 grid grid-cols-1 gap-3 text-xs text-slate-600 sm:grid-cols-2">
                    <Detail label="API Event ID" value={selectedApiEvent.api_event_id} />
                    <Detail label="Client" value={selectedApiEvent.client_host || 'unknown'} />
                    <Detail label="Captured" value={bodyStatus(selectedApiEvent.request_body)} />
                  </div>
                  <JsonBlock title="Query Params" value={selectedApiEvent.query_params} />
                  <JsonBlock title="Request Headers" value={selectedApiEvent.request_headers} />
                  <JsonBlock title="Request Body" value={selectedApiEvent.request_body} />
                  <JsonBlock title="Response Headers" value={selectedApiEvent.response_headers} />
                  <JsonBlock title="Response Body" value={selectedApiEvent.response_body} />
                  <JsonBlock title="Raw Event" value={selectedApiEvent} />
                </div>
              ) : (
                <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-4 py-8 text-center text-sm text-slate-500">
                  Select an API request to inspect.
                </div>
              )}
            </div>
          </div>
        </section>
      </main>
      {(activeTab === 'api' ? apiStatus.error : status.error) && (
        <div className="mx-auto max-w-6xl px-6 pb-6 text-xs text-rose-600">
          {activeTab === 'api' ? apiStatus.error : status.error}
        </div>
      )}
    </div>
  );
}

function Detail({ label, value }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2">
      <div className="text-[10px] uppercase tracking-[0.2em] text-slate-400">{label}</div>
      <div className="mt-1 break-all font-mono text-xs text-slate-700">{value}</div>
    </div>
  );
}

function JsonBlock({ title, value }) {
  return (
    <div className="mt-4">
      <h3 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">{title}</h3>
      <pre className="mt-2 max-h-64 overflow-x-auto rounded-2xl border border-slate-200 bg-slate-950/95 p-3 text-xs text-slate-100">
        {JSON.stringify(value || {}, null, 2)}
      </pre>
    </div>
  );
}

const SPAN_COLORS = {
  think: { bar: 'bg-sky-400', badge: 'bg-sky-100 text-sky-800 border-sky-200' },
  tool_call: { bar: 'bg-amber-400', badge: 'bg-amber-100 text-amber-800 border-amber-200' },
  subagent_call: { bar: 'bg-purple-400', badge: 'bg-purple-100 text-purple-800 border-purple-200' },
  cold_start: { bar: 'bg-slate-400', badge: 'bg-slate-100 text-slate-700 border-slate-200' },
  context_load: { bar: 'bg-cyan-400', badge: 'bg-cyan-100 text-cyan-800 border-cyan-200' },
  step: { bar: 'bg-emerald-400', badge: 'bg-emerald-100 text-emerald-800 border-emerald-200' },
  trace: { bar: 'bg-slate-300', badge: 'bg-slate-100 text-slate-600 border-slate-200' },
};

function TraceSummaryBar({ analysis }) {
  const timing = analysis.analysis?.timing;
  if (!timing) return null;
  const total = timing.total_duration_ms || 1;
  const segments = [
    { label: 'Think', ms: timing.total_think_ms, pct: timing.pct_think, color: 'bg-sky-400' },
    { label: 'Tool', ms: timing.total_tool_ms, pct: timing.pct_tool, color: 'bg-amber-400' },
    { label: 'Subagent', ms: timing.total_subagent_ms, pct: timing.pct_subagent, color: 'bg-purple-400' },
    { label: 'Cold Start', ms: timing.cold_start_ms, pct: timing.pct_cold_start, color: 'bg-slate-400' },
    { label: 'Idle', ms: timing.idle_ms, pct: total > 0 ? ((timing.idle_ms || 0) / total * 100) : 0, color: 'bg-slate-200' },
  ].filter((s) => s.ms > 0);

  const counts = analysis.counts || {};
  const tokens = analysis.tokens || {};

  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${
            analysis.status === 'completed'
              ? 'border-emerald-200 bg-emerald-100 text-emerald-800'
              : analysis.status === 'error'
              ? 'border-rose-200 bg-rose-100 text-rose-800'
              : 'border-amber-200 bg-amber-100 text-amber-800'
          }`}>
            {analysis.status}
          </span>
          <span className="text-sm font-semibold text-slate-800">
            {formatMs(timing.total_duration_ms)}
          </span>
        </div>
        <div className="flex flex-wrap gap-3 text-xs text-slate-600">
          <span>{counts.step_count} step{counts.step_count !== 1 ? 's' : ''}</span>
          <span>{counts.tool_call_count} tool call{counts.tool_call_count !== 1 ? 's' : ''}</span>
          {counts.tool_error_count > 0 && (
            <span className="text-rose-600">{counts.tool_error_count} error{counts.tool_error_count !== 1 ? 's' : ''}</span>
          )}
          <span>{(tokens.input_tokens || 0) + (tokens.output_tokens || 0)} tokens</span>
        </div>
      </div>

      <div className="mt-3 flex h-3 w-full overflow-hidden rounded-full bg-slate-100">
        {segments.map((seg) => (
          <div
            key={seg.label}
            className={`${seg.color} h-full`}
            style={{ width: `${Math.max(seg.pct, 0.5)}%` }}
            title={`${seg.label}: ${formatMs(seg.ms)} (${seg.pct.toFixed(1)}%)`}
          />
        ))}
      </div>

      <div className="mt-2 flex flex-wrap gap-3 text-[11px] text-slate-500">
        {segments.map((seg) => (
          <span key={seg.label} className="flex items-center gap-1">
            <span className={`inline-block h-2 w-2 rounded-full ${seg.color}`} />
            {seg.label} {formatMs(seg.ms)} ({seg.pct.toFixed(1)}%)
          </span>
        ))}
      </div>
    </div>
  );
}

function SpanWaterfall({ spanTree, totalDuration, expanded, toggleExpand }) {
  if (!spanTree) return null;
  const total = totalDuration || spanTree.duration_ms || 1;

  return (
    <div className="rounded-2xl border border-slate-200 bg-white">
      <div className="border-b border-slate-200 px-4 py-2">
        <h3 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Span Waterfall</h3>
      </div>
      <div className="divide-y divide-slate-100">
        {renderSpanRows(spanTree, total, 0, expanded, toggleExpand)}
      </div>
    </div>
  );
}

function renderSpanRows(span, totalDuration, depth, expanded, toggleExpand) {
  const rows = [];
  const hasChildren = span.children && span.children.length > 0;
  const isOpen = expanded.has(span.span_id);
  const colors = SPAN_COLORS[span.kind] || SPAN_COLORS.trace;

  const offsetPct = totalDuration > 0
    ? ((span.start_time - (span.start_time - span.duration_ms / 1000)) / (totalDuration / 1000)) * 100
    : 0;
  const widthPct = totalDuration > 0 ? Math.max((span.duration_ms / totalDuration) * 100, 0.5) : 100;

  if (span.kind !== 'trace') {
    rows.push(
      <div
        key={span.span_id}
        className="flex items-center gap-2 px-4 py-2 hover:bg-slate-50 cursor-pointer"
        onClick={() => toggleExpand(span.span_id)}
      >
        <div className="flex items-center gap-1 shrink-0" style={{ width: `${depth * 20 + 120}px` }}>
          <span style={{ width: `${depth * 20}px` }} />
          {hasChildren && (
            <span className="text-slate-400 text-xs w-4 text-center">{isOpen ? '▾' : '▸'}</span>
          )}
          {!hasChildren && <span className="w-4" />}
          <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${colors.badge}`}>
            {span.kind}
          </span>
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between gap-2">
            <span className="truncate text-xs font-medium text-slate-800">{span.name}</span>
            <span className="shrink-0 text-xs text-slate-500">{formatMs(span.duration_ms)}</span>
          </div>
          <div className="mt-1 h-2 w-full rounded-full bg-slate-100 relative">
            <div
              className={`absolute h-full rounded-full ${colors.bar} ${span.status === 'error' ? 'bg-rose-400' : ''}`}
              style={{ width: `${widthPct}%`, maxWidth: '100%' }}
            />
          </div>
        </div>
      </div>
    );
  }

  if (span.kind === 'trace' || isOpen) {
    for (const child of (span.children || [])) {
      rows.push(...renderSpanRows(child, totalDuration, span.kind === 'trace' ? depth : depth + 1, expanded, toggleExpand));
    }
  }

  if (isOpen && span.attributes && Object.keys(span.attributes).length > 0) {
    rows.push(
      <div key={`${span.span_id}-attrs`} className="px-4 py-2 bg-slate-50">
        <pre className="ml-8 overflow-x-auto text-[11px] text-slate-600" style={{ marginLeft: `${(depth + 1) * 20 + 32}px` }}>
          {JSON.stringify(span.attributes, null, 2)}
        </pre>
      </div>
    );
  }

  return rows;
}

function TraceStatsPanel({ analysis }) {
  const data = analysis.analysis;
  if (!data) return null;
  const toolStats = data.tool_stats || [];
  const stepBreakdown = data.step_breakdown || [];
  const slowest = data.slowest_operations || [];

  return (
    <div className="space-y-4">
      {toolStats.length > 0 && (
        <div className="rounded-2xl border border-slate-200 bg-white">
          <button
            className="w-full px-4 py-3 text-left"
            onClick={(e) => {
              const panel = e.currentTarget.nextElementSibling;
              panel.classList.toggle('hidden');
            }}
          >
            <h3 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
              Tool Stats ({toolStats.length})
            </h3>
          </button>
          <div className="border-t border-slate-200">
            <table className="w-full text-left text-xs">
              <thead className="bg-slate-50 text-[10px] uppercase tracking-[0.2em] text-slate-500">
                <tr>
                  <th className="px-4 py-2 font-semibold">Tool</th>
                  <th className="px-4 py-2 font-semibold">Count</th>
                  <th className="px-4 py-2 font-semibold">Total</th>
                  <th className="px-4 py-2 font-semibold">Avg</th>
                  <th className="px-4 py-2 font-semibold">Max</th>
                  <th className="px-4 py-2 font-semibold">Errors</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {toolStats.map((tool) => (
                  <tr key={tool.tool_name}>
                    <td className="px-4 py-2 font-mono text-slate-800">{tool.tool_name}</td>
                    <td className="px-4 py-2 text-slate-600">{tool.count}</td>
                    <td className="px-4 py-2 text-slate-600">{formatMs(tool.total_ms)}</td>
                    <td className="px-4 py-2 text-slate-600">{formatMs(tool.avg_ms)}</td>
                    <td className="px-4 py-2 text-slate-600">{formatMs(tool.max_ms)}</td>
                    <td className={`px-4 py-2 ${tool.error_count > 0 ? 'text-rose-600' : 'text-slate-400'}`}>
                      {tool.error_count}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {stepBreakdown.length > 0 && (
        <div className="rounded-2xl border border-slate-200 bg-white">
          <button
            className="w-full px-4 py-3 text-left"
            onClick={(e) => {
              const panel = e.currentTarget.nextElementSibling;
              panel.classList.toggle('hidden');
            }}
          >
            <h3 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
              Step Breakdown ({stepBreakdown.length})
            </h3>
          </button>
          <div className="border-t border-slate-200 hidden">
            <table className="w-full text-left text-xs">
              <thead className="bg-slate-50 text-[10px] uppercase tracking-[0.2em] text-slate-500">
                <tr>
                  <th className="px-4 py-2 font-semibold">Step</th>
                  <th className="px-4 py-2 font-semibold">Think</th>
                  <th className="px-4 py-2 font-semibold">Tool</th>
                  <th className="px-4 py-2 font-semibold">Overhead</th>
                  <th className="px-4 py-2 font-semibold">Total</th>
                  <th className="px-4 py-2 font-semibold">Tools</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {stepBreakdown.map((step) => (
                  <tr key={step.step_index}>
                    <td className="px-4 py-2 font-mono text-slate-800">{step.step_index}</td>
                    <td className="px-4 py-2 text-slate-600">{formatMs(step.think_ms)}</td>
                    <td className="px-4 py-2 text-slate-600">{formatMs(step.tool_ms)}</td>
                    <td className="px-4 py-2 text-slate-600">{formatMs(step.overhead_ms)}</td>
                    <td className="px-4 py-2 text-slate-600">{formatMs(step.total_ms)}</td>
                    <td className="px-4 py-2 font-mono text-slate-500">{(step.tool_calls || []).join(', ') || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {slowest.length > 0 && (
        <div className="rounded-2xl border border-slate-200 bg-white">
          <button
            className="w-full px-4 py-3 text-left"
            onClick={(e) => {
              const panel = e.currentTarget.nextElementSibling;
              panel.classList.toggle('hidden');
            }}
          >
            <h3 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
              Slowest Operations (top {slowest.length})
            </h3>
          </button>
          <div className="border-t border-slate-200 hidden">
            <div className="divide-y divide-slate-100">
              {slowest.map((op, idx) => {
                const colors = SPAN_COLORS[op.kind] || SPAN_COLORS.trace;
                return (
                  <div key={idx} className="flex items-center justify-between px-4 py-2">
                    <div className="flex items-center gap-2">
                      <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${colors.badge}`}>
                        {op.kind}
                      </span>
                      <span className="text-xs text-slate-800">{op.name}</span>
                      {op.step_index != null && (
                        <span className="text-[10px] text-slate-400">step {op.step_index}</span>
                      )}
                    </div>
                    <span className="text-xs font-medium text-slate-700">{formatMs(op.duration_ms)}</span>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function formatMs(ms) {
  if (ms == null) return '0ms';
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${Math.round(ms)}ms`;
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
  const value = event?.created_at ?? event?.timestamp ?? event?.time;
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
  return `${event.event_id || idx}`;
}

function upsertById(items, item, idKey) {
  const id = item?.[idKey];
  if (id == null) return items;
  const idx = items.findIndex((existing) => String(existing?.[idKey]) === String(id));
  if (idx === -1) return [...items, item];
  const next = [...items];
  next[idx] = item;
  return next;
}

function filterApiEvents(events, filters) {
  return [...events]
    .filter((event) => {
      if (filters.method && event.method !== filters.method) return false;
      if (filters.statusFamily && String(event.status_code || '')[0] !== filters.statusFamily) return false;
      if (filters.path && !String(event.path || '').toLowerCase().includes(filters.path.toLowerCase())) return false;
      return true;
    })
    .sort((a, b) => (Number(b.api_event_id) || 0) - (Number(a.api_event_id) || 0));
}

function statusClass(statusCode) {
  const status = Number(statusCode);
  if (status >= 500) return 'border-rose-200 bg-rose-100 text-rose-800';
  if (status >= 400) return 'border-amber-200 bg-amber-100 text-amber-800';
  if (status >= 300) return 'border-sky-200 bg-sky-100 text-sky-800';
  if (status >= 200) return 'border-emerald-200 bg-emerald-100 text-emerald-800';
  return 'border-slate-200 bg-slate-100 text-slate-600';
}

function bodyStatus(body) {
  if (!body || typeof body !== 'object') return 'unknown';
  const status = body.capture_status || 'unknown';
  return body.truncated ? `${status}, truncated` : status;
}

function eventSummary(event) {
  const parts = [];
  if (event.loop_index != null) parts.push(`step ${event.loop_index}`);
  if (event?.payload?.duration_ms != null) parts.push(`${event.payload.duration_ms}ms`);
  if (event?.payload?.action_type) parts.push(event.payload.action_type);
  if (event?.payload?.model) parts.push(event.payload.model);
  if (event?.payload?.total_tokens != null) parts.push(`${event.payload.total_tokens} tokens`);
  if (event?.payload?.finish_reason) parts.push(`finish ${event.payload.finish_reason}`);
  if (event?.payload?.command_name) parts.push(event.payload.command_name);

  const payloadTool =
    event?.payload?.tool ||
    event?.payload?.tool_name ||
    event?.payload?.name ||
    event?.payload?.payload?.tool_name;
  if (payloadTool) parts.push(`tool ${payloadTool}`);

  const toolCalls = Array.isArray(event?.payload?.tool_calls) ? event.payload.tool_calls : null;
  if (toolCalls && toolCalls.length) parts.push(`${toolCalls.length} tool(s)`);

  if (event?.payload?.error) parts.push('error');

  return parts.length ? parts.join(' - ') : 'event';
}

function eventFamily(eventType) {
  const [family] = String(eventType || '').split('.');
  return family || 'default';
}

function eventFamilyLabel(eventType) {
  const family = eventFamily(eventType);
  return family === 'default' ? 'Event' : family;
}

function styleForEvent(eventType) {
  return EVENT_STYLES[eventFamily(eventType)] || EVENT_STYLES.default;
}
