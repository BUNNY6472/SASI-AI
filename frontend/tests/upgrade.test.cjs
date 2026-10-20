const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

// Compile the same production modules without a browser, credentials or network.
const modules = new Map();
function load(relative) {
  const base = path.resolve(__dirname, "..", relative);
  const target = fs.existsSync(base) ? base : [base + ".ts", base + ".tsx"].find((candidate) => fs.existsSync(candidate));
  if (!target) throw new Error(`Test module not found: ${relative}`);
  if (modules.has(target)) return modules.get(target).exports;
  const module = { exports: {} };
  modules.set(target, module);
  const output = ts.transpileModule(fs.readFileSync(target, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
    fileName: target,
  }).outputText;
  const localRequire = (name) => {
    if (name.startsWith("@/")) return load(name.slice(2));
    if (name.startsWith(".")) return load(path.relative(path.resolve(__dirname, ".."), path.resolve(path.dirname(target), name)));
    return require(name);
  };
  vm.runInThisContext(`(function(require,module,exports){${output}\n})`, { filename: target })(localRequire, module, module.exports);
  return module.exports;
}
const { uploadFile, committedBytes, validateAttachment, uploadSettings } = load("lib/uploads.ts");
const UsageDetails = load("components/UsageDetails.tsx").default;
const FileCoverage = load("components/FileCoverage.tsx").default;
const { RunClock, ActiveCalls } = load("components/RunTiming.tsx");
const { CollaborationSummary } = load("components/BridgeSession.tsx");
const BridgeSession = load("components/BridgeSession.tsx").default;
const Workspace = load("components/Workspace.tsx").default;
const { questionRequest } = load("lib/question-request.ts");
const GiB = 1024 ** 3;
const MiB = 1024 ** 2;
const limits = { max_run_bytes: 10 * GiB, max_file_bytes: 10 * GiB, max_files_per_run: 20, resumable_uploads: true, upload_storage: "cloud" };
const localLimits = { ...limits, max_run_bytes: 60 * MiB, max_file_bytes: 25 * MiB, raw_upload_max_bytes: 25 * MiB, resumable_uploads: false, upload_storage: "local" };
const file = (size) => ({ name: "data.csv", size, type: "text/csv", slice(start, end) { return { size: end - start }; } });
const session = { id: "upload1", upload_url: "https://storage.googleapis.com/bucket/private-session", chunk_size: 8 * MiB };
const ref = { id: "upload1", name: "data.csv", size: 20 * MiB, mime: "text/csv", kind: "text", preview_complete: false, preview_bytes: 2 * MiB, coverage_note: "A bounded prefix was examined; full-file coverage is not established.", storage: "cloud" };

function mocks(handler, fetchHandler) {
  const beforeFetch = global.fetch;
  const beforeXHR = global.XMLHttpRequest;
  const requests = [];
  const apiCalls = [];
  global.fetch = async (url, options) => {
    apiCalls.push({ url, options });
    const body = fetchHandler ? await fetchHandler(url, options) : url.endsWith("/complete") ? ref : options.method === "DELETE" ? {} : session;
    return { ok: true, json: async () => body };
  };
  global.XMLHttpRequest = class {
    upload = {};
    headers = {};
    status = 0;
    responseText = "";
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    getResponseHeader(name) { return name === "Range" ? this.range || null : null; }
    send(body) {
      this.body = body;
      requests.push(this);
      queueMicrotask(() => handler(this, requests.length - 1));
    }
    abort() { this.onabort?.(); }
    respond(status, range, body = "") { this.status = status; this.range = range; this.responseText = body; this.upload.onprogress?.({ loaded: this.body.size }); this.onload?.(); }
  };
  return { requests, apiCalls, restore() { global.fetch = beforeFetch; global.XMLHttpRequest = beforeXHR; } };
}

test("10 GiB aggregate boundary, single file and file count follow confirmed server limits", () => {
  assert.equal(validateAttachment(file(10 * GiB), 0, 0, limits), null);
  assert.equal(validateAttachment(file(6 * GiB), 4 * GiB, 1, limits), null);
  assert.match(validateAttachment(file(10 * GiB + 1), 0, 0, limits), /per-file limit/);
  assert.match(validateAttachment(file(1), 10 * GiB, 1, limits), /in total/);
  assert.match(validateAttachment(file(1), 0, 20, limits), /20 files/);
  assert.match(validateAttachment(file(0), 0, 0, limits), /empty/);
  assert.match(validateAttachment(file(26 * MiB), 0, 0, localLimits), /per-file limit/);
  assert.equal(validateAttachment(file(33 * MiB), 0, 0, limits), null);
});

test("unknown, invalid, and disabled cloud settings never silently choose a raw limit", async () => {
  const m = mocks(() => assert.fail("No upload can begin without confirmed settings"));
  try {
    assert.equal(uploadSettings(null), null);
    for (const unknown of [
      {}, { ...limits, max_run_bytes: undefined }, { ...limits, max_file_bytes: NaN },
      { ...limits, max_files_per_run: 0 }, { ...limits, resumable_uploads: undefined },
      { ...limits, resumable_uploads: false }, { ...localLimits, upload_storage: undefined },
      { ...localLimits, raw_upload_max_bytes: undefined },
    ]) {
      assert.equal(uploadSettings(unknown), null);
      assert.match(validateAttachment(file(33 * MiB), 0, 0, unknown), /settings are unavailable/);
      await assert.rejects(uploadFile(file(33 * MiB), unknown, new AbortController().signal, () => {}), /settings are unavailable/);
    }
    assert.equal(m.apiCalls.length, 0);
    assert.equal(m.requests.length, 0);
  } finally { m.restore(); }
});

test("only an explicitly local store can use the small direct-upload transport", async () => {
  const m = mocks((xhr) => xhr.respond(200, null, JSON.stringify(ref)));
  try {
    await uploadFile(file(20 * MiB), localLimits, new AbortController().signal, () => {});
    assert.equal(m.requests[0].url, "/api/files");
    assert.equal(m.requests[0].method, "POST");
    assert.equal(m.requests[0].withCredentials, true);
    assert.equal(m.apiCalls.length, 0);
  } finally { m.restore(); }
});

test("only valid committed ranges can advance progress", () => {
  assert.equal(committedBytes(null, 20), 0);
  assert.equal(committedBytes("bytes=0-9", 20), 10);
  for (const range of ["bytes=2-9", "bytes=0-20", "other", "bytes=0-9007199254740992"])
    assert.throws(() => committedBytes(range, 20), /invalid progress/);
});

test("large files use bounded slices, cloud requests omit cookies, and completion is explicit", async () => {
  const m = mocks((xhr, index) => xhr.respond(index === 2 ? 200 : 308, `bytes=0-${Math.min((index + 1) * 8 * MiB, 20 * MiB) - 1}`));
  try {
    const updates = [];
    const actual = await uploadFile(file(20 * MiB), limits, new AbortController().signal, (update) => updates.push(update));
    assert.equal(actual.id, ref.id);
    assert.deepEqual(m.requests.map((r) => r.body.size), [8 * MiB, 8 * MiB, 4 * MiB]);
    assert.deepEqual(m.requests.map((r) => r.headers["Content-Range"]), [`bytes 0-${8 * MiB - 1}/${20 * MiB}`, `bytes ${8 * MiB}-${16 * MiB - 1}/${20 * MiB}`, `bytes ${16 * MiB}-${20 * MiB - 1}/${20 * MiB}`]);
    assert.ok(m.requests.every((r) => r.withCredentials === false));
    assert.equal(m.apiCalls.at(-1).url, "/api/files/uploads/upload1/complete");
    assert.ok(updates.every((update) => update.progress < 100));
    assert.equal(updates.at(-1).status, "finalizing");
  } finally { m.restore(); }
});

test("a 10 GiB file is sliced without reading it into memory", async () => {
  const total = 10 * GiB;
  const m = mocks((xhr) => {
    const match = /bytes (\d+)-(\d+)\/(\d+)/.exec(xhr.headers["Content-Range"]);
    assert.ok(match);
    const end = Number(match[2]);
    xhr.respond(end + 1 === total ? 200 : 308, `bytes=0-${end}`);
  });
  try {
    await uploadFile(file(total), limits, new AbortController().signal, () => {});
    assert.equal(m.requests.length, 1280);
    assert.ok(m.requests.every((request) => request.body.size === 8 * MiB));
  } finally { m.restore(); }
});

test("uncertain writes query cloud progress before continuing, avoiding duplicate chunks", async () => {
  const m = mocks((xhr, index) => {
    if (index === 0) return xhr.onerror();
    if (index === 1) return xhr.respond(308, `bytes=0-${8 * MiB - 1}`);
    xhr.respond(200);
  });
  try {
    await uploadFile(file(10 * MiB), limits, new AbortController().signal, () => {});
    assert.equal(m.requests[1].headers["Content-Range"], `bytes */${10 * MiB}`);
    assert.equal(m.requests[1].body.size, 0);
    assert.equal(m.requests[2].headers["Content-Range"], `bytes ${8 * MiB}-${10 * MiB - 1}/${10 * MiB}`);
  } finally { m.restore(); }
});

test("cancelling during initiation still releases its newly created reservation", async () => {
  const controller = new AbortController();
  const m = mocks(() => assert.fail("A cancelled upload must not transfer data"), (url, options) => {
    if (options.method === "POST") { controller.abort(); return session; }
    return {};
  });
  try {
    await assert.rejects(uploadFile(file(10), limits, controller.signal, () => {}), { name: "AbortError" });
    assert.equal(m.apiCalls.at(-1).url, "/api/files/upload1");
    assert.equal(m.apiCalls.at(-1).options.method, "DELETE");
  } finally { m.restore(); }
});

test("cancelling a live transfer aborts the request and removes the staged file", async () => {
  const controller = new AbortController();
  const m = mocks(() => controller.abort());
  try {
    await assert.rejects(uploadFile(file(10), limits, controller.signal, () => {}), { name: "AbortError" });
    assert.equal(m.apiCalls.at(-1).options.method, "DELETE");
    assert.ok(!m.apiCalls.some((call) => call.url.endsWith("/complete")));
  } finally { m.restore(); }
});

test("unsupported destinations never receive file contents", async () => {
  const m = mocks(() => assert.fail("External upload must not start"), (url, options) => options.method === "DELETE" ? {} : { ...session, upload_url: "https://example.com/private" });
  try {
    await assert.rejects(uploadFile(file(10), limits, new AbortController().signal, () => {}), /unsupported destination/);
    assert.equal(m.requests.length, 0);
    assert.equal(m.apiCalls.at(-1).options.method, "DELETE");
  } finally { m.restore(); }
});

test("legacy usage shows totals without fabricated cache or reasoning values", () => {
  const html = renderToStaticMarkup(React.createElement(UsageDetails, { usage: { calls: 5, input_tokens: 1000, output_tokens: 20, by_provider: { claude: { calls: 5, input_tokens: 1000, output_tokens: 20 } } } }));
  assert.match(html, /breakdown is not available/);
  assert.match(html, /1,020/);
  assert.doesNotMatch(html, /<dt>Uncached input/);
  assert.doesNotMatch(html, /\$/);
});

test("detailed usage separates accounting subsets and flags unclassified input", () => {
  const usage = { calls: 4, detailed_calls: 3, input_tokens: 1000, output_tokens: 100, uncached_input_tokens: 300, cache_read_input_tokens: 500, cache_write_input_tokens: 100, thinking_tokens: 40, estimated_tokens: 50, usage_details_available: true, by_provider: {}, by_stage: { analysis: { calls: 2, input_tokens: 900, output_tokens: 100, detailed_calls: 2, cache_read_input_tokens: 400 } } };
  const html = renderToStaticMarkup(React.createElement(UsageDetails, { usage }));
  assert.match(html, /1,100/);
  assert.match(html, /Input without a breakdown/);
  assert.match(html, /Output, including reasoning/);
  assert.match(html, /Estimated tokens are already included/);
  assert.match(html, /By stage/);
  assert.match(html, /Analysis/);
  assert.doesNotMatch(html, /\$/);
});

test("coverage describes a stored original separately from its limited preview", () => {
  const html = renderToStaticMarkup(React.createElement(FileCoverage, { files: [ref] }));
  assert.match(html, /Limited previews/);
  assert.match(html, /2.0 MB processed for preview \/ 20.0 MB stored/);
  assert.match(html, /does not mean the models inspected every byte/);
  const historical = renderToStaticMarkup(React.createElement(FileCoverage, { files: [{ id: "old", name: "old.csv", size: 100 }] }));
  assert.match(historical, /coverage was not recorded/);
});

test("ZIP coverage keeps server evidence and describes large-archive inspection limits", () => {
  const html = renderToStaticMarkup(React.createElement(FileCoverage, { files: [{ ...ref, name: "research.ZIP", mime: "application/octet-stream", kind: "other" }] }));
  assert.match(html, /A bounded prefix was examined/);
  assert.match(html, /Large ZIP archives are stored unchanged/);
  assert.match(html, /member files are not automatically inspected/);
});

test("question-driven runs never render legacy timers even when stale timing fields are present", () => {
  const base = { status: "running", time_limit_s: 7200, total_time_limit_s: 7200, clock_started_at: Date.now() / 1000, used_s: 10, active_calls: { a: { provider: "gemini", kind: "answer", label: "Preparing answer", started_at: 1, soft_deadline_at: 2, hard_deadline_at: 3 } } };
  for (const route of [{ workflow: "bridge" }, { timing_policy: "question_driven" }]) {
    assert.equal(renderToStaticMarkup(React.createElement(RunClock, { run: { ...base, ...route } })), "");
    assert.equal(renderToStaticMarkup(React.createElement(ActiveCalls, { run: { ...base, ...route } })), "");
  }
  const historical = renderToStaticMarkup(React.createElement(RunClock, { run: { ...base, status: "completed", timing_policy: "strict" } }));
  assert.match(historical, /legacy limit/);
});

test("bridge collaboration presents actual usage, stop reasons and review caveat without countdowns", () => {
  const html = renderToStaticMarkup(React.createElement(CollaborationSummary, { run: {
    status: "completed", usage: { calls: 3, input_tokens: 1200, output_tokens: 400 },
    events: [{ at: 10, provider: "gemini", type: "no_progress", message: "A repeated unresolved issue stopped further revision." }],
    active_calls: {},
  } }));
  assert.match(html, /3 calls/);
  assert.match(html, /1,600/);
  assert.match(html, /repeated unresolved issue/);
  assert.match(html, /not proof of correctness/);
  assert.doesNotMatch(html, /minutes|timeout|countdown|accuracy|CUDA|SLM/);
});

test("new-question payload drops obsolete timing and approval inputs and preserves selected files", () => {
  const payload = questionRequest({ request: "  Explain the evidence  ", materials: "Background", eris: false, mode: "deep", file_ids: ["stored-file"], time_limit_minutes: 120, require_plan_approval: true });
  assert.equal(payload.workflow, "bridge");
  assert.equal(payload.request, "Explain the evidence");
  assert.deepEqual(payload.file_ids, ["stored-file"]);
  assert.ok(!("time_limit_minutes" in payload));
  assert.ok(!("require_plan_approval" in payload));
  assert.ok(!("compare_all" in payload));
});

test("question workspace has effort choices and upload recovery without a time selector", () => {
  const html = renderToStaticMarkup(React.createElement(Workspace, { open() {}, needsKeys: false, providers: [] }));
  assert.match(html, /Ask SASI|Checking limits/);
  assert.match(html, /Answer effort/);
  assert.match(html, /Attach files/);
  assert.match(html, /50,00,000/);
  assert.doesNotMatch(html, /<select|Research time target|minutes total|Approve the plan|Always compare/);
});

test("bridge answer retains failed assurance and limits instead of presenting completed work as verified", () => {
  const html = renderToStaticMarkup(React.createElement(BridgeSession, { run: {
    status: "completed", live: {}, events: [], usage: { calls: 2, input_tokens: 100, output_tokens: 20, by_provider: {} },
    final: { output: "", verification_status: "failed", meets_criteria: false, gaps: ["The data was not independently checked."], limitations: ["No execution evidence."] },
  }, evidence: null, solution: null, copyAnswer: null }));
  assert.equal((html.match(/aria-label="SASI answer"/g) || []).length, 1);
  assert.match(html, /unmet criteria/);
  assert.match(html, /Checks found an issue/);
  assert.match(html, /The data was not independently checked/);
  assert.match(html, /No execution evidence/);
  assert.doesNotMatch(html, /Plan &amp; tasks|Model review score|minute|accuracy guarantee/);
});
