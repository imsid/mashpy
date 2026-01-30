import React, { useEffect, useMemo, useState } from 'react';

const MAX_EVENTS = 5000;
const DEFAULT_LIMIT = 2000;

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
  const [expanded, setExpanded] = useState(new Set());
  const [status, setStatus] = useState({ connected: false, error: null });
  const [logPath, setLogPath] = useState('');

  useEffect(() => {
    fetch(`/api/logs?limit=${DEFAULT_LIMIT}`)
      .then((res) => res.json())
      .then((data) => {
        setEvents(Array.isArray(data.events) ? data.events : []);
        setLogPath(data.path || '');
      })
      .catch((err) => {
        setStatus((prev) => ({ ...prev, error: String(err) }));
      });
  }, []);

  useEffect(() => {
    const stream = new EventSource('/api/stream');
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
  }, []);

  const sessionMap = useMemo(() => {
    const map = new Map();
    for (const event of events) {
      const sessionId = event.session_id || 'unknown';
      if (!map.has(sessionId)) {
        map.set(sessionId, { events: [], traces: new Map() });
      }
      const session = map.get(sessionId);
      session.events.push(event);

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
        return { id: sessionId, start, end, traces };
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
    if (!selectedTraceId || !available.has(selectedTraceId)) {
      const firstTrace = Array.from(available.keys())[0];
      setSelectedTraceId(firstTrace);
    }
  }, [selectedSessionId, selectedTraceId, sessionMap]);

  const selectedSession = selectedSessionId ? sessionMap.get(selectedSessionId) : null;
  const selectedEvents =
    selectedSession && selectedTraceId ? selectedSession.traces.get(selectedTraceId) || [] : [];
  const baseTs = selectedEvents.length ? timestamp(selectedEvents[0]) : null;

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
                <button
                  className="w-full text-left"
                  onClick={() => setSelectedSessionId(session.id)}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-xs">{session.id}</span>
                    <span className="rounded-full bg-white/20 px-2 py-0.5 text-xs font-medium">
                      {session.traces.length}
                    </span>
                  </div>
                  <div className="mt-2 text-xs opacity-80">
                    {formatDateTime(session.end)} - {formatDuration(session.start, session.end)}
                  </div>
                </button>
                {session.id === selectedSessionId && (
                  <div className="mt-3 space-y-2 border-t border-white/10 pt-3">
                    {session.traces.map((trace) => (
                      <button
                        key={`${session.id}-${trace.id}`}
                        className={`w-full rounded-2xl border px-3 py-2 text-left text-xs transition ${
                          trace.id === selectedTraceId
                            ? 'border-amber-200 bg-amber-50 text-slate-900'
                            : 'border-white/20 bg-white/10 text-slate-100 hover:border-white/40'
                        }`}
                        onClick={() => setSelectedTraceId(trace.id)}
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
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </aside>

        <section className="rounded-3xl border border-slate-200/70 bg-white/80 p-6 shadow-[0_20px_60px_-40px_rgba(15,23,42,0.4)]">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/70 pb-4">
            <div>
              <h2 className="text-xl font-semibold tracking-tight font-display">Trace Timeline</h2>
              <p className="text-sm text-slate-500">
                {selectedTraceId ? `Trace ${selectedTraceId}` : 'Select a trace to inspect'}
              </p>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <button
                className="rounded-full border border-slate-200 bg-white px-3 py-1 text-slate-700 shadow-sm hover:border-slate-300"
                onClick={expandAll}
                disabled={!selectedTraceId}
              >
                Expand all
              </button>
              <button
                className="rounded-full border border-slate-200 bg-white px-3 py-1 text-slate-700 shadow-sm hover:border-slate-300"
                onClick={collapseAll}
                disabled={!selectedTraceId}
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
