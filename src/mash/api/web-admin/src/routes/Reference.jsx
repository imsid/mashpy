import { useSearchParams } from 'react-router-dom';
import { PageHeader } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { Disclosure } from '../components/Json.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import cliDocs from '../cli.json';

const TABS = [
  { id: 'api', label: 'API' },
  { id: 'cli', label: 'CLI' },
];

const METHODS = ['get', 'post', 'put', 'patch', 'delete'];

// Group operations by the path segment after the API prefix (agent, hosts,
// telemetry, …) since the routers don't set OpenAPI tags.
function groupOperations(spec) {
  const groups = new Map();
  for (const [path, item] of Object.entries(spec.paths || {})) {
    const seg = path.replace(/^\/api\/v\d+\//, '').split('/')[0] || 'root';
    for (const method of METHODS) {
      const op = item[method];
      if (!op) continue;
      const list = groups.get(seg) || [];
      list.push({ method, path, op });
      groups.set(seg, list);
    }
  }
  return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

function Operation({ method, path, op }) {
  const params = op.parameters || [];
  const responses = Object.keys(op.responses || {});
  return (
    <Disclosure
      label={
        <span className="inline-flex items-center gap-2">
          <span className="w-12 shrink-0 font-mono text-xs font-semibold uppercase text-slate-500">
            {method}
          </span>
          <span className="font-mono text-xs text-slate-700">{path}</span>
        </span>
      }
      hint={op.summary || op.operationId}
    >
      <div className="space-y-3 text-sm">
        {op.summary ? <p className="text-slate-600">{op.summary}</p> : null}
        {params.length ? (
          <div>
            <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">
              Parameters
            </div>
            <ul className="space-y-1">
              {params.map((p) => (
                <li key={`${p.in}-${p.name}`} className="flex items-center gap-2 text-xs">
                  <Mono>{p.name}</Mono>
                  <span className="text-slate-400">{p.in}</span>
                  {p.required ? <Chip tone="amber">required</Chip> : null}
                  {p.schema?.type ? (
                    <span className="text-slate-400">{p.schema.type}</span>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {op.requestBody ? (
          <div className="text-xs text-slate-500">Accepts a JSON request body.</div>
        ) : null}
        {responses.length ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-xs text-slate-400">Responses:</span>
            {responses.map((code) => (
              <Chip key={code}>{code}</Chip>
            ))}
          </div>
        ) : null}
      </div>
    </Disclosure>
  );
}

function ApiReference() {
  const state = useApi(() => api.openapi(), []);
  return (
    <Async state={state}>
      {(spec) => (
        <div className="space-y-6">
          <p className="text-sm text-slate-500">
            {spec.info?.title} {spec.info?.version} · generated from{' '}
            <a className="underline" href="/openapi.json">
              /openapi.json
            </a>{' '}
            (also at <a className="underline" href="/docs">/docs</a>).
          </p>
          {groupOperations(spec).map(([group, ops]) => (
            <section key={group}>
              <h2 className="mb-2 font-display text-sm font-semibold capitalize">{group}</h2>
              <div className="space-y-1.5">
                {ops.map((o) => (
                  <Operation key={`${o.method}-${o.path}`} {...o} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </Async>
  );
}

function CliArg({ arg }) {
  return (
    <li className="flex flex-wrap items-center gap-2 text-xs">
      <Mono>{arg.flags.join(', ')}</Mono>
      {arg.required ? <Chip tone="amber">required</Chip> : null}
      <span className="text-slate-500">{arg.help}</span>
    </li>
  );
}

function CliCommand({ command, prefix }) {
  const invocation = `${prefix} ${command.name}`.trim();
  return (
    <div className="rounded-md border border-slate-200 p-3">
      <div className="flex items-center gap-2">
        <Mono>{invocation}</Mono>
        <span className="text-sm text-slate-500">{command.help}</span>
      </div>
      {command.args?.length ? (
        <ul className="mt-2 space-y-1">
          {command.args.map((a) => (
            <CliArg key={a.name} arg={a} />
          ))}
        </ul>
      ) : null}
      {command.subcommands?.length ? (
        <div className="mt-3 space-y-2 border-l border-slate-100 pl-3">
          {command.subcommands.map((sub) => (
            <CliCommand key={sub.name} command={sub} prefix={invocation} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function CliReference() {
  const { cli, repl } = cliDocs;
  return (
    <div className="space-y-6">
      <section>
        <h2 className="mb-2 font-display text-sm font-semibold">
          <Mono>{cli.prog}</Mono> commands
        </h2>
        <div className="space-y-2">
          {cli.commands.map((c) => (
            <CliCommand key={c.name} command={c} prefix={cli.prog} />
          ))}
        </div>
      </section>
      <section>
        <h2 className="mb-2 font-display text-sm font-semibold">REPL commands</h2>
        <p className="mb-2 text-xs text-slate-400">
          Slash commands available inside <Mono>mash repl</Mono>.
        </p>
        <div className="overflow-hidden rounded-md border border-slate-200">
          {repl.commands.map((c, i) => (
            <div
              key={c.name}
              className={`flex items-baseline gap-3 px-3 py-2 text-sm ${
                i ? 'border-t border-slate-100' : ''
              }`}
            >
              <Mono>/{c.name}</Mono>
              <span className="text-slate-600">{c.help}</span>
              {c.aliases?.length ? (
                <span className="ml-auto text-xs text-slate-400">
                  aliases: {c.aliases.map((a) => `/${a}`).join(', ')}
                </span>
              ) : null}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

export default function Reference() {
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') || 'api';

  return (
    <div>
      <PageHeader
        title="Reference"
        description="API endpoints and CLI commands for this deployment."
      />
      <div className="mb-4 flex gap-1 border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setParams({ tab: t.id }, { replace: true })}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition ${
              tab === t.id
                ? 'border-slate-900 text-slate-900'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === 'api' ? <ApiReference /> : <CliReference />}
    </div>
  );
}
