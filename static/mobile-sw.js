/* Jarvis Lite service worker — cache-first shell, never cache API.
   Version the cache name on shell changes; old caches are purged on activate. */
var CACHE = 'jarvis-lite-v2';
var SHELL = ['/mobile', '/static/mobile.css', '/static/mobile.js',
             '/static/icon.svg', '/mobile-manifest.json'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); })
    .then(function () { return self.skipWaiting(); }).catch(function () {}));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.map(function (k) {
      return k === CACHE ? null : caches.delete(k);
    }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
  var url = new URL(e.request.url);
  // API + pairing calls always hit the network (auth, single-use codes).
  if (url.pathname.indexOf('/api/mobile/') === 0) return;
  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request).then(function (hit) { return hit || fetch(e.request); })
      .catch(function () { return caches.match('/mobile'); })
  );
});
