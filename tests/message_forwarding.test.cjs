const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const source = readFileSync(join(__dirname, "../inbox/static/inbox/app.js"), "utf8");
const appSource = source.slice(0, source.indexOf("function renderPhone"));

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.textContent = "";
    this.hidden = false;
    this.value = "";
    this.checked = false;
    this.className = "";
    this.attributes = {};
  }
  append(...items) {
    for (const item of items) {
      if (item.tagName === "#document-fragment") this.children.push(...item.children);
      else this.children.push(item);
    }
  }
  replaceChildren(...items) {
    this.children = [];
    this.append(...items);
  }
  addEventListener() {}
  setAttribute(name, value) { this.attributes[name] = value; }
  dispatchEvent() {}
}

function setup(fetchImpl) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const document = {
    hidden: false,
    getElementById: get,
    querySelector: () => ({ value: "synthetic-csrf-token" }),
    querySelectorAll: () => [],
    createElement: (tag) => new Element(tag),
    createDocumentFragment: () => new Element("#document-fragment"),
  };
  const context = {
    document,
    fetch: fetchImpl,
    location: { assign() {} },
    URLSearchParams,
    Date,
    Object,
    Set,
    console,
  };
  runInNewContext(`${appSource}\nglobalThis.__test = {forwardingStatus, load, setOpened: value => { opened = value; }};`, context);
  return { api: context.__test, get };
}

function jsonResponse(data) {
  return { status: 200, ok: true, json: async () => data };
}

function statusNode(page, message, enabled = true) {
  const container = new Element();
  container.append(page.api.forwardingStatus(message, enabled));
  return container;
}

test("forwarding status stays hidden when forwarding is disabled", () => {
  const page = setup(async () => jsonResponse({}));
  const status = statusNode(page, { forwarding: { state: "sent", sent_at: "2026-10-20T12:00:00Z" } }, false);

  assert.equal(status.children.length, 0);
});

test("sent status includes its timestamp", () => {
  const page = setup(async () => jsonResponse({}));
  const status = statusNode(page, { forwarding: { state: "sent", sent_at: "2026-10-20T12:00:00Z" } });

  assert.equal(status.children[0].children[0].textContent, "Email sent");
  assert.equal(status.children[0].children[1].dateTime, "2026-10-20T12:00:00Z");
  assert.match(status.children[0].children[1].textContent, /^Sent /);
});

test("pending, error, and unqueued states never display a sent timestamp", () => {
  const page = setup(async () => jsonResponse({}));
  for (const state of ["pending", "error", "not_queued"]) {
    const status = statusNode(page, { forwarding: { state, sent_at: null } });
    assert.equal(status.children[0].children.length, 1, state);
    assert.match(status.children[0].children[0].textContent, /^Email /, state);
  }
});

test("load updates the open reader status even when filters exclude its row", async () => {
  let requestedUrl;
  const page = setup(async (url) => {
    requestedUrl = new URL(url, "http://example.test");
    return jsonResponse({
      messages: [], page: 1, pages: 4, count: 0, forwarding_enabled: true,
      opened_forwarding: { id: 47, forwarding: { state: "sent", sent_at: "2026-10-20T12:00:00Z" } },
    });
  });
  page.api.setOpened(47);
  page.get("search").value = "outside current page";
  page.get("reader-forwarding").append(new Element());

  await page.api.load();

  assert.equal(requestedUrl.searchParams.get("q"), "outside current page");
  assert.equal(requestedUrl.searchParams.get("opened"), "47");
  const readerStatus = page.get("reader-forwarding").children[0];
  assert.equal(readerStatus.children[0].textContent, "Email sent");
  assert.equal(readerStatus.children[1].dateTime, "2026-10-20T12:00:00Z");
});

test("a stale list response cannot replace the latest reader forwarding status", async () => {
  const pending = [];
  const page = setup((url) => new Promise((resolve) => pending.push({ url, resolve })));
  page.api.setOpened(47);
  page.get("reader-forwarding").append(new Element());

  const staleLoad = page.api.load();
  const currentLoad = page.api.load();
  pending[1].resolve(jsonResponse({
    messages: [], page: 1, pages: 1, count: 0, forwarding_enabled: true,
    opened_forwarding: { id: 47, forwarding: { state: "error", sent_at: null } },
  }));
  await currentLoad;
  pending[0].resolve(jsonResponse({
    messages: [], page: 1, pages: 1, count: 0, forwarding_enabled: true,
    opened_forwarding: { id: 47, forwarding: { state: "sent", sent_at: "2026-10-20T12:00:00Z" } },
  }));
  await staleLoad;

  const readerStatus = page.get("reader-forwarding").children[0];
  assert.equal(readerStatus.children[0].textContent, "Email error");
  assert.equal(readerStatus.children.length, 1);
});

test("load hides and restores the open reader status as forwarding is disabled and enabled", async () => {
  const responses = [
    { forwarding_enabled: false, forwarding: { state: "sent", sent_at: "2026-10-20T12:00:00Z" } },
    { forwarding_enabled: true, forwarding: { state: "sent", sent_at: "2026-10-20T12:00:00Z" } },
    { forwarding_enabled: false, forwarding: { state: "sent", sent_at: "2026-10-20T12:00:00Z" } },
    { forwarding_enabled: true, forwarding: { state: "pending", sent_at: null } },
  ];
  const page = setup(async () => {
    const next = responses.shift();
    return jsonResponse({
      messages: [], page: 1, pages: 1, count: 0,
      forwarding_enabled: next.forwarding_enabled,
      opened_forwarding: { id: 47, forwarding: next.forwarding },
    });
  });
  page.api.setOpened(47);
  page.get("reader-forwarding").append(new Element());

  await page.api.load();
  assert.equal(page.get("reader-forwarding").children.length, 0);

  await page.api.load();
  let readerStatus = page.get("reader-forwarding").children[0];
  assert.equal(readerStatus.children[0].textContent, "Email sent");
  assert.equal(readerStatus.children[1].dateTime, "2026-10-20T12:00:00Z");

  await page.api.load();
  assert.equal(page.get("reader-forwarding").children.length, 0);

  await page.api.load();
  readerStatus = page.get("reader-forwarding").children[0];
  assert.equal(readerStatus.children[0].textContent, "Email pending");
  assert.equal(readerStatus.children.length, 1);
});

test("a response for a previously open message cannot update the new reader", async () => {
  let requestedUrl;
  let resolveResponse;
  const page = setup((url) => {
    requestedUrl = new URL(url, "http://example.test");
    return new Promise((resolve) => { resolveResponse = resolve; });
  });
  page.api.setOpened(47);
  page.get("reader-forwarding").append(new Element());

  const staleLoad = page.api.load();
  page.api.setOpened(48);
  const currentReaderStatus = page.api.forwardingStatus(
    { forwarding: { state: "pending", sent_at: null } }, true,
  );
  page.get("reader-forwarding").replaceChildren(currentReaderStatus);
  resolveResponse(jsonResponse({
    messages: [], page: 1, pages: 1, count: 0, forwarding_enabled: true,
    opened_forwarding: { id: 47, forwarding: { state: "sent", sent_at: "2026-10-20T12:00:00Z" } },
  }));
  await staleLoad;

  assert.equal(requestedUrl.searchParams.get("opened"), "47");
  assert.equal(page.get("reader-forwarding").children[0], currentReaderStatus);
  assert.equal(currentReaderStatus.children[0].textContent, "Email pending");
});
