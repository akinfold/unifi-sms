"use strict";
const byId = (id) => document.getElementById(id);
const token = document.querySelector("#csrf input").value;
let phoneBusy = false, phoneTimer;
let page = 1, pages = 1, selected = new Set(), opened = null, requestNumber = 0;
const date = (value) => new Date(value).toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
const node = (tag, className, text) => { const item = document.createElement(tag); item.className = className; if (text !== undefined) item.textContent = text; return item; };
function showError(error) { byId("alert").textContent = error.message; byId("alert").hidden = false; }
async function api(url, data) {
  const response = await fetch(url, {method: data === undefined ? "GET" : "POST", headers: {"X-CSRFToken": token, "Content-Type": "application/json"}, body: data === undefined ? undefined : JSON.stringify(data), credentials: "same-origin"});
  if (response.status === 403) { location.assign("/login/"); throw new Error("Please sign in again."); }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The request failed. Please try again.");
  return result;
}
function selection() {
  byId("selection-count").textContent = selected.size ? `${selected.size} selected` : "Select messages";
  byId("delete").disabled = !selected.size;
  const boxes = [...document.querySelectorAll(".message-checkbox")];
  byId("select-all").checked = boxes.length > 0 && boxes.every((box) => box.checked);
  byId("select-all").indeterminate = boxes.some((box) => box.checked) && !byId("select-all").checked;
}
async function load() {
  const sequence = ++requestNumber;
  try {
    const query = new URLSearchParams({page, q: byId("search").value, unread: byId("unread").checked ? "1" : "0"});
    const data = await api(`/api/messages/?${query}`);
    if (sequence !== requestNumber) return;
    page = data.page; pages = data.pages; selected.clear();
    byId("count").textContent = data.count;
    byId("list").replaceChildren();
    if (!data.messages.length) byId("list").append(node("div", "empty", byId("search").value || byId("unread").checked ? "No messages match your filters." : "Your inbox is ready. New messages will appear here automatically."));
    for (const message of data.messages) {
      const row = node("div", `message-row ${message.read ? "" : "unread"} ${opened === message.id ? "active" : ""}`);
      const checkbox = node("input", "message-checkbox"); checkbox.type = "checkbox"; checkbox.setAttribute("aria-label", `Select message from ${message.sender}`);
      checkbox.addEventListener("change", () => { if (checkbox.checked) selected.add(message.id); else selected.delete(message.id); selection(); });
      const button = node("button", "message-open"); button.type = "button";
      const top = node("div", "message-meta"); top.append(node("strong", "sender", message.sender));
      const receivedAt = node("time", "received-at", `Received ${date(message.received_at)}`);
      receivedAt.dateTime = message.received_at;
      button.append(top, receivedAt, node("p", "preview", message.text));
      if (message.total_parts > 1 || message.pending_cleanup) button.append(node("span", "badge", message.pending_cleanup ? "Awaiting device cleanup" : `${message.saved_parts}/${message.total_parts} parts saved`));
      button.addEventListener("click", () => openMessage(message.id));
      row.append(checkbox, button); byId("list").append(row);
    }
    byId("page-label").textContent = `Page ${page} of ${pages}`;
    byId("previous").disabled = page <= 1; byId("next").disabled = page >= pages; selection();
  } catch (error) { showError(error); }
}
async function openMessage(id) {
  try {
    const message = await api(`/api/messages/${id}/`, {}); opened = id;
    const head = node("div", "reader-head"); head.append(node("div", "eyebrow", "RECEIVED MESSAGE"), node("h2", "reader-sender", message.sender), node("p", "muted", date(message.received_at)));
    const body = node("div", "message-body", message.text);
    const note = node("p", "footnote", message.pending_cleanup ? "Saved in your archive. Device cleanup is pending." : "Saved in your archive. Device slot cleared.");
    if (message.saved_parts < message.total_parts) head.append(node("p", "badge", `Incomplete message: ${message.saved_parts} of ${message.total_parts} parts saved.`));
    byId("reader").replaceChildren(head, body, note); await load();
  } catch (error) { showError(error); }
}
function renderPhone(data) {
  phoneBusy = ["queued", "refreshing"].includes(data.state);
  byId("phone-number").textContent = data.numbers.length ? data.numbers.join(" · ") : (phoneBusy ? "Checking…" : "Not available");
  byId("phone-detail").textContent = ({
    waiting: "Waiting for the first device check.",
    queued: "Waiting for the importer to check the device…",
    refreshing: "Reading device information…",
    ok: "Device information is checked automatically every hour",
    empty: "This SIM does not provide a phone number.",
    unconfigured: "Configure the LTE SSH connection to read the number.",
    error: data.numbers.length ? "Update failed. Showing the last saved number." : data.error
  })[data.state] || "Waiting for device information";
  byId("phone-time").textContent = data.checked_at ? `Last checked ${new Date(data.checked_at).toLocaleString([], {dateStyle: "medium", timeStyle: "medium"})}` : "";
  byId("phone-status").classList.toggle("has-error", data.state === "error");
  byId("refresh-phone").disabled = phoneBusy || data.state === "unconfigured";
  byId("refresh-phone").textContent = phoneBusy ? "Updating…" : "Update from device";
  clearTimeout(phoneTimer);
  if (phoneBusy) phoneTimer = setTimeout(syncStatus, 2000);
}
async function syncStatus() {
  try {
    const data = await api("/api/status/");
    byId("sync-title").textContent = ({ok:"Inbox up to date", syncing:"Importing messages", waiting:"Import not configured", warning:"Some slots need attention", error:"Connection needs attention", stale:"Importer unavailable"})[data.state] || "Waiting";
    byId("sync-detail").textContent = data.detail;
    byId("sync-time").textContent = data.last_success ? `Last import ${date(data.last_success)}` : "No completed import yet";
    byId("sync-dot").className = `dot ${data.state}`;
    byId("device-address").textContent = data.device.address || "Not configured";
    byId("device-model").textContent = data.device.model || "Not available";
    byId("model-detail").textContent = data.device.error ? (data.device.model ? "Update failed · saved model" : "Could not read device model") : "Model reported by device";
    renderPhone(data.phone);
  } catch (error) { showError(error); }
}
byId("refresh-phone").addEventListener("click", async () => {
  byId("refresh-phone").disabled = true;
  byId("refresh-phone").textContent = "Updating…";
  try { renderPhone(await api("/api/phone/refresh/", {})); }
  catch (error) {
    byId("phone-detail").textContent = error.message;
    byId("phone-status").classList.add("has-error");
    byId("refresh-phone").disabled = false;
    byId("refresh-phone").textContent = "Update from device";
  }
});
let debounce;
byId("search").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(() => {page = 1; load();}, 250); });
byId("unread").addEventListener("change", () => {page = 1; load();});
byId("refresh").addEventListener("click", () => {byId("alert").hidden = true; load(); syncStatus();});
byId("previous").addEventListener("click", () => {if (page > 1) {page--; load();}});
byId("next").addEventListener("click", () => {if (page < pages) {page++; load();}});
byId("select-all").addEventListener("change", (event) => {const checked = event.target.checked; document.querySelectorAll(".message-checkbox").forEach((box) => {box.checked = checked; box.dispatchEvent(new Event("change"));}); selection();});
byId("delete").addEventListener("click", () => byId("delete-dialog").showModal());
byId("cancel-delete").addEventListener("click", () => byId("delete-dialog").close());
byId("confirm-delete").addEventListener("click", async () => {
  const ids = [...selected]; byId("confirm-delete").disabled = true;
  try { await api("/api/delete/", {ids}); if (ids.includes(opened)) {opened = null; byId("reader").replaceChildren(node("div", "empty", "Message deleted. Select another message to read."));} byId("delete-dialog").close(); await load(); }
  catch (error) {byId("delete-dialog").close(); showError(error);}
  finally {byId("confirm-delete").disabled = false;}
});
load(); syncStatus();
setInterval(() => { if (!document.hidden) {syncStatus(); if (!selected.size && !byId("delete-dialog").open) load();} }, 30000);
