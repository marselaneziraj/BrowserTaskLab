document.querySelectorAll('form[data-endpoint]').forEach(form => {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    if (button.disabled) return;
    button.disabled = true;
    const errors = form.querySelector('[data-errors]');
    errors.textContent = '';
    try {
      const response = await fetch(location.pathname + '/' + form.dataset.endpoint, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(Object.fromEntries(new FormData(form)))
      });
      const data = await response.json();
      if (!response.ok) { errors.textContent = data.error || 'Validation failed'; button.disabled = false; return; }
      // HTML is produced only by this local server, which escapes every submitted value.
      document.querySelector('#result').outerHTML = data.html;
    } catch (_) { errors.textContent = 'Connection failed. Inspect the page before retrying.'; button.disabled = false; }
  });
});
