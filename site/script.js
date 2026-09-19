/* HyperData Terminal — site script.
   Vanilla JS, no dependencies. Everything degrades to a complete static page
   when prefers-reduced-motion is set or when JS is unavailable. */
(function () {
  'use strict';
  window.__hdReady = true;   // tells the inline <head> fallback that reveals will run

  var reduceMq = window.matchMedia('(prefers-reduced-motion: reduce)');
  function reduced() { return reduceMq.matches; }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function rand(a, b) { return a + Math.random() * (b - a); }
  function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function pad(s, n) { s = String(s); while (s.length < n) s += ' '; return s; }
  function lpad(s, n) { s = String(s); while (s.length < n) s = ' ' + s; return s; }

  /* ---------- Scroll reveals (once) ---------- */
  var revealTargets = document.querySelectorAll('.rv, .rule');
  if (reduced() || !('IntersectionObserver' in window)) {
    revealTargets.forEach(function (el) { el.classList.add('in'); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    revealTargets.forEach(function (el) { io.observe(el); });
  }

  /* ---------- Count-ups ---------- */
  function countUp(el) {
    var target = parseInt(el.getAttribute('data-count'), 10);
    var suffix = el.getAttribute('data-suffix') || '';
    if (reduced()) { el.textContent = target + suffix; return; }
    var dur = 1400, start = null;
    function ease(t) { return t === 1 ? 1 : 1 - Math.pow(2, -10 * t); }
    function frame(ts) {
      if (start === null) start = ts;
      var t = Math.min(1, (ts - start) / dur);
      el.textContent = Math.round(ease(t) * target) + (t === 1 ? suffix : '');
      if (t < 1) requestAnimationFrame(frame);
    }
    el.textContent = '0';
    requestAnimationFrame(frame);
  }
  var counters = document.querySelectorAll('[data-count]');
  if (counters.length) {
    if (reduced() || !('IntersectionObserver' in window)) {
      counters.forEach(countUp);
    } else {
      var cio = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) { countUp(e.target); cio.unobserve(e.target); }
        });
      }, { threshold: 0.4 });
      counters.forEach(function (el) { cio.observe(el); });
    }
  }

  /* ---------- Copy button ---------- */
  document.querySelectorAll('.copy[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var src = document.getElementById(btn.getAttribute('data-copy'));
      if (!src) return;
      var text = Array.prototype.map.call(src.querySelectorAll('[data-cmd]'), function (n) { return n.textContent; }).join('\n');
      function done() {
        btn.textContent = 'Copied';
        btn.classList.add('done');
        setTimeout(function () { btn.textContent = 'Copy'; btn.classList.remove('done'); }, 1800);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { fallback(); });
      } else { fallback(); }
      function fallback() {
        var ta = document.createElement('textarea');
        ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'absolute'; ta.style.left = '-9999px';
        document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); done(); } catch (e) { /* leave button as is */ }
        document.body.removeChild(ta);
      }
    });
  });

  /* ---------- Terminal replay ---------- */
  var out = document.getElementById('term-out');
  if (!out) return;
  var dot = document.getElementById('term-dot');
  var clockEl = document.getElementById('term-clock');
  var cur = document.createElement('span');
  cur.className = 'term-cur';

  var clock = { h: 15, m: 22, s: 36 };
  function clockText() { return lpad(clock.h, 2).replace(' ', '0') + ':' + lpad(clock.m, 2).replace(' ', '0') + ':' + lpad(clock.s, 2).replace(' ', '0') + ' UTC'; }
  function tickClock() {
    clock.s += 1;
    if (clock.s === 60) { clock.s = 0; clock.m += 1; }
    if (clock.m === 60) { clock.m = 0; clock.h = (clock.h + 1) % 24; }
    if (clockEl) clockEl.textContent = clockText();
    var hdr = out.querySelector('[data-clock]');
    if (hdr) hdr.textContent = clockText().slice(0, 8);
  }
  if (clockEl) clockEl.textContent = clockText();

  var screen = out.parentNode;
  function line(html) {
    var ln = document.createElement('span');
    ln.className = 'ln';
    ln.innerHTML = html;
    out.appendChild(ln);
    screen.scrollTop = screen.scrollHeight;
    return ln;
  }
  function withCursor(ln) { ln.appendChild(cur); screen.scrollTop = screen.scrollHeight; }
  window.addEventListener('resize', function () { screen.scrollTop = screen.scrollHeight; });

  var menu = [
    ['1', 'Liquidation Watch', 'BTC positions closest to liquidation'],
    ['2', 'Liquidation Stream', 'Multi-exchange liquidation feed'],
    ['3', 'Liquidation Heatmap', 'Price-level liquidation risk (all cryptos)'],
    ['4', 'CVD Order Flow', 'BTC cumulative volume delta & signals'],
    ['5', 'Market Overview', 'Funding rates, OI, prices for all assets'],
    ['6', 'Whale Tracker', 'Largest open positions on Hyperliquid'],
    ['0', 'All Dashboards', 'Combined view, everything at once']
  ];
  var components = [
    ['Liquidation Feed', '4 exchanges: Hyperliquid, Binance, Bybit, OKX'],
    ['Order Flow Engine', 'WebSocket trades for 8 symbols'],
    ['Position Scanner', 'Whale tracking + liquidation distance calc'],
    ['Market Data', '50 assets: prices, funding, OI']
  ];
  var rows = [
    { sym: 'BTC', price: 71171.00, dp: 2, chg: 4.42, fund: 0.0013, oi: '$1.95B' },
    { sym: 'ETH', price: 2206.20, dp: 2, chg: 6.08, fund: 0.0013, oi: '$1.32B' },
    { sym: 'SOL', price: 83.163, dp: 3, chg: 5.40, fund: 0.0013, oi: '$288.31M' },
    { sym: 'HYPE', price: 38.697, dp: 3, chg: 7.04, fund: 0.0013, oi: '$814.31M' }
  ];
  function fmtPrice(r) {
    var s = r.price.toFixed(r.dp);
    var parts = s.split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return parts.join('.');
  }
  function fmtChg(r) { return (r.chg >= 0 ? '+' : '') + r.chg.toFixed(2) + '%'; }
  function fmtFund(r) { return (r.fund >= 0 ? '+' : '') + r.fund.toFixed(4) + '%'; }

  function menuLine(m) {
    return '  <span class="a">[' + m[0] + ']</span>  <span class="k">' + pad(esc(m[1]), 20) + '</span><span class="td d">' + esc(m[2]) + '</span>';
  }
  function componentLine(c) {
    return '  <span class="p">-</span> <span class="k">' + pad(esc(c[0]), 19) + '</span><span class="td d">' + pad(esc(c[1]), 46) + '</span><span class="a">ready</span>';
  }
  function marketRow(r, i) {
    return '  <span class="k">' + pad(r.sym, 6) + '</span>' +
      '<span class="cell" data-cell="price" data-i="' + i + '">' + lpad(fmtPrice(r), 10) + '</span>' +
      '<span class="cell" data-cell="chg" data-i="' + i + '">' + lpad(fmtChg(r), 10) + '</span>' +
      '<span class="tf d">' + lpad(fmtFund(r), 11) + '</span>' +
      '<span class="d">' + lpad(r.oi, 11) + '</span>';
  }
  var marketHeader = '  <span class="m">' + pad('SYM', 6) + lpad('PRICE', 10) + lpad('CHG', 10) + '</span><span class="tf m">' + lpad('FUND', 11) + '</span><span class="m">' + lpad('OI', 11) + '</span>';
  function marketTitle() {
    return '<span class="a">MARKET</span>  <span class="d">4 of 50 assets</span>   <span class="d">OI</span> <span class="a">$5.60B</span>   <span class="d">Vol</span> <span class="a">$7.27B</span>   <span class="m" data-clock>' + clockText().slice(0, 8) + '</span>';
  }

  var bar = '█';
  function progressLine(pct) {
    var n = Math.round(pct / 4);
    var filled = ''; for (var i = 0; i < n; i++) filled += bar;
    var empty = ''; for (var j = n; j < 25; j++) empty += '░';
    return '  <span class="d">Starting hub</span>  <span class="a">' + filled + '</span><span class="m">' + empty + '</span>  <span class="a">' + lpad(pct, 3) + '%</span>';
  }

  /* Static final state, used for reduced motion (and as the layout reference). */
  function renderStatic() {
    line('<span class="p">~ %</span> hyperdata');
    line('');
    line('<span class="a">HYPERDATA</span>  <span class="td d">Hyperliquid trading data terminal  </span><span class="m">v1.0</span>');
    line('<span class="m">' + '─'.repeat(46) + '</span>');
    components.forEach(function (c) { line(componentLine(c)); });
    line(progressLine(100));
    line('');
    menu.forEach(function (m) { line(menuLine(m)); });
    line('');
    line('  <span class="d">Select:</span> 5');
    line('');
    line(marketTitle());
    line(marketHeader);
    rows.forEach(function (r, i) { line(marketRow(r, i)); });
    line('');
    var last = line('  <span class="m">Press Ctrl+C to return to menu.</span> ');
    withCursor(last);
    if (dot) dot.classList.add('on');
  }

  async function typeInto(ln, text, prefixHtml) {
    for (var i = 1; i <= text.length; i++) {
      ln.innerHTML = prefixHtml + esc(text.slice(0, i));
      withCursor(ln);
      await sleep(rand(45, 110));
    }
  }

  var running = false;
  async function play() {
    if (running) return;
    running = true;
    var ln = line('<span class="p">~ %</span> ');
    withCursor(ln);
    await sleep(700);
    await typeInto(ln, 'hyperdata', '<span class="p">~ %</span> ');
    await sleep(380);
    cur.remove();
    line('');
    await sleep(120);
    line('<span class="a">HYPERDATA</span>  <span class="td d">Hyperliquid trading data terminal  </span><span class="m">v1.0</span>');
    await sleep(70);
    line('<span class="m">' + '─'.repeat(46) + '</span>');
    await sleep(160);
    for (var c = 0; c < components.length; c++) {
      var cl = line(componentLine(components[c]).replace('<span class="a">ready</span>', '<span class="m">…</span>'));
      await sleep(rand(170, 320));
      cl.innerHTML = componentLine(components[c]);
    }
    await sleep(120);
    var pl = line(progressLine(0));
    for (var p = 0; p <= 100; p += 4) { pl.innerHTML = progressLine(p); await sleep(22); }
    if (dot) dot.classList.add('on');
    await sleep(320);
    line('');
    for (var m = 0; m < menu.length; m++) { line(menuLine(menu[m])); await sleep(rand(55, 95)); }
    line('');
    var sel = line('  <span class="d">Select:</span> ');
    withCursor(sel);
    await sleep(900);
    await typeInto(sel, '5', '  <span class="d">Select:</span> ');
    await sleep(420);
    cur.remove();
    line('');
    line(marketTitle());
    await sleep(60);
    line(marketHeader);
    for (var r = 0; r < rows.length; r++) { line(marketRow(rows[r], r)); await sleep(rand(50, 90)); }
    line('');
    var last = line('  <span class="m">Press Ctrl+C to return to menu.</span> ');
    withCursor(last);
    startTicks();
  }

  /* In-place ticks: one cell at a time, settles between updates. Pauses off-screen. */
  var tickTimer = null, clockTimer = null, visible = true;
  function tickOnce() {
    var i = Math.floor(Math.random() * rows.length);
    var r = rows[i];
    var delta = r.price * rand(-0.00045, 0.00055);
    r.price = Math.max(0.001, r.price + delta);
    r.chg = r.chg + (delta / r.price) * 100;
    var dir = delta >= 0 ? 'up' : 'dn';
    var cells = out.querySelectorAll('[data-i="' + i + '"]');
    cells.forEach(function (cell) {
      var kind = cell.getAttribute('data-cell');
      cell.textContent = lpad(kind === 'price' ? fmtPrice(r) : fmtChg(r), 10);
      cell.classList.remove('up', 'dn');
      void cell.offsetWidth;
      cell.classList.add(dir);
      setTimeout(function () { cell.classList.remove(dir); }, 160);
    });
    tickTimer = setTimeout(tickOnce, rand(700, 1900));
  }
  function startTicks() {
    if (tickTimer || clockTimer || !visible || document.hidden) return;
    clockTimer = setInterval(tickClock, 1000);
    tickTimer = setTimeout(tickOnce, 800);
  }
  function stopTicks() {
    clearTimeout(tickTimer); clearInterval(clockTimer);
    tickTimer = null; clockTimer = null;
  }

  if (reduced() || !('IntersectionObserver' in window)) {
    renderStatic();
    return;
  }

  var term = document.getElementById('hero-term');
  var started = false;
  var tio = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      visible = e.isIntersecting;
      if (visible && !started) { started = true; setTimeout(play, 500); }
      else if (visible && started && !running) { /* nothing */ }
      else if (visible && running && !tickTimer && out.querySelector('[data-cell]')) { startTicks(); }
      else if (!visible) { stopTicks(); }
    });
  }, { threshold: 0.15 });
  tio.observe(term);

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stopTicks();
    else if (visible && out.querySelector('[data-cell]')) startTicks();
  });

  reduceMq.addEventListener && reduceMq.addEventListener('change', function () {
    if (reduced()) { stopTicks(); }
  });
})();
