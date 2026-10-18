(() => {
  const action = document.getElementById('id_password_action');
  if (!action || action.disabled) return;

  const password = document.getElementById('id_password');
  const editor = document.getElementById('password-editor');
  const cancel = document.getElementById('cancel-password');
  const remove = document.getElementById('remove-credentials');
  const undo = document.getElementById('undo-removal');
  const fields = document.getElementById('credentials-fields');
  const actions = document.getElementById('credential-actions');
  const notice = document.getElementById('credentials-removal');

  password?.addEventListener('input', () => {
    action.value = password.value ? 'replace' : 'keep';
  });
  if (cancel) {
    cancel.hidden = false;
    cancel.addEventListener('click', () => {
      password.value = '';
      action.value = 'keep';
      editor.open = false;
      editor.querySelector('summary').focus();
    });
    editor.addEventListener('toggle', () => {
      if (!editor.open) {
        password.value = '';
        if (action.value !== 'remove') action.value = 'keep';
      }
    });
  }
  if (remove) {
    remove.hidden = false;
    document.getElementById('remove-help').hidden = false;
    remove.addEventListener('click', () => {
      password.value = '';
      if (editor) editor.open = false;
      action.value = 'remove';
      fields.hidden = true;
      actions.hidden = true;
      notice.hidden = false;
      undo.focus();
    });
  }
  if (undo) {
    undo.hidden = false;
    undo.addEventListener('click', () => {
      action.value = 'keep';
      fields.hidden = false;
      if (actions) actions.hidden = false;
      notice.hidden = true;
      (remove || document.getElementById('id_username')).focus();
    });
  }

  const form = document.getElementById('smtp-settings');
  const testButton = document.getElementById('send-test-email');
  const testStatus = document.getElementById('test-email-status');
  if (form && testButton && testStatus) {
    testButton.hidden = false;
    testButton.addEventListener('click', async () => {
      if (testButton.disabled) return;

      const saveButton = form.querySelector('button[type="submit"]');
      const previousTestDisabled = testButton.disabled;
      const previousSaveDisabled = saveButton?.disabled;
      const previousLabel = testButton.textContent;
      const payload = new FormData(form);
      payload.set('action', 'test-email');
      const csrfToken = form.querySelector('[name="csrfmiddlewaretoken"]')?.value;
      const headers = { Accept: 'application/json' };
      if (csrfToken) headers['X-CSRFToken'] = csrfToken;

      testButton.disabled = true;
      if (saveButton) saveButton.disabled = true;
      testButton.textContent = 'Sending…';
      testStatus.hidden = false;
      testStatus.setAttribute('aria-live', 'polite');
      testStatus.classList.remove('is-error', 'is-success');
      testStatus.textContent = 'Sending test email…';

      try {
        const response = await fetch(form.getAttribute('action') || window.location.pathname, {
          method: 'POST',
          headers,
          body: new URLSearchParams(payload),
          credentials: 'same-origin',
        });

        if (response.redirected) {
          throw new Error('The request was redirected instead of returning a test result. Reload the page and try again.');
        }
        if (response.status === 401) {
          throw new Error('Your session could not be authenticated. Sign in again and retry.');
        }
        if (response.status === 403) {
          throw new Error('The request was denied (HTTP 403). Check your access and try again.');
        }

        let result;
        try {
          result = await response.json();
        } catch (_error) {
          throw new Error(`The server returned an unexpected response (HTTP ${response.status}).`);
        }

        if (response.status === 200 && result?.ok === true) {
          testStatus.textContent = typeof result.message === 'string' ? result.message : 'Test email sent.';
          testStatus.classList.add('is-success');
          return;
        }

        const details = [];
        if (result?.errors && typeof result.errors === 'object') {
          Object.values(result.errors).forEach((fieldErrors) => {
            if (!Array.isArray(fieldErrors)) return;
            fieldErrors.forEach((entry) => {
              if (typeof entry?.message === 'string' && entry.message) details.push(entry.message);
            });
          });
        }
        const message = typeof result?.message === 'string' && result.message
          ? result.message
          : `The test email request failed (HTTP ${response.status}).`;
        testStatus.textContent = [message, ...details].join(' ');
        testStatus.classList.add('is-error');
        testStatus.setAttribute('aria-live', 'assertive');
      } catch (error) {
        testStatus.textContent = error instanceof TypeError
          ? 'Could not reach the server. Check your connection and try again.'
          : (error.message || 'The test email request could not be completed.');
        testStatus.classList.add('is-error');
        testStatus.setAttribute('aria-live', 'assertive');
      } finally {
        testButton.disabled = previousTestDisabled;
        if (saveButton) saveButton.disabled = previousSaveDisabled;
        testButton.textContent = previousLabel;
      }
    });
  }
})();
