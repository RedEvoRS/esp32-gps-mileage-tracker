/* Endpoint requests share one queue. Provider text is always inert text. */
(() => {
  'use strict';
  const useEnd = document.getElementById('use-end-place');
  const endLabel = document.querySelector('[data-endpoint="end"] [data-place-label]');
  const suggestionLabel = document.getElementById('end-suggestion');
  let endSuggestion = useEnd && !useEnd.disabled ? endLabel.textContent : '';
  const destination = document.getElementById('destination');
  const autofillNote = document.getElementById('destination-autofill');
  // Auto lookup may fill an empty Destination with the end suggestion. It never
  // replaces a saved or typed destination, and nothing is saved until Save.
  let autofilled = null;
  let destinationTouched = false;
  if (destination) destination.addEventListener('input', () => {
    destinationTouched = true;
    if (autofillNote) autofillNote.hidden = true;
  });
  function autofill() {
    if (!(autoAllowed && autoLookup) || !destination || !endSuggestion || destinationTouched) return;
    const untouchedFill = autofilled !== null && destination.value === autofilled;
    if (destination.value.trim() !== '' && !untouchedFill) return;
    destination.value = endSuggestion;
    autofilled = endSuggestion;
    if (autofillNote) autofillNote.hidden = false;
  }
  const forms = Array.from(document.querySelectorAll('form[data-place-lookup]'));
  const completed = new Set(forms.filter(form => form.dataset.placeCached === 'true'));
  const failed = new Set();
  const queued = new Set();
  const queue = [];
  let busy = false;
  forms.forEach(form => { form.querySelector('button').disabled = false; });
  function syncSuggestion(form, text) {
    if (form.closest('[data-endpoint]').dataset.endpoint !== 'end') return;
    if (suggestionLabel) suggestionLabel.textContent = 'End: ' +
      (text === 'No mapped address found.' ? 'no mapped address found' : text);
    if (useEnd) useEnd.disabled = true;
  }
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
          failed.add(form);
          label.textContent = data.error || 'Address lookup failed. Try again later.';
        } else {
          failed.delete(form);
          completed.add(form);
          label.textContent = data.label || 'No mapped address found.';
          if (container.dataset.endpoint === 'end' && useEnd) {
            endSuggestion = data.label;
            useEnd.disabled = !endSuggestion;
            autofill();
          }
        }
      } catch (_) {
        failed.add(form);
        label.textContent = 'Address lookup failed. Check your session and try again.';
      } finally {
        if (container.dataset.endpoint === 'end') {
          syncSuggestion(form, label.textContent);
          if (useEnd) useEnd.disabled = failed.has(form) || !endSuggestion;
        }
        queued.delete(form);
        updateAutoLookup();
        form.querySelector('button').removeAttribute('aria-disabled');
      }
    }
    busy = false;
  }
  function enqueue(form) {
    if (queued.has(form)) return;
    failed.delete(form);
    queued.add(form);
    updateAutoLookup();
    queue.push(form);
    syncSuggestion(form, 'looking up\u2026');
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
    if (useEnd.disabled || !endSuggestion || !destination) return;
    destination.value = endSuggestion;
    // Use is an explicit choice: later lookups must not replace it.
    destinationTouched = true;
    if (autofillNote) autofillNote.hidden = true;
    destination.focus();
  });
  const autoAllowed = document.currentScript && document.currentScript.dataset.autoLookup === 'true';
  const autoSwitch = document.getElementById('auto-lookup');
  const autoStatus = document.querySelector('[data-auto-lookup-status]');
  let autoLookup = true;
  try { autoLookup = localStorage.getItem('viewer-auto-lookup') !== 'off'; } catch (_) {}
  function updateAutoLookup() {
    const active = autoAllowed && autoLookup;
    if (both) both.hidden = active;
    forms.forEach(form => {
      const button = form.querySelector('button');
      button.hidden = active && !failed.has(form);
      button.textContent = active && failed.has(form) ? 'Try again' :
        'Look up ' + form.closest('[data-endpoint]').dataset.endpoint + ' place';
    });
    if (autoSwitch) {
      autoSwitch.textContent = autoLookup ? 'Auto lookup: On' : 'Auto lookup: Off';
      autoSwitch.setAttribute('aria-pressed', String(autoLookup));
    }
    if (autoStatus) autoStatus.textContent = autoLookup
      ? 'Uncached endpoints are looked up when a trip opens.'
      : 'Automatic lookup is off in this browser. Nothing is sent until you click.';
  }
  if (document.documentElement) document.documentElement.classList.remove('auto-places');
  if (autoAllowed) {
    updateAutoLookup();
    if (autoSwitch) {
      autoSwitch.addEventListener('click', () => {
        autoLookup = !autoLookup;
        try {
          if (autoLookup) localStorage.removeItem('viewer-auto-lookup');
          else localStorage.setItem('viewer-auto-lookup', 'off');
        } catch (_) {}
        updateAutoLookup();
        if (autoLookup) {
          autofill();
          lookupMissing();
        }
      });
      autoSwitch.hidden = false;
    }
    if (autoLookup) {
      autofill();
      lookupMissing();
    }
  }
})();
