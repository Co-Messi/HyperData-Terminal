// Copy-to-clipboard for the install block. Everything else on the page is static.
(function () {
  var button = document.querySelector('[data-copy]');
  if (!button || !navigator.clipboard) return;

  var source = document.getElementById(button.getAttribute('data-copy'));
  if (!source) return;

  var label = button.textContent;
  var timer = null;

  button.addEventListener('click', function () {
    // Only copy the commands, not the "$ " prompts.
    var text = Array.prototype.map.call(source.querySelectorAll('[data-cmd]'), function (el) {
      return el.textContent;
    }).join('\n');

    navigator.clipboard.writeText(text).then(function () {
      button.textContent = 'Copied';
      button.setAttribute('data-state', 'copied');
      clearTimeout(timer);
      timer = setTimeout(function () {
        button.textContent = label;
        button.removeAttribute('data-state');
      }, 1800);
    }).catch(function () {
      // Clipboard access denied: select the commands so a manual copy is one keystroke away.
      var range = document.createRange();
      range.selectNodeContents(source);
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      button.textContent = 'Selected, copy it';
    });
  });
})();
