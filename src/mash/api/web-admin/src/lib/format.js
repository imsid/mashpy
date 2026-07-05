export function formatIso(isoString) {
  if (!isoString) return '—';
  return new Date(isoString).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function compactNumber(n) {
  const value = Number(n) || 0;
  if (Math.abs(value) >= 1000) {
    return `${(value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)}k`;
  }
  return String(value);
}

export function formatTime(unixSeconds) {
  if (!unixSeconds) return '—';
  const d = new Date(Number(unixSeconds) * 1000);
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function formatDuration(ms) {
  const value = Number(ms) || 0;
  if (value >= 1000) return `${(value / 1000).toFixed(2)}s`;
  return `${Math.round(value)}ms`;
}

export function tokensInOut(input, output) {
  return `${compactNumber(input)}→${compactNumber(output)}`;
}
