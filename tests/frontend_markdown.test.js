'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const staticDir = path.resolve(__dirname, '..', 'zero_agent', 'frontends', 'desktop', 'static');
const source = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
const sanitizerStart = source.indexOf('function sanitizeMarkdown(html) {');
assert.ok(sanitizerStart > 0);

const context = {
  localStorage: { getItem: () => null },
  navigator: { platform: 'test' },
  window: {},
  marked: require(path.join(staticDir, 'vendor', 'marked.min.js')).marked,
  katex: require(path.join(staticDir, 'vendor', 'katex', 'katex.min.js')),
};
context.globalThis = context;
vm.runInNewContext(source.slice(0, sanitizerStart) + `
function sanitizeMarkdown(html) { return html; }
globalThis.renderMarkdownForTest = renderMarkdown;
`, context, { filename: path.join(staticDir, 'app.js') });

const render = context.renderMarkdownForTest;
const response = render(String.raw`# 原理

行内概率 \(p(y\mid s,q)\)，以及 $x^2$。

\[
\frac{c_{\text{false alarm}}}{c_{\text{miss}}}
\]

$$E=mc^2$$

${'```'}text
\(not math\)
${'```'}

代码 ${'`'}$also_not_math$${'`'}.`);

assert.match(response, /<h1>原理<\/h1>/);
assert.equal((response.match(/class="katex"/g) || []).length, 4);
assert.equal((response.match(/class="katex-display"/g) || []).length, 2);
assert.match(response, /<code class="language-text">\\\(not math\\\)\n<\/code>/);
assert.match(response, /<code>\$also_not_math\$<\/code>/);
assert.doesNotMatch(response, /<p>\[<br>/);

console.log('frontend Markdown and math rendering tests passed');
