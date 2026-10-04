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
})();
