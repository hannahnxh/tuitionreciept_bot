const CACHE = "tuitionbot-v2";
const ASSETS = ["/", "/static/style.css", "/static/app.js", "/static/icon.svg", "/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Network-first for the app shell so updates show up immediately; the cache
// is only a fallback for when there's no connection at all.
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) return; // always network for API calls
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        return res;
      })
      .catch(() => caches.match(event.request))
  );
});
