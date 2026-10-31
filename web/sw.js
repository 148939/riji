/* 日迹 service worker：只缓存图标，页面永远走网络（页面里嵌着你的日记） */
const CACHE = 'riji-v1';

self.addEventListener('install', function(){ self.skipWaiting(); });
self.addEventListener('activate', function(e){
  e.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', function(e){
  const url = e.request.url;
  if (url.indexOf('/icons/') === -1) return; // 其他一律走网络
  e.respondWith(
    caches.open(CACHE).then(function(c){
      return c.match(e.request).then(function(hit){
        if (hit) return hit;
        return fetch(e.request).then(function(res){
          c.put(e.request, res.clone());
          return res;
        });
      });
    })
  );
});
