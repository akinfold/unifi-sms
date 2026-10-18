const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const source = readFileSync(join(__dirname, "../inbox/static/inbox/device_settings.js"), "utf8");

function setup(response) {
  let generate;
  let request;
  const button = { disabled: false, textContent: "Generate SSH key", setAttribute() {}, removeAttribute() {},
    addEventListener(event, handler) { generate = handler; } };
  const status = { textContent: "" };
  const details = { hidden: true };
  const publicKey = { value: "" };
  const next = { disabled: false };
  const actionButtons = { toString: () => "[object RadioNodeList]" };
  const form = {
    action: actionButtons,
    getAttribute: name => name === "action" ? "/settings/device/" : null,
    querySelector: selector => selector.includes("csrfmiddlewaretoken") ? { value: "synthetic-csrf-token" }
      : selector.includes("save-access") ? next : null,
    querySelectorAll: () => [],
  };
  const select = value => ({ value, addEventListener() {} });
  const elements = {
    "device-access-form": form, "id_auth_method": select("private_key"), "id_key_source": select("generate"),
    "generate-ssh-key": button, "key-generation-status": status,
    "generated-key-details": details, "generated-public-key": publicKey,
  };
  runInNewContext(source, {
    document: { getElementById: id => elements[id] || null },
    window: { setTimeout, clearTimeout }, AbortController, URLSearchParams,
    fetch: async (url, options) => { request = { url, options }; return response; },
  });
  return { click: () => generate(), button, status, details, publicKey, next, request: () => request };
}

function response({ status = 200, contentType = "application/json", redirected = false, data = {}, json } = {}) {
  return { status, ok: status < 400, redirected, headers: { get: () => contentType },
    json: json || (async () => data) };
}

test("key generation uses the form action attribute when action buttons shadow the property", async () => {
  const page = setup(response({ data: { public_key: "ssh-rsa synthetic-public-key" } }));
  await page.click();
  assert.equal(page.request().url, "/settings/device/");
  assert.equal(page.request().options.method, "POST");
  assert.equal(page.request().options.headers["Content-Type"], "application/x-www-form-urlencoded");
  assert.equal(page.request().options.headers["X-CSRFToken"], "synthetic-csrf-token");
  assert.equal(page.request().options.body.toString(), "action=generate-key");
  assert.equal(page.publicKey.value, "ssh-rsa synthetic-public-key");
  assert.equal(page.details.hidden, false);
  assert.equal(page.button.disabled, true);
  assert.equal(page.next.disabled, false);
});

test("an HTML server error is reported with its HTTP status without claiming the session expired", async () => {
  const page = setup(response({ status: 500, contentType: "text/html" }));
  await page.click();
  assert.match(page.status.textContent, /HTTP 500/);
  assert.doesNotMatch(page.status.textContent, /session/i);
  assert.equal(page.details.hidden, true);
  assert.equal(page.button.disabled, false);
  assert.equal(page.next.disabled, false);
});

test("a CSRF rejection asks the user to reload the page", async () => {
  const page = setup(response({ status: 403, contentType: "text/html" }));
  await page.click();
  assert.match(page.status.textContent, /security check failed/);
  assert.equal(page.button.disabled, false);
});

test("a login redirect is reported as an expired session", async () => {
  const page = setup(response({ redirected: true, contentType: "text/html" }));
  await page.click();
  assert.match(page.status.textContent, /session has expired/);
});

test("a JSON error from the server is displayed without replacing it with a session error", async () => {
  const page = setup(response({ status: 409, data: { error: "This setup has expired. Choose the device address again." } }));
  await page.click();
  assert.match(page.status.textContent, /Choose the device address again/);
  assert.equal(page.details.hidden, true);
});

test("malformed JSON is reported as invalid data", async () => {
  const page = setup(response({ json: async () => JSON.parse("<html>") }));
  await page.click();
  assert.match(page.status.textContent, /invalid data/);
  assert.doesNotMatch(page.status.textContent, /session/i);
});
