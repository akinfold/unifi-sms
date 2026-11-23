const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { setImmediate } = require("node:timers/promises");
const { runInNewContext } = require("node:vm");

const source = readFileSync(join(__dirname, "../inbox/static/inbox/app.js"), "utf8");
const appSource = source.slice(0, source.indexOf('byId("refresh-phone").addEventListener'));
const catalogUrl = "https://static.ui.com/fingerprint/ui/public.json";
const euImage = "5dc0d4df-cdaa-44dc-ac4f-d430260700c6";
const usImage = "089999e2-2756-4382-ac3e-ef7dbfa6c290";
const proHash = "429452fe47c59a453f377ab882e30de7";
const json = (data) => ({ok: true, json: async () => data});
const sampleCatalog = {
  devices: [
    {id: euImage, sku: "U-LTE-Backup Pro", shortnames: ["U-LTE-Pro-EU"], images: {nopadding: proHash}},
    {id: usImage, sku: "U-LTE-Backup Pro", shortnames: ["ULTEPUS"], images: {nopadding: proHash}},
  ],
};

function setup(loadCatalog = () => json(sampleCatalog)) {
  const fallback = {};
  const images = [];
  const symbol = {
    firstElementChild: fallback,
    replaceChildren(child) { this.firstElementChild = child; },
  };
  const elements = new Map([["device-symbol", symbol]]);
  const catalogRequests = [];
  let now = 1;
  let model;
  const context = {
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, {classList: {toggle() {}}});
        return elements.get(id);
      },
      querySelector: () => ({value: "synthetic-csrf-token"}),
    },
    Image: class extends EventTarget {
      constructor() {
        super();
        images.push(this);
      }
    },
    AbortSignal,
    Date: class extends Date { static now() { return now; } },
    async fetch(url, options) {
      if (url === catalogUrl) {
        catalogRequests.push(options);
        return loadCatalog();
      }
      assert.equal(url, "/api/status/");
      return json({state: "ok", device: {model}, phone: {state: "ok", numbers: []}});
    },
    clearTimeout() {},
  };
  runInNewContext(appSource, context);
  return {
    fallback, images, symbol, catalogRequests,
    advanceTime(ms) { now += ms; },
    async refresh(reportedModel) {
      model = reportedModel;
      await context.syncStatus();
      await setImmediate();
      assert.equal(elements.get("alert")?.hidden, undefined);
    },
  };
}

test("the reported model selects the official image and ignores stale image loads", async () => {
  const { refresh, fallback, images, symbol, catalogRequests } = setup();
  await refresh("");
  assert.equal(images.length, 0);
  assert.equal(catalogRequests.length, 0);

  await refresh(" U-lte-pro-eu ");
  assert.match(images[0].src, /^https:\/\/static\.ui\.com\/fingerprint\/ui\/images\/5dc0d4df-/);
  assert.equal(images[0].referrerPolicy, "no-referrer");
  assert.equal(symbol.firstElementChild, fallback);

  await refresh("ULTEPUS");
  assert.match(images[1].src, /^https:\/\/static\.ui\.com\/fingerprint\/ui\/images\/089999e2-/);
  images[0].dispatchEvent(new Event("load"));
  assert.equal(symbol.firstElementChild, fallback);
  images[1].dispatchEvent(new Event("load"));
  assert.equal(symbol.firstElementChild, images[1]);

  await refresh("U-LTE-Pro-Unknown");
  assert.equal(symbol.firstElementChild, fallback);
  assert.equal(images.length, 2);
  assert.equal(catalogRequests.length, 1);
  assert.equal(catalogRequests[0].credentials, "omit");
  assert.equal(catalogRequests[0].referrerPolicy, "no-referrer");
});

test("an unavailable image leaves the icon visible across status refreshes", async () => {
  const { refresh, fallback, images, symbol } = setup();
  await refresh("U-LTE-Backup Pro");
  assert.match(images[0].src, new RegExp(`${euImage}/nopadding/${proHash}\\.png$`));
  images[0].dispatchEvent(new Event("error"));
  await refresh("U-LTE-Backup Pro");
  assert.equal(symbol.firstElementChild, fallback);
  assert.equal(images.length, 1);
});

test("a catalog update adds device identifiers without a code change", async () => {
  const newDevice = {
    id: "12345678-9abc-def0-1234-56789abcdef0",
    shortnames: ["LTE-Synthetic"],
    triplets: [{k2: "Test Board 2"}],
    sysids: ["ea91"],
    images: {default: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  };
  let catalog = sampleCatalog;
  const { refresh, advanceTime, fallback, images, symbol } = setup(() => json(catalog));
  await refresh("Test Board 2");
  assert.equal(symbol.firstElementChild, fallback);

  catalog = {devices: [...sampleCatalog.devices, newDevice]};
  advanceTime(86_400_000);
  for (const model of ["Test Board 2", "EA91", "lte-synthetic"]) {
    await refresh(model);
    assert.equal(images.at(-1).src, `https://static.ui.com/fingerprint/ui/images/${newDevice.id}/default/${newDevice.images.default}.png`);
    images.at(-1).dispatchEvent(new Event("load"));
    assert.equal(symbol.firstElementChild, images.at(-1));
  }
});

test("ambiguous catalog entries and failed catalog requests leave the icon visible", async () => {
  let catalogResponse = {ok: false};
  const { refresh, advanceTime, fallback, images, symbol, catalogRequests } = setup(() => catalogResponse);
  await refresh("U-LTE-Backup Pro");
  await refresh("U-LTE-Backup Pro");
  assert.equal(catalogRequests.length, 1);
  assert.equal(symbol.firstElementChild, fallback);

  catalogResponse = json({devices: [
    sampleCatalog.devices[0],
    {...sampleCatalog.devices[1], images: {nopadding: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},
  ]});
  advanceTime(60_000);
  await refresh("U-LTE-Backup Pro");
  assert.equal(catalogRequests.length, 2);
  assert.equal(images.length, 0);
  assert.equal(symbol.firstElementChild, fallback);
});
