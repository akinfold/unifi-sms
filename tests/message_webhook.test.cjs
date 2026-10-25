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
    document, fetch: fetchImpl, location: { assign() {} }, URLSearchParams, Date, Object, Set, console,
  };
  runInNewContext(`${appSource}\nglobalThis.__test = {webhookStatus, load, setOpened: value => { opened = value; }};`, context);
  return { api: context.__test, get };
}

function jsonResponse(data) {
  return { status: 200, ok: true, json: async () => data };
}

function statusNode(page, webhook, enabled = true) {
  const container = new Element();
  container.append(page.api.webhookStatus({ webhook }, enabled));
  return container;
}

const timestamp = "2026-10-20T12:00:00Z";

test("webhook status stays hidden when delivery is disabled", () => {
  const page = setup(async () => jsonResponse({}));
  assert.equal(statusNode(page, { state: "sent", sent_at: timestamp }, false).children.length, 0);
});

test("sent webhook status includes its timestamp and delivery contract", () => {
  const page = setup(async () => jsonResponse({}));
  const status = statusNode(page, { state: "sent", sent_at: timestamp });

  assert.equal(status.children[0].children[0].textContent, "Webhook sent");
  assert.equal(status.children[0].children[0].className, "webhook-label webhook-sent");
  assert.match(status.children[0].children[0].title, /does not confirm downstream processing/);
  assert.equal(status.children[0].children[1].className, "webhook-sent-at");
  assert.equal(status.children[0].children[1].dateTime, timestamp);
  assert.match(status.children[0].children[1].textContent, /^Sent /);
});

test("pending, error, and unqueued webhook states do not show a sent time", () => {
  const page = setup(async () => jsonResponse({}));
  for (const [state, label] of [["pending", "Webhook pending"], ["error", "Webhook error"], ["not_queued", "Webhook not queued"]]) {
    const status = statusNode(page, { state, sent_at: null });
    assert.equal(status.children[0].children.length, 1, state);
    assert.equal(status.children[0].children[0].textContent, label);
  }
});

test("refresh updates webhook status in an open reader outside the current filters", async () => {
  let requestedUrl;
  const page = setup(async (url) => {
    requestedUrl = new URL(url, "http://example.test");
    return jsonResponse({
      messages: [], page: 1, pages: 1, count: 0,
      webhook_enabled: true,
      opened_webhook: { id: 47, webhook: { state: "sent", sent_at: timestamp } },
    });
  });
  page.api.setOpened(47);
  page.get("search").value = "outside current page";
  page.get("reader-webhook").append(new Element());

  await page.api.load();

  assert.equal(requestedUrl.searchParams.get("q"), "outside current page");
  assert.equal(requestedUrl.searchParams.get("opened"), "47");
  const readerStatus = page.get("reader-webhook").children[0];
  assert.equal(readerStatus.children[0].textContent, "Webhook sent");
  assert.equal(readerStatus.children[1].dateTime, timestamp);
});

test("refresh hides and restores reader webhook status as configuration changes", async () => {
  const responses = [false, true, false, true].map((webhook_enabled, index) => ({
    messages: [], page: 1, pages: 1, count: 0, webhook_enabled,
    opened_webhook: { id: 47, webhook: { state: index === 3 ? "pending" : "sent", sent_at: timestamp } },
  }));
  const page = setup(async () => jsonResponse(responses.shift()));
  page.api.setOpened(47);
  page.get("reader-webhook").append(new Element());

  await page.api.load();
  assert.equal(page.get("reader-webhook").children.length, 0);
  await page.api.load();
  assert.equal(page.get("reader-webhook").children[0].children[0].textContent, "Webhook sent");
  await page.api.load();
  assert.equal(page.get("reader-webhook").children.length, 0);
  await page.api.load();
  assert.equal(page.get("reader-webhook").children[0].children[0].textContent, "Webhook pending");
});

test("a stale refresh cannot replace the latest webhook status", async () => {
  const pending = [];
  const page = setup((url) => new Promise((resolve) => pending.push({ url, resolve })));
  page.api.setOpened(47);
  page.get("reader-webhook").append(new Element());
  const stale = page.api.load();
  const current = page.api.load();
  pending[1].resolve(jsonResponse({
    messages: [], page: 1, pages: 1, count: 0, webhook_enabled: true,
    opened_webhook: { id: 47, webhook: { state: "error", sent_at: null } },
  }));
  await current;
  pending[0].resolve(jsonResponse({
    messages: [], page: 1, pages: 1, count: 0, webhook_enabled: true,
    opened_webhook: { id: 47, webhook: { state: "sent", sent_at: timestamp } },
  }));
  await stale;

  assert.equal(page.get("reader-webhook").children[0].children[0].textContent, "Webhook error");
});

test("a refresh for a previous open message cannot update the new reader", async () => {
  let resolveResponse;
  const page = setup(() => new Promise((resolve) => { resolveResponse = resolve; }));
  page.api.setOpened(47);
  page.get("reader-webhook").append(new Element());
  const stale = page.api.load();
  page.api.setOpened(48);
  const currentStatus = page.api.webhookStatus({ webhook: { state: "pending", sent_at: null } }, true);
  page.get("reader-webhook").replaceChildren(currentStatus);
  resolveResponse(jsonResponse({
    messages: [], page: 1, pages: 1, count: 0, webhook_enabled: false,
    opened_webhook: { id: 47, webhook: { state: "sent", sent_at: timestamp } },
  }));
  await stale;

  assert.equal(page.get("reader-webhook").children[0], currentStatus);
  assert.equal(currentStatus.children[0].textContent, "Webhook pending");
});
