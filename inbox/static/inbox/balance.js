(() => {
  const id = (name) => document.getElementById(name);
  const app = id('balance-app');
  const token = document.querySelector('#csrf input').value;
  const working = new Set(['detecting', 'queued', 'requesting', 'waiting', 'activating']);
  const stages = {carrier: 0, manual: 0, testing: 1, manual_result: 2, verified: 2, settings: 3};
  let data, poll, clock, pending;
  let screen = 'loading', settingsMode = 'enable', manualBack = 'carrier';
  let posting = false;

  const value = (name) => id(name).value.trim();
  const say = (name, message) => {
    const element = id(name);
    if (element.textContent !== message) element.textContent = message;
  };
  const date = (timestamp) => timestamp ? new Date(timestamp).toLocaleString() : 'Not yet';
  const money = (amount, currency) => amount == null ? 'Not available' : [amount, currency].filter(Boolean).join(' ');
  const frequency = (hours) => hours === 1 ? 'Every hour' : 'Every ' + hours + ' hours';

  function show(name) {
    if (screen === name) return;
    screen = name;
    app.querySelectorAll('[data-screen]').forEach((element) => {
      element.hidden = element.dataset.screen !== name;
    });
    const stage = name === 'testing' && data.wizard.state === 'detecting' ? 0 :
      name === 'testing' && data.wizard.state === 'activating' ? 3 : stages[name];
    id('balance-steps').hidden = stage === undefined || (name === 'settings' && settingsMode === 'settings');
    [...id('balance-steps').children].forEach((step, index) => {
      if (index === stage) step.setAttribute('aria-current', 'step');
      else step.removeAttribute('aria-current');
      step.classList.toggle('complete', index < stage);
    });
  }

  function showError(message) {
    say('balance-error', message);
    id('balance-error').hidden = !message;
  }

  function schedule() {
    clearTimeout(poll);
    const active = !data || working.has(data.wizard.state) || data.summary.checking;
    poll = setTimeout(() => request(), active ? 2000 : 15000);
  }

  async function request(action, payload = {}) {
    if (posting) return;
    clearTimeout(poll);
    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    posting = Boolean(action);
    if (posting) {
      showError('');
      app.classList.add('submitting');
    }
    const options = action ? {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': token},
      body: JSON.stringify({action, ...payload}),
    } : {};
    try {
      const response = await fetch(action ? app.dataset.actionUrl : app.dataset.statusUrl, {
        ...options, cache: 'no-store', credentials: 'same-origin', signal: controller.signal,
      });
      if (response.redirected || response.status === 403 && !action) {
        location.assign(response.redirected ? response.url : '/login/');
        return;
      }
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not load balance settings.');
      render(result, action);
      if (!action && id('balance-error').dataset.pollError) showError('');
      delete id('balance-error').dataset.pollError;
    } catch (error) {
      if (error.name !== 'AbortError') {
        if (!action) id('balance-error').dataset.pollError = 'true';
        showError(error instanceof SyntaxError ? 'Could not load balance settings.' : error.message);
      }
    } finally {
      if (pending === controller) {
        pending = null;
        posting = false;
        app.classList.remove('submitting');
        schedule();
      }
    }
  }

  function renderOverview(summary) {
    const settings = summary.settings;
    say('inactive-detail', summary.detail || 'Set up a carrier profile to check your SIM balance.');
    say('summary-amount', money(summary.amount, summary.currency));
    id('summary-low').hidden = !summary.low;
    say('summary-detail', [
      summary.checking ? 'Balance check in progress.' : '',
      summary.stale ? 'Showing the last confirmed balance.' : '',
      summary.detail,
    ].filter(Boolean).join(' '));
    app.querySelector('[data-action="check"]').disabled = summary.checking;
    say('summary-checked', date(summary.checked_at));
    say('summary-next', summary.checking ? 'In progress' : date(summary.next_check_at));
    say('summary-interval', summary.interval_hours ? frequency(summary.interval_hours) : '');
    say('summary-profile', summary.profile);
    say('summary-method', summary.method);
    say('summary-threshold', settings.threshold == null ? 'No threshold set' :
      'Alert at or below ' + money(settings.threshold, summary.currency));
    say('summary-email', settings.email || 'Not configured');
    say('summary-phone', settings.phone || 'Not configured');
    const alertLabels = {
      pending: 'Pending', sending: 'Sending', sent: 'Accepted by the delivery service', failed: 'Failed', unknown: 'Delivery status unknown',
    };
    for (const channel of ['email', 'sms']) {
      const state = summary.alerts[channel];
      say('summary-' + channel + '-state', alertLabels[state] || state || '');
    }
  }

  function selectedCarrier() {
    return data.carriers.find((carrier) => carrier.id === value('carrier'));
  }

  function renderSenderField() {
    const profiles = selectedCarrier()?.profiles || [];
    const needsSender = profiles.some((profile) => profile.transport !== 'ussd');
    const onlySms = needsSender && profiles.every((profile) => profile.transport !== 'ussd');
    id('auto-sender-field').hidden = !needsSender;
    id('auto-sender').required = onlySms;
    say('auto-sender-help', (onlySms ? '' : 'Optional for SMS fallback. ') +
      'Enter the number or sender name as it appears in received SMS.');
  }

  function renderCarriers(wizard) {
    const select = id('carrier');
    const matching = data.carriers.filter((carrier) => wizard.network && carrier.network.startsWith(wizard.network));
    const suggested = matching.length === 1 ? matching[0].id : '';
    select.replaceChildren(new Option('Select a carrier and plan', ''), ...data.carriers.map(
      (carrier) => new Option(carrier.label, carrier.id)
    ));
    select.value = suggested;
    id('auto-sender').value = '';
    renderSenderField();
  }

  function renderTest(wizard) {
    const {state, attempts, total, detail, deadline} = wizard;
    const failed = state === 'failed';
    say('test-heading', {
      detecting: 'Identifying the SIM', activating: 'Enabling monitoring', failed: 'Could not verify the profile',
    }[state] || 'Testing balance requests');
    say('test-detail', detail || 'Checking the carrier profile…');
    id('retry-setup').hidden = !failed;
    id('test-manual').hidden = !failed || !wizard.network && !attempts.length;
    say('test-cancel', failed ? 'Close' : 'Cancel setup');

    const progress = id('progress');
    progress.hidden = !total || state === 'activating';
    progress.max = total || 1;
    progress.value = attempts.filter((attempt) => !working.has(attempt.state)).length;
    id('attempts').replaceChildren(...attempts.map((attempt) => {
      const row = document.createElement('li');
      const label = document.createElement('strong');
      label.textContent = attempt.label;
      row.append(label, document.createTextNode(' · ' + (attempt.detail || 'Sending request…')));
      return row;
    }));

    clearInterval(clock);
    id('countdown').hidden = !deadline;
    if (deadline) {
      const update = () => {
        const seconds = Math.max(0, Math.ceil((new Date(deadline) - Date.now()) / 1000));
        say('countdown', 'Time remaining ' + Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0'));
      };
      update();
      clock = setInterval(update, 1000);
    }
  }

  function render(result, action) {
    const previous = data?.wizard.state;
    data = result;
    const {summary, wizard} = result;
    renderOverview(summary);
    if (working.has(wizard.state) || wizard.state === 'failed') {
      renderTest(wizard);
      if (screen === 'testing' && previous !== wizard.state) screen = '';
      if (screen !== 'manual' || previous !== wizard.state) show('testing');
    } else {
      clearInterval(clock);
      if (wizard.state === 'carrier') {
        if (previous !== 'carrier') renderCarriers(wizard);
        say('carrier-detail', wizard.detail);
        if (screen !== 'manual') show('carrier');
      } else if (wizard.state === 'manual_result') {
        say('reply', wizard.reply);
        if (previous !== 'manual_result') {
          id('recognize-currency').value = wizard.currency || value('currency');
          id('recognize-marker').value = wizard.marker || value('marker');
        }
        if (screen !== 'manual') show('manual_result');
      } else if (wizard.state === 'verified') {
        say('verified-method', [wizard.profile, wizard.method].filter(Boolean).join(' · '));
        say('verified-amount', money(wizard.amount, wizard.currency));
        if (screen !== 'settings' && screen !== 'manual') show('verified');
      } else if (action || screen !== 'settings') {
        show(summary.enabled ? 'overview' : 'inactive');
      }
    }
  }

  function updateTransport() {
    const sms = value('transport') === 'sms';
    const ussd = value('transport') === 'ussd';
    id('code-field').hidden = sms;
    id('destination-field').hidden = id('text-field').hidden = !sms;
    id('sender-field').hidden = ussd;
    id('code').required = !sms;
    id('destination').required = sms;
    id('sender').required = !ussd;
  }

  function openManual() {
    showError('');
    manualBack = screen;
    if (screen === 'carrier') {
      const profile = selectedCarrier()?.profiles[0];
      if (profile) {
        id('transport').value = profile.transport;
        for (const field of ['code', 'destination', 'text', 'currency']) id(field).value = profile[field] || '';
        id('sender').value = value('auto-sender');
        id('marker').value = '';
      }
    }
    updateTransport();
    show('manual');
  }

  function updateSettingsFields() {
    const custom = value('interval') === 'custom';
    id('custom-interval-field').hidden = !custom;
    id('custom-interval').required = custom;
    let alerts = false;
    for (const [channel, field] of [['email', 'email'], ['sms', 'phone']]) {
      const enabled = id(channel + '-enabled').checked;
      id(field + '-field').hidden = !enabled;
      id(field).required = enabled;
      alerts ||= enabled;
    }
    id('threshold').required = alerts;
  }

  function openSettings(mode) {
    settingsMode = mode;
    const enabling = mode === 'enable';
    const settings = data.summary.settings;
    const profile = enabling ? data.wizard : data.summary;
    say('threshold-label', 'Alert threshold (' + profile.currency + ')');
    id('threshold').value = settings.threshold ?? '';
    id('threshold').placeholder = 'Optional';
    const defaults = profile.method === 'USSD' ? 6 : 24;
    id('interval').options[0].textContent = frequency(defaults) + ' (method default)';
    const interval = String(settings.interval_hours ?? '');
    id('interval').value = ['', '1', '6', '24', '168'].includes(interval) ? interval : 'custom';
    id('custom-interval').value = interval;
    for (const [channel, field, dependency] of [['email', 'email', 'smtp'], ['sms', 'phone', 'sms']]) {
      const checkbox = id(channel + '-enabled');
      const available = data.dependencies[dependency];
      checkbox.checked = Boolean(settings[field]);
      checkbox.disabled = !available && !checkbox.checked;
      id(field).value = settings[field] || '';
      id(channel + '-guidance').hidden = available;
    }
    say('save-settings', enabling ? 'Enable monitoring' : 'Save settings');
    showError('');
    updateSettingsFields();
    show('settings');
  }

  for (const button of app.querySelectorAll('[data-action]')) {
    button.addEventListener('click', () => request(button.dataset.action));
  }
  for (const button of app.querySelectorAll('[data-manual]')) button.addEventListener('click', openManual);
  id('carrier').addEventListener('change', () => { id('auto-sender').value = ''; renderSenderField(); });
  id('manual-back').addEventListener('click', () => { showError(''); show(manualBack); });
  id('transport').addEventListener('change', updateTransport);
  for (const name of ['interval', 'email-enabled', 'sms-enabled']) id(name).addEventListener('change', updateSettingsFields);
  id('use-profile').addEventListener('click', () => openSettings('enable'));
  id('edit-settings').addEventListener('click', () => openSettings('settings'));
  id('cancel-settings').addEventListener('click', () => {
    showError('');
    show(settingsMode === 'enable' ? 'verified' : 'overview');
  });
  id('disable').addEventListener('click', () => {
    if (confirm('Disable SIM balance monitoring?')) request('disable');
  });
  id('carrier-form').addEventListener('submit', (event) => {
    event.preventDefault();
    request('auto', {carrier: value('carrier'), sender: id('auto-sender-field').hidden ? '' : value('auto-sender')});
  });
  id('manual-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const transport = value('transport');
    const fields = transport === 'sms' ? {destination: value('destination'), text: value('text')} : {code: value('code')};
    if (transport !== 'ussd') fields.sender = value('sender');
    request('manual', {...fields, transport, currency: value('currency').toUpperCase(), marker: value('marker')});
  });
  id('recognize-form').addEventListener('submit', (event) => {
    event.preventDefault();
    request('recognize', {currency: value('recognize-currency').toUpperCase(), marker: value('recognize-marker')});
  });
  id('settings-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const interval = value('interval');
    request(settingsMode, {
      interval_hours: interval === 'custom' ? value('custom-interval') : interval,
      threshold: value('threshold'),
      email: id('email-enabled').checked ? value('email') : '',
      phone: id('sms-enabled').checked ? value('phone') : '',
    });
  });

  request();
})();
