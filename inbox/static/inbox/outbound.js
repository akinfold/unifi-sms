(() => {
  'use strict';
  const root = document.getElementById('outbound');
  const el = (id) => document.getElementById(id);
  const pages = [...root.querySelectorAll('[data-page]')];
  const csrf = root.querySelector('[name=csrfmiddlewaretoken]').value;
  const recipient = el('recipient');
  const message = el('message');
  const draftKey = 'outbound-sms-draft';
  let status = {};
  let pending = false;
  let timer = null;
  let result = 'idle';
  let jobId = sessionStorage.getItem('outbound-sms-job') || '';
  let currentPage = null;
  let generation = 0;
  let pollSequence = 0;
  let deadline = null;
  let keepCompose = false;
  let dismissedFailure = false;
  let uncertain = sessionStorage.getItem('outbound-sms-uncertain') === '1';
  try {
    const draft = JSON.parse(sessionStorage.getItem(draftKey) || '{}');
    recipient.value = draft.recipient || '';
    message.value = draft.message || '';
  } catch (_) {}
  function saveDraft() {
    try { sessionStorage.setItem(draftKey, JSON.stringify({recipient: recipient.value, message: message.value})); } catch (_) {}
  }
  recipient.addEventListener('input', saveDraft);
  message.addEventListener('input', saveDraft);
  function show(name) {
    if (name === 'number' && currentPage !== 'number') {
      const sim = status.phone || status.detected_number || '';
      el('sim-number').value = sim;
      el('sim-note').textContent = status.detected_number && sim === status.detected_number
        ? 'Detected on the LTE device. You can edit it.'
        : 'Use an international number starting with + and the country code.';
    }
    currentPage = name;
    pages.forEach((section) => { section.hidden = section.dataset.page !== name; });
    el('page-title').textContent = ['number', 'testing', 'verified', 'failed'].includes(name) ? 'Set up SMS sending' : 'Send SMS';
    el('step-label').textContent = ({start: 'Setup required', number: 'Step 1 of 3', testing: 'Step 2 of 3', failed: 'Step 2 of 3', verified: 'Step 3 of 3'})[name] || '';
    el('rerun').hidden = name !== 'compose';
  }
  function error(text) { el('connection-error').textContent = text; el('connection-error').hidden = !text; }
  const profileNames = {qmi_ims_off: 'QMI · IMS off', qmi_ims_on: 'QMI · IMS on', at_pdu: 'AT commands · PDU mode'};
  function profileName(id) { return profileNames[id] || id || ''; }
  function updateCountdown() {
    const seconds = deadline ? Math.max(0, Math.ceil((deadline - Date.now()) / 1000)) : null;
    el('deadline').textContent = seconds === null || Number.isNaN(seconds) ? '' : `Time remaining: ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  }
  setInterval(updateCountdown, 1000);
  function normalizePhone(input) {
    const value = input.value.trim().replace(/[ ()\-.]/g, '');
    input.setCustomValidity(/^\+[1-9][0-9]{1,14}$/.test(value) ? '' : 'Enter an international number starting with + and the country code (2–15 digits).');
    if (!input.reportValidity()) return null;
    return value;
  }
  const attemptLabels = {
    queued: 'Queued', sending: 'Sending', waiting: 'Awaiting test SMS',
    verified: 'Verified', failed: 'Failed', timed_out: 'Timed out',
  };
  function renderProfiles(target, profiles) {
    target.replaceChildren();
    for (const profile of profiles || []) {
      const li = document.createElement('li');
      const name = document.createElement('strong');
      const detail = document.createElement('span');
      const attempt = (status.attempts || []).find((item) => item.profile === profile);
      name.textContent = profileName(profile);
      detail.textContent = attempt ? [attemptLabels[attempt.state], attempt.detail].filter(Boolean).join(' · ') : 'Queued';
      li.append(name, detail); target.append(li);
    }
  }
  function render(data) {
    if (currentPage === 'compose' && status.enabled && !data.enabled) {
      keepCompose = true;
      sendState('failed', 'SMS sending is unavailable', data.detail || 'Run setup again before sending.');
    }
    status = data;
    const state = data.state || 'idle';
    const sim = data.phone || data.detected_number || '';
    root.querySelectorAll('.sim-value').forEach((node) => { node.textContent = sim; });
    root.querySelectorAll('.device-name').forEach((node) => { node.textContent = data.device_model || 'LTE device'; });
    const attempts = data.attempts || [];
    const active = attempts.length;
    el('profile-count').textContent = active ? `Profile ${active} of ${data.profiles.length}` : 'Preparing profiles';
    el('progress').max = Math.max(data.profiles.length, 1);
    el('progress').value = attempts.filter((item) => !['queued', 'sending', 'waiting'].includes(item.state)).length;
    el('activity').textContent = data.detail || (state === 'waiting' ? 'Waiting for the test SMS…' : state === 'discovering' ? 'Detecting SIM number…' : '');
    deadline = data.deadline ? Date.parse(data.deadline) : null;
    updateCountdown();
    el('winning-profile').textContent = profileName(attempts.length ? attempts[attempts.length - 1].profile : data.profile);
    el('saved-profile').textContent = profileName(data.profile);
    el('failure-detail').textContent = data.detail || 'Check the SIM number and try again.';
    renderProfiles(el('profiles'), data.profiles);
    renderProfiles(el('failed-profiles'), data.profiles);
    if (keepCompose) show('compose');
    else if (['discovering', 'queued', 'sending', 'waiting'].includes(state)) show('testing');
    else if (state === 'number') show('number');
    else if (state === 'verified') show('verified');
    else if (state === 'failed' && !dismissedFailure) show('failed');
    else show(data.enabled ? 'compose' : 'start');
    if (data.job && data.job.id === jobId) renderJob(data.job);
  }
  function renderJob(job) {
    if (['queued', 'sending'].includes(job.status)) sendState('pending', 'Sending SMS', job.detail || 'Waiting for the device…');
    else if (job.status === 'accepted') { sendState('success', 'SMS sent', job.detail || 'The device accepted the message.'); }
    else if (job.status === 'failed') { keepCompose = true; show('compose'); sendState('failed', 'Couldn’t send the SMS', job.detail || 'The device rejected the message. Try again.'); jobId = ''; sessionStorage.removeItem('outbound-sms-job'); }
    else if (job.status === 'unknown') sendState('unknown', 'Send status unknown', job.detail || 'The message may have been sent. Check before sending it again.');
  }
  async function request(url, options = {}) {
    const response = await fetch(url, {credentials: 'same-origin', ...options});
    const body = await response.json();
    if (!response.ok) { const failure = new Error(body.detail || body.error || 'The request failed.'); failure.rejected = true; throw failure; }
    return body;
  }
  async function refresh() {
    clearTimeout(timer);
    const version = generation;
    const sequence = ++pollSequence;
    try {
      const data = await request(root.dataset.statusUrl + (jobId ? '?job_id=' + encodeURIComponent(jobId) : ''));
      if (version === generation && sequence === pollSequence && !pending) { render(data); error(''); }
    } catch (_) {
      if (version === generation && sequence === pollSequence && !pending) error('Could not load the sending status. Please try again.');
    }
    if (sequence === pollSequence) timer = setTimeout(refresh, 3000);
  }
  async function action(name, values = {}) {
    if (pending) return null;
    pending = true;
    generation++;
    try {
      const body = await request(root.dataset.actionUrl, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf}, body: JSON.stringify({action: name, ...values})});
      error(''); return body;
    } finally { pending = false; }
  }
  function makeKey() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  function sendState(state, title = '', detail = '') {
    result = state;
    el('send-result').hidden = state === 'idle';
    el('send-result').className = 'outbound-result' + (state === 'success' ? ' good' : state === 'failed' || state === 'unknown' ? ' bad' : '');
    el('send-title').textContent = title; el('send-detail').textContent = detail;
    el('send-button').disabled = ['pending', 'success', 'unknown'].includes(state);
    recipient.readOnly = message.readOnly = ['pending', 'success', 'unknown'].includes(state);
    el('send-button').textContent = state === 'pending' ? 'Sending…' : state === 'failed' ? 'Try again' : 'Send SMS';
    el('new-message').hidden = !['success', 'unknown'].includes(state);
  }
  root.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const name = button.dataset.action;
    if (name === 'open') { keepCompose = false; dismissedFailure = false; button.disabled = true; try { await action('prepare'); await refresh(); } catch (err) { error(err.message); } finally { button.disabled = false; } return; }
    if (name === 'close') { dismissedFailure = true; show(status.enabled ? 'compose' : 'start'); return; }
    button.disabled = true;
    try { await action(name); await refresh(); }
    catch (err) { error(err.message); }
    finally { button.disabled = false; }
  });
  el('rerun').addEventListener('click', async () => { keepCompose = false; dismissedFailure = false; try { await action('prepare'); await refresh(); } catch (err) { error(err.message); } });
  el('number-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const phone = normalizePhone(el('sim-number'));
    if (!phone) return;
    try { await action('start', {phone}); await refresh(); } catch (err) { error(err.message); }
  });
  el('sim-number').addEventListener('input', () => el('sim-number').setCustomValidity(''));
  recipient.addEventListener('input', () => recipient.setCustomValidity(''));
  el('send-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!['idle', 'failed'].includes(result)) return;
    const to = normalizePhone(recipient);
    if (!to) return;
    saveDraft(); sessionStorage.setItem('outbound-sms-uncertain', '1'); uncertain = true; sendState('pending', 'Sending SMS', 'Waiting for the device…');
    try {
      const response = await action('send', {to, text: message.value, idempotency_key: makeKey()});
      if (!response.job || !response.job.id) throw new Error('Missing job');
      jobId = response.job.id; sessionStorage.setItem('outbound-sms-job', jobId); sessionStorage.removeItem('outbound-sms-uncertain'); uncertain = false;
      renderJob(response.job);
    } catch (err) {
      if (err.rejected) { keepCompose = true; show('compose'); sessionStorage.removeItem('outbound-sms-uncertain'); uncertain = false; sendState('failed', 'Couldn’t send the SMS', err.message); }
      else sendState('unknown', 'Send status unknown', 'The message may have been sent. Check before sending it again.');
    }
  });
  el('new-message').addEventListener('click', () => {
    if (result === 'success') { recipient.value = ''; message.value = ''; saveDraft(); }
    jobId = ''; sessionStorage.removeItem('outbound-sms-job'); sessionStorage.removeItem('outbound-sms-uncertain'); uncertain = false;
    sendState('idle');
  });
  if (uncertain) sendState('unknown', 'Send status unknown', 'The message may have been sent. Check before sending it again.');
  else if (jobId) sendState('pending', 'Checking SMS status', 'Checking the previous send…');
  refresh();
})();
