/* JARVIS LITE · PWA client (Phase 0 remote)
   Pairs once via 8-char code -> per-device Bearer token in localStorage.
   Chat hits the SAME think->execute pipeline (host-silent, HITL-gated).
   NOTE works offline: queued in localStorage outbox, pushed on SYNC.
   Sync is idempotent (client UUIDs) + cursor-pulled (hub line offset).
   100% vanilla JS, no deps, no build step. */
(function () {
'use strict';

var LS = { hub: 'jarvis_hub', token: 'jarvis_token', dev: 'jarvis_device',
           cursor: 'jarvis_cursor', outbox: 'jarvis_outbox', name: 'jarvis_name' };

function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function uuid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '').slice(0, 16);
  return 'id' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
}
function store(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
function load(k, d) { try { var v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } }

var hub = '', token = '', deviceId = '', cursor = 0, outbox = [];
function persist() {
  store(LS.hub, hub); store(LS.token, token); store(LS.dev, deviceId);
  store(LS.cursor, String(cursor)); store(LS.outbox, JSON.stringify(outbox));
}
function restore() {
  hub = (load(LS.hub, '') || '').replace(/\/+$/, '');
  token = load(LS.token, '') || '';
  deviceId = load(LS.dev, '') || '';
  cursor = parseInt(load(LS.cursor, '0') || '0', 10) || 0;
  try { outbox = JSON.parse(load(LS.outbox, '[]') || '[]') || []; }
  catch (e) { outbox = []; }
  if (!Array.isArray(outbox)) outbox = [];
}

function api(path, opts) {
  opts = opts || {};
  var headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = 'Bearer ' + token;
  return fetch(hub + path, {
    method: opts.method || 'POST',
    headers: headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  }).then(function (r) {
    if (r.status === 401) { setOffline('UNPAIRED?'); throw new Error('unauthorized — token rejected (revoked?)'); }
    if (r.status === 429) throw new Error('rate-limited — try later');
    return r.json().then(function (j) {
      if (!r.ok || j.ok === false) throw new Error(j.error || ('http ' + r.status));
      return j;
    });
  });
}

function setOffline(t) {
  $('dot').classList.remove('on'); $('stat').classList.remove('on');
  $('stat').textContent = t || 'OFFLINE';
}
function setOnline() {
  $('dot').classList.add('on'); $('stat').classList.add('on');
  $('stat').textContent = 'LINKED';
}
function cursorLine() {
  $('cursor').textContent = 'cursor ' + cursor + ' · outbox ' + outbox.length +
    (deviceId ? ' · ' + deviceId : '');
}
function bubble(who, text, pending) {
  var box = $('msgs'), d = document.createElement('div');
  d.className = 'msg ' + (who === 'you' ? 'me' : 'jarvis') + (pending ? ' pending' : '');
  d.innerHTML = '<span class="who">' + (who === 'you' ? 'YOU' : 'JARVIS') + '</span>' + esc(text);
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  return d;
}
function showPair() { $('pair').hidden = false; $('chat').hidden = true; }
function showChat() { $('pair').hidden = true; $('chat').hidden = false; cursorLine(); }

function doSync(silent) {
  if (!hub || !token) return Promise.resolve();
  var payload = outbox.slice(0, 100);
  return api('/api/mobile/sync', { body: { events: payload, cursor: cursor } })
    .then(function (j) {
      // The hub counts EVERY pushed event as applied or skipped, so the
      // whole batch is acknowledged — drop it. Skipped events (dup id,
      // unknown type) are dropped too: retrying them can never succeed,
      // and sync is idempotent by client UUID so a failed push that DID
      // land server-side won't double-apply on retry.
      if (payload.length) outbox = outbox.slice(payload.length);
      cursor = (j.cursor != null ? j.cursor : cursor);
      (j.events || []).forEach(function (e) {
        var t = (e.type || ''), p = e.payload || {};
        var line = t === 'memory.remember' ? '✎ ' + (p.text || '').slice(0, 140)
          : t === 'memory.forget' ? '⌫ forget: ' + ((p.keyword || p.memory_id || '') + '').slice(0, 100)
          : '• ' + t;
        var he = $('hub-events'), d = document.createElement('div');
        d.className = 'he'; d.textContent = line;
        he.prepend(d);
        while (he.children.length > 20) he.removeChild(he.lastChild);
      });
      persist(); cursorLine(); setOnline();
      if (!silent) bubble('jarvis', 'sync ok · +' + (j.applied || 0) + ' / cursor ' + cursor);
      return j;
    })
    .catch(function (e) {
      setOffline();
      if (!silent) bubble('jarvis', 'sync failed: ' + e.message + ' (notes stay queued)');
    });
}

function sendChat(text) {
  text = (text || '').trim();
  if (!text || !hub || !token) return;
  bubble('you', text);
  $('in').value = '';
  var pend = bubble('jarvis', '…', true);
  api('/api/mobile/chat', { body: { text: text, wait: true } })
    .then(function (j) { pend.classList.remove('pending'); pend.innerHTML = '<span class="who">JARVIS</span>' + esc(j.result || 'Done.'); setOnline(); })
    .catch(function (e) { pend.classList.remove('pending'); pend.innerHTML = '<span class="who">JARVIS</span>' + esc('failed: ' + e.message); setOffline(); });
}

function saveNote(text) {
  text = (text || '').trim().slice(0, 2000);
  if (!text) return;
  // Bound the offline queue (localStorage is ~5MB; hub caps batches at 100).
  if (outbox.length >= 200) outbox.shift();
  outbox.push({ id: uuid(), type: 'memory.remember',
    payload: { text: text, category: 'note', tags: ['mobile'], importance: 5 } });
  persist(); cursorLine();
  bubble('you', '✎ ' + text);
  bubble('jarvis', outbox.length === 1 ? 'queued offline — hit SYNC to push' : outbox.length + ' notes queued — hit SYNC to push');
}

document.addEventListener('DOMContentLoaded', function () {
  restore();
  if (hub && token) { showChat(); setOnline(); doSync(true); }
  else {
    showPair();
    // Same-origin by default: the PWA is served by the hub itself, so the
    // hub URL is usually the origin the app was installed from. Prefill it
    // (avoids a cross-origin fetch, which the hub does not allow) — the
    // user only types a URL when reaching the hub via another address
    // (e.g. Tailscale name vs LAN IP).
    try {
      if (!hub && window.location && window.location.origin &&
          window.location.origin.indexOf('http') === 0) hub = window.location.origin;
    } catch (e) {}
    if (hub) $('hub').value = hub;
    setOffline(token ? 'OFFLINE' : 'UNPAIRED');
  }

  // jarvis://pair?hub=<origin>&code=<8-CHAR> — same single-use code as
  // manual entry, shared by dashboard QR + Flutter Lite QR/paste flows.
  function parsePairLink(link) {
    try {
      var m = String(link || '').trim().match(/^jarvis:\/\/pair\?(.+)$/i);
      if (!m) return null;
      var q = {};
      m[1].split('&').forEach(function (kv) {
        var i = kv.indexOf('=');
        if (i < 0) return;
        q[decodeURIComponent(kv.slice(0, i))] =
          decodeURIComponent(kv.slice(i + 1));
      });
      var h = String(q.hub || '').trim().replace(/\/+$/, '');
      var c = String(q.code || '').trim().toUpperCase();
      if (!/^https?:\/\//.test(h)) return null;
      if (!/^[23456789ABCDEFGHJKMNPQRSTUWXYZ]{8}$/.test(c)) return null;
      return { hub: h, code: c };
    } catch (e) { return null; }
  }

  var fromLinkBtn = $('fromlink');
  if (fromLinkBtn) fromLinkBtn.addEventListener('click', function () {
    var info = parsePairLink($('plink') ? $('plink').value : '');
    var err = $('pair-err'); err.textContent = '';
    if (!info) {
      err.textContent = 'that is not a jarvis pair link (jarvis://pair?hub=..&code=..)';
      return;
    }
    $('hub').value = info.hub;
    $('code').value = info.code;
    err.textContent = 'link parsed — hit REDEEM & CONNECT';
  });

  $('redeem').addEventListener('click', function () {
    hub = ($('hub').value || '').trim().replace(/\/+$/, '');
    var code = ($('code').value || '').trim().toUpperCase();
    var name = ($('dname').value || '').trim() || 'phone';
    var err = $('pair-err'); err.textContent = '';
    if (!hub || !code) { err.textContent = 'hub URL + code required'; return; }
    if (!/^https?:\/\//.test(hub)) hub = 'http://' + hub;
    $('redeem').textContent = '…';
    fetch(hub + '/api/mobile/redeem', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: code, device_name: name }),
    }).then(function (r) { return r.json().then(function (j) {
      if (!r.ok || !j.token) throw new Error(j.error || ('http ' + r.status));
      return j;
    }); }).then(function (j) {
      token = j.token; deviceId = j.device_id || ''; cursor = 0; outbox = [];
      persist(); showChat(); setOnline();
      bubble('jarvis', 'paired as ' + name + ' (' + deviceId + '). Ask anything — destructive actions still hold for a human on the Mac.');
    }).catch(function (e) { err.textContent = e.message; })
    .finally(function () { $('redeem').textContent = 'REDEEM & CONNECT'; });
  });

  $('send').addEventListener('click', function () { sendChat($('in').value); });
  $('in').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); sendChat($('in').value); }
  });
  $('note').addEventListener('click', function () { saveNote($('in').value); $('in').value = ''; });
  $('sync').addEventListener('click', function () { doSync(false); });

  // Handoff GET is dashboard-gated on the hub, so the phone can't pull
  // it directly. PULL = sync (push outbox + pull hub memory events into
  // the feed); PUSH = fold a one-line context note into hub memory.
  $('pull').addEventListener('click', function () { doSync(false); });
  $('push').addEventListener('click', function () {
    var t = prompt('One-line context to hand to the Mac hub:');
    if (t && t.trim()) { saveNote('[handoff] ' + t.trim()); doSync(false); }
  });
  $('unpair').addEventListener('click', function () {
    if (!confirm('Unpair this phone? (Also REVOKE it on the Mac dashboard.)')) return;
    token = ''; deviceId = ''; cursor = 0; outbox = [];
    persist(); showPair(); setOffline('UNPAIRED');
  });
});
})();
