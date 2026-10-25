"use strict";
const form = document.getElementById("integration-settings");
const status = document.getElementById("integration-key-status");
function report(text, error = false) {
  status.textContent = text;
  status.classList.toggle("is-error", error);
}
for (const button of form.querySelectorAll("[data-generate-key]")) {
  button.addEventListener("click", async () => {
    const name = button.dataset.generateKey;
    button.disabled = true;
    report("Generating key…");
    try {
      const response = await fetch(form.dataset.keyUrl, {
        method: "POST", credentials: "same-origin",
        headers: {"X-CSRFToken": form.querySelector("[name=csrfmiddlewaretoken]").value},
      });
      if (!response.ok) throw new Error(response.status === 403 ? "Reload this page and sign in again." : "Could not generate a key. Try again.");
      const data = await response.json();
      const input = document.getElementById(`id_${name}_key`);
      input.value = data.key;
      document.getElementById(`id_${name}_key_action`).value = "replace";
      input.focus();
      report("Key generated. Copy it, then save settings to activate it.");
    } catch (error) { report(error.message, true); }
    finally { button.disabled = false; }
  });
}
for (const button of form.querySelectorAll("[data-reveal-key]")) {
  button.addEventListener("click", () => {
    const input = document.getElementById(`id_${button.dataset.revealKey}_key`);
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    button.textContent = show ? "Hide key" : "Show key";
    button.setAttribute("aria-pressed", String(show));
  });
}
for (const button of form.querySelectorAll("[data-copy-key]")) {
  button.addEventListener("click", async () => {
    const input = document.getElementById(`id_${button.dataset.copyKey}_key`);
    if (!input.value) { report("Enter or generate a new key first. Saved keys cannot be retrieved.", true); return; }
    try {
      await navigator.clipboard.writeText(input.value);
      report("Key copied. Save settings to activate it.");
    } catch {
      input.type = "text";
      input.focus(); input.select();
      const reveal = form.querySelector(`[data-reveal-key="${button.dataset.copyKey}"]`);
      reveal.textContent = "Hide key"; reveal.setAttribute("aria-pressed", "true");
      report("Select and copy the key manually. Clipboard access requires HTTPS or localhost.");
    }
  });
}
