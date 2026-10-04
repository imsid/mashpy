import test from 'node:test';
import assert from 'node:assert/strict';
import { thoughtAccents, remarkThoughts, thoughtLogsPath } from './thoughts.js';

test('accents preserve original Unicode text exactly without overlapping', () => {
  const source = "🧐 I'm currently marveling. However, I'm diving deep into potential pitfalls.\nNo rewrite.";
  const accents = thoughtAccents(source);
  assert.deepEqual(accents.map((a) => a.expression), ['pleased', 'puzzled', 'focused']);
  let text = '', offset = 0;
  for (const a of accents) { text += source.slice(offset, a.start) + a.text; offset = a.end; }
  assert.equal(text + source.slice(offset), source);
  assert.equal(thoughtAccents('No stance or emotion stated.').length, 0);
});

test('Markdown transform only accents prose, leaves code and links intact', () => {
  const inlineCode = { type: 'inlineCode', value: 'currently marveling' };
  const link = { type: 'link', url: '/doc', children: [{ type: 'text', value: 'currently marveling' }] };
  const tree = { type: 'root', children: [{ type: 'paragraph', children: [
    { type: 'text', value: "I'm currently marveling <script>alert(1)</script>" }, inlineCode, link,
  ] }] };
  remarkThoughts({ prefix: 7 })(tree);
  const nodes = tree.children[0].children;
  assert.equal(nodes[1].type, 'thoughtPhrase');
  assert.equal(nodes[1].data.hChildren[0].value, 'currently marveling');
  assert.equal(nodes[1].data.hProperties['data-phrase-id'], '7-0');
  assert.equal(nodes[2].value, ' <script>alert(1)</script>');
  assert.strictEqual(nodes[3], inlineCode);
  assert.strictEqual(nodes[4], link);
  assert.equal(link.children[0].type, 'text');
});

test('Log link carries the exact agent session and trace, safely encoded', () => {
  const path = thoughtLogsPath({ agent_id: 'agent & name', session_id: 's/1', trace_id: 't?2' });
  const params = new URL(path, 'http://localhost').searchParams;
  assert.equal(params.get('agent'), 'agent & name');
  assert.equal(params.get('session'), 's/1');
  assert.equal(params.get('trace'), 't?2');
});
