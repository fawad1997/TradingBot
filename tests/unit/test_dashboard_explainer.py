"""The learning layer: the five-minute walkthrough, the page tours and the words that explain themselves.

None of it can be checked by looking at a number, so these tests guard the promises it makes instead:

  * every page loads it, in the order the scripts depend on each other;
  * it works with the network down - nothing is fetched from anywhere but the trading machine itself;
  * it never acts: no new file can reach the control API;
  * nothing points at nothing - every term a page marks has an explanation, and every tour step names an
    element that exists on its page;
  * anyone who has asked their system for less motion gets it.

Files are read directly rather than through TestClient (which needs httpx2 on this starlette).
"""
import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "app" / "dashboard" / "static"
PAGES = {"index.html": "overview", "realtime.html": "realtime", "logs.html": "logs",
         "analysis.html": "analysis", "config.html": "config", "learn.html": "learn"}
NEW_FILES = ["viz.js", "help.js", "learn.js"]
ORDER = ["glossary.js", "viz.js", "help.js", "learn.js", "app.js"]


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def asgi_get(app, path):
    """One GET through the ASGI app, no TestClient (it needs httpx2): returns (status, headers)."""
    import asyncio
    out, sent = {"status": None, "headers": {}, "done": False}, {"req": False}

    async def receive():
        if not sent["req"]:
            sent["req"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        while not out["done"]:
            await asyncio.sleep(0.001)
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body" and not msg.get("more_body"):
            out["done"] = True

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"testserver")], "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}
    asyncio.run(app(scope, receive, send))
    return out["status"], out["headers"]


def make_app():
    from app.config import ConfigManager
    from app.core.bridge import Bridge
    from app.dashboard.api import create_app
    from app.storage.repo import MemoryRepo
    return create_app(Bridge(), MemoryRepo(), ConfigManager(), "tok")


class TestEveryPageLoadsIt(unittest.TestCase):
    def test_scripts_are_present_and_in_dependency_order(self):
        for page in PAGES:
            html = read(page)
            found = re.findall(r'<script src="/static/([^"?]+)(?:\?v=[^"]*)?"', html)
            self.assertEqual(found, ORDER, f"{page} must load {ORDER} in that order")

    def test_every_page_asks_for_the_same_version_of_every_file(self):
        """A browser keeps a copy of each file; a new ?v= is a new address, so it cannot reuse an old copy.
        A page left on an older version would mix new scripts with an old stylesheet - the 20 Oct 2026 bug,
        where the new header buttons showed as bare grey browser buttons."""
        versions = set()
        for page in PAGES:
            html = read(page)
            assets = re.findall(r'(?:src|href)="/static/[\w.-]+\.(?:css|js)(\?v=[^"]*)?"', html)
            self.assertTrue(assets, page)
            self.assertNotIn("", assets, f"{page} loads a stylesheet or script with no ?v= version")
            versions |= set(assets)
        self.assertEqual(len(versions), 1, f"pages disagree on the asset version: {sorted(versions)}")

    def test_the_learn_page_is_installable_like_the_others(self):
        html = read("learn.html")
        self.assertIn('rel="manifest"', html)
        self.assertIn('apple-mobile-web-app-capable', html)
        self.assertIn('data-page="learn"', html)
        self.assertIn('id="learnInline"', html)

    def test_the_learn_route_exists(self):
        self.assertIn("/learn", [getattr(r, "path", None) for r in make_app().routes])


class TestNoStaleCopies(unittest.TestCase):
    """Without Cache-Control a browser guesses freshness (about a tenth of a file's age) and reuses old files."""

    def test_pages_and_static_files_must_be_rechecked(self):
        app = make_app()
        for path in ("/", "/learn", "/static/style.css", "/static/app.js", "/static/learn.js"):
            status, headers = asgi_get(app, path)
            with self.subTest(path=path):
                self.assertEqual(status, 200)
                self.assertIn("no-cache", headers.get("cache-control", ""))

    def test_the_service_worker_rechecks_every_page_with_the_server(self):
        """The worker sits in front of every page and is itself never cached, so this fix reaches a browser on its
        next visit - no server restart needed (20 Oct 2026: a saved old Live page showed nothing)."""
        sw = read("sw.js")
        self.assertIn('cache: "no-cache"', sw)
        self.assertIn("reg.update()", read("app.js"))

    def test_an_old_saved_page_recovers_instead_of_going_blank(self):
        """An old page loads the new app.js without viz/help/learn: it must reload once, then fetch what is
        missing itself, with a flag so it can never loop."""
        js = read("app.js")
        self.assertIn("(function start()", js)
        self.assertIn("function boot()", js)
        self.assertIn('typeof HELP !== "undefined"', js)
        self.assertIn("location.reload()", js)
        self.assertIn('"qa_stale_reload"', js)
        # boot must not run at load time on its own - only start() decides
        self.assertEqual(len(re.findall(r"^boot\(\);", js, re.M)), 0)
        self.assertEqual(len(re.findall(r"^header\(\);", js, re.M)), 0)

    def test_the_funnel_query_does_not_block_the_event_loop(self):
        """It reads up to 50,000 rows; on the event loop it froze every other request while it ran."""
        src = (Path(__file__).resolve().parents[2] / "app" / "dashboard" / "api.py").read_text(encoding="utf-8")
        self.assertIn("run_in_threadpool(_candidate_summary_sync", src)


class TestItIsDarkOnPurpose(unittest.TestCase):
    """Declaring the page dark makes the browser's own controls dark, and stops "force dark" modes re-tinting it."""

    def test_every_page_declares_itself_dark(self):
        for page in PAGES:
            self.assertIn('<meta name="color-scheme" content="dark">', read(page), page)

    def test_the_stylesheet_does_too_and_leaves_no_button_unstyled(self):
        css = read("style.css")
        self.assertIn("color-scheme:dark", css)
        self.assertIn("button:not([class])", css)
        self.assertIn(".hbtn{", css)


class TestItWorksOffline(unittest.TestCase):
    """The dashboard is served from the trading machine and must work with the network down."""

    REMOTE = re.compile(r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//|fetch\(\s*["']\s*(?:https?:)?//"""
                        r"""|@import|url\(\s*["']?\s*(?:https?:)?//|import\s+[^;]*from\s+["']https?:""", re.I)

    def test_nothing_is_loaded_from_another_host(self):
        for f in sorted(STATIC.iterdir()):
            if f.suffix in (".html", ".js", ".css"):
                hit = self.REMOTE.search(f.read_text(encoding="utf-8"))
                self.assertIsNone(hit, f"{f.name} loads something remote: {hit and hit.group(0)}")


class TestItNeverActs(unittest.TestCase):
    def test_no_new_file_can_reach_the_control_api(self):
        for name in NEW_FILES:
            src = read(name)
            self.assertNotIn("/api/control", src, name)
            self.assertNotIn("control(", src, name)
            self.assertNotIn("X-Dashboard-Token", src, name)

    def test_the_tour_blocks_clicks_on_the_page_underneath(self):
        # while the spotlight points at the stop button, a stray click must not press it
        self.assertIn("tour-block", read("help.js"))
        self.assertIn(".tour-block{position:fixed;inset:0", read("style.css"))

    def test_the_stop_button_in_the_walkthrough_is_a_drawing(self):
        src = read("learn.js")
        self.assertIn('class="mockbtn"', src)
        self.assertIn("a drawing", src)


class TestNothingPointsAtNothing(unittest.TestCase):
    def _terms(self):
        g = read("glossary.js")
        block = g[g.index("const TERMS = {"):]
        block = block[:block.index("\n};") + 3]
        return block, set(re.findall(r"^\s{2}([A-Z0-9_]+):\s*\{", block, re.M))

    def test_every_marked_word_has_an_explanation(self):
        _, keys = self._terms()
        self.assertGreaterEqual(len(keys), 20)
        used = set()
        for f in list(PAGES) + ["app.js", "help.js", "learn.js", "viz.js"]:
            src = read(f)
            used |= set(re.findall(r'data-term="([A-Z0-9_]+)"', src))
            used |= set(re.findall(r'defn\("([A-Z0-9_]+)"', src))
        self.assertTrue(used, "nothing on the screen is marked as a term")
        self.assertEqual(used - keys, set(), "terms used on screen but missing from TERMS")

    def test_every_explanation_has_a_title_and_a_short_line(self):
        block, keys = self._terms()
        for key in keys:
            entry = re.search(rf"^\s{{2}}{key}:\s*\{{(.*?)\}},\s*$", block, re.M | re.S)
            self.assertIsNotNone(entry, key)
            self.assertIn("title:", entry.group(1), key)
            self.assertIn("short:", entry.group(1), key)

    def test_every_tour_step_points_at_an_element_on_its_page(self):
        help_js = read("help.js")
        tours = help_js[help_js.index("const TOURS = {"):help_js.index("let tourState")]
        header_ids = set(re.findall(r'id="([\w-]+)"', read("app.js")))     # the header is built by app.js
        for page, key in PAGES.items():
            if key == "learn":
                continue
            m = re.search(rf"^\s{{4}}{key}: \[(.*?)^\s{{4}}\],", tours, re.M | re.S)
            self.assertIsNotNone(m, f"no tour for {key}")
            ids = re.findall(r'\["#([\w-]+)"', m.group(1))
            self.assertTrue(ids, key)
            on_page = set(re.findall(r'id="([\w-]+)"', read(page))) | header_ids
            self.assertEqual(set(ids) - on_page, set(), f"{key} tour points at missing elements")

    @staticmethod
    def _secs(block):
        return [int(s) for s in re.findall(r'\{short: "[^"]+", title: "[^"]+", secs: (\d+)', block)]

    def test_the_walkthrough_has_seven_scenes_of_about_five_minutes(self):
        src = read("learn.js")
        secs = self._secs(src[src.index("const SCENES = ["):src.index("const DEEPER = [")])
        self.assertEqual(len(secs), 7)
        self.assertTrue(240 <= sum(secs) <= 330, f"walkthrough runs {sum(secs)} s")

    def test_going_deeper_is_a_separate_series_of_about_four_minutes(self):
        """The second series must not lengthen the first five minutes; it is reached from the switch at the top of
        the player, the button at the end of the walkthrough, and #deeper in the address."""
        src = read("learn.js")
        secs = self._secs(src[src.index("const DEEPER = ["):src.index("async function loadLive")])
        self.assertEqual(len(secs), 5)
        self.assertTrue(200 <= sum(secs) <= 300, f"going deeper runs {sum(secs)} s")
        self.assertIn('data-a="deeper"', src)
        self.assertIn('"#deeper"', src)

    def test_going_deeper_reads_its_thresholds_from_the_settings(self):
        """The scenes show the bot's own limits, not numbers typed into the page."""
        src = read("learn.js")
        for key in ("max_risk_fraction_per_trade", "research_risk_fraction", "max_total_open_risk_fraction",
                    "correlation_group_fraction", "max_concurrent_positions", "profit_giveback_fraction",
                    "small_win_activation_r", "ambition_floor_r", "pre_minutes", "event_minutes",
                    "arm_minutes_before", "entry_distance_atr", "allowed_regimes"):
            self.assertIn(key, src, key)

    def test_the_news_window_matches_what_the_engine_blocks(self):
        """In block mode the engine refuses entries PRE_EVENT and EVENT only - not POST_EVENT - so the scene must not
        claim the minutes after the release window are blocked."""
        engine = (Path(__file__).resolve().parents[2] / "app" / "core" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("if ev in (EventState.PRE_EVENT, EventState.EVENT):", engine)
        self.assertNotIn("post_minutes", read("learn.js"))


class TestLessMotion(unittest.TestCase):
    def test_reduced_motion_is_honoured(self):
        self.assertIn("prefers-reduced-motion: reduce", read("viz.js"))
        self.assertIn("REDUCED", read("learn.js"))
        self.assertIn("prefers-reduced-motion:reduce", read("style.css"))


class TestMoneyAddedIsNotMoneyMade(unittest.TestCase):
    """20 Oct 2026: a $500 -> $5,000 demo top-up read as "+4,899 today" on the hero."""

    def test_the_hero_splits_trading_from_deposits(self):
        js = read("app.js")
        self.assertIn("function todaySplit", js)
        self.assertIn("not from trading", js)
        # the same tolerance as tools/daily_review.py:unexplained_balance
        self.assertIn("Math.max(1, 0.002 * Math.abs(+a.balance || 0))", js)

    def test_the_funnel_counts_real_fills(self):
        g = read("glossary.js")
        self.assertIn("function funnelModel", g)
        self.assertIn("FILLED", g)


if __name__ == "__main__":
    unittest.main()
