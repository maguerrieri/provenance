// Runs the review app's own <script> against a minimal DOM, so tests can drive it the way a
// reviewer does (tick a box, press a key, import a file) and read back what the page shows and
// what it stored. tests/test_review_app.py parses the rendered page and pipes it in as JSON:
//
//   {"tree": <body element tree>, "script": "<the page's script>",
//    "storage": {"<key>": "<value>"}, "actions": [...]}
//
// and gets back {"storage": ..., "rows": [...], "claims": [...], "notice": ..., "alerts": [...]}.
//
// The DOM supports only what the page uses. A selector it doesn't know throws, so a template
// change that needs more fails the test loudly rather than passing against a stub that
// silently matched nothing.
"use strict";
const vm = require("vm");

class ClassList {
  constructor(names) { this.set = new Set(names); }
  contains(n) { return this.set.has(n); }
  add(n) { this.set.add(n); }
  remove(n) { this.set.delete(n); }
  toggle(n, force) {
    const on = force === undefined ? !this.set.has(n) : !!force;
    if (on) this.set.add(n); else this.set.delete(n);
    return on;
  }
}

function matcher(sel) {
  if (/^\.[\w-]+$/.test(sel)) return el => el.classList.contains(sel.slice(1));
  throw new Error("harness: unsupported selector " + JSON.stringify(sel));
}

class El {
  constructor(node, parent, doc) {
    this.tagName = node.tag.toUpperCase();
    this.id = node.attrs.id || "";
    this.classList = new ClassList((node.attrs.class || "").split(/\s+/).filter(Boolean));
    this.dataset = {};
    for (const [k, v] of Object.entries(node.attrs)) {
      if (k.startsWith("data-")) {
        this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
      }
    }
    this.parentNode = parent;
    this.doc = doc;
    this.style = {};
    this.checked = "checked" in node.attrs;
    this.value = node.attrs.value || "";
    this.textContent = "";
    this.listeners = {};
    this.children = node.children.map(c => new El(c, this, doc));
  }
  *walk() { for (const c of this.children) { yield c; yield* c.walk(); } }
  querySelectorAll(sel) { const m = matcher(sel); return [...this.walk()].filter(m); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) {
    const m = matcher(sel);
    for (let el = this; el; el = el.parentNode) if (m(el)) return el;
    return null;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  focus() { this.doc.activeElement = this; }
  click() { this.doc.dispatch("click", this); }
  scrollIntoView() {}
}

class Doc {
  constructor(tree) {
    this.activeElement = null;
    this.listeners = {};
    this.body = new El(tree, null, this);
  }
  querySelectorAll(sel) { return this.body.querySelectorAll(sel); }
  querySelector(sel) { return this.body.querySelector(sel); }
  getElementById(id) { return [...this.body.walk()].find(e => e.id === id) || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  createElement(tag) {
    return new El({tag, attrs: {}, children: []}, null, this);
  }
  // Every listener the page registers is on document or on the target itself, so this is
  // all the bubbling it needs.
  dispatch(type, target, extra = {}) {
    const ev = {type, target, preventDefault() {}, ...extra};
    for (const fn of target.listeners[type] || []) fn(ev);
    for (const fn of this.listeners[type] || []) fn(ev);
  }
}

// A claim citing one url and snippet twice has two rows with one key; `index` picks among them.
function row(doc, key, index = 0) {
  const el = doc.querySelectorAll(".src").filter(e => e.dataset.key === key)[index];
  if (!el) throw new Error("harness: no row " + key + " #" + index);
  return el;
}

async function main() {
  const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
  const doc = new Doc(input.tree);
  const store = new Map(Object.entries(input.storage || {}));
  const alerts = [];
  const sandbox = {
    document: doc,
    localStorage: {
      getItem: k => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
    },
    navigator: {clipboard: {writeText() {}}},
    alert: msg => alerts.push(String(msg)),
    Blob: class {},
    URL: {createObjectURL: () => "blob:"},
    setTimeout, console,
  };
  vm.runInNewContext(input.script, sandbox);

  for (const a of input.actions || []) {
    if (a.do === "tick") {
      const cb = row(doc, a.row, a.index).querySelector(".cb");
      cb.checked = a.checked;
      doc.dispatch("change", cb);
    } else if (a.do === "key") {
      doc.dispatch("click", row(doc, a.row, a.index));  // select the row, as a click does
      doc.dispatch("keydown", doc.body, {key: a.key});
    } else if (a.do === "flag") {
      doc.dispatch("click", row(doc, a.row, a.index).querySelector(".flag"));
    } else if (a.do === "dismiss") {
      doc.getElementById("dismiss").click();
    } else if (a.do === "import") {
      const file = doc.getElementById("file");
      file.files = [{text: async () => a.text}];
      file.value = "progress.json";
      doc.dispatch("change", file);
    } else {
      throw new Error("harness: unknown action " + JSON.stringify(a));
    }
    await new Promise(r => setTimeout(r, 0));          // let an async import settle
  }

  const notice = doc.getElementById("migrated");
  process.stdout.write(JSON.stringify({
    storage: Object.fromEntries(store),
    rows: doc.querySelectorAll(".src").map(el => ({
      key: el.dataset.key,
      fp: el.dataset.fp,
      checked: el.querySelector(".cb").checked,
      stale: !el.querySelector(".stale").classList.contains("hidden"),
      flagged: el.classList.contains("flagged"),
      note: el.querySelector(".note").value,
    })),
    claims: doc.querySelectorAll(".claim").map(c => ({
      qid: c.dataset.qid, done: c.classList.contains("done"),
    })),
    progress: doc.getElementById("pct").textContent,
    notice: notice.classList.contains("hidden") ? ""
      : doc.getElementById("migrated-text").textContent,
    fileInput: doc.getElementById("file").value,
    alerts,
  }));
}

main().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
