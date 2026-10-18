(() => {
  const accessForm = document.getElementById("device-access-form");
  if (accessForm) {
    const method = document.getElementById("id_auth_method");
    const source = document.getElementById("id_key_source");
    const setPanel = (selector, value, attribute) => {
      accessForm.querySelectorAll(selector).forEach((panel) => {
        const visible = panel.dataset[attribute] === value;
        panel.hidden = !visible;
        panel.querySelectorAll("input, select, textarea").forEach((field) => {
          field.disabled = !visible;
        });
      });
    };
    const update = () => {
      const selectedMethod = method?.value;
      setPanel("[data-auth-method]", selectedMethod, "authMethod");
      const keyPanel = accessForm.querySelector('[data-auth-method="private_key"]');
      if (keyPanel && !keyPanel.hidden) {
        setPanel("[data-key-source]", source?.value, "keySource");
      }
    };
    method?.addEventListener("change", update);
    source?.addEventListener("change", update);
    update();

    const generate = document.getElementById("generate-ssh-key");
    const status = document.getElementById("key-generation-status");
    const details = document.getElementById("generated-key-details");
    const publicKey = document.getElementById("generated-public-key");
    const next = accessForm.querySelector('button[value="save-access"]');
    generate?.addEventListener("click", async () => {
      generate.disabled = true;
      generate.setAttribute("aria-busy", "true");
      generate.textContent = "Generating…";
      status.textContent = "Generating your SSH key…";
      next.disabled = true;
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 15000);
      try {
        const response = await fetch(accessForm.getAttribute("action"), {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRFToken": accessForm.querySelector('[name="csrfmiddlewaretoken"]').value,
          },
          body: new URLSearchParams({ action: "generate-key" }),
          signal: controller.signal,
        });
        if (response.redirected || response.status === 401) {
          throw new Error("Your session has expired. Reload this page and sign in again.");
        }
        if (response.status === 403) {
          throw new Error("The security check failed. Reload this page and try again.");
        }
        if (!response.headers.get("Content-Type")?.includes("application/json")) {
          throw new Error(`The server returned an unexpected response (HTTP ${response.status}). Try again.`);
        }
        const result = await response.json();
        if (!response.ok || !result?.public_key) {
          throw new Error(result?.error || "Could not generate a key. Try again.");
        }
        publicKey.value = result.public_key;
        details.hidden = false;
        generate.textContent = "SSH key generated";
        status.textContent = "Key saved. Add the public key to your controller below.";
      } catch (error) {
        generate.disabled = false;
        generate.textContent = "Generate SSH key";
        status.textContent = error.name === "AbortError"
          ? "The request timed out. Try again; an existing key will be kept."
          : error.name === "SyntaxError"
            ? "The server returned invalid data. Try again."
            : error.message || "Could not generate a key. Try again.";
      } finally {
        window.clearTimeout(timeout);
        generate.removeAttribute("aria-busy");
        next.disabled = false;
      }
    });
  }

  const addressForm = document.getElementById("device-address-form");
  if (addressForm) {
    const button = document.getElementById("discover-devices");
    const status = document.getElementById("discovery-status");
    const results = document.getElementById("discovery-results");
    const progress = document.getElementById("discovery-progress");
    const countdown = document.getElementById("discovery-countdown");
    const searched = document.getElementById("discovery-networks");
    const searchLabel = button?.textContent;
    const searchTimeoutMs = 75000;
    const host = document.getElementById("id_host");
    const network = document.getElementById("discovery-network");
    const token = addressForm.querySelector('input[name="csrfmiddlewaretoken"]');

    const renderDevices = (devices) => {
      results.replaceChildren();
      devices.forEach((device) => {
        if (!device.address) return;
        const choice = document.createElement("button");
        choice.type = "button";
        choice.className = "wizard-device-choice";
        choice.dataset.address = device.address;
        choice.setAttribute("aria-pressed", String(host.value === device.address));
        const address = document.createElement("strong");
        address.textContent = device.address;
        const description = document.createElement("span");
        if (device.confirmed === false) {
          description.textContent = device.hostname
            ? `DNS name: ${device.hostname}`
            : "SSH candidate · Model will be checked after sign-in";
        } else {
          description.textContent = [device.name || device.hostname, device.model].filter(Boolean).join(" · ");
        }
        choice.append(address);
        if (device.confirmed === false && device.likely_lte === true) {
          const badge = document.createElement("span");
          badge.className = "wizard-device-badge";
          badge.textContent = "Likely LTE Pro";
          choice.append(badge);
        }
        choice.append(description);
        if (device.confirmed === false && device.hostname) {
          const hint = document.createElement("span");
          hint.className = "wizard-device-hint";
          hint.textContent = device.likely_lte === true
            ? "Model suggested by DNS; checked after sign-in."
            : "Model will be checked after sign-in.";
          choice.append(hint);
        }
        choice.addEventListener("click", () => {
          host.value = device.address;
          results.querySelectorAll(".wizard-device-choice").forEach((item) => {
            item.setAttribute("aria-pressed", String(item === choice));
          });
          host.focus();
          status.textContent = `${device.address} selected. Continue when ready.`;
        });
        results.append(choice);
      });
    };

    button?.addEventListener("click", async () => {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = "Searching…";
      progress.hidden = false;
      const deadline = performance.now() + searchTimeoutMs;
      const updateCountdown = () => {
        const remaining = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
        const minutes = Math.floor(remaining / 60);
        const seconds = String(remaining % 60).padStart(2, "0");
        countdown.textContent = `Time remaining: ${minutes}:${seconds}`;
      };
      updateCountdown();
      const countdownTimer = setInterval(updateCountdown, 1000);
      if (searched) searched.textContent = "";
      status.textContent = "Searching for devices… The search stops when the countdown reaches zero.";
      results.replaceChildren();
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), searchTimeoutMs);
      try {
        const body = new URLSearchParams();
        if (network.value.trim()) body.set("network", network.value.trim());
        const response = await fetch(addressForm.dataset.discoveryUrl, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": token?.value || "" },
          body,
          signal: controller.signal,
        });
        const data = await response.json();
        if (!response.ok || data.error) {
          status.textContent = data.error || "The search could not complete. Enter the address manually.";
          return;
        }
        const devices = Array.isArray(data.devices) ? data.devices : [];
        renderDevices(devices);
        status.textContent = devices.length
          ? `${devices.length} candidate${devices.length === 1 ? "" : "s"} found. Likely LTE Pro devices appear first. Select a device to continue.`
          : "No devices responded. Enter your LAN subnet below and search again, or enter the device address manually.";
        if (searched) searched.textContent = Array.isArray(data.networks) && data.networks.length
          ? `Searched networks: ${data.networks.join(", ")}` : "";
      } catch (_error) {
        status.textContent = controller.signal.aborted
          ? "The search timed out. Enter the address manually or try again."
          : "The search could not complete. Enter the address manually or try again.";
      } finally {
        clearTimeout(timeout);
        clearInterval(countdownTimer);
        progress.hidden = true;
        button.textContent = searchLabel;
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
    });
    host?.addEventListener("input", () => {
      results.querySelectorAll(".wizard-device-choice").forEach((choice) => {
        choice.setAttribute("aria-pressed", String(choice.dataset.address === host.value));
      });
    });
    button?.click();
  }

  const copy = document.getElementById("copy-public-key");
  copy?.addEventListener("click", async () => {
    const source = document.getElementById(copy.dataset.copyTarget);
    const status = document.getElementById("copy-status");
    if (!source || !status) return;
    try {
      await navigator.clipboard.writeText(source.value);
      status.textContent = "Public key copied.";
    } catch (_error) {
      source.focus();
      source.select();
      status.textContent = "Copy was unavailable. Select and copy the public key manually.";
    }
  });
})();
