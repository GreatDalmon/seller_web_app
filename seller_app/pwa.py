import json

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.urls import reverse


def manifest(request):
    data = {
        "name": "Seller Ops",
        "short_name": "Seller Ops",
        "description": "Track sales, stock and customer debts.",
        "id": "/",
        "start_url": reverse('dashboard'),
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#f7f5ef",
        "theme_color": "#15803d",
        "icons": [
            {"src": static("seller_app/icons/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static("seller_app/icons/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static("seller_app/icons/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "New sale", "url": reverse('add_sale')},
            {"name": "Debts", "url": reverse('debts')},
        ],
    }
    return JsonResponse(data, content_type="application/manifest+json")


SW_JS = """
// Seller Ops service worker. Served from the site root so it controls every page.
// Only the offline page and icons are cached; seller data is never stored.
const CACHE = 'seller-ops-v1';
const OFFLINE_URL = %(offline)s;
const PRECACHE = [OFFLINE_URL, %(icon192)s, %(icon512)s];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE_URL)));
    return;
  }
  if (new URL(req.url).pathname.startsWith('/static/')) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
});
"""


def service_worker(request):
    body = SW_JS % {
        'offline': json.dumps(reverse('offline')),
        'icon192': json.dumps(static("seller_app/icons/icon-192.png")),
        'icon512': json.dumps(static("seller_app/icons/icon-512.png")),
    }
    response = HttpResponse(body, content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


def offline(request):
    return render(request, 'seller_app/offline.html')
