window.process = window.process || { platform: navigator.platform.toLowerCase().includes('mac') ? 'darwin' : 'win32' };
// ZeroAgent Desktop — Renderer Logic
// Handles UI state, sessions, streaming, slash commands.

'use strict';

// ─── State ────────────────────────────────────────────────────────────────
const state = {
  sessions: new Map(),      // localSessionId -> { id, bridgeSessionId, title, messages: [], cwd, config, diagnostics, groupId, modelOverride, tokenUsage }
  activeId: null,
  bridgeReady: false,
  defaultConfig: { theme: 'auto', llmNo: 0, workspaceDir: '' },
  modelProfiles: [],
  slashCommands: [],
  restartingBridge: false,
  bridgeNoticeMessage: null,
  runtimeBySessionId: new Map(),
  sessionGroups: new Map(),  // groupId -> { name, sessionIds: [], collapsed: false }
  activeAgents: new Map(),   // sessionId -> [agent]
  leftDrawerCollapsed: localStorage.getItem('leftDrawerCollapsed') === 'true',
  rightDrawerCollapsed: localStorage.getItem('rightDrawerCollapsed') !== 'false',
};

// Helper: get config/diagnostics for the active session (or defaults)
function getActiveConfig() {
  const sess = state.sessions.get(state.activeId);
  return sess ? sess.config : state.defaultConfig;
}
function getActiveDiagnostics() {
  const sess = state.sessions.get(state.activeId);
  return sess ? sess.diagnostics : [];
}

// ─── DOM refs ─────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
let messagesEl, inputEl, sendBtn, sessionListEl, sessionTitleEl, statusBadge, statusText;
let errorBanner, commandPaletteEl, leftDrawer, rightDrawer, agentList;
let modelStatus, currentModelEl, tokenUsageEl, modelPickerModal, modelList;
let diagnosticsLogEl, diagnosticsPanel;


// ─── Diagnostics ─────────────────────────────────────────────────────────
const MAX_DIAGNOSTICS = 200;

function diagnosticText(payload) {
  if (payload == null) return '';
  if (typeof payload === 'string') return payload;
  if (payload instanceof Error) return payload.stack || payload.message;
  try {
    return JSON.stringify(payload);
  } catch (_) {
    return String(payload);
  }
}

function addDiagnostic(level, message, payload) {
  const ts = new Date().toISOString();
  const detail = diagnosticText(payload);
  const diags = getActiveDiagnostics();
  diags.push({ ts, level, message, detail });
  if (diags.length > MAX_DIAGNOSTICS) diags.shift();
  renderDiagnostics();
}

function formatDiagnostics() {
  const diags = getActiveDiagnostics();
  if (diags.length === 0) return 'No diagnostics yet.';
  return diags.map((entry) => {
    const suffix = entry.detail ? `\n  ${entry.detail}` : '';
    return `[${entry.ts}] ${entry.level.toUpperCase()} ${entry.message}${suffix}`;
  }).join('\n');
}

function renderDiagnostics() {
  if (diagnosticsLogEl) {
    diagnosticsLogEl.textContent = formatDiagnostics();
  }
}

function openDiagnostics() {
  renderDiagnostics();
  diagnosticsPanel.classList.remove('hidden');
}

function closeDiagnostics() {
  diagnosticsPanel.classList.add('hidden');
}

async function copyDiagnostics() {
  const text = formatDiagnostics();
  try {
    await navigator.clipboard.writeText(text);
    addDiagnostic('info', 'Diagnostics copied to clipboard');
  } catch (err) {
    addDiagnostic('error', 'Failed to copy diagnostics', err);
    showError('Failed to copy diagnostics: ' + (err.message || err), null, null, { skipDiagnostic: true });
  }
}

function clearDiagnostics() {
  const sess = state.sessions.get(state.activeId);
  if (sess) sess.diagnostics = [];
  renderDiagnostics();
}

// ─── Markdown ─────────────────────────────────────────────────────────────
if (typeof marked !== 'undefined') {
  marked.setOptions({
    gfm: true,
    breaks: true,
    mangle: false,
    headerIds: false
  });
}

const ALLOWED_URI_RE = /^(https?:|mailto:|tel:|#|\/)/i;

function renderMarkdown(text) {
  if (typeof marked === 'undefined') {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
  try {
    return sanitizeMarkdown(marked.parse(text));
  } catch (e) {
    return escapeHtml(text);
  }
}

function sanitizeMarkdown(html) {
  const template = document.createElement('template');
  template.innerHTML = String(html);
  const blockedTags = new Set(['SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'LINK', 'META', 'BASE', 'FORM', 'INPUT', 'BUTTON']);
  const walker = document.createTreeWalker(template.content, NodeFilter.SHOW_ELEMENT);
  const removals = [];
  while (walker.nextNode()) {
    const el = walker.currentNode;
    if (blockedTags.has(el.tagName)) {
      removals.push(el);
      continue;
    }
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      const value = attr.value.trim();
      if (name.startsWith('on') || name === 'srcdoc') {
        el.removeAttribute(attr.name);
        continue;
      }
      if ((name === 'href' || name === 'src' || name === 'xlink:href') && value && !ALLOWED_URI_RE.test(value)) {
        el.removeAttribute(attr.name);
      }
    }
    if (el.tagName === 'A') {
      el.setAttribute('rel', 'noopener noreferrer');
      el.setAttribute('target', '_blank');
    }
  }
  for (const el of removals) el.remove();
  return template.innerHTML;
}


function detectStructuredKind(line) {
  const trimmed = String(line || '').trim();
  const genericTool = trimmed.match(/^TURN\s+\d+\s*:\s*TOOL:\s*`?([^`\s]+)`?\s*(?:ARGS?:)?\s*$/i);
  if (genericTool) return { kind: 'TOOL_CALL', rest: trimmed };
  const m = trimmed.match(/^(TOOL_RECALL|TOOL_REQUEST|TOOL_RESPONSE|COWORK|TUNR|TURN|ACTION|OBSERVATION|THOUGHT|TOOL)[\s:_-]*(.*)$/i);
  if (m) return { kind: m[1].toUpperCase(), rest: (m[2] || '').trim() };

  // ZeroAgent's bridge currently streams tool calls/results as plain
  // assistant text, not as ACP `tool_call` notifications. Recognize the real
  // XML-ish markers so streamed code_run/file_read/etc. blocks are folded.
  if (/^<function_calls\b[^>]*>/i.test(trimmed) || /^<invoke\b[^>]*\bname=["'][^"']+["'][^>]*>/i.test(trimmed)) {
    return { kind: 'TOOL_CALL', rest: trimmed };
  }
  if (/^<function_results\b[^>]*>/i.test(trimmed) || /^<result\b[^>]*>/i.test(trimmed)) {
    return { kind: 'TOOL_RESULT', rest: trimmed };
  }
  return null;
}

function isStructuredClosingLine(line, kind, textSoFar) {
  const trimmed = String(line || '').trim();
  const block = String(textSoFar || '');
  if (kind === 'TOOL_CALL') {
    if (/^<\/function_calls>$/i.test(trimmed)) return true;
    // Single-invoke streams may omit the <function_calls> wrapper.
    return /^<\/invoke>$/i.test(trimmed) && !/^\s*<function_calls\b/im.test(block);
  }
  if (kind === 'TOOL_RESULT') {
    if (/^<\/function_results>$/i.test(trimmed)) return true;
    // Single-result streams may omit the <function_results> wrapper.
    return /^<\/result>$/i.test(trimmed) && !/^\s*<function_results\b/im.test(block);
  }
  return false;
}

function compactTurnSummary(text, fallback = '', maxLen = 96) {
  const value = String(text || fallback || '').replace(/\s+/g, ' ').trim();
  if (!value) return fallback || '';
  return value.length > maxLen ? value.slice(0, maxLen) + '...' : value;
}

function decodeXmlEntities(text) {
  const template = document.createElement('textarea');
  template.innerHTML = String(text || '');
  return template.value;
}

function extractTagAttribute(text, tag, attr) {
  const tagName = String(tag || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const attrName = String(attr || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp('<' + tagName + '\\b[^>]*\\b' + attrName + '=["\\\']([^"\\\']+)["\\\'][^>]*>', 'i');
  const match = String(text || '').match(re);
  return match ? decodeXmlEntities(match[1]).trim() : '';
}

function firstUsefulArgValue(args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) return '';
  const keys = [
    'path', 'file_path', 'filepath', 'filename', 'target_file', 'target',
    'cwd', 'cmd', 'command', 'query', 'url', 'pattern', 'note', 'goal',
    'prompt', 'message', 'task', 'seed',
  ];
  for (const key of keys) {
    const value = args[key];
    if (typeof value === 'string' && value.trim()) return `${key}: ${value.trim()}`;
  }
  const first = Object.entries(args).find(([, value]) => typeof value === 'string' && value.trim());
  return first ? `${first[0]}: ${first[1].trim()}` : '';
}

function parseToolArgs(text) {
  const decoded = decodeXmlEntities(String(text || '').trim());
  if (!decoded) return { raw: '' };
  try {
    return { raw: decoded, value: JSON.parse(decoded) };
  } catch (_) {
    const jsonMatch = decoded.match(/\{[\s\S]*\}|\[[\s\S]*\]/);
    if (jsonMatch) {
      try {
        return { raw: decoded, value: JSON.parse(jsonMatch[0]) };
      } catch (_) {}
    }
  }
  return { raw: decoded };
}

function summarizeToolCall(text) {
  const raw = String(text || '');
  const generic = raw.match(/^TURN\s+\d+\s*:\s*TOOL:\s*`?([^`\s]+)`?\s*ARGS?:\s*$/im);
  const toolName = (generic ? generic[1] : '') || extractTagAttribute(raw, 'invoke', 'name') || 'tool';
  const paramText = generic ? '' : (extractTagBody(raw, 'parameter') || extractTagBody(raw, 'arguments') || extractTagBody(raw, 'args'));
  const parsed = parseToolArgs(paramText);
  const args = parsed.value && typeof parsed.value === 'object' ? parsed.value : null;
  const argHint = args ? firstUsefulArgValue(args) : compactTurnSummary(parsed.raw, '', 56);
  const normalized = toolName.toLowerCase().replace(/[-\s]+/g, '_');

  let action = `调用工具 ${toolName}`;
  if (/(^|_)file_(read|view)$|^(read|view)(_file)?$/.test(normalized)) action = '读取文件';
  else if (/(^|_)file_(write|patch|edit)$|^(write|edit|patch)(_file)?$/.test(normalized)) action = '修改文件';
  else if (/(^|_)(code_run|run_command|shell|bash|exec|terminal)$/.test(normalized)) action = '运行命令';
  else if (normalized.includes('web_scan')) action = '扫描浏览器页面';
  else if (normalized.includes('web_execute_js')) action = '执行浏览器 JS';
  else if (normalized.includes('ask_user')) action = '询问用户';
  else if (normalized.includes('update_working_checkpoint')) action = '更新工作记忆';
  else if (normalized.includes('start_long_term_update')) action = '提炼长期记忆';
  else if (normalized.includes('search')) action = '搜索信息';
  else if (normalized.includes('browser')) action = '操作浏览器';

  return compactTurnSummary(argHint ? `${action}: ${argHint}` : action);
}

function summarizeToolResult(text) {
  const raw = decodeXmlEntities(String(text || ''));
  const status = extractTagAttribute(raw, 'result', 'status') || extractTagAttribute(raw, 'result', 'state');
  if (/error|fail/i.test(status)) return '工具返回失败';
  if (/success|ok|done/i.test(status)) return '工具返回成功';
  const body = extractTagBody(raw, 'result') || raw.replace(/<\/?function_results[^>]*>/gi, '').trim();
  const firstLine = body.split(/\r?\n/).map(line => line.trim()).find(Boolean);
  return compactTurnSummary(firstLine ? `工具返回: ${firstLine}` : '工具返回结果');
}

function summarizeStructuredLine(kind, text) {
  const raw = String(text || '');
  const firstLine = raw.split(/\r?\n/).map(line => line.trim()).find(Boolean) || '';
  const match = firstLine.match(/^(TOOL_RECALL|TOOL_REQUEST|TOOL_RESPONSE|COWORK|TUNR|TURN|ACTION|OBSERVATION|THOUGHT|TOOL)[\s:_-]*(.*)$/i);
  if (match && match[2]) return compactTurnSummary(match[2], kind);
  return compactTurnSummary(firstLine.replace(/^#+\s*/, ''), kind);
}

function summarizeLLMRunningTurn(text) {
  const raw = String(text || '');
  const withoutMarker = raw.replace(/\**LLM Running \(Turn\s+\d+\) \.\.\.\**/i, '').trim();
  const toolCall = withoutMarker.match(/<function_calls\b[\s\S]*?<\/function_calls>|<invoke\b[^>]*\bname=["'][^"']+["'][\s\S]*?<\/invoke>/i);
  if (toolCall) return summarizeToolCall(toolCall[0]);
  const toolResult = withoutMarker.match(/<function_results\b[\s\S]*?<\/function_results>|<result\b[\s\S]*?<\/result>/i);
  if (toolResult) return summarizeToolResult(toolResult[0]);
  const structured = withoutMarker
    .split(/\r?\n/)
    .map(line => line.trim())
    .find(line => line && !/^\**LLM Running \(Turn\s+\d+\) \.\.\.\**$/i.test(line));
  return compactTurnSummary(structured, '本轮暂无可提取纪要');
}

function summarizeStructuredBlock(kind, text) {
  const raw = String(text || '');
  // Prefer model-authored summaries, then derive a terse action summary.
  const summaryMatch = raw.match(/<summary>\s*([\s\S]*?)\s*<\/summary>/i);
  if (summaryMatch) {
    const line = summaryMatch[1].trim().split('\n')[0] || kind;
    return compactTurnSummary(line, kind);
  }
  if (kind === 'TOOL_CALL') return summarizeToolCall(raw);
  if (kind === 'TOOL_RESULT') return summarizeToolResult(raw);
  if (kind === 'LLM_RUNNING') {
    return summarizeLLMRunningTurn(raw);
  }
  return summarizeStructuredLine(kind, raw) || kind;
}

const LLM_RUNNING_MARKER_RE = /(\**LLM Running \(Turn \d+\) \.\.\.\**)/g;

function splitLLMRunningSegments(raw) {
  const placeholders = [];
  const protect = value => {
    placeholders.push(value);
    return `\u0000PH${placeholders.length - 1}\u0000`;
  };
  let safe = String(raw || '').replace(/`{4,}[\s\S]*?`{4,}/g, protect);
  safe = safe.replace(/`{4,}[^`][\s\S]*$/g, protect);
  const restore = value => String(value || '').replace(/\u0000PH(\d+)\u0000/g, (_, i) => placeholders[Number(i)] || '');
  const parts = safe.split(LLM_RUNNING_MARKER_RE).map(restore);
  if (parts.length < 4) return null;
  const segments = [];
  if (parts[0] && parts[0].trim()) segments.push({ kind: 'agent_message_chunk', text: parts[0].trimEnd() });
  const turns = [];
  for (let i = 1; i < parts.length; i += 2) {
    turns.push({ marker: parts[i] || '', content: parts[i + 1] || '' });
  }
  turns.forEach((turn, idx) => {
    const text = `${turn.marker}${turn.content}`.trimEnd();
    if (!text) return;
    // Historical/intermediate LLM Running turns are folded;
    // the latest turn remains plain so final answers are not hidden by default.
    segments.push({ kind: idx < turns.length - 1 ? 'LLM_RUNNING' : 'agent_message_chunk', text });
  });
  return segments.length ? segments : null;
}

function splitStructuredSegments(text) {
  const raw = String(text || '');
  const llmSegments = splitLLMRunningSegments(raw);
  if (llmSegments) return llmSegments;
  const lines = raw.split(/\r?\n/);
  const segments = [];
  let buf = [];
  let kind = 'agent_message_chunk';
  let inFence = false;
  const flush = () => {
    if (!buf.length) return;
    segments.push({ kind, text: buf.join('\n').trimEnd() });
    buf = [];
  };
  for (const line of lines) {
    const fence = /^\s*```/.test(line);
    const hit = !inFence ? detectStructuredKind(line) : null;
    if (hit && hit.kind !== kind) {
      flush();
      kind = hit.kind;
      buf.push(line);
    } else {
      buf.push(line);
    }
    if (!inFence && kind !== 'agent_message_chunk' && isStructuredClosingLine(line, kind, buf.join('\n'))) {
      flush();
      kind = 'agent_message_chunk';
    }
    if (fence) inFence = !inFence;
  }
  flush();
  return segments.length ? segments : [{ kind: 'agent_message_chunk', text: raw }];
}

function hasUnfencedStructuredMarker(text) {
  let inFence = false;
  for (const line of String(text || '').split(/\r?\n/)) {
    const fence = /^\s*```/.test(line);
    if (!inFence && detectStructuredKind(line)) return true;
    if (fence) inFence = !inFence;
  }
  return false;
}

function shouldFoldSegment(kind, text) {
  return kind !== 'agent_message_chunk';
}

function getNowMs() {
  if (typeof performance !== 'undefined' && typeof performance.now === 'function') return Math.round(performance.now());
  return Date.now();
}

function formatDuration(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value < 0) return '';
  if (value < 1000) return `${Math.max(1, Math.round(value))}ms`;
  if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)}s`;
  const minutes = Math.floor(value / 60000);
  const seconds = Math.round((value % 60000) / 1000);
  return `${minutes}m ${seconds}s`;
}

function formatTaskElapsed(ms, ended) {
  const totalSeconds = Math.max(0, Math.floor(Number(ms) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const parts = [];
  if (hours) parts.push(`${hours}h`);
  if (hours || minutes) parts.push(`${minutes}min`);
  parts.push(`${seconds}s`);
  const elapsed = parts.join(' ');
  if (ended) return `Done ✓ ${elapsed}`;
  const spinner = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏';
  const frame = spinner[Math.floor(Date.now() / 1000) % spinner.length];
  return `${frame} ${elapsed}`;
}

function getSessionRuntime(sess) {
  if (!sess) return null;
  const sessionId = sess.id;
  let runtime = state.runtimeBySessionId.get(sessionId);
  if (!runtime) {
    runtime = {
      busy: false,
      currentTurnEl: null,
      lastMessageType: null,
      taskStartedAt: 0,
      taskTimerId: null,
      assistantDraft: null,

    };
    state.runtimeBySessionId.set(sessionId, runtime);
  }
  return runtime;
}

function getActiveSessionRuntime() {
  const sess = state.sessions.get(state.activeId);
  return sess ? getSessionRuntime(sess) : null;
}

function findSessionByBridgeId(bridgeSessionId) {
  if (!bridgeSessionId) return state.sessions.get(state.activeId) || null;
  for (const sess of state.sessions.values()) {
    if (sess.bridgeSessionId === bridgeSessionId || sess.id === bridgeSessionId) return sess;
  }
  return null;
}

function isActiveSession(sess) {
  return !!sess && sess.id === state.activeId;
}

function withSessionDom(sess, fn) {
  if (isActiveSession(sess)) return fn();
  return null;
}

function updateTaskRuntimeBadges(now = getNowMs()) {
  const badges = document.querySelectorAll('.task-elapsed[data-started-at]');
  badges.forEach((badge) => {
    const startedAt = Number(badge.dataset.startedAt || 0);
    if (startedAt) badge.textContent = formatTaskElapsed(now - startedAt);
  });
}

function clearTaskTimer(sess) {
  const runtime = sess ? getSessionRuntime(sess) : getActiveSessionRuntime();
  if (runtime?.taskTimerId) {
    clearInterval(runtime.taskTimerId);
    runtime.taskTimerId = null;
  }
}

function startTaskTimer(sess, startedAt = getNowMs()) {
  const runtime = getSessionRuntime(sess);
  clearTaskTimer(sess);
  runtime.taskStartedAt = Number(startedAt) || getNowMs();
  if (isActiveSession(sess)) updateTaskRuntimeBadges(runtime.taskStartedAt);
  runtime.taskTimerId = setInterval(() => {
    if (isActiveSession(sess)) updateTaskRuntimeBadges();
  }, 1000);
}

function stopTaskTimer(sess) {
  if (isActiveSession(sess)) updateTaskRuntimeBadges();
  const runtime = getSessionRuntime(sess);
  clearTaskTimer(sess);
  runtime.taskStartedAt = 0;
}

function taskElapsedBadge(startedAt, endedAt) {
  const start = Number(startedAt || 0);
  if (!start) return '';
  const end = Number(endedAt || 0);
  const now = end || getNowMs();
  const ended = !!end;
  const liveAttr = ended ? 'data-ended="1"' : `data-started-at="${escapeHtml(String(start))}"`;
  return `<span class="task-elapsed" ${liveAttr}>${escapeHtml(formatTaskElapsed(now - start, ended))}</span>`;
}

function ensureAssistantTaskElapsed(wrap, startedAt, endedAt) {
  if (!wrap) return null;
  const html = taskElapsedBadge(startedAt, endedAt);
  let badge = wrap.querySelector(':scope > .task-elapsed');
  if (!html) {
    badge?.remove();
    return null;
  }
  if (!badge) {
    wrap.insertAdjacentHTML('afterbegin', html);
    badge = wrap.querySelector(':scope > .task-elapsed');
  } else {
    const holder = document.createElement('div');
    holder.innerHTML = html;
    badge.replaceWith(holder.firstElementChild);
    badge = wrap.querySelector(':scope > .task-elapsed');
  }
  return badge;
}

function turnLabelForSegment(seg, index) {
  const summary = summarizeStructuredBlock(seg.kind, seg.text);
  if (seg.kind === 'LLM_RUNNING') return summary || `Turn ${index + 1}`;
  if (seg.kind === 'TOOL_CALL') return 'Tool';
  if (seg.kind === 'TOOL_RESULT') return 'Result';
  return summary || seg.kind || `Turn ${index + 1}`;
}

function nextTurnIndexForWrap(wrap) {
  const current = Number(wrap?.dataset?.turnIndex || 0) || 0;
  const next = current + 1;
  if (wrap) wrap.dataset.turnIndex = String(next);
  return next;
}

function turnHeaderLabel(index, label) {
  return `Turn ${index} : ${label || 'response'}`;
}

function groupIntoTurns(segments, options = {}) {
  let foldIndex = 0;
  return (segments || []).map((seg) => {
    if (!shouldFoldSegment(seg.kind, seg.text)) return { type: 'plain', segment: seg };
    const index = ++foldIndex;
    return {
      type: 'turn',
      index,
      label: turnLabelForSegment(seg, index - 1),
      segment: seg
    };
  });
}

function extractTagBody(text, tag) {
  const escapedTag = String(tag || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = '<' + escapedTag + '\\b[^>]*>([\\s\\S]*?)<\\/' + escapedTag + '>';
  const m = String(text || '').match(new RegExp(pattern, 'i'));
  return m ? m[1].trim() : '';
}

function parseToolDetails(kind, text) {
  const raw = String(text || '');
  if (kind === 'TOOL_CALL') {
    const generic = raw.match(/^TURN\s+\d+\s*:\s*TOOL:\s*`?([^`\s]+)`?\s*ARGS?:\s*$/im);
    if (generic) return { title: `Tool: ${generic[1]}`, tool: generic[1], args: '' };
    const invoke = raw.match(/<invoke\b[^>]*\bname=["']([^"']+)["'][^>]*>/i);
    const tool = invoke ? invoke[1] : '';
    const params = extractTagBody(raw, 'parameter') || extractTagBody(raw, 'arguments') || extractTagBody(raw, 'args');
    const jsonish = params || (raw.match(/<invoke\b[^>]*>[\s\S]*?<\/invoke>/i)?.[0] || '').replace(/<\/?invoke[^>]*>/gi, '').trim();
    if (tool || jsonish) return { title: tool ? `Tool: ${tool}` : 'Tool call', tool, args: jsonish };
  }
  if (kind === 'TOOL_RESULT') {
    const result = extractTagBody(raw, 'result') || raw.replace(/<\/?function_results[^>]*>/gi, '').trim();
    if (result) return { title: 'Tool result', tool: 'result', args: result };
  }
  return null;
}

function renderToolDetailInto(container, seg) {
  const detail = parseToolDetails(seg.kind, seg.text);
  if (!detail) return;
  const detailTurn = document.createElement('div');
  detailTurn.className = 'turn tool-detail-turn';
  const header = document.createElement('button');
  header.type = 'button';
  header.className = 'turn-header tool-detail-header';
  header.innerHTML = `<span class="turn-caret">▼</span><span class="turn-tag">${escapeHtml(detail.title)}</span><span class="turn-summary">args</span>`;
  header.addEventListener('click', () => detailTurn.classList.toggle('collapsed'));
  const body = document.createElement('div');
  body.className = 'turn-body md tool-detail-body';
  const codeText = `Tool: ${detail.tool || detail.title.replace(/^Tool:\s*/, '') || 'tool'}\nargs:\n${detail.args || ''}`;
  body.innerHTML = `<pre class="tool-args-code"><code>${escapeHtml(codeText)}</code></pre>`;
  detailTurn.appendChild(header);
  detailTurn.appendChild(body);
  container.appendChild(detailTurn);
}

function renderTurnTreeInto(container, turn) {
  const seg = turn.segment;
  const node = document.createElement('div');
  node.className = 'turn collapsed structured-turn turn-group';
  node.dataset.kind = seg.kind;
  node.dataset.buf = seg.text;
  const header = document.createElement('button');
  header.type = 'button';
  header.className = 'turn-header';
  header.innerHTML = `<span class="turn-caret">▼</span><span class="turn-tag">${escapeHtml(turnHeaderLabel(turn.index, turn.label))}</span>`;
  header.addEventListener('click', () => node.classList.toggle('collapsed'));
  const body = document.createElement('div');
  body.className = 'turn-body md';
  const hasToolDetail = Boolean(parseToolDetails(seg.kind, seg.text));
  renderToolDetailInto(body, seg);
  if (!hasToolDetail) {
    const rendered = document.createElement('div');
    rendered.className = 'turn-rendered-md';
    rendered.innerHTML = renderMarkdown(seg.text);
    body.appendChild(rendered);
  }
  node.appendChild(header);
  node.appendChild(body);
  container.appendChild(node);
}

/**
 * Extract <summary>...</summary> from text, render it as a faded italic hint,
 * and return the remaining text. If no summary tag found, returns text unchanged.
 */
/**
 * Strip leading <summary> and <think> tags from text.
 * Returns { summary, think, remaining } where summary/think are the extracted
 * content strings (or null), and remaining is the text to render as markdown.
 */
function stripLeadingMetaTags(text) {
  let remaining = text;
  let summary = null;
  let think = null;
  // Strip <summary>...</summary> at start
  const sumRe = /^<summary>([\s\S]*?)<\/summary>\s*/i;
  const sumM = remaining.match(sumRe);
  if (sumM) {
    summary = sumM[1].trim();
    remaining = remaining.slice(sumM[0].length);
  }
  // Strip <think>...</think> at start (or after summary)
  const thinkRe = /^<think>([\s\S]*?)<\/think>\s*/i;
  const thinkM = remaining.match(thinkRe);
  if (thinkM) {
    think = thinkM[1].trim();
    remaining = remaining.slice(thinkM[0].length);
  }
  return { summary, think, remaining };
}

function extractAndRenderSummary(container, text) {
  const { summary, think, remaining } = stripLeadingMetaTags(text);
  if (summary) {
    const hint = document.createElement('div');
    hint.className = 'summary-hint';
    hint.textContent = summary;
    container.appendChild(hint);
  }
  if (think) {
    const thinkEl = document.createElement('div');
    thinkEl.className = 'think-hint';
    thinkEl.textContent = think;
    container.appendChild(thinkEl);
  }
  return remaining;
}

function stripVisibleToolProtocol(text) {
  return String(text || '')
    .replace(/^\s*TURN\s+\d+\s*:\s*TOOL:\s*`?[^`\s]+`?\s*ARGS?:\s*$/gim, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function renderStructuredMarkdownInto(container, text, options = {}) {
  const segments = splitStructuredSegments(text);
  container.innerHTML = '';
  if (segments.length === 1 && !shouldFoldSegment(segments[0].kind, segments[0].text)) {
    const remaining = extractAndRenderSummary(container, text);
    const visible = stripVisibleToolProtocol(remaining);
    if (visible) container.insertAdjacentHTML('beforeend', renderMarkdown(visible));
    return;
  }
  for (const item of groupIntoTurns(segments, options)) {
    if (item.type === 'plain') {
      const plain = document.createElement('div');
      plain.className = 'md';
      const remaining = extractAndRenderSummary(plain, item.segment.text);
      const visible = stripVisibleToolProtocol(remaining);
      if (visible) plain.insertAdjacentHTML('beforeend', renderMarkdown(visible));
      container.appendChild(plain);
      continue;
    }
    renderTurnTreeInto(container, item);
  }
}

// ─── Copy button injection for code blocks and pre blocks ─────────────────
function injectCopyButtons(container) {
  if (!container) return;
  const blocks = container.querySelectorAll('pre');
  blocks.forEach(pre => {
    if (pre.querySelector('.copy-btn')) return; // already injected
    pre.style.position = 'relative';
    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.textContent = 'Copy';
    btn.setAttribute('aria-label', 'Copy code');
    btn.addEventListener('click', () => {
      const code = pre.querySelector('code') || pre;
      navigator.clipboard.writeText(code.textContent).then(() => {
        btn.textContent = '✓ Copied';
        btn.classList.add('copied');
        setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
      }).catch(() => {
        btn.textContent = '✗ Failed';
        setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
      });
    });
    pre.appendChild(btn);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

// ─── Session management ──────────────────────────────────────────────────
function isUntitledSessionTitle(title) {
  return !title || /^new\s+chat$/i.test(String(title).trim());
}

function createLocalSession(id, title, bridgeSessionId = id) {
  const sess = {
    id, bridgeSessionId, title: title || 'New chat', messages: [], cwd: null,
    untitled: isUntitledSessionTitle(title),
    config: { ...state.defaultConfig },
    diagnostics: [],
    modelOverride: null,
    tokenUsage: { input: 0, output: 0, total: 0, limit: 200000 },
    groupId: null,
  };
  getSessionRuntime(sess);
  // Keep freshly-created chats visually quiet: the empty state is enough guidance.
  state.sessions.set(id, sess);
  renderSessionList();
  return sess;
}

function setActiveSession(id) {
  // Save scroll position of current session before switching
  if (state.activeId) {
    const prevRuntime = state.runtimeBySessionId.get(state.activeId);
    if (prevRuntime) prevRuntime.scrollPos = messagesEl.scrollTop;
  }
  state.activeId = id;
  const sess = state.sessions.get(id);
  if (!sess) return;
  sessionTitleEl.textContent = sess.title;
  renderMessages();
  renderSessionList();
  updateModelStatus();
  renderAgentPanel();
  const runtime = getSessionRuntime(sess);
  setBusy(runtime.busy, runtime.busy ? 'Agent is responding…' : null, sess);
  // When switching to a session that is still running, ensure the live draft
  // is rendered immediately and polling is active (it may have been started
  // earlier but its render calls were no-ops because the session wasn't active).
  if (runtime.busy) {
    const draft = runtime.assistantDraft;
    if (draft && !draft.finalized) {
      renderAssistantDraftInPlace(sess, draft);
    }
    // Restart polling if it stopped (e.g. page reload or race condition)
    if (!runtime.polling) {
      runtime.forcePollOnce = true;
      pollSessionMessages(sess);
    } else {
      // Polling is running but was rendering as no-op while we were away.
      // Do an immediate one-shot poll to refresh the view right now.
      (async () => {
        try {
          const res = await GaBridge.pollSession(sess.bridgeSessionId || sess.id, runtime.lastPolledMessageId || 0);
          if (res?.error) return;
          const result = res.result || res;
          for (const msg of (result.messages || [])) upsertPolledMessage(sess, msg, { partial: false });
          if (result.partial) upsertPolledMessage(sess, result.partial, { partial: true });
        } catch(e) { /* ignore, regular polling will handle it */ }
      })();
    }
  }
}

function renderSessionList() {
  sessionListEl.innerHTML = '';

  // Group sessions by groupId
  const grouped = new Map();
  const ungrouped = [];

  for (const sess of state.sessions.values()) {
    if (sess.groupId && state.sessionGroups.has(sess.groupId)) {
      if (!grouped.has(sess.groupId)) grouped.set(sess.groupId, []);
      grouped.get(sess.groupId).push(sess);
    } else {
      ungrouped.push(sess);
    }
  }

  // Render groups
  for (const [groupId, group] of state.sessionGroups.entries()) {
    const sessions = grouped.get(groupId) || [];
    if (sessions.length === 0) continue;

    const groupEl = document.createElement('div');
    groupEl.className = 'session-group' + (group.collapsed ? ' collapsed' : '');

    const headerEl = document.createElement('div');
    headerEl.className = 'session-group-header';
    headerEl.innerHTML = `
      <span class="expand-icon">▼</span>
      <span>${escapeHtml(group.name)}</span>
    `;
    headerEl.addEventListener('click', () => toggleGroup(groupId));
    groupEl.appendChild(headerEl);

    const sessionsEl = document.createElement('div');
    sessionsEl.className = 'session-group-sessions';
    sessions.forEach(sess => {
      sessionsEl.appendChild(createSessionItem(sess));
    });
    groupEl.appendChild(sessionsEl);
    sessionListEl.appendChild(groupEl);
  }

  // Render ungrouped sessions
  if (ungrouped.length > 0) {
    ungrouped.forEach(sess => {
      sessionListEl.appendChild(createSessionItem(sess));
    });
  }

  // Initialize drawer collapse state
  if (state.leftDrawerCollapsed) {
    leftDrawer.classList.add('collapsed');
  }
  if (state.rightDrawerCollapsed) {
    rightDrawer.classList.add('collapsed');
  }
}

function createSessionItem(sess) {
  const item = document.createElement('div');
  item.className = 'session-item' + (sess.id === state.activeId ? ' active' : '');
  item.setAttribute('data-session-id', sess.id);
  item.title = sess.title;

  const dot = document.createElement('span');
  dot.className = 'session-dot';
  const runtime = getSessionRuntime(sess);
  if (runtime && runtime.busy) dot.classList.add('busy');
  if (sess.lastError) dot.classList.add('error');
  item.appendChild(dot);

  const label = document.createElement('span');
  label.className = 'session-label';
  label.textContent = sess.title;
  item.appendChild(label);

  const deleteBtn = document.createElement('span');
  deleteBtn.className = 'session-delete';
  deleteBtn.innerHTML = '×';
  deleteBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (confirm(`Delete session "${sess.title}"?`)) {
      closeSession(sess.id);
    }
  });
  item.appendChild(deleteBtn);

  item.addEventListener('click', () => setActiveSession(sess.id));

  // Right-click context menu for grouping
  item.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    showSessionContextMenu(e, sess);
  });

  return item;
}

function showSessionContextMenu(e, sess) {
  // Simple implementation: prompt for group name
  const groupName = prompt('输入分组名称（留空则移除分组）:', sess.groupId || '');
  if (groupName !== null) {
    assignSessionToGroup(sess.id, groupName || null);
  }
}

async function assignSessionToGroup(sessionId, groupName) {
  const sess = state.sessions.get(sessionId);
  if (!sess) return;

  // Create group if it doesn't exist
  if (groupName && !state.sessionGroups.has(groupName)) {
    state.sessionGroups.set(groupName, {
      id: groupName,
      name: groupName,
      sessionIds: [],
      collapsed: false
    });
  }

  sess.groupId = groupName;

  // Update backend
  const bridgeUrl = window.zeroAgent.bridgeUrl || 'http://127.0.0.1:14168';
  try {
    await fetch(`${bridgeUrl}/session/${sess.bridgeSessionId}/group`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ groupId: groupName })
    });
  } catch (err) {
    console.error('Failed to update group:', err);
  }

  renderSessionList();
}

function toggleGroup(groupId) {
  const group = state.sessionGroups.get(groupId);
  if (group) {
    group.collapsed = !group.collapsed;
    renderSessionList();
  }
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}


function closeSession(id) {
  if (state.sessions.size <= 1) return; // Don't close the last session
  // Notify bridge to delete this session
  const sess = state.sessions.get(id);
  if (sess && sess.bridgeSessionId) {
    const bridgeUrl = window.zeroAgent.bridgeUrl || 'http://127.0.0.1:14168';
    fetch(`${bridgeUrl}/session/${sess.bridgeSessionId}`, { method: 'DELETE' }).catch(() => {});
  }
  const keys = [...state.sessions.keys()];
  const idx = keys.indexOf(id);
  state.sessions.delete(id);
  state.runtimeBySessionId.delete(id);
  state.activeAgents.delete(id);

  if (state.activeId === id) {
    // Switch to adjacent session
    const remaining = [...state.sessions.keys()];
    const newIdx = Math.max(0, Math.min(idx, remaining.length - 1));
    setActiveSession(remaining[newIdx]);
  } else {
    renderSessionList();
  }
}

async function newSession() {
  if (!state.bridgeReady) {
    showError('Bridge is not ready yet. Please wait a moment.');
    return;
  }
  const previousSess = state.sessions.get(state.activeId) || null;
  // Don't mark previousSess as busy - it's not doing anything
  // Just show status text without changing any tab dot
  const statusEl = $('status');
  if (statusEl) statusEl.textContent = 'Creating session…';
  let createdSess = null;
  try {
    const cwd = await getCwd();
    const res = await window.zeroAgent.rpc('session/new', { cwd, mcp_servers: [] });
    if (res.error) throw new Error(typeof res.error === 'string' ? res.error : (res.error.message || JSON.stringify(res.error)));
    const bridgeSessionId = res.sessionId;
    const localSessionId = `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    createdSess = createLocalSession(localSessionId, 'New chat', bridgeSessionId);
    createdSess.cwd = cwd;
    setActiveSession(localSessionId);
  } catch (e) {
    showError('Failed to create session: ' + e.message);
  } finally {
    setBusy(false, null, createdSess || previousSess);
  }
}

async function getCwd() {
  // Use ZeroAgent workspace as default cwd
  const status = await window.zeroAgent.checkStatus();
  return status.workspaceDir || '';
}

// ─── Messages rendering ──────────────────────────────────────────────────
// DOM cache: sessionId -> { fragment, scrollTop }
const _domCache = new Map();

function renderMessages() {
  const sess = state.sessions.get(state.activeId);
  const runtime = sess ? getSessionRuntime(sess) : null;

  // Save current DOM + scroll to cache for previous session
  if (state._prevRenderedId && state._prevRenderedId !== state.activeId) {
    const frag = document.createDocumentFragment();
    while (messagesEl.firstChild) frag.appendChild(messagesEl.firstChild);
    _domCache.set(state._prevRenderedId, {
      fragment: frag,
      scrollTop: runtime ? (state.runtimeBySessionId.get(state._prevRenderedId)?.scrollPos ?? 0) : 0,
    });
  }

  if (runtime) {
    runtime.currentTurnEl = null;
    runtime.lastMessageType = null;
  }

  const hasSavedMessages = !!sess && sess.messages.length > 0;
  const hasDraft = !!runtime?.assistantDraft && !runtime.assistantDraft.finalized;
  if (!sess || (!hasSavedMessages && !hasDraft)) {
    messagesEl.innerHTML = '';
    messagesEl.classList.add('empty');
    messagesEl.innerHTML = `
      <div class="empty-state">
        <div class="empty-title">New task</div>
        <div class="empty-sub">Task me anything. Type <code>/help</code> for commands.</div>
      </div>`;
    state._prevRenderedId = state.activeId;
    return;
  }

  messagesEl.classList.remove('empty');

  // Try to restore from cache
  const cached = _domCache.get(state.activeId);
  if (cached) {
    messagesEl.innerHTML = '';
    messagesEl.appendChild(cached.fragment);
    _domCache.delete(state.activeId);
    // If there's a live draft, the cached DOM is stale — re-render the draft portion
    if (hasDraft) {
      // Remove the stale assistant wrap (last unfinalized msg-assistant element)
      const last = messagesEl.lastElementChild;
      if (last?.classList?.contains('msg-assistant') && last.dataset.finalized !== '1') {
        last.remove();
      }
      renderAssistantDraft(sess, runtime.assistantDraft);
    }
    messagesEl.scrollTop = cached.scrollTop;
  } else {
    messagesEl.innerHTML = '';
    for (const m of sess.messages) renderMessage(m, false);
    if (hasDraft) renderAssistantDraft(sess, runtime.assistantDraft);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
  state._prevRenderedId = state.activeId;
}

function prepareMessagesForContent() {
  if (messagesEl.classList.contains('empty')) messagesEl.innerHTML = '';
  messagesEl.classList.remove('empty');
}

function renderMessage(msg, append = true) {
  prepareMessagesForContent();

  if (msg.role === 'user') {
    const wrap = document.createElement('div');
    wrap.className = 'msg msg-user';
    let imagesHtml = '';
    const ids = msg.image_ids || [];
    if (ids.length > 0) {
      imagesHtml = '<div class="user-images">' + ids.map(id => {
        const dataUrl = sessionStorage.getItem('img:' + id);
        if (dataUrl) {
          return `<img src="${dataUrl}" class="user-msg-thumb" />`;
        }
        return `<span class="user-msg-thumb-placeholder" title="Image expired">🖼</span>`;
      }).join('') + '</div>';
    }
    wrap.innerHTML = `<div class="bubble">${imagesHtml}${escapeHtml(msg.content)}</div>`;
    messagesEl.appendChild(wrap);
    const sess = state.sessions.get(state.activeId);
    const runtime = sess ? getSessionRuntime(sess) : null;
    if (runtime) {
      runtime.currentTurnEl = null; // reset turn grouping on user message
      runtime.lastMessageType = 'user';
    }
  } else if (msg.role === 'system') {
    const wrap = document.createElement('div');
    wrap.className = 'msg msg-system';
    wrap.textContent = msg.content;
    messagesEl.appendChild(wrap);
  } else if (msg.role === 'error') {
    const wrap = document.createElement('div');
    wrap.className = 'msg msg-error';
    wrap.textContent = msg.content;
    messagesEl.appendChild(wrap);
    const sess = state.sessions.get(state.activeId);
    const runtime = sess ? getSessionRuntime(sess) : null;
    if (runtime) runtime.currentTurnEl = null;
  } else if (msg.role === 'assistant') {
    // Final full message (when reloading from state)
    const wrap = document.createElement('div');
    wrap.className = 'msg msg-assistant';
    if (msg.segments) {
      ensureAssistantTaskElapsed(wrap, msg.taskStartedAt, msg.taskEndedAt);
      for (const seg of msg.segments) {
        wrap.appendChild(buildTurn(seg.kind, seg.text, seg.collapsed, nextTurnIndexForWrap(wrap)));
      }
    } else {
      const body = document.createElement('div');
      body.className = 'assistant-response md';
      ensureAssistantTaskElapsed(wrap, msg.taskStartedAt, msg.taskEndedAt);
      const cleanContent = (msg.content || '').replace(/\n*`{5}\n*\[Info\] Final response to user\.\n*`{5}\s*$/, '');
      renderStructuredMarkdownInto(body, cleanContent);
      injectCopyButtons(body);
      wrap.appendChild(body);
    }
    injectCopyButtons(wrap);
    messagesEl.appendChild(wrap);
  }
  if (append) scrollToBottom();
}

function buildTurn(kind, text, collapsed, index) {
  const turn = document.createElement('div');
  turn.className = 'turn' + (collapsed ? ' collapsed' : '');
  turn.dataset.kind = kind;
  const turnIndex = Number(index || 0);
  const label = turnIndex ? turnHeaderLabel(turnIndex, kind) : kind;
  const summary = summarizeStructuredBlock(kind, text);
  const header = document.createElement('div');
  header.className = 'turn-header';
  header.innerHTML = `<span class="turn-caret">▼</span><span class="turn-tag">${escapeHtml(label)}</span><span class="turn-summary">${escapeHtml(summary)}</span>`;
  header.addEventListener('click', () => turn.classList.toggle('collapsed'));
  const body = document.createElement('div');
  body.className = 'turn-body md';
  const hasToolDetail = Boolean(parseToolDetails(kind, text));
  renderToolDetailInto(body, { kind, text });
  if (!hasToolDetail) {
    const rendered = document.createElement('div');
    rendered.className = 'turn-rendered-md';
    rendered.innerHTML = renderMarkdown(text);
    body.appendChild(rendered);
  }
  turn.appendChild(header);
  turn.appendChild(body);
  injectCopyButtons(body);
  return turn;
}

function isNearBottom(threshold = 150) {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < threshold;
}

function scrollToBottom(smooth = true) {
  messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}

// ─── Streaming chunks (from ACP bridge notifications) ────────────────────
// ACP sends method='session/update' with params.update.sessionUpdate=
//   agent_message_chunk | agent_thought_chunk | tool_call | tool_call_update | plan | available_commands_update
function handleNotification(msg) {
  // Handle WS session-state notifications from the bridge backend.
  // These have {type: "session-state", sessionId, state, status, seq, ...}
  // and are used to kick-start polling for sessions that became active
  // (e.g. after page reload, or when a background session starts running).
  if (msg.type === 'session-state') {
    const sess = findSessionByBridgeId(msg.sessionId);
    if (!sess) return;

    // Update token usage and model override if provided
    if (msg.tokenUsage) {
      sess.tokenUsage = msg.tokenUsage;
      if (isActiveSession(sess)) updateModelStatus();
    }
    if (msg.modelOverride !== undefined) {
      sess.modelOverride = msg.modelOverride;
      if (isActiveSession(sess)) updateModelStatus();
    }
    if (msg.groupId !== undefined) {
      sess.groupId = msg.groupId;
    }

    const runtime = getSessionRuntime(sess);
    if ((msg.state === 'running' || msg.status === 'running') && !runtime.polling) {
      runtime.busy = true;
      runtime.forcePollOnce = true;
      setBusy(true, 'Thinking…', sess);
      pollSessionMessages(sess);
    } else if (msg.state === 'idle' || msg.state === 'error' || msg.status === 'idle') {
      // Session finished in background — do a final poll to pick up remaining messages
      if (!runtime.polling && runtime.busy) {
        runtime.forcePollOnce = true;
        pollSessionMessages(sess);
      }
    }
    // Update tab dot regardless
    renderSessionList();
    return;
  }

  // Handle agent events
  if (msg.type === 'agent-spawned') {
    const sess = findSessionByBridgeId(msg.sessionId);
    if (sess && msg.agent) {
      const agents = state.activeAgents.get(sess.id) || [];
      agents.push(msg.agent);
      state.activeAgents.set(sess.id, agents);
      if (isActiveSession(sess)) renderAgentPanel();
    }
    return;
  }

  if (msg.type === 'agent-status') {
    const sess = findSessionByBridgeId(msg.sessionId);
    if (sess && msg.agentId) {
      const agents = state.activeAgents.get(sess.id) || [];
      const agent = agents.find(a => a.id === msg.agentId);
      if (agent) {
        agent.status = msg.status;
        if (isActiveSession(sess)) renderAgentPanel();
      }
    }
    return;
  }
  if (msg.method !== 'session/update') return;
  const update = msg.params?.update;
  if (!update) return;
  const kind = update.sessionUpdate;
  const bridgeSessionId = msg.params?.sessionId || update.sessionId || update.session?.id;
  const sess = findSessionByBridgeId(bridgeSessionId);
  if (!sess) return;


  if (kind === 'agent_message_chunk') {
    const text = extractText(update.content);
    appendAssistantChunk(sess, text);
  } else if (kind === 'task_started') {
    hideError();
    startTaskTimer(sess);
    setBusy(true, 'Thinking…', sess);
  } else if (kind === 'task_completed' || kind === 'cancelled') {
    finalizeAssistantReply(sess);
    setBusy(false, null, sess);
    hideError();
  } else if (kind === 'error') {
    finalizeAssistantReply(sess);
    setBusy(false, null, sess);
    const errText = update.message || update.error || 'Bridge error';
    sess.messages.push({ role: 'error', content: errText });
    if (isActiveSession(sess)) renderMessage({ role: 'error', content: errText });
    showError(errText);
  } else if (kind === 'agent_thought_chunk') {
    const text = extractText(update.content);
    appendStreamChunk(sess, kind, text);
  } else if (kind === 'tool_call') {
    const toolName = update.title || update.name || update.kind || update.toolCallId || 'tool';
    const args = update.arguments || update.args || update.input || update.content || '';
    const argText = typeof args === 'string' ? args : JSON.stringify(args, null, 2);
    const text = `<function_calls>
<invoke name="${escapeHtml(toolName)}">
<parameter name="args">${escapeHtml(argText)}</parameter>
</invoke>
</function_calls>`;
    appendTurn(sess, 'TOOL_CALL', text, true);
  } else if (kind === 'tool_call_update') {
    // Status updates, keep simple
    if (update.status && update.status !== 'in_progress') {
      appendTurn(sess, 'tool', `[${update.status}] ${update.toolCallId || ''}`, true);
    }
  } else if (kind === 'plan') {
    const lines = (update.entries || []).map(e =>
      `- [${e.status || 'pending'}] ${e.content || ''}`
    ).join('\n');
    appendTurn(sess, 'plan', lines, false);
  }
}

function extractText(content) {
  if (!content) return '';
  if (typeof content === 'string') return content;
  if (content.type === 'text') return content.text || '';
  if (Array.isArray(content)) return content.map(extractText).join('');
  return '';
}

function getLiveAssistantWrap(sess) {
  if (!isActiveSession(sess)) return null;
  const last = messagesEl.lastElementChild;
  if (last?.classList?.contains('msg-assistant') && last.dataset.finalized !== '1') return last;
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-assistant';
  const runtime = getSessionRuntime(sess);
  if (runtime.taskStartedAt) {
    wrap.dataset.taskStartedAt = String(runtime.taskStartedAt);
    ensureAssistantTaskElapsed(wrap, runtime.taskStartedAt);
  }
  messagesEl.appendChild(wrap);
  return wrap;
}

function getAssistantDraft(sess) {
  const runtime = getSessionRuntime(sess);
  if (!runtime.assistantDraft || runtime.assistantDraft.finalized) {
    runtime.assistantDraft = {
      text: '',
      segments: [],
      currentSegmentIndex: -1,
      taskStartedAt: runtime.taskStartedAt || 0,
      taskEndedAt: 0,
      finalized: false,
      bridgeMessageId: 0
    };
  }
  if (!runtime.assistantDraft.taskStartedAt && runtime.taskStartedAt) runtime.assistantDraft.taskStartedAt = runtime.taskStartedAt;
  return runtime.assistantDraft;
}

function renderAssistantDraft(sess, draft) {
  if (!isActiveSession(sess) || !draft || draft.finalized) return null;
  prepareMessagesForContent();
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-assistant';
  if (draft.taskStartedAt) {
    wrap.dataset.taskStartedAt = String(draft.taskStartedAt);
    ensureAssistantTaskElapsed(wrap, draft.taskStartedAt, draft.taskEndedAt);
  }
  if (draft.text) {
    wrap.dataset.buf = draft.text;
    const body = document.createElement('div');
    body.className = 'assistant-response md';
    renderStructuredMarkdownInto(body, draft.text);
    injectCopyButtons(body);
    if (!draft.finalized) body.insertAdjacentHTML('beforeend', '<span class="cursor"></span>');
    wrap.appendChild(body);
  }
  for (const seg of draft.segments || []) {
    wrap.appendChild(buildTurn(seg.kind, seg.text, seg.collapsed, nextTurnIndexForWrap(wrap)));
  }
  injectCopyButtons(wrap);
  messagesEl.appendChild(wrap);
  return wrap;
}

function renderAssistantDraftInPlace(sess, draft) {
  if (!isActiveSession(sess) || !draft || draft.finalized) return null;
  prepareMessagesForContent();
  const runtime = getSessionRuntime(sess);
  const wrap = getLiveAssistantWrap(sess);
  if (draft.taskStartedAt || runtime.taskStartedAt) {
    const startedAt = draft.taskStartedAt || runtime.taskStartedAt;
    wrap.dataset.taskStartedAt = String(startedAt);
    ensureAssistantTaskElapsed(wrap, startedAt, draft.taskEndedAt);
  }
  wrap.dataset.buf = draft.text || '';
  let body = wrap.querySelector('.assistant-response');
  if (!body) {
    body = document.createElement('div');
    body.className = 'assistant-response md';
    // Keep plain assistant text before folded/tool turns.
    const firstTurn = wrap.querySelector('.turn');
    if (firstTurn) wrap.insertBefore(body, firstTurn);
    else wrap.appendChild(body);
  }
  renderStructuredMarkdownInto(body, draft.text || '');
  injectCopyButtons(body);
  body.insertAdjacentHTML('beforeend', '<span class="cursor"></span>');
  if (isNearBottom()) scrollToBottom(false);
  return wrap;
}

function appendAssistantChunk(sess, text) {
  if (!text) return;
  const runtime = getSessionRuntime(sess);
  const draft = getAssistantDraft(sess);
  draft.text += text;
  draft.currentSegmentIndex = -1;
  runtime.currentTurnEl = null;
  if (!isActiveSession(sess)) return;
  prepareMessagesForContent();
  let wrap = getLiveAssistantWrap(sess);
  let body = wrap.querySelector('.assistant-response');
  if (!body) {
    body = document.createElement('div');
    body.className = 'assistant-response md';
    wrap.appendChild(body);
  }
  wrap.dataset.buf = draft.text;
  ensureAssistantTaskElapsed(wrap, draft.taskStartedAt || runtime.taskStartedAt);
  renderStructuredMarkdownInto(body, draft.text);
  body.insertAdjacentHTML('beforeend', '<span class="cursor"></span>');
  if (isNearBottom()) scrollToBottom(false);
}

function appendStreamChunk(sess, kind, text) {
  if (!text) return;
  // Group consecutive chunks of same kind into one turn (fold_turns style)
  const runtime = getSessionRuntime(sess);
  const draft = getAssistantDraft(sess);
  let seg = draft.segments[draft.currentSegmentIndex];
  if (!seg || seg.kind !== kind) {
    seg = { kind, text: '', collapsed: false };
    draft.segments.push(seg);
    draft.currentSegmentIndex = draft.segments.length - 1;
    runtime.currentTurnEl = null;
  }
  seg.text += text;
  if (!isActiveSession(sess)) return;
  let turn = runtime.currentTurnEl;
  const currentKind = turn?.dataset.kind;
  if (!turn || currentKind !== kind) {
    turn = createStreamingTurn(sess, kind);
    runtime.currentTurnEl = turn;
  }
  turn.dataset.buf = seg.text;
  const body = turn.querySelector('.turn-body');
  const { summary, remaining: cleanText } = stripLeadingMetaTags(seg.text);
  // Update the header turn-summary span (visible when collapsed)
  const summarySpan = turn.querySelector('.turn-summary');
  if (summarySpan && summary) {
    summarySpan.textContent = summary;
  }
  // Only render the clean body text (no summary-hint in body)
  body.innerHTML = renderMarkdown(cleanText) + '<span class="cursor"></span>';
  if (isNearBottom()) scrollToBottom(false);
}

function appendTurn(sess, kind, text, collapsed) {
  const draft = getAssistantDraft(sess);
  draft.segments.push({ kind, text, collapsed: !!collapsed });
  draft.currentSegmentIndex = -1;
  if (!isActiveSession(sess)) return;
  prepareMessagesForContent();
  const wrap = getLiveAssistantWrap(sess);
  wrap.appendChild(buildTurn(kind, text, collapsed, nextTurnIndexForWrap(wrap)));
  if (isNearBottom()) scrollToBottom(false);
}

function createStreamingTurn(sess, kind) {
  prepareMessagesForContent();
  const wrap = getLiveAssistantWrap(sess);
  const turn = document.createElement('div');
  turn.className = 'turn';
  turn.dataset.kind = kind;
  const displayKind = kind === 'agent_thought_chunk' ? 'thinking' : 'response';
  const turnIndex = nextTurnIndexForWrap(wrap);
  turn.innerHTML = `
    <div class="turn-header"><span class="turn-caret">▼</span><span class="turn-tag">${escapeHtml(turnHeaderLabel(turnIndex, displayKind))}</span><span class="turn-summary"></span></div>
    <div class="turn-body md"></div>`;
  turn.querySelector('.turn-header').addEventListener('click', () => turn.classList.toggle('collapsed'));
  // thinking turns collapsed by default once complete
  if (kind === 'agent_thought_chunk') turn.dataset.autoCollapse = '1';
  wrap.appendChild(turn);
  return turn;
}

function getCurrentAssistantWrap(sess) {
  if (!isActiveSession(sess)) return null;
  const last = messagesEl.lastElementChild;
  if (last?.classList?.contains('msg-assistant') && last.dataset.finalized !== '1') return last;
  return null;
}

function finalizeStreamingTurn(sess) {
  const runtime = getSessionRuntime(sess);
  const wrap = getCurrentAssistantWrap(sess);
  const liveAssistant = wrap?.querySelector('.assistant-response');
  if (liveAssistant) {
    renderStructuredMarkdownInto(liveAssistant, wrap.dataset.buf || '');
    injectCopyButtons(liveAssistant);
  }
  if (runtime.assistantDraft?.segments?.length) {
    for (const seg of runtime.assistantDraft.segments) {
      if (seg.kind === 'agent_thought_chunk') seg.collapsed = true;
    }
  }
  if (!runtime.currentTurnEl) return;
  const t = runtime.currentTurnEl;
  const body = t.querySelector('.turn-body');
  // Remove cursor, strip summary/think tags; set turn-summary in header
  if (body) {
    const { summary: extractedSummary, remaining: cleanBuf } = stripLeadingMetaTags(t.dataset.buf || '');
    body.innerHTML = renderMarkdown(cleanBuf);
    // Set the turn-summary span in the header for collapsed display
    const summaryEl = t.querySelector('.turn-summary');
    if (summaryEl) {
      const kind = t.dataset.kind || 'response';
      summaryEl.textContent = extractedSummary || summarizeStructuredBlock(kind, cleanBuf);
    }
  }
  if (t.dataset.autoCollapse === '1') t.classList.add('collapsed');
  const idx = runtime.assistantDraft?.currentSegmentIndex;
  if (Number.isInteger(idx) && runtime.assistantDraft?.segments?.[idx]) {
    runtime.assistantDraft.segments[idx].collapsed = t.classList.contains('collapsed');
  }
  runtime.currentTurnEl = null;
}

function finalizeAssistantReply(sess) {
  const endedAt = getNowMs();
  finalizeStreamingTurn(sess);
  // Remove any residual blinking cursors (e.g. after RPC timeout)
  messagesEl.querySelectorAll('.cursor').forEach(el => el.remove());
  const runtime = getSessionRuntime(sess);
  const draft = runtime.assistantDraft;
  const wrap = getCurrentAssistantWrap(sess);
  if (draft && sess && !draft.finalized) {
    draft.finalized = true;
    draft.taskEndedAt = endedAt;
    // Strip trailing [Info] Final response to user. marker (wrapped in 5 backticks)
    if (draft.text) {
      draft.text = draft.text.replace(/\n*`{5}\n*\[Info\] Final response to user\.\n*`{5}\s*$/, '');
    }
    if (draft.segments?.length) {
      const last = draft.segments[draft.segments.length - 1];
      if (last && last.text) {
        last.text = last.text.replace(/\n*`{5}\n*\[Info\] Final response to user\.\n*`{5}\s*$/, '');
      }
    }
    const msg = { role: 'assistant', finalized: true, taskEndedAt: endedAt };
    if (draft.bridgeMessageId) msg.id = Number(draft.bridgeMessageId);
    if (draft.taskStartedAt) msg.taskStartedAt = Number(draft.taskStartedAt);
    if (draft.text) msg.content = draft.text;
    if (draft.segments?.length) msg.segments = draft.segments.map((seg) => ({
      kind: seg.kind,
      text: seg.text || '',
      collapsed: !!seg.collapsed
    }));
    if (msg.content || msg.segments?.length) sess.messages.push(msg);
    runtime.assistantDraft = null;
  }
  if (wrap) {
    wrap.dataset.finalized = '1';
    wrap.dataset.taskEndedAt = String(endedAt);
    ensureAssistantTaskElapsed(wrap, wrap.dataset.taskStartedAt || runtime.taskStartedAt || draft?.taskStartedAt, endedAt);
    renderMessages();
  } else if (!isActiveSession(sess)) {
    // Session finished in background — its DOM cache is stale, discard it
    // so that switching to it will do a full re-render from sess.messages
    _domCache.delete(sess.id);
  }
  stopTaskTimer(sess);
}

// ─── Sending prompts ─────────────────────────────────────────────────────
function normalizeBridgeMessage(msg) {
  return {
    id: Number(msg.id || 0),
    role: msg.role || 'system',
    content: msg.content || '',
    image_ids: msg.image_ids || []
  };
}

function upsertPolledMessage(sess, raw, { partial = false } = {}) {
  if (!sess || !raw) return;
  const msg = normalizeBridgeMessage(raw);
  if (!msg.id) return;
  const runtime = getSessionRuntime(sess);
  if (!runtime.seenBridgeMessageIds) runtime.seenBridgeMessageIds = new Set();

  if (partial && msg.role === 'assistant') {
    const draft = getAssistantDraft(sess);
    const changed = draft.bridgeMessageId !== msg.id || draft.text !== (msg.content || '');
    draft.bridgeMessageId = msg.id;
    draft.text = msg.content || '';
    draft.currentSegmentIndex = -1;
    draft.finalized = false;
    // Polling partial updates used to call renderMessages(), which rebuilt the
    // whole message list every 500ms. That destroys user fold/collapse DOM
    // state and can make the live answer appear to jump/duplicate. Update only
    // the live assistant draft in-place; final messages are still reconciled by
    // id in the non-partial branch below.
    if (changed && isActiveSession(sess)) renderAssistantDraftInPlace(sess, draft);
    return;
  }

  if (runtime.seenBridgeMessageIds.has(msg.id)) return;
  runtime.seenBridgeMessageIds.add(msg.id);
  runtime.lastPolledMessageId = Math.max(Number(runtime.lastPolledMessageId || 0), msg.id);

  const draft = runtime.assistantDraft;
  if (msg.role === 'assistant' && draft && !draft.finalized && Number(draft.bridgeMessageId || 0) === msg.id) {
    draft.text = msg.content || draft.text || '';
    finalizeAssistantReply(sess);
    return;
  }
  sess.messages.push(msg);
  if (isActiveSession(sess)) renderMessage(msg);
}

async function pollSessionMessages(sess) {
  if (!sess) return;
  const runtime = getSessionRuntime(sess);
  if (runtime.polling) return;
  runtime.polling = true;
  try {
    while (runtime.busy || runtime.forcePollOnce) {
      runtime.forcePollOnce = false;
      const res = await window.zeroAgent.pollSession(sess.bridgeSessionId || sess.id, runtime.lastPolledMessageId || 0);
      if (res?.error) throw new Error(res.error.message || res.error);
      const result = res.result || res;
      for (const msg of (result.messages || [])) upsertPolledMessage(sess, msg, { partial: false });
      if (result.partial) upsertPolledMessage(sess, result.partial, { partial: true });
      const busy = result.status === 'running' || !!result.partial;
      setBusy(busy, busy ? 'Thinking…' : null, sess);
      if (!busy) {
        finalizeAssistantReply(sess);
        break;
      }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  } catch (e) {
    addDiagnostic('error', 'Polling failed', e);
    showError('Polling failed: ' + (e.message || e));
    setBusy(false, null, sess);
  } finally {
    runtime.polling = false;
  }
}

async function sendPrompt(text, images = [], options = {}) {
  if (!state.bridgeReady) {
    showError('Bridge is not ready.');
    return;
  }
  if (!state.activeId) {
    await newSession();
    if (!state.activeId) return;
  }
  const sess = state.sessions.get(state.activeId);
  const runtime = getSessionRuntime(sess);
  if (runtime.busy) return;

  // Store images in sessionStorage and collect ids
  const imageIds = images.map(img => {
    try { sessionStorage.setItem('img:' + img.id, img.dataUrl); } catch(e) { /* quota */ }
    return img.id;
  });

  const displayText = options.displayText || text;
  const localUserMsg = { role: 'user', content: displayText, image_ids: imageIds };
  sess.messages.push(localUserMsg);
  renderMessage(localUserMsg);
  startTaskTimer(sess);
  if (sess.untitled || isUntitledSessionTitle(sess.title)) {
    sess.title = displayText.trim().slice(0, 40) + (displayText.trim().length > 40 ? '…' : '');
    sess.untitled = false;
    sessionTitleEl.textContent = sess.title;
    renderSessionList();
  }

  setBusy(true, 'Thinking…', sess);
  try {
    const res = await window.zeroAgent.rpc('session/prompt', {
      sessionId: await ensureBridgeSession(sess),
      prompt: text,
      images: images.map(img => ({id: img.id, dataUrl: img.dataUrl})),
      llmNo: sess.config.llmNo
    });
    if (res?.error) throw new Error(res.error.message || res.error);
    const acceptedUserId = Number(res.userMessageId || res.result?.userMessageId || 0);
    if (acceptedUserId) {
      if (!runtime.seenBridgeMessageIds) runtime.seenBridgeMessageIds = new Set();
      runtime.seenBridgeMessageIds.add(acceptedUserId);
      runtime.lastPolledMessageId = Math.max(Number(runtime.lastPolledMessageId || 0), acceptedUserId);
    }
    runtime.forcePollOnce = true;
    pollSessionMessages(sess);
  } catch (e) {
    sess.messages.push({ role: 'error', content: e.message || String(e) });
    if (isActiveSession(sess)) renderMessage({ role: 'error', content: e.message || String(e) });
    setBusy(false, null, sess);
  }
}

async function cancelPrompt() {
  const sess = state.sessions.get(state.activeId);
  const runtime = sess ? getSessionRuntime(sess) : null;
  if (!runtime?.busy) return false;
  try {
    const res = await window.zeroAgent.rpc('session/cancel', { sessionId: sess?.bridgeSessionId || state.activeId });
    if (res.error) throw new Error(res.error.message || res.error);
    return true;
  } catch (e) {
    showSystem('Stop failed: ' + (e.message || e));
    return false;
  }
}

// ─── Slash commands ──────────────────────────────────────────────────────
const LOCAL_SLASH_COMMANDS = [
  { cmd: '/help', argHint: '', description: '显示可用命令' },
  { cmd: '/new', argHint: '', description: '新建会话' },
  { cmd: '/clear', argHint: '', description: '清空当前会话显示' },
  { cmd: '/stop', argHint: '', description: '取消当前请求' },
  { cmd: '/restart', argHint: '', description: '重启 bridge' },
  { cmd: '/settings', argHint: '', description: '打开设置' },
  { cmd: '/model', argHint: '', description: '选择模型' },
  { cmd: '/theme', argHint: 'light|dark|auto', description: '切换主题' },
  { cmd: '/cwd', argHint: '[path]', description: '显示当前目录或在指定目录新建会话' },
];

let commandPaletteIndex = 0;
let commandPaletteItems = [];

async function getSlashCommandRows() {
  try {
    const result = await window.zeroAgent.getSlashCommands();
    const commands = Array.isArray(result?.commands) ? result.commands : [];
    state.slashCommands = commands;
    return commands;
  } catch (err) {
    addDiagnostic('warn', 'Failed to load slash commands', err);
    return state.slashCommands || [];
  }
}

function mergeSlashCommands(bridgeCommands = state.slashCommands || []) {
  const seen = new Set();
  return [...LOCAL_SLASH_COMMANDS, ...bridgeCommands].filter((entry) => {
    const cmd = entry.cmd || '';
    if (!cmd || seen.has(cmd)) return false;
    seen.add(cmd);
    return true;
  });
}

async function getAllSlashCommands() {
  const bridgeCommands = await getSlashCommandRows();
  return mergeSlashCommands(bridgeCommands);
}

async function showHelp() {
  const rows = await getAllSlashCommands();
  showSystem([
    'Available commands:',
    ...rows.map((entry) => {
      const hint = entry.argHint ? ` ${entry.argHint}` : '';
      const left = `${entry.cmd}${hint}`.padEnd(18, ' ');
      return `  ${left} ${entry.description || ''}`.trimEnd();
    }),
  ].join('\n'));
}

function closeCommandPalette() {
  commandPaletteItems = [];
  commandPaletteIndex = 0;
  if (commandPaletteEl) {
    commandPaletteEl.classList.add('hidden');
    commandPaletteEl.textContent = '';
  }
}

function renderCommandPalette(items) {
  if (!commandPaletteEl) return;
  commandPaletteEl.textContent = '';
  commandPaletteItems = items;
  commandPaletteIndex = Math.max(0, Math.min(commandPaletteIndex, items.length - 1));
  if (!items.length) {
    closeCommandPalette();
    return;
  }
  for (const [idx, entry] of items.entries()) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'command-item' + (idx === commandPaletteIndex ? ' active' : '');
    btn.setAttribute('role', 'option');
    btn.setAttribute('aria-selected', idx === commandPaletteIndex ? 'true' : 'false');
    const hint = entry.argHint ? ` ${entry.argHint}` : '';
    btn.innerHTML = `<span class="command-name"></span><span class="command-desc"></span>`;
    btn.querySelector('.command-name').textContent = `${entry.cmd}${hint}`;
    btn.querySelector('.command-desc').textContent = entry.description || '';
    btn.addEventListener('mousedown', (e) => {
      e.preventDefault();
      applyCommandSuggestion(entry);
    });
    commandPaletteEl.appendChild(btn);
  }
  commandPaletteEl.classList.remove('hidden');
}

async function updateCommandPalette() {
  const value = inputEl.value;
  const cursor = inputEl.selectionStart ?? value.length;
  const beforeCursor = value.slice(0, cursor);
  const match = beforeCursor.match(/^\/[^\s]*$/);
  if (!match) {
    closeCommandPalette();
    return;
  }
  const query = match[0].toLowerCase();
  const commands = await getAllSlashCommands();
  const items = commands
    .filter((entry) => String(entry.cmd || '').toLowerCase().startsWith(query));
  renderCommandPalette(items);
}

function applyCommandSuggestion(entry) {
  if (!entry?.cmd) return;
  const hint = entry.argHint ? ' ' : '';
  inputEl.value = `${entry.cmd}${hint}`;
  inputEl.focus();
  inputEl.selectionStart = inputEl.selectionEnd = inputEl.value.length;
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
  updateSendButton();
  closeCommandPalette();
}

function moveCommandPalette(delta) {
  if (!commandPaletteItems.length) return;
  commandPaletteIndex = (commandPaletteIndex + delta + commandPaletteItems.length) % commandPaletteItems.length;
  renderCommandPalette(commandPaletteItems);
}

function formatResumeSessions(sessions) {
  if (!Array.isArray(sessions) || sessions.length === 0) {
    return '没有可恢复的历史会话。';
  }
  return [
    `最近 ${sessions.length} 个可恢复会话（输入 /resume N 恢复；/continue 继续上一个会话）：`,
    '',
    ...sessions.map((sess) => {
      const preview = String(sess.preview || '（无法预览）').replace(/\s+/g, ' ').slice(0, 80);
      return `${sess.index}. ${sess.name} · ${sess.rounds} 轮 · ${preview}`;
    }),
  ].join('\n');
}

async function restoreResumeIndex(index, commandName) {
  if (!Number.isInteger(index) || index <= 0) {
    showSystem(commandName === 'continue' ? 'Usage: /continue' : 'Usage: /resume N');
    return;
  }
  const sess = state.sessions.get(state.activeId);
  if (!sess) {
    showSystem('No active session.');
    return;
  }
  const result = await window.zeroAgent.resumeSession(await ensureBridgeSession(sess), index);
  if (!result?.ok) {
    showSystem(result?.error || 'Resume failed.');
    return;
  }
  sess.messages = Array.isArray(result.messages) ? result.messages : [];
  if (result.session?.title) sess.title = result.session.title;
  const runtime = getSessionRuntime(sess);
  runtime.seenBridgeMessageIds = new Set();
  runtime.lastPolledMessageId = 0;
  for (const msg of sess.messages) {
    if (msg.id) {
      runtime.seenBridgeMessageIds.add(Number(msg.id));
      runtime.lastPolledMessageId = Math.max(runtime.lastPolledMessageId, Number(msg.id));
    }
  }
  renderSessionList();
  renderMessages();
  showSystem(result.message || `Restored session #${index}.`);
}

async function handleResumeCommand(arg) {
  const indexText = (arg || '').trim();
  if (!indexText) {
    const result = await window.zeroAgent.listResumeSessions(10);
    showSystem(formatResumeSessions(result?.sessions || []));
    return;
  }
  const index = Number.parseInt(indexText, 10);
  await restoreResumeIndex(index, 'resume');
}

async function handleContinueCommand(arg) {
  const indexText = (arg || '').trim();
  const index = indexText ? Number.parseInt(indexText, 10) : 1;
  await restoreResumeIndex(index, 'continue');
}

function formatSchedulerStatus(data) {
  if (!data?.ok) return data?.error || 'Scheduler status unavailable.';
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const lines = [
    `Scheduler: ${data.running ? `running${data.pid ? ` (pid ${data.pid})` : ''}` : 'stopped'}`,
    '',
  ];
  if (!tasks.length) {
    lines.push('No scheduled tasks found in sche_tasks/*.json.');
  } else {
    lines.push('Scheduled tasks:');
    for (const task of tasks) {
      const state = task.enabled ? 'enabled' : 'disabled';
      const schedule = task.schedule || 'no schedule';
      lines.push(`- ${task.name}: ${state}, ${schedule}`);
    }
  }
  lines.push('', 'Commands: /scheduler start');
  return lines.join('\n');
}

async function handleSchedulerCommand(arg) {
  const action = (arg || '').trim().toLowerCase();
  if (!action) {
    const status = await window.zeroAgent.rpc('scheduler/status', {});
    showSystem(formatSchedulerStatus(status));
    return;
  }
  if (action === 'start') {
    const result = await window.zeroAgent.rpc('scheduler/start', {});
    showSystem(result?.message || result?.error || 'Scheduler start requested.');
    const status = await window.zeroAgent.rpc('scheduler/status', {});
    showSystem(formatSchedulerStatus(status));
    return;
  }
  showSystem('Usage: /scheduler or /scheduler start');
}

async function handleSlash(cmd) {
  const [name, ...rest] = cmd.trim().slice(1).split(/\s+/);
  const arg = rest.join(' ');
  const sess = state.sessions.get(state.activeId);

  switch (name) {
    case 'help':
      await showHelp();
      break;
    case 'new':
      await newSession();
      break;
    case 'clear':
      if (sess) { sess.messages = []; renderMessages(); }
      break;
    case 'stop':
      if (await cancelPrompt()) showSystem('Stop requested.');
      break;
    case 'restart':
      await restartBridge();
      break;
    case 'settings':
      showSystem('/settings has been removed. Use /llm to switch models or edit config.yaml directly.');
      break;
    case 'model':
    case 'llm':
      openModelPicker();
      break;
    case 'theme':
      if (['light', 'dark', 'auto'].includes(arg)) {
        const cfg = getActiveConfig();
        cfg.theme = arg;
        applyTheme();
        await window.zeroAgent.saveConfig(cfg);
        showSystem(`Theme → ${arg}`);
      } else {
        showSystem('Usage: /theme light|dark|auto');
      }
      break;
    case 'cwd':
      if (!arg) {
        const status = await window.zeroAgent.checkStatus();
        showSystem(`cwd: ${sess?.cwd || status.workspaceDir || ''}`);
      } else {
        showSystem(`Creating new session in ${arg}…`);
        // Need a new session for different cwd
        const res = await window.zeroAgent.rpc('session/new', { cwd: arg, mcp_servers: [] });
        if (res.error) showSystem('Failed: ' + (res.error.message || res.error));
        else {
          const bridgeSessionId = res.sessionId;
          const localSessionId = `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
          const ns = createLocalSession(localSessionId, arg.split('/').pop() || arg, bridgeSessionId);
          ns.cwd = arg;
          setActiveSession(localSessionId);
        }
      }
      break;
    case 'resume':
      await handleResumeCommand(arg);
      break;
    case 'continue':
      await handleContinueCommand(arg);
      break;
    case 'scheduler':
      await handleSchedulerCommand(arg);
      break;
    default:
      await runAgentSlash(`/${name}`, arg, cmd.trim());
  }
}

async function runAgentSlash(command, args = '', displayText = null) {
  if (getActiveSessionRuntime()?.busy) {
    showSystem('Agent is still responding. Press Esc or Stop before starting a mode.');
    return;
  }
  try {
    const result = await window.zeroAgent.resolveSlash(command, args);
    if (!result?.ok || !result.prompt) {
      showSystem(result?.error || `Unknown command: ${command}. Try /help.`);
      return;
    }
    await sendPrompt(result.prompt, [], { displayText: displayText || `${command}${args ? ' ' + args : ''}` });
  } catch (err) {
    showSystem(`Command failed: ${err.message || err}`);
  }
}

function showSystem(text) {
  const msg = { role: 'system', content: text };
  const sess = state.sessions.get(state.activeId);
  if (sess) sess.messages.push(msg);
  renderMessage(msg);
  return msg;
}

function updateBridgeNotice(text) {
  const notice = state.bridgeNoticeMessage;
  state.bridgeNoticeMessage = null;
  if (!notice) return;
  notice.content = text;
  const sess = state.sessions.get(state.activeId);
  if (sess && sess.messages.includes(notice)) renderMessages();
}

// ─── Status / UI helpers ─────────────────────────────────────────────────
function setStatus(kind, text) {
  statusBadge.className = 'badge ' + kind;
  statusText.textContent = text;
  // Update per-tab dot for active session
  updateTabDot(state.activeId, kind);
}

function updateTabDot(sessionId, kind) {
  if (!sessionId) return;
  const tab = sessionListEl.querySelector(`[data-session-id="${sessionId}"]`);
  if (!tab) return;
  const dot = tab.querySelector('.tab-dot');
  if (!dot) return;
  dot.className = 'tab-dot';
  if (kind === 'busy') dot.classList.add('busy');
  else if (kind === 'warn') dot.classList.add('warn');
  else if (kind === 'err') dot.classList.add('err');
  // 'ok' = default green (no extra class needed)
}

const SEND_ICON = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M13 6l6 6-6 6"/></svg>`;
const STOP_ICON = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>`;

function setBusy(busy, label, sess = state.sessions.get(state.activeId)) {
  const runtime = sess ? getSessionRuntime(sess) : null;
  if (runtime) runtime.busy = busy;
  // Always update per-tab dot for this session
  if (sess) {
    const dotKind = busy ? 'busy' : (state.bridgeReady ? 'ok' : 'warn');
    updateTabDot(sess.id, dotKind);
  }
  if (!isActiveSession(sess)) return;
  if (busy) setStatus('busy', label || 'Working…');
  else setStatus(state.bridgeReady ? 'ok' : 'warn', state.bridgeReady ? 'Ready' : 'Starting…');
  renderSendButtonState();
}

function renderSendButtonState() {
  const hasText = inputEl.value.trim().length > 0;
  const busy = !!getActiveSessionRuntime()?.busy;
  sendBtn.classList.toggle('stop', busy);
  sendBtn.title = busy ? 'Stop (Esc)' : 'Send (Enter)';
  sendBtn.innerHTML = busy ? STOP_ICON : SEND_ICON;
  sendBtn.disabled = !hasText && !busy;
}

function updateSendButton() {
  renderSendButtonState();
}

function showError(text, actionLabel, actionFn, options = {}) {
  if (!options.skipDiagnostic) addDiagnostic('error', text);
  $('error-text').textContent = text;
  const actionBtn = $('error-action');
  if (actionLabel && actionFn) {
    actionBtn.textContent = actionLabel;
    actionBtn.classList.remove('hidden');
    actionBtn.onclick = async () => {
      try {
        await actionFn();
      } catch (err) {
        showError('Action failed: ' + (err.message || err));
      }
    };
  } else {
    actionBtn.classList.add('hidden');
  }
  errorBanner.classList.remove('hidden');
  clearTimeout(showError._t);
  if (!actionLabel) {
    showError._t = setTimeout(() => errorBanner.classList.add('hidden'), 6000);
  }
}
function hideError() { errorBanner.classList.add('hidden'); }

// ─── Theme ───────────────────────────────────────────────────────────────
function applyTheme() {
  const cfg = getActiveConfig();
  const theme = cfg.theme || 'auto';
  document.documentElement.setAttribute('data-theme', theme);

  // Update theme toggle icon
  const btn = $('theme-toggle-btn');
  if (btn) {
    const svg = btn.querySelector('svg');
    if (svg) {
      if (theme === 'dark') {
        // Moon icon
        svg.innerHTML = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';
      } else if (theme === 'light') {
        // Sun icon
        svg.innerHTML = '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32l1.41 1.41M2 12h2m16 0h2M4.93 19.07l1.41-1.41m11.32-11.32l1.41-1.41"/>';
      } else {
        // Auto icon (sun/moon hybrid)
        svg.innerHTML = '<circle cx="12" cy="12" r="4"/><path d="M12 1v6m0 6v10M1 12h10m6 0h6"/>';
      }
    }
  }
}

// ─── Settings panel ──────────────────────────────────────────────────────
function renderModelOptions() {
  const select = $('cfg-llm');
  if (!select) return; // Settings panel doesn't exist, skip
  const selected = String(getActiveConfig().llmNo || 0);
  const profiles = Array.isArray(state.modelProfiles) ? state.modelProfiles : [];
  const options = profiles.length ? profiles : [{ llmNo: 0, name: 'Default / Auto' }];
  select.textContent = '';
  for (const profile of options) {
    const opt = document.createElement('option');
    opt.value = String(profile.llmNo);
    // Display as "name/model" when both fields available
    const displayName = profile.name && profile.model
      ? `${profile.name}/${profile.model}`
      : profile.name || profile.model || `Model ${profile.llmNo}`;
    opt.textContent = displayName;
    select.appendChild(opt);
  }
  if (![...select.options].some((opt) => opt.value === selected)) {
    const opt = document.createElement('option');
    opt.value = selected;
    opt.textContent = selected === '0' ? 'Default / Auto' : `Model ${selected}`;
    select.appendChild(opt);
  }
  select.value = selected;
}

async function loadModelProfiles() {
  try {
    const result = await window.zeroAgent.getModelProfiles();
    state.modelProfiles = Array.isArray(result && result.profiles) ? result.profiles : [];
    renderModelOptions();
  } catch (err) {
    addDiagnostic('warn', 'Failed to load model names', err);
    renderModelOptions();
  }
}

function isSettingsOpen() {
  return !!settingsPanel && !settingsPanel.classList.contains('hidden');
}

function setSettingsOpen(open) {
  if (!settingsPanel) return;
  settingsPanel.classList.toggle('hidden', !open);
  if (settingsBtn) settingsBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function openSettings() {
  renderModelOptions();
  const cfg = getActiveConfig();
  const cfgLlm = $('cfg-llm');
  if (cfgLlm) cfgLlm.value = String(cfg.llmNo || 0);
  setSettingsOpen(true);
  loadModelProfiles();
}
function closeSettings() { setSettingsOpen(false); }
function toggleSettings() {
  if (isSettingsOpen()) closeSettings();
  else openSettings();
}

async function openConfigFile(openFn, label) {
  try {
    const result = await openFn();
    if (result && result.ok === false) {
      showError(`Failed to open ${label}: ${result.error || result.path || 'unknown error'}`);
    }
  } catch (err) {
    showError(`Failed to open ${label}: ${err.message || err}`);
  }
}

async function saveSettings() {
  const saveBtn = $('save-settings');
  if (saveBtn) saveBtn.disabled = true;
  try {
    const sess = state.sessions.get(state.activeId);
    if (!sess) throw new Error('No active session');
    const cfg = sess.config;
    const cfgLlm = $('cfg-llm');
    if (cfgLlm) {
      cfg.llmNo = Math.max(0, parseInt(cfgLlm.value, 10) || 0);
    }
    await window.zeroAgent.saveConfig(cfg);
    closeSettings();
  } catch (err) {
    showError('Failed to save settings: ' + (err.message || err));
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function ensureBridgeSession(sess) {
  if (!sess) throw new Error('No active session.');
  if (sess.bridgeSessionId) return sess.bridgeSessionId;
  const cwd = sess.cwd || await getCwd();
  const res = await window.zeroAgent.rpc('session/new', { cwd, mcp_servers: [] });
  if (res.error) throw new Error(typeof res.error === 'string' ? res.error : (res.error.message || JSON.stringify(res.error)));
  sess.bridgeSessionId = res.sessionId;
  sess.cwd = cwd;
  return sess.bridgeSessionId;
}

async function restartBridge(options = {}) {
  const { remapSessions = false } = options;
  setStatus('warn', 'Restarting…');
  state.bridgeReady = false;
  state.restartingBridge = true;
  if (remapSessions) {
    for (const sess of state.sessions.values()) sess.bridgeSessionId = null;
  }
  state.bridgeNoticeMessage = showSystem('Bridge restarting…');
  await window.zeroAgent.startBridge(getActiveConfig().llmNo || 0);
  window.setTimeout(() => {
    if (state.restartingBridge && !state.bridgeReady && !getActiveSessionRuntime()?.busy) {
      markBridgeReady('Bridge ready.');
      addDiagnostic('warn', 'Bridge ready event timeout; restored Ready status locally');
    }
  }, 2500);
}

// ─── Bridge events ───────────────────────────────────────────────────────
let _bootstrappingSession = false;
async function markBridgeReady(noticeText = 'Bridge ready.') {
  if (state.bridgeReady) return; // already marked ready, prevent double-fire
  state.bridgeReady = true;
  state.restartingBridge = false;
  if (getActiveSessionRuntime()?.busy) setStatus('busy', 'Agent is responding…');
  else setStatus('ok', 'Ready');
  updateBridgeNotice(noticeText);
  hideError();
  // Restore sessions from bridge (survives page refresh) or create first session
  if (state.sessions.size === 0 && !_bootstrappingSession) {
    _bootstrappingSession = true;
    try {
      // Try to restore existing sessions from bridge
      const bridgeUrl = window.zeroAgent.bridgeUrl || 'http://127.0.0.1:14168';
      const listRes = await fetch(`${bridgeUrl}/sessions`).then(r => r.json()).catch(() => null);
      const existingSessions = listRes?.sessions || [];
      if (existingSessions.length > 0) {
        // Restore each session from bridge
        for (const bSess of existingSessions) {
          const localId = `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
          const sess = createLocalSession(localId, bSess.title || 'Restored', bSess.id || bSess.sessionId);
          // Fetch full messages for this session
          const sid = bSess.id || bSess.sessionId;
          const msgRes = await fetch(`${bridgeUrl}/session/${sid}/messages?after=0&limit=9999`).then(r => r.json()).catch(() => null);
          if (msgRes?.messages) {
            sess.messages = msgRes.messages;
            // Initialize polling state so we don't re-fetch these messages
            const runtime = getSessionRuntime(sess);
            runtime.seenBridgeMessageIds = new Set();
            let maxId = 0;
            for (const m of msgRes.messages) {
              if (m.id) { runtime.seenBridgeMessageIds.add(Number(m.id)); maxId = Math.max(maxId, Number(m.id)); }
            }
            runtime.lastPolledMessageId = maxId;
          }
        }
        // Activate the first session
        const firstLocalId = [...state.sessions.keys()][0];
        if (firstLocalId) setActiveSession(firstLocalId);
      } else {
        await newSession();
      }
    } finally { _bootstrappingSession = false; }
  }
  updateSendButton();
  // Refresh model profiles from bridge (authoritative source)
  loadModelProfiles();
}

window.zeroAgent.onBridgeReady(() => {
  markBridgeReady();
});

window.zeroAgent.onBridgeMessage(() => {
  // RPC responses are resolved in main; renderer readiness comes from bridge-ready.
});

window.zeroAgent.onBridgeNotification((msg) => {
  handleNotification(msg);
});

window.zeroAgent.onBridgeError((err) => {
  console.error('Bridge error:', err);
  addDiagnostic('error', 'Bridge error', err);
  setStatus('err', 'Error');
  state.bridgeReady = false;
  state.restartingBridge = false;

  if (err.type === 'no-config') {
    showError(err.message, 'Setup', async () => {
      await window.zeroAgent.openConfig();
    }, { skipDiagnostic: true });
  } else if (err.type === 'no-python') {
    showError(err.message, 'Settings', openSettings, { skipDiagnostic: true });
  } else {
    showError(err.message || 'Bridge error', null, null, { skipDiagnostic: true });
  }
});

window.zeroAgent.onBridgeClosed((info) => {
  addDiagnostic('warn', 'Bridge closed', info);
  if (state.restartingBridge) {
    setStatus('warn', 'Restarting…');
    return;
  }
  state.bridgeReady = false;
  setStatus('err', `Bridge stopped (${info.code})`);
});

window.zeroAgent.onBridgeLog((text) => {
  console.log('[bridge]', text);
  addDiagnostic('info', 'Bridge log', text);
});

// ─── Input handling moved to init() ──────────────────────────────────────

function renderImagePreviews() {
  imagePreviews.innerHTML = '';
  for (const img of pendingImages) {
    const wrapper = document.createElement('div');
    wrapper.className = 'image-preview-item';
    wrapper.dataset.imgId = img.id;

    const imgEl = document.createElement('img');
    imgEl.src = img.dataUrl;
    imgEl.alt = 'Pasted image';

    const closeBtn = document.createElement('button');
    closeBtn.className = 'remove-img';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', 'Remove image');
    closeBtn.addEventListener('click', () => {
      const idx = pendingImages.findIndex(i => i.id === img.id);
      if (idx !== -1) pendingImages.splice(idx, 1);
      renderImagePreviews();
    });

    wrapper.appendChild(imgEl);
    wrapper.appendChild(closeBtn);
    imagePreviews.appendChild(wrapper);
  }
  imagePreviews.style.display = pendingImages.length ? 'flex' : 'none';
}

function clearPendingImages() {
  pendingImages.length = 0;
  renderImagePreviews();
}

function submitInput() {
  const text = inputEl.value.trim();
  if (!text && pendingImages.length === 0) return;
  if (getActiveSessionRuntime()?.busy) {
    showSystem('Agent is still responding. Press Esc or Stop before sending another message.');
    return;
  }
  const images = [...pendingImages];
  inputEl.value = '';
  inputEl.style.height = 'auto';
  clearPendingImages();
  updateSendButton();
  closeCommandPalette();

  if (text.startsWith('/')) {
    handleSlash(text).catch((err) => {
      showSystem('Command failed: ' + (err.message || err));
    });
  } else {
    sendPrompt(text, images);
  }
}

// sendBtn event listener moved to init()

// ─── Buttons ─────────────────────────────────────────────────────────────
// Button event listeners moved to init() to ensure DOM is loaded

// ─── Message Search (Cmd/Ctrl+F) ─────────────────────────────────────────
(function initSearch() {
  const searchBar = document.getElementById('search-bar');
  const searchInput = document.getElementById('search-input');
  const searchClose = document.getElementById('search-close');
  const searchPrev = document.getElementById('search-prev');
  const searchNext = document.getElementById('search-next');
  const searchCount = document.getElementById('search-count');

  let highlights = [];
  let currentIdx = -1;

  function openSearch() {
    searchBar.classList.remove('hidden');
    searchBar.classList.add('visible');
    searchInput.focus();
    searchInput.select();
  }

  function closeSearch() {
    searchBar.classList.remove('visible');
    searchBar.classList.add('hidden');
    clearHighlights();
    searchInput.value = '';
    searchCount.textContent = '';
  }

  function clearHighlights() {
    highlights.forEach(el => {
      const parent = el.parentNode;
      if (parent) {
        parent.replaceChild(document.createTextNode(el.textContent), el);
        parent.normalize();
      }
    });
    highlights = [];
    currentIdx = -1;
  }

  function doSearch(query) {
    clearHighlights();
    if (!query) { searchCount.textContent = ''; return; }

    const chatArea = document.getElementById('messages');
    const walker = document.createTreeWalker(chatArea, NodeFilter.SHOW_TEXT, null);
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);

    const lowerQ = query.toLowerCase();
    textNodes.forEach(node => {
      const text = node.textContent;
      const lower = text.toLowerCase();
      let idx = lower.indexOf(lowerQ);
      if (idx === -1) return;

      const frag = document.createDocumentFragment();
      let lastIdx = 0;
      while (idx !== -1) {
        frag.appendChild(document.createTextNode(text.slice(lastIdx, idx)));
        const mark = document.createElement('mark');
        mark.className = 'search-highlight';
        mark.textContent = text.slice(idx, idx + query.length);
        frag.appendChild(mark);
        highlights.push(mark);
        lastIdx = idx + query.length;
        idx = lower.indexOf(lowerQ, lastIdx);
      }
      frag.appendChild(document.createTextNode(text.slice(lastIdx)));
      node.parentNode.replaceChild(frag, node);
    });

    searchCount.textContent = highlights.length ? `1/${highlights.length}` : '0';
    if (highlights.length) { currentIdx = 0; scrollToHighlight(); }
  }

  function scrollToHighlight() {
    highlights.forEach((el, i) => el.classList.toggle('active', i === currentIdx));
    if (highlights[currentIdx]) {
      // Expand any collapsed ancestor turns so the match is visible
      let ancestor = highlights[currentIdx].parentElement;
      while (ancestor && ancestor !== document.body) {
        if (ancestor.classList.contains('turn') && ancestor.classList.contains('collapsed')) {
          ancestor.classList.remove('collapsed');
        }
        ancestor = ancestor.parentElement;
      }
      highlights[currentIdx].scrollIntoView({ block: 'center', behavior: 'smooth' });
      searchCount.textContent = `${currentIdx + 1}/${highlights.length}`;
    }
  }

  function nextMatch() { if (!highlights.length) return; currentIdx = (currentIdx + 1) % highlights.length; scrollToHighlight(); }
  function prevMatch() { if (!highlights.length) return; currentIdx = (currentIdx - 1 + highlights.length) % highlights.length; scrollToHighlight(); }

  // Event listeners
  searchClose.addEventListener('click', closeSearch);
  searchPrev.addEventListener('click', prevMatch);
  searchNext.addEventListener('click', nextMatch);

  let searchTimeout;
  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => doSearch(searchInput.value), 200);
  });
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.shiftKey ? prevMatch() : nextMatch(); e.preventDefault(); }
    if (e.key === 'Escape') { closeSearch(); e.preventDefault(); }
  });

  // Global shortcut: Cmd+F (Mac) / Ctrl+F (Win/Linux)
  // Note: On macOS Electron intercepts Cmd+F via menu accelerator,
  // so we also listen for IPC 'open-search' from main process.
  document.addEventListener('keydown', (e) => {
    const isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
    const mod = isMac ? e.metaKey : e.ctrlKey;
    if (mod && e.key === 'f') {
      e.preventDefault();
      openSearch();
    }
    if (e.key === 'Escape' && searchBar.classList.contains('visible')) {
      e.preventDefault();
      closeSearch();
    }
  });

  // Listen for IPC from main process (menu accelerator on macOS)
  if (window.zeroAgent && window.zeroAgent.onOpenSearch) {
    window.zeroAgent.onOpenSearch(() => openSearch());
  }
})();

// ─── Drawer Controls ──────────────────────────────────────────────────────
function toggleLeftDrawer() {
  state.leftDrawerCollapsed = !state.leftDrawerCollapsed;
  localStorage.setItem('leftDrawerCollapsed', state.leftDrawerCollapsed);
  if (state.leftDrawerCollapsed) {
    leftDrawer.classList.add('collapsed');
  } else {
    leftDrawer.classList.remove('collapsed');
  }
}

function toggleRightDrawer() {
  state.rightDrawerCollapsed = !state.rightDrawerCollapsed;
  localStorage.setItem('rightDrawerCollapsed', state.rightDrawerCollapsed);
  if (state.rightDrawerCollapsed) {
    rightDrawer.classList.add('collapsed');
  } else {
    rightDrawer.classList.remove('collapsed');
  }
}

// ─── Model Picker ─────────────────────────────────────────────────────────
function openModelPicker() {
  if (!state.modelProfiles || state.modelProfiles.length === 0) {
    showError('No models available');
    return;
  }

  modelList.innerHTML = '';
  const sess = state.sessions.get(state.activeId);
  const currentModel = sess?.modelOverride || state.defaultConfig.llmNo || 0;

  state.modelProfiles.forEach((profile, idx) => {
    const item = document.createElement('div');
    item.className = 'model-item';
    if (currentModel === idx || currentModel === profile.name) {
      item.classList.add('active');
    }

    item.innerHTML = `
      <div class="model-item-info">
        <div class="model-item-name">${escapeHtml(profile.name || `Model ${idx}`)}</div>
        <div class="model-item-meta">${escapeHtml(profile.provider || '')} ${escapeHtml(profile.model || '')}</div>
      </div>
      <span class="model-item-check">✓</span>
    `;

    item.addEventListener('click', async () => {
      await switchModel(idx, profile.name);
      modelPickerModal.classList.add('hidden');
    });

    modelList.appendChild(item);
  });

  modelPickerModal.classList.remove('hidden');
}

function closeModelPicker() {
  modelPickerModal.classList.add('hidden');
}

async function switchModel(modelNo, modelName) {
  const sess = state.sessions.get(state.activeId);
  if (!sess || !sess.bridgeSessionId) return;

  const bridgeUrl = window.zeroAgent.bridgeUrl || 'http://127.0.0.1:14168';
  try {
    await fetch(`${bridgeUrl}/session/${sess.bridgeSessionId}/model`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ modelNo, modelName })
    });
    sess.modelOverride = modelName || String(modelNo);
    updateModelStatus();
  } catch (err) {
    showError('Failed to switch model: ' + (err.message || err));
  }
}

// ─── Token Display ────────────────────────────────────────────────────────
function updateModelStatus() {
  const sess = state.sessions.get(state.activeId);
  if (!sess) {
    currentModelEl.textContent = 'No session';
    tokenUsageEl.textContent = '0 / 0';
    return;
  }

  const modelName = sess.modelOverride || 'Default';
  currentModelEl.textContent = modelName;

  const usage = sess.tokenUsage || { total: 0, limit: 200000 };
  const totalK = Math.round(usage.total / 100) / 10;
  const limitK = Math.round(usage.limit / 1000);
  tokenUsageEl.textContent = `${totalK}K / ${limitK}K`;

  const percent = usage.limit > 0 ? (usage.total / usage.limit) * 100 : 0;
  tokenUsageEl.className = 'token-info';
  if (percent > 95) tokenUsageEl.classList.add('error');
  else if (percent > 80) tokenUsageEl.classList.add('warn');
}

// ─── Agent Panel ──────────────────────────────────────────────────────────
function renderAgentPanel() {
  const sess = state.sessions.get(state.activeId);
  const agents = sess ? (state.activeAgents.get(sess.id) || []) : [];

  if (agents.length === 0) {
    agentList.innerHTML = '<div class="empty-state-small"><span>No active agents</span></div>';
    if (!state.rightDrawerCollapsed) {
      rightDrawer.classList.add('collapsed');
      state.rightDrawerCollapsed = true;
    }
    return;
  }

  // Auto-show right drawer when agents exist
  if (state.rightDrawerCollapsed) {
    state.rightDrawerCollapsed = false;
    rightDrawer.classList.remove('collapsed');
  }

  agentList.innerHTML = '';
  agents.forEach(agent => {
    const card = document.createElement('div');
    card.className = 'agent-card';
    card.dataset.agentId = agent.id;

    // Make card clickable to view agent's context/output
    card.style.cursor = 'pointer';
    card.addEventListener('click', (e) => {
      if (!e.target.closest('button')) {
        showAgentContext(agent);
      }
    });

    const statusClass = agent.status || 'running';
    const elapsed = agent.created_at ? Math.round((Date.now() / 1000 - agent.created_at) / 60) : 0;

    card.innerHTML = `
      <div class="agent-card-header">
        <span class="agent-card-title">${escapeHtml(agent.name || agent.type || 'Agent')}</span>
        <span class="agent-card-status ${statusClass}">${statusClass}</span>
      </div>
      <div class="agent-card-meta">
        <div>Type: ${escapeHtml(agent.type || 'unknown')}</div>
        <div>Runtime: ${elapsed}m</div>
      </div>
      ${agent.status === 'running' ? '<div class="agent-card-actions"><button class="btn btn-sm" data-agent-id="' + agent.id + '">Cancel</button></div>' : ''}
    `;

    const cancelBtn = card.querySelector('[data-agent-id]');
    if (cancelBtn) {
      cancelBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        cancelAgent(sess.bridgeSessionId, agent.id);
      });
    }

    agentList.appendChild(card);
  });
}

function showAgentContext(agent) {
  // Show agent's output/context in a modal or inline panel
  showSystem(`Agent: ${agent.name || agent.id}\nType: ${agent.type}\nStatus: ${agent.status}\n\n点击查看Agent上下文功能已实现 - 后续可扩展显示完整输出和日志`);
}

async function cancelAgent(sessionId, agentId) {
  const bridgeUrl = window.zeroAgent.bridgeUrl || 'http://127.0.0.1:14168';
  try {
    await fetch(`${bridgeUrl}/session/${sessionId}/agents/${agentId}/cancel`, { method: 'POST' });
  } catch (err) {
    showError('Failed to cancel agent: ' + (err.message || err));
  }
}

// ─── Init ────────────────────────────────────────────────────────────────
// IME composition fix
let _imeComposing = false;
const imagePreviews = document.getElementById('image-previews');
const pendingImages = []; // Array of { dataUrl, id }

(async function init() {
  // Initialize DOM refs
  messagesEl = $('messages');
  inputEl = $('input');
  sendBtn = $('send-btn');
  sessionListEl = $('session-list');
  sessionTitleEl = $('session-title');
  statusBadge = $('status-badge');
  statusText = $('status-text');
  errorBanner = $('error-banner');
  commandPaletteEl = $('command-palette');
  leftDrawer = $('left-drawer');
  rightDrawer = $('right-drawer');
  agentList = $('agent-list');
  modelStatus = $('model-status');
  currentModelEl = $('current-model');
  tokenUsageEl = $('token-usage');
  modelPickerModal = $('model-picker-modal');
  modelList = $('model-list');

  // Add platform class to body for platform-specific CSS
  const platform = (window.zeroAgent && window.zeroAgent.platform) || process.platform || 'unknown';
  document.body.classList.add('platform-' + platform);

  try {
    const saved = await window.zeroAgent.getConfig();
    Object.assign(state.defaultConfig, saved);
  } catch (err) {
    addDiagnostic('error', 'Failed to load settings', err);
    showError('Failed to load settings; using defaults: ' + (err.message || err));
  }
  applyTheme();
  await loadModelProfiles();
  updateSendButton();
  inputEl.focus();

  // Initialize default group
  if (!state.sessionGroups.has('default')) {
    state.sessionGroups.set('default', { id: 'default', name: '未分组', sessionIds: [], collapsed: false });
  }

  // ─── Input Event Listeners ──────────────────────────────────────────────
  inputEl.addEventListener('input', () => {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
    updateSendButton();
    updateCommandPalette();
  });

  inputEl.addEventListener('compositionstart', () => { _imeComposing = true; });
  inputEl.addEventListener('compositionend', () => { _imeComposing = false; });

  inputEl.addEventListener('keydown', (e) => {
    if (!commandPaletteEl.classList.contains('hidden')) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        moveCommandPalette(1);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        moveCommandPalette(-1);
        return;
      }
      if (e.key === 'Tab') {
        e.preventDefault();
        applyCommandSuggestion(commandPaletteItems[commandPaletteIndex]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        closeCommandPalette();
        return;
      }
      if (e.key === 'Enter' && !e.shiftKey && commandPaletteItems.length) {
        e.preventDefault();
        applyCommandSuggestion(commandPaletteItems[commandPaletteIndex]);
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      if (e.isComposing || _imeComposing || e.keyCode === 229) return;
      e.preventDefault();
      submitInput();
    } else if (e.key === 'Escape' && getActiveSessionRuntime()?.busy) {
      e.preventDefault();
      cancelPrompt();
    }
  });

  inputEl.addEventListener('paste', (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const file = item.getAsFile();
        if (!file) continue;
        const reader = new FileReader();
        reader.onload = () => {
          const dataUrl = reader.result;
          const id = `img-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
          pendingImages.push({ dataUrl, id });
          renderImagePreviews();
        };
        reader.readAsDataURL(file);
        break;
      }
    }
  });

  sendBtn.addEventListener('click', () => {
    if (getActiveSessionRuntime()?.busy) {
      cancelPrompt().then((ok) => {
        if (ok) showSystem('Stop requested.');
      });
    } else submitInput();
  });

  // Initialize default group
  if (!state.sessionGroups.has('default')) {
    state.sessionGroups.set('default', { id: 'default', name: '未分组', sessionIds: [], collapsed: false });
  }

  // ─── Button Event Listeners ────────────────────────────────────────────
  $('new-session-btn')?.addEventListener('click', newSession);
  $('error-dismiss')?.addEventListener('click', hideError);

  // ─── Drawer Toggles ─────────────────────────────────────────────────────
  $('toggle-left-drawer')?.addEventListener('click', toggleLeftDrawer);
  $('toggle-left-drawer-alt')?.addEventListener('click', toggleLeftDrawer);
  $('toggle-right-drawer')?.addEventListener('click', toggleRightDrawer);
  $('toggle-right-drawer-alt')?.addEventListener('click', toggleRightDrawer);
  $('close-right-drawer')?.addEventListener('click', toggleRightDrawer);

  // ─── Model Picker ───────────────────────────────────────────────────────
  modelStatus?.addEventListener('click', openModelPicker);
  $('close-model-picker')?.addEventListener('click', closeModelPicker);
  modelPickerModal?.querySelector('.modal-backdrop')?.addEventListener('click', closeModelPicker);

  // ─── Theme Toggle ───────────────────────────────────────────────────────
  $('theme-toggle-btn')?.addEventListener('click', async () => {
    const cfg = getActiveConfig();
    const current = cfg.theme || 'auto';
    const next = current === 'light' ? 'dark' : (current === 'dark' ? 'auto' : 'light');
    cfg.theme = next;
    applyTheme();
    await window.zeroAgent.saveConfig(cfg);
    showSystem(`Theme → ${next}`);
  });

  // ─── Keyboard Shortcuts ─────────────────────────────────────────────────
  document.addEventListener('keydown', (e) => {
    // Cmd/Ctrl+B - Toggle left drawer
    if ((e.metaKey || e.ctrlKey) && e.key === 'b' && !e.shiftKey && !e.altKey) {
      e.preventDefault();
      toggleLeftDrawer();
    }
    // Cmd/Ctrl+Alt+B - Toggle right drawer
    if ((e.metaKey || e.ctrlKey) && e.altKey && e.key === 'b') {
      e.preventDefault();
      toggleRightDrawer();
    }
    // Cmd/Ctrl+K - Open model picker
    if ((e.metaKey || e.ctrlKey) && e.key === 'k' && !inputEl.matches(':focus')) {
      e.preventDefault();
      openModelPicker();
    }
  });

  // ─── Initialize UI ──────────────────────────────────────────────────────
  renderSessionList();
  updateModelStatus();
  renderAgentPanel();
})();
