/* DUYS Boost — Service Worker v1 */
const CACHE_NAME = 'duys-v1';

self.addEventListener('install', function(e) {
  self.skipWaiting();
});

self.addEventListener('activate', function(e) {
  e.waitUntil(self.clients.claim());
});

/* ── Push notifications ─────────────────────────────────────────────────── */
self.addEventListener('push', function(e) {
  var data = {};
  try { data = e.data ? e.data.json() : {}; } catch (_) {}

  var title   = data.title || 'DUYS Boost';
  var options = {
    body:    data.body  || '',
    icon:    data.icon  || '/static/img/icon-192.png',
    badge:   '/static/img/icon-192.png',
    tag:     data.tag   || 'duys-notif',
    data:    { url: data.url || '/feed' },
    requireInteraction: false
  };

  e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(e) {
  e.notification.close();
  var targetUrl = (e.notification.data && e.notification.data.url) || '/feed';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(cs) {
      for (var i = 0; i < cs.length; i++) {
        if (cs[i].url.includes(self.location.origin) && 'focus' in cs[i]) {
          cs[i].focus();
          return cs[i].navigate(targetUrl);
        }
      }
      return clients.openWindow(targetUrl);
    })
  );
});
