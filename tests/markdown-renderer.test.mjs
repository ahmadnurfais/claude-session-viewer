import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import createDOMPurify from 'dompurify';
import { JSDOM } from 'jsdom';
import { marked } from 'marked';

const rendererSource = await readFile(new URL('../app/static/markdown.js', import.meta.url), 'utf8');

function createRenderer() {
  const dom = new JSDOM('', { runScripts: 'outside-only' });
  const { window } = dom;
  window.marked = marked;
  window.DOMPurify = createDOMPurify(window);
  window.eval(rendererSource);
  return window.renderMarkdown;
}

function renderDocument(markdown) {
  const html = createRenderer()(markdown);
  return new JSDOM(html).window.document;
}

test('renders GFM tables with a scrollable wrapper', () => {
  const document = renderDocument(`| Item | Path | Copy? |
|---|---|---|
| Agent definition | ~/.claude/agents/lint-churn-guard.md | Yes - required |
| Per-project maps | ~/.claude/lint-churn-guard/projects/<slug>/ | Optional / skip |`);

  const table = document.querySelector('.markdown-table-wrap table');
  assert.ok(table);
  assert.deepEqual([...table.querySelectorAll('th')].map((cell) => cell.textContent), ['Item', 'Path', 'Copy?']);
  assert.equal(table.querySelectorAll('tbody tr').length, 2);
});

test('renders common GFM structures used in agent responses', () => {
  const document = renderDocument(`### Summary

- [x] Complete
  - Nested item
- [ ] Pending

> A quoted note

~~Removed text~~ with **bold**, *emphasis*, and \`inline code\`.

---

\`\`\`js
const value = 1;
\`\`\`

[Documentation](https://example.com) and ![Preview](https://example.com/image.png)`);

  assert.equal(document.querySelector('h3').textContent, 'Summary');
  assert.equal(document.querySelectorAll('input[type="checkbox"]').length, 2);
  assert.ok(document.querySelector('ul ul'));
  assert.equal(document.querySelector('blockquote').textContent.trim(), 'A quoted note');
  assert.equal(document.querySelector('del').textContent, 'Removed text');
  assert.equal(document.querySelector('strong').textContent, 'bold');
  assert.equal(document.querySelector('em').textContent, 'emphasis');
  assert.equal(document.querySelector('code').textContent, 'inline code');
  assert.ok(document.querySelector('hr'));
  assert.equal(document.querySelector('pre code').className, 'language-js');
  assert.equal(document.querySelector('a').target, '_blank');
  assert.equal(document.querySelector('a').rel, 'noopener noreferrer');
  assert.equal(document.querySelector('img').src, 'https://example.com/image.png');
});

test('removes unsafe raw HTML and links', () => {
  const document = renderDocument('<script>alert(1)</script><img src="x" onerror="alert(1)"> [bad](javascript:alert(1))');

  assert.equal(document.querySelector('script'), null);
  assert.equal(document.querySelector('[onerror]'), null);
  assert.equal(document.querySelector('a[href^="javascript:"]'), null);
});
