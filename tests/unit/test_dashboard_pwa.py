"""Installing the dashboard as a phone app.

Three things have to hold together or the "Install app" option silently never appears,
with no error anywhere to explain why:

  1. the manifest is reachable and valid JSON with the icons it names,
  2. every page links it and carries the iOS meta tags,
  3. the service worker is served from the ROOT, not from /static/.

Point 3 is the one that bites. A service worker may only control pages at or below its
own URL, so /static/sw.js controls /static/ and nothing else; the pages themselves stay
uncontrolled and the browser never considers the site installable.

The last class is the important one. The worker must not cache dashboard data. A cached
balance or position list looks authoritative while being wrong, which is worse than
showing nothing at all.
"""
import json
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from app.config import ConfigManager
from app.core.bridge import Bridge
from app.dashboard.api import STATIC, create_app
from app.storage.repo import MemoryRepo

PAGES = ["/", "/realtime", "/logs", "/config", "/analysis", "/learn"]


def client():
    return TestClient(create_app(Bridge(), MemoryRepo(), ConfigManager(), "tok"))


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))

    def test_it_is_served(self):
        r = client().get("/static/manifest.json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["short_name"], "Quantara")

    def test_standalone_display_is_what_removes_the_address_bar(self):
        self.assertEqual(self.manifest["display"], "standalone")

    def test_it_carries_the_ink_colour_from_the_logo(self):
        """Sampled from the logo artwork itself (#030F25), so the mark sits on the header with no seam
        around it. A generic slate like #0f172a, or the previous #0B1826, shows as a visible square."""
        self.assertEqual(self.manifest["theme_color"], "#030F25")
        self.assertEqual(self.manifest["background_color"], "#030F25")

    def test_both_icon_sizes_exist_and_one_is_maskable(self):
        by_purpose = {}
        for icon in self.manifest["icons"]:
            path = STATIC / Path(icon["src"]).name
            self.assertTrue(path.is_file(), f"{icon['src']} is named in the manifest but missing")
            self.assertGreater(path.stat().st_size, 0)
            by_purpose.setdefault(icon.get("purpose", "any"), []).append(icon["sizes"])
        self.assertIn("192x192", by_purpose["any"])
        self.assertIn("512x512", by_purpose["any"])
        self.assertIn("512x512", by_purpose["maskable"],
                      "without a maskable icon Android crops the artwork to a circle")

    def test_shortcuts_point_at_pages_that_exist(self):
        c = client()
        for s in self.manifest.get("shortcuts", []):
            self.assertEqual(c.get(s["url"]).status_code, 200, f"shortcut {s['url']} is a dead link")


class TestEveryPageOptsIn(unittest.TestCase):
    def test_all_five_pages_carry_the_tags(self):
        c = client()
        for path in PAGES:
            page = c.get(path).text
            with self.subTest(page=path):
                self.assertIn('rel="manifest"', page)
                self.assertIn('name="theme-color" content="#030F25"', page)
                self.assertIn("apple-mobile-web-app-capable", page)
                self.assertIn('apple-mobile-web-app-title" content="Quantara"', page)
                self.assertIn("apple-touch-icon", page)

    def test_viewport_covers_the_notch(self):
        """Without viewport-fit=cover the installed app letterboxes on a notched phone."""
        c = client()
        for path in PAGES:
            with self.subTest(page=path):
                self.assertIn("viewport-fit=cover", c.get(path).text)

    def test_the_header_clears_a_translucent_status_bar(self):
        """black-translucent draws the status bar OVER the page, so the header must pad for it."""
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        self.assertIn("env(safe-area-inset-top", css)
        self.assertIn("env(safe-area-inset-left", css)


class TestServiceWorkerScope(unittest.TestCase):
    def test_it_is_served_from_the_root(self):
        r = client().get("/sw.js")
        self.assertEqual(r.status_code, 200, "/sw.js must exist at the root or scope is only /static/")
        self.assertIn("javascript", r.headers["content-type"])

    def test_it_is_not_cached_by_the_browser(self):
        self.assertIn("no-cache", client().get("/sw.js").headers.get("cache-control", ""))

    def test_the_page_registers_it_at_the_root_path(self):
        app_js = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('navigator.serviceWorker.register("/sw.js")', app_js)
        self.assertIn("serviceWorker\" in navigator", app_js,
                      "registering without the guard throws on browsers that lack it")


class TestTheWorkerCachesNoTradingData(unittest.TestCase):
    """The property worth defending: a stale trading screen is worse than no screen."""

    def setUp(self):
        self.src = (STATIC / "sw.js").read_text(encoding="utf-8")

    def test_it_only_intercepts_navigations(self):
        self.assertIn('event.request.mode !== "navigate"', self.src)

    def test_it_never_caches_a_response_from_the_network(self):
        """cache.put(request, networkResponse) anywhere here would persist live account data."""
        self.assertNotIn("cache.put(event.request", self.src)
        self.assertNotIn("cache.addAll", self.src)
        self.assertNotIn("response.clone()", self.src)

    def test_it_caches_exactly_one_thing_and_that_is_the_offline_page(self):
        self.assertEqual(self.src.count("cache.put("), 1)
        self.assertIn("OFFLINE_URL", self.src)

    def test_no_api_or_websocket_path_appears_in_it(self):
        for path in ("/api/", "/ws", "/api/state", "equity"):
            self.assertNotIn(path, self.src, f"the worker must not know about {path}")

    def test_old_versions_are_cleared_on_activate(self):
        self.assertIn("caches.delete", self.src)


if __name__ == "__main__":
    unittest.main()
