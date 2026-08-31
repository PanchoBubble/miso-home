const CACHE_NAME = "miso-shell-v14";
const SHELL_PATHS = [
  "/",
  "/index.html",
  "/companion",
  "/companion.html",
  "/companion.css",
  "/companion.js",
  "/styles.css",
  "/app.js",
  "/manifest.webmanifest",
  "/favicon-32.png",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-maskable-512.png",
  "/assets/miso-face.riv",
  "/vendor/rive/rive.js",
  "/vendor/rive/rive.wasm",
  "/vendor/rive/rive_fallback.wasm",
];
const STATIC_PATHS = new Set(SHELL_PATHS);

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_PATHS)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((name) => name.startsWith("miso-shell-") && name !== CACHE_NAME)
          .map((name) => caches.delete(name)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "skip-waiting") self.skipWaiting();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // API responses, authenticated requests, streams, and cross-origin content are
  // always network-only. Household data must never enter the shared shell cache.
  if (
    request.method !== "GET"
    || url.origin !== self.location.origin
    || url.pathname.startsWith("/api/")
    || request.headers.has("Authorization")
  ) {
    return;
  }

  if (request.mode === "navigate") {
    const fallbackPath = url.pathname === "/companion" || url.pathname === "/companion.html"
      ? "/companion"
      : "/";
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.status < 500) return response;
          return caches.match(fallbackPath).then((cached) => cached || response);
        })
        .catch(() => caches.match(fallbackPath)),
    );
    return;
  }

  if (url.search === "" && STATIC_PATHS.has(url.pathname)) {
    event.respondWith(caches.match(url.pathname).then((cached) => cached || fetch(request)));
  }
});
