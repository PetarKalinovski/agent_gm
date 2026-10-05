// Drives the chat renderer lifted VERBATIM out of index.html's inline
// script: a scripted /api/play SSE stream through the real sendMessage, and
// stored turns through the real renderHistoryMessage. The DOM is a small
// fake that parses the HTML the renderer writes, so what is asserted is the
// element tree a browser would build (order, which blocks exist, their text).
//
// Usage: node chat_render_harness.js <index.html> <scenarios.json>
// Prints {scenario: result} as JSON on stdout.
'use strict';
const fs = require('fs');

const html = fs.readFileSync(process.argv[2], 'utf8');
const scenarios = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const inline = html.match(/<script>([\s\S]*?)<\/script>/);
if (!inline) throw new Error('index.html has no inline <script>');
const app = inline[1];

/** The source of one top-level function. Its end is the first `}` at which
 *  the text compiles — braces inside regexes, strings and templates cannot
 *  fool that the way brace counting can. */
function lift(name) {
    const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(app);
    if (!m) throw new Error(`index.html no longer defines ${name}()`);
    for (let j = app.indexOf('}', m.index); j !== -1; j = app.indexOf('}', j + 1)) {
        const candidate = app.slice(m.index, j + 1);
        try { new Function(candidate); return candidate; } catch (e) { /* not the end yet */ }
    }
    throw new Error(`could not lift ${name}()`);
}
const debugDecl = app.match(/const DEBUG_UI = [\s\S]*?\}\)\(\);/);
if (!debugDecl) throw new Error('index.html no longer declares DEBUG_UI');

// ---------------------------------------------------------------- fake DOM
const VOID = new Set(['img', 'br', 'hr', 'input']);
const ENT = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&ldquo;': '“',
              '&rdquo;': '”', '&hellip;': '…' };
const decode = s => s.replace(/&(?:#\d+|[a-z]+);/g,
    e => ENT[e] || (e.startsWith('&#') ? String.fromCodePoint(+e.slice(2, -1)) : e));

class TextNode {
    constructor(text, encoded) { this.text = text; this.encoded = encoded; this.parent = null; }
    get textContent() { return this.encoded ? decode(this.text) : this.text; }
    get outerHTML() { return this.text; }
    remove() { if (this.parent) this.parent.childNodes = this.parent.childNodes.filter(n => n !== this); }
}

class El {
    constructor(tag, attrs = '') {
        this.tagName = tag.toUpperCase();
        this.attrs = attrs;
        this.className = (attrs.match(/class="([^"]*)"/) || [])[1] || '';
        this.childNodes = []; this.parent = null;
        this.dataset = {}; this.style = {};
        this.scrollTop = 0; this.scrollHeight = 0; this.value = ''; this.disabled = false;
    }
    get children() { return this.childNodes.filter(n => n instanceof El); }
    get childElementCount() { return this.children.length; }
    get classList() {
        const el = this, list = () => el.className.split(/\s+/).filter(Boolean);
        return {
            add: c => { if (!list().includes(c)) el.className = [...list(), c].join(' '); },
            remove: c => { el.className = list().filter(x => x !== c).join(' '); },
            contains: c => list().includes(c),
            toggle: c => { list().includes(c) ? el.classList.remove(c) : el.classList.add(c); },
        };
    }
    attr(name) { return (this.attrs.match(new RegExp(`${name}="([^"]*)"`)) || [])[1]; }
    appendChild(c) { if (c.parent) c.remove(); c.parent = this; this.childNodes.push(c); return c; }
    prepend(c) { if (c.parent) c.remove(); c.parent = this; this.childNodes.unshift(c); return c; }
    remove() {
        if (this.parent) { this.parent.childNodes = this.parent.childNodes.filter(n => n !== this); this.parent = null; }
    }
    set innerHTML(h) { this.childNodes = []; parseInto(this, String(h)); }
    get innerHTML() { return this.childNodes.map(n => n.outerHTML).join(''); }
    get outerHTML() {
        const tag = this.tagName.toLowerCase();
        return VOID.has(tag) ? `<${tag}${this.attrs}>` : `<${tag}${this.attrs}>${this.innerHTML}</${tag}>`;
    }
    set textContent(t) { this.childNodes = []; this.appendChild(new TextNode(String(t), false)); }
    get textContent() { return this.childNodes.map(n => n.textContent).join(''); }
    querySelector(sel) {
        const cls = sel.replace(/^\./, '');
        for (const c of this.children) {
            if (c.className.split(/\s+/).includes(cls)) return c;
            const hit = c.querySelector(sel);
            if (hit) return hit;
        }
        return null;
    }
    addEventListener() {}
    focus() {}
}

function parseInto(root, h) {
    const stack = [root];
    const re = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*)>|([^<]+)/g;
    let m;
    while ((m = re.exec(h))) {
        const top = stack[stack.length - 1];
        if (m[4] !== undefined) { top.appendChild(new TextNode(m[4], true)); continue; }
        if (m[1]) { if (stack.length > 1) stack.pop(); continue; }
        const el = new El(m[2], m[3]);
        top.appendChild(el);
        if (!VOID.has(m[2].toLowerCase())) stack.push(el);
    }
}

/** What the player sees: each message's rendered blocks in order. CSS hides
 *  an empty .message-content / .tools-container (`:empty`), so do we. */
function snapshot(chat) {
    return chat.children.map(msg => ({
        role: msg.className.replace('message', '').trim(),
        blocks: msg.children
            .filter(b => !(['message-content', 'tools-container'].includes(b.className) && !b.childNodes.length))
            .map(b => {
                const img = b.children.find(c => c.tagName === 'IMG');
                return {
                    kind: b.className,
                    text: b.textContent.replace(/\s+/g, ' ').trim(),
                    html: b.innerHTML,
                    portrait: img ? img.attr('src') : null,
                };
            }),
    }));
}

// ------------------------------------------------------------- the runtime
const NAMES = ['clearThinking', 'npcThinking', 'escapeHtml', 'esc', 'formatText', 'renderToolPill',
               'addMessage', 'cleanProse', 'buildSpeechLine', 'buildSceneHeader',
               'renderHistoryMessage', 'createStreamingMessage', 'sendMessage'];
const SOURCE = NAMES.map(lift).join('\n');

function boot(search, frames) {
    const document = { createElement: t => new El(t), getElementById: () => new El('div') };
    const window = { location: { search: search || '' }, forgeTurnBusy: false };
    const raf = fn => setTimeout(fn, 0);
    const fetch = async (url, opts) => {
        let i = 0;
        const read = async () => {
            await new Promise(r => setTimeout(r, 1));
            if (i >= frames.length) return { done: true };
            const f = frames[i++];
            if (f.__abort) { const e = new Error('The user aborted a request.'); e.name = 'AbortError'; throw e; }
            return { done: false, value: Buffer.from(`data: ${JSON.stringify(f)}\n\n`) };
        };
        return { ok: true, status: 200, body: { getReader: () => ({ read }) } };
    };
    const body = `
        ${debugDecl[0]}
        let game = null, isProcessing = false, currentPlayerId = 'p1', currentNpcId = null,
            currentNpcName = null, combatInputLock = false, currentAbort = null;
        const chatMessages = document.createElement('div');
        const userInput = document.createElement('input');
        const sendBtn = document.createElement('button');
        function applyPlayerState() {} async function handleLocationChange() {}
        async function getSessionData() { return null; } function showObjectivesDone() {}
        function showToast() {} function showLevelUp() {} function pulseXpBar() {}
        function refreshCampaignView() {}
        ${SOURCE}
        return { chatMessages, sendMessage, renderHistoryMessage, cleanProse, DEBUG_UI };`;
    return new Function('document', 'window', 'fetch', 'requestAnimationFrame', body)(
        document, window, fetch, raf);
}

(async () => {
    const out = {};
    for (const [name, sc] of Object.entries(scenarios)) {
        const rt = boot(sc.search, sc.frames || []);
        if (sc.clean) { out[name] = sc.clean.map(t => rt.cleanProse(t)); continue; }
        for (const msg of sc.history || []) rt.renderHistoryMessage(msg);
        if (sc.frames) {
            await rt.sendMessage(sc.input || 'I look around.');
            await new Promise(r => setTimeout(r, 10));   // let queued frames render
        }
        out[name] = { debug: rt.DEBUG_UI, messages: snapshot(rt.chatMessages),
                      thinking: !!rt.chatMessages.querySelector('.thinking') };
    }
    process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
