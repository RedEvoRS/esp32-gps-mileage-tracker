/* Endpoint requests share one queue. Provider text is always inert text. */
(() => {
  'use strict';
  const useEnd = document.getElementById('use-end-place');
  const endLabel = document.querySelector('[data-endpoint="end"] [data-place-label]');
  let endSuggestion = useEnd && !useEnd.disabled ? endLabel.textContent : '';
  const forms = Array.from(document.querySelectorAll('form[data-place-lookup]'));
  const completed = new Set(forms.filter(form => form.dataset.placeCached === 'true'));
  const queued = new Set();
  const queue = [];
  let busy = false;
  forms.forEach(form => { form.querySelector('button').disabled = false; });
  async function drain() {
    if (busy) return;
    busy = true;
    while (queue.length) {
      const form = queue.shift();
      const container = form.closest('[data-endpoint]');
      const label = container.querySelector('[data-place-label]');
      label.textContent = 'Looking up place…';
      try {
        const response = await fetch(form.action, {
          method: 'POST', credentials: 'same-origin', body: new FormData(form),
        });
        const data = await response.json();
        if (!response.ok || typeof data.label !== 'string') {
          label.textContent = data.error || 'Address lookup failed. Try again later.';
        } else {
          completed.add(form);
          label.textContent = data.label || 'No mapped address found.';
          if (container.dataset.endpoint === 'end' && useEnd) {
            endSuggestion = data.label;
            useEnd.disabled = !endSuggestion;
          }
        }
      } catch (_) {
        label.textContent = 'Address lookup failed. Check your session and try again.';
      } finally {
        queued.delete(form);
        form.querySelector('button').removeAttribute('aria-disabled');
      }
    }
    busy = false;
  }
  function enqueue(form) {
    if (queued.has(form)) return;
    queued.add(form);
    queue.push(form);
    form.querySelector('button').setAttribute('aria-disabled', 'true');
    form.closest('[data-endpoint]').querySelector('[data-place-label]').textContent =
      'Waiting for the other lookup…';
    return drain();
  }
  forms.forEach(form => form.addEventListener('submit', event => {
    event.preventDefault();
    return enqueue(form);
  }));
  function lookupMissing() {
    forms.forEach(form => { if (!completed.has(form)) enqueue(form); });
  }
  const both = document.getElementById('lookup-both-places');
  if (both) {
    both.disabled = false;
    both.addEventListener('click', lookupMissing);
  }
  if (useEnd) useEnd.addEventListener('click', () => {
    if (!endSuggestion) return;
    const destination = document.getElementById('destination');
    destination.value = endSuggestion;
    destination.focus();
  });
  if (document.currentScript && document.currentScript.dataset.autoLookup === 'true') lookupMissing();
})();
