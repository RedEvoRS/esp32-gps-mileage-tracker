/* Endpoint lookups are explicit actions. Provider text is always inert text. */
(() => {
  'use strict';
  const useEnd = document.getElementById('use-end-place');
  const endLabel = document.querySelector('[data-endpoint="end"] [data-place-label]');
  let endSuggestion = useEnd && !useEnd.disabled ? endLabel.textContent : '';
  const forms = Array.from(document.querySelectorAll('form[data-place-lookup]'));
  forms.forEach(form => { form.querySelector('button').disabled = false; });
  let busy = false;
  forms.forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    const container = form.closest('[data-endpoint]');
    const label = container.querySelector('[data-place-label]');
    forms.forEach(item => { item.querySelector('button').disabled = true; });
    label.textContent = 'Looking up place…';
    try {
      const response = await fetch(form.action, {
        method: 'POST', credentials: 'same-origin', body: new FormData(form),
      });
      const data = await response.json();
      if (!response.ok || typeof data.label !== 'string') {
        // Upstream text is never used as an error. API messages are fixed.
        label.textContent = data.error || 'Address lookup failed. Try again later.';
      } else {
        label.textContent = data.label || 'No mapped address found.';
        if (container.dataset.endpoint === 'end' && useEnd) {
          endSuggestion = data.label;
          useEnd.disabled = !endSuggestion;
        }
      }
    } catch (_) {
      label.textContent = 'Address lookup failed. Check your session and try again.';
    } finally {
      busy = false;
      forms.forEach(item => { item.querySelector('button').disabled = false; });
    }
  }));
  if (useEnd) useEnd.addEventListener('click', () => {
    if (!endSuggestion) return;
    const destination = document.getElementById('destination');
    destination.value = endSuggestion;
    destination.focus();
  });
})();
