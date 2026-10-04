// Presentation accents only: exact substrings of exposed summaries. These rules
// never rewrite text, infer an outcome, or send summaries to another model.
const RULES = [
  ['pleased', /\b(?:currently marveling|marveling|a satisfying simplification|expressed satisfaction|satisfying realization|impressive|elegant simplification)\b/gi],
  ['puzzled', /\b(?:I'm (?:not sure|uncertain|confused)|I am (?:not sure|uncertain|confused)|reconsidering|stepping back|something doesn't add up|found a contradiction|resolving discrepancies|however)\b/gi],
  ['curious', /\b(?:exploring (?:optional components|possibilities|alternatives)|seamless integration|three distinctly different tiny agents|starting to understand|considering alternatives|curious about)\b/gi],
  ['focused', /\b(?:diving deep into potential pitfalls|diving into the specifics|drilling down into the source code|zeroing in|carefully (?:examine|examining|checking|analyzing)|scrutinizing edge cases|seeking any subtle catches|tracing the data flow|currently (?:focused|dissecting)|focusing on|getting a clear picture|hard or soft constraint)\b/gi],
];

export function thoughtAccents(text) {
  const matches = RULES.flatMap(([expression, regex]) =>
    [...text.matchAll(new RegExp(regex))].map((m) => ({
      start: m.index, end: m.index + m[0].length, text: m[0], expression,
    })),
  ).sort((a, b) => a.start - b.start || b.end - a.end);
  const result = [];
  for (const match of matches) {
    if (!result.length || match.start >= result.at(-1).end) result.push(match);
  }
  return result;
}

// A remark transform keeps Markdown structure, code and links intact. React
// escapes the new text nodes; no source text is interpolated into HTML.
export function remarkThoughts({ prefix }) {
  return (tree) => {
    let index = 0;
    function visit(parent) {
      if (!parent.children || ['link', 'linkReference', 'code', 'inlineCode', 'image'].includes(parent.type)) return;
      parent.children = parent.children.flatMap((node) => {
        if (node.type !== 'text') { visit(node); return [node]; }
        const matches = thoughtAccents(node.value);
        if (!matches.length) return [node];
        const nodes = [];
        let offset = 0;
        for (const match of matches) {
          if (match.start > offset) nodes.push({ type: 'text', value: node.value.slice(offset, match.start) });
          nodes.push({ type: 'thoughtPhrase', data: {
            hName: 'button',
            hProperties: { type: 'button', 'data-expression': match.expression, 'data-phrase-id': `${prefix}-${index++}` },
            hChildren: [{ type: 'text', value: match.text }],
          } });
          offset = match.end;
        }
        if (offset < node.value.length) nodes.push({ type: 'text', value: node.value.slice(offset) });
        return nodes;
      });
    }
    visit(tree);
  };
}

export function thoughtLogsPath(trace) {
  const params = new URLSearchParams({ tab: 'sessions', agent: trace.agent_id, trace: trace.trace_id });
  if (trace.session_id) params.set('session', trace.session_id);
  return `/logs?${params}`;
}

export function traceThoughtsPath(trace) {
  const params = new URLSearchParams({ trace: trace.trace_id });
  if (trace.agent_id) params.set('agent', trace.agent_id);
  return `/thoughts?${params}`;
}
