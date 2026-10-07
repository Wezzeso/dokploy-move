import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const html = readFileSync(new URL("./dokploy-move-ui.html", import.meta.url), "utf8");
const controller = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(controller, "The standalone console must contain its controller");
const mutations = ["plan", "prepare", "move", "cutover", "resume-source"];

// Exercise the shipped controller with a minimal DOM, not a second workflow
// implementation. Timers and HTTP are recorded; nothing reaches a server.
function consoleHarness({ token = "test-local-token" } = {}) {
  const nodes = new Map();
  for (const [, id] of html.matchAll(/id="([^"]+)"/g)) {
    nodes.set(id, {
      id, value: "", textContent: "", checked: false, disabled: false,
      hidden: false, dataset: {}, handlers: {}, scrollHeight: 0,
      scrollTop: 0, clientHeight: 0,
      addEventListener(type, listener) { this.handlers[type] = listener; },
      setCustomValidity(message) { this.validationMessage = message; },
      reportValidity() {},
      setAttribute(name, value) { this[name] = value; },
    });
  }
  const element = (id) => {
    assert.ok(nodes.has(id), `Missing console element: ${id}`);
    return nodes.get(id);
  };
  const translated = [...html.matchAll(/data-i18n="([^"]+)"/g)]
    .map(([, key]) => ({ dataset: { i18n: key }, textContent: "" }));
  const requests = [], responses = [], stored = [], timers = new Map();
  let nextTimer = 0;
  const document = {
    getElementById: element,
    querySelectorAll: () => translated,
    documentElement: { dataset: {} },
    title: "",
  };
  const history = { replaceState(...args) { this.last = args; } };
  const context = vm.createContext({
    document, history, URLSearchParams, AbortController,
    location: { hash: token ? `#token=${token}` : "", pathname: "/", search: "" },
    localStorage: { getItem: () => null, setItem: (...args) => stored.push(args) },
    setTimeout(callback, delay) { timers.set(++nextTimer, { callback, delay }); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(path, options) {
      requests.push({ path, options });
      const response = responses.shift();
      if (response instanceof Error) throw response;
      assert.ok(response, "Tests must explicitly provide every HTTP response");
      return { ok: true, json: async () => response };
    },
  });
  const run = (code) => vm.runInContext(code, context);
  vm.runInContext(controller, context, { filename: "dokploy-move-ui.html" });
  element("source").value = "root@old";
  element("target").value = "root@new";
  element("new-ip").value = "10.0.0.20";
  run("render()");
  return {
    element, document, history, requests, responses, stored, timers, translated, run,
    snapshot(overrides = {}, adoptConfig = false) {
      context.testSnapshot = { ok: true, job: {
        id: "job-1", action: "plan", target: "root@new", new_ip: "10.0.0.20",
        active: false, result: "success", log: "Destination check passed.", phase: null,
        ...overrides,
      } };
      run(`acceptSnapshot(testSnapshot, ${adoptConfig}); render()`);
    },
    check(id, checked = true) {
      element(id).checked = checked;
      element(id).handlers.change();
    },
    edit(id, value) {
      element(id).value = value;
      element(id).handlers.input();
    },
  };
}

function assertMutationsDisabled(ui) {
  for (const action of mutations) assert.equal(ui.element(action).disabled, true, action);
}

test("confirmed destination activation keeps failed orchestration visible and forbids stale recovery", () => {
  const ui = consoleHarness();
  ui.snapshot({ action: "move", phase: "active", result: "failed" });
  ui.check("resume-ack");
  assertMutationsDisabled(ui);
  assert.equal(ui.element("finish").hidden, false);
  assert.equal(ui.element("badge").textContent, "Needs attention");
  assert.match(ui.element("status-copy").textContent, /did not finish successfully/);
});

test("move requires a successful destination check and acknowledgement; active jobs lock controls", () => {
  const ui = consoleHarness();
  ui.check("acknowledge");
  assert.equal(ui.element("move").disabled, true);
  ui.snapshot({ active: true, result: "running" });
  assertMutationsDisabled(ui);
  for (const id of ["source", "target", "new-ip"]) assert.equal(ui.element(id).disabled, true);
  assert.equal(ui.element("refresh").disabled, false);

  ui.check("acknowledge", false);
  ui.snapshot();
  assert.equal(ui.element("move").disabled, true);
  assert.equal(ui.element("finish").hidden, true, "A successful plan is not a finished move");
  ui.check("acknowledge");
  assert.equal(ui.element("move").disabled, false);
});

test("changing any server setting invalidates the previous destination check", () => {
  for (const [id, value] of [["source", "root@other-source"], ["target", "root@other-target"], ["new-ip", "10.0.0.21"]]) {
    const ui = consoleHarness();
    ui.snapshot();
    ui.check("acknowledge");
    assert.equal(ui.element("move").disabled, false);
    ui.edit(id, value);
    ui.check("acknowledge");
    assert.equal(ui.element("move").disabled, true, id);
  }
});

test("reconnect allows prepared continuation, but unknown results block every action", () => {
  const ui = consoleHarness();
  ui.snapshot({ action: "prepare", phase: "prepared" }, true);
  assert.equal(ui.element("prepare").disabled, false, "Server retains the prior plan approval");
  assert.equal(ui.element("cutover").disabled, true);
  ui.check("acknowledge");
  assert.equal(ui.element("cutover").disabled, false);

  ui.snapshot({ action: "move", phase: "source-stopped", result: "unknown" }, true);
  ui.check("resume-ack");
  assertMutationsDisabled(ui);
  assert.equal(ui.element("refresh").disabled, false);
});

test("failed final copying exposes deliberate recovery; activation forbids stale-source recovery", () => {
  const ui = consoleHarness();
  ui.snapshot({ action: "cutover", phase: "source-stopped", result: "failed" }, true);
  assert.equal(ui.element("move").disabled, true);
  assert.equal(ui.element("resume-source").disabled, true);
  ui.check("acknowledge");
  ui.check("resume-ack");
  assert.equal(ui.element("cutover").disabled, false);
  assert.equal(ui.element("resume-source").disabled, false);

  for (const phase of ["activating", "active"]) {
    ui.snapshot({ action: "cutover", phase, result: phase === "active" ? "success" : "failed" }, true);
    assertMutationsDisabled(ui);
    assert.equal(ui.element("finish").hidden, false);
  }
});

test("a new plan ignores the active phase retained from an earlier server move", () => {
  const ui = consoleHarness();
  ui.snapshot({ action: "plan", phase: "active", source_phase: "active" }, true);
  ui.check("acknowledge");
  assert.equal(ui.element("move").disabled, false);
  assert.equal(ui.element("finish").hidden, true);
  assert.equal(ui.element("status-title").textContent, "Destination check passed");
});

test("lost action response requires reconnect and never automatically retries a mutation", async () => {
  const ui = consoleHarness();
  assert.deepEqual(ui.history.last, [null, "", "/"], "Token is removed from the address bar");
  ui.snapshot();
  ui.check("acknowledge");
  ui.responses.push(new Error("Connection lost"));
  await ui.run('runAction("move")');
  assertMutationsDisabled(ui);
  assert.equal(ui.element("refresh").disabled, false);
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].options.headers["X-Migration-Token"], "test-local-token");
  assert.equal(JSON.parse(ui.requests[0].options.body).action, "move");
  assert.equal(ui.stored.length, 0, "Credentials and server settings are not persisted in local storage");

  ui.responses.push({ ok: true, job: {
    id: "job-2", action: "move", target: "root@new", new_ip: "10.0.0.20",
    active: true, result: "running", log: "Copying", phase: "prepared",
  } });
  await ui.run("refreshStatus()");
  assert.equal(ui.requests.length, 2);
  assert.equal(ui.requests[1].path, "/api/status?source=root%40old");
  assert.equal(ui.requests[1].options.headers["X-Migration-Token"], "test-local-token");
  assert.equal(ui.requests[1].options.method, undefined, "Reconnect is a status read");
  assert.ok([...ui.timers.values()].some(({ delay }) => delay === 5000));
  assertMutationsDisabled(ui);
});

test("all locales cover the UI and untrusted output stays literal text", () => {
  const ui = consoleHarness();
  const keys = ui.run('Object.keys(dictionaries.en).sort().join("|")');
  for (const locale of ["ru", "kk", "en"]) {
    assert.equal(ui.run(`Object.keys(dictionaries.${locale}).sort().join("|")`), keys);
    ui.run(`applyLanguage("${locale}")`);
    assert.equal(ui.document.documentElement.lang, locale);
    for (const element of ui.translated) {
      assert.equal(ui.run(`Object.hasOwn(dictionaries.${locale}, ${JSON.stringify(element.dataset.i18n)})`), true);
      assert.notEqual(element.textContent, "");
    }
  }
  const unsafe = '<img src=x onerror="throw Error()">';
  ui.snapshot({ action: "move", phase: "active", log: unsafe });
  assert.equal(ui.element("log").textContent, unsafe);
  assert.equal(ui.element("log").innerHTML, undefined);

  const locked = consoleHarness({ token: "" });
  assertMutationsDisabled(locked);
  assert.equal(locked.element("refresh").disabled, true);
  assert.equal(locked.element("error").hidden, false);
});
