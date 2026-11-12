/* Native radios retain keyboard navigation and a no-script clear choice. */
(() => {
  'use strict';
  const choices = Array.from(document.querySelectorAll('.purpose-choices input'));
  const none = choices.find(input => input.value === '');
  let selected = (choices.find(input => input.checked) || {}).value;
  choices.forEach(input => {
    input.addEventListener('click', () => {
      if (input.value && selected === input.value && none) none.checked = true;
      selected = (choices.find(choice => choice.checked) || {}).value;
    });
    input.addEventListener('change', () => {
      selected = (choices.find(choice => choice.checked) || {}).value;
    });
  });
})();
