"""Real browser checks use synthetic inputs and loopback HTTP only."""

import functools
import json
import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from build_site import build
from playwright.sync_api import expect, sync_playwright
from test_site_build import synthetic_catalog


@contextmanager
def serve(directory):
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.integration
@pytest.mark.parametrize(
    "viewport", [{"width": 1440, "height": 900}, {"width": 390, "height": 844}]
)
def test_browser_search_sort_filters_evidence_navigation_downloads(tmp_path, viewport):
    catalog = synthetic_catalog(tmp_path / "catalog")
    site = tmp_path / "public/subpath"
    build(catalog, site, "https://github.com/a/b")
    with serve(site.parent) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chromium")
        page = browser.new_page(viewport=viewport)
        errors, external = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def local_only(route):
            if not route.request.url.startswith(base + "/"):
                external.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        page.route("**/*", local_only)
        page.goto(base + "/subpath/")
        rows = page.locator(".catalog tbody tr")
        expect(rows).to_have_count(4)
        expect(rows.locator(".catalog-name a")).to_have_count(4)
        expect(page.locator(".preview")).to_have_count(0)
        expect(rows.first).to_contain_text("Fast <img")
        assert page.locator("img").count() == 0
        expect(page.locator("#count")).to_have_text("4 stars")
        page.get_by_role("button", name="ID", exact=True).click()
        expect(rows.locator(".catalog-number")).to_have_text(["1", "2", "10", "100"])
        page.get_by_role("button", name="ID ↑", exact=True).click()
        expect(rows.locator(".catalog-number")).to_have_text(["100", "10", "2", "1"])
        page.get_by_role("button", name="v_GRF", exact=True).click()
        page.get_by_role("button", name="v_GRF ↓", exact=True).click()
        expect(rows.first).to_contain_text("Example")
        expect(rows.nth(2)).to_contain_text("Only bound")
        expect(rows.last).to_contain_text("Missing")
        search = page.get_by_role("searchbox")
        search.fill(" 1234567890123456789 ")
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text("Example")
        search.fill("not found")
        expect(rows.first).to_contain_text("No stars match")
        search.fill("")
        page.get_by_label("Has v_GRF", exact=True).check()
        expect(rows).to_have_count(3)
        page.get_by_label("Studied in ≥2 papers", exact=True).check()
        expect(rows).to_have_count(2)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if viewport["width"] < 650:
            wrapper = page.locator(".table-scroll")
            assert wrapper.evaluate("e => e.scrollWidth > e.clientWidth")
            fixed = [
                rows.first.locator(selector)
                for selector in (".catalog-number", ".catalog-name")
            ]
            before = [cell.bounding_box()["x"] for cell in fixed]
            wrapper.evaluate("e => e.scrollLeft = 200")
            assert all(
                abs(cell.bounding_box()["x"] - x) < 1
                for cell, x in zip(fixed, before, strict=True)
            )
        rows.get_by_role("link", name="Example", exact=True).click()
        expect(page.get_by_role("heading", name="Example", exact=True)).to_be_visible()
        expect(page.locator(".catalog-label")).to_have_text("hvs-catalog-000010")
        expect(page.locator(".aliases")).to_contain_text("also reported as")
        more = page.locator(".aliases button")
        expect(more).to_have_text("+1 more")
        more.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#additional-aliases")).to_be_visible()
        expect(more).to_have_attribute("aria-expanded", "true")
        page.get_by_role("button", name="Show less", exact=True).press("Enter")
        expect(page.locator("#additional-aliases")).to_be_hidden()
        expect(
            page.get_by_role("button", name="+1 more", exact=True)
        ).to_have_attribute("aria-expanded", "false")
        papers = page.locator(".literature tbody tr")
        expect(papers).to_have_count(1)
        latest = page.get_by_role("table", name="Latest measurements", exact=True)
        measurement = latest.get_by_role(
            "button",
            name="30 ± 2 km/s; Example et al. 2020 · 1 of 2 values; open evidence",
            exact=True,
        )
        measurement.focus()
        page.keyboard.press("Enter")
        expect(page.get_by_role("dialog")).to_be_visible()
        expect(page.get_by_role("link", name="Fixed PDF · page 12")).to_have_attribute(
            "href", "https://arxiv.org/pdf/2001.00001v1#page=12"
        )
        expect(page.get_by_role("link", name="Fixed PDF · page 12")).to_have_count(1)
        expect(page.locator("blockquote")).to_have_text(
            ["star | 10", "second passage on the same page"]
        )
        expect(page.get_by_role("dialog")).not_to_contain_text("Reported value:")
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).not_to_be_visible()
        expect(measurement).to_be_focused()
        expect(page.locator(".literature .quoted")).to_have_count(0)
        page.get_by_label("Show quoted and non-comparable values").check()
        expect(page.locator(".literature .quoted")).to_have_count(3)
        expect(papers).to_have_count(2)
        page.get_by_label("Show papers without visible values").check()
        expect(papers).to_have_count(3)
        page.get_by_label("Show quoted and non-comparable values").uncheck()
        expect(papers).to_have_count(3)
        page.get_by_label("Show papers without visible values").uncheck()
        expect(papers).to_have_count(1)
        if viewport["width"] < 650:
            page.get_by_label("Show quoted and non-comparable values").check()
            wrapper = page.locator(".literature").locator("..")
            fixed = [
                page.locator(selector)
                for selector in (
                    ".literature th.literature-year",
                    ".literature th.literature-paper",
                    ".literature tbody tr:first-child .literature-year",
                    ".literature tbody tr:first-child .literature-paper",
                )
            ]
            before = [cell.bounding_box()["x"] for cell in fixed]
            wrapper.evaluate("e => e.scrollLeft = 180")
            assert wrapper.evaluate("e => e.scrollLeft > 0")
            assert all(
                abs(cell.bounding_box()["x"] - x) < 1
                for cell, x in zip(fixed, before, strict=True)
            )
            page.get_by_label("Show quoted and non-comparable values").uncheck()
        page.get_by_role("button", name="+1", exact=True).click()
        expect(page.get_by_role("dialog")).to_contain_text("Synthetic model A")
        expect(
            page.get_by_role("dialog").get_by_role(
                "button", name="20 km/s", exact=False
            )
        ).to_be_visible()
        page.keyboard.press("Escape")
        page.reload()
        expect(page.get_by_role("heading", name="Example", exact=True)).to_be_visible()
        with page.expect_download() as download:
            page.get_by_role("link", name="Download JSON", exact=True).click()
        assert (
            download.value.path().read_bytes()
            == (catalog / "stars/0.json").read_bytes()
        )
        page.get_by_role("button", name="Download", exact=True).click()
        for label, file in [
            ("stars.csv", "stars.csv"),
            ("Complete catalog ZIP", "catalog.zip"),
        ]:
            with page.expect_download() as download:
                page.get_by_role("link", name=label, exact=True).click()
            assert (
                download.value.path().read_bytes()
                == (site / "downloads" / file).read_bytes()
            )
        page.keyboard.press("Escape")
        page.get_by_role("link", name="All stars", exact=True).click()
        expect(page.locator(".catalog")).to_be_visible()
        page.go_back()
        expect(page.get_by_role("heading", name="Example", exact=True)).to_be_visible()
        page.go_forward()
        expect(page.locator(".catalog")).to_be_visible()
        page.get_by_role("link", name="About", exact=True).click()
        expect(
            page.get_by_role("heading", name="About HVS Catalog", exact=True)
        ).to_be_visible()
        expect(page.locator(".about")).to_contain_text(
            "Of 4 papers read, 3 report at least one qualifying star."
        )
        expect(page.locator(".about")).to_contain_text("not a best estimate")
        expect(page.locator(".about pre code")).to_contain_text("pd.read_csv")
        expect(page.get_by_role("link", name="Known issues")).to_have_attribute(
            "href", "https://github.com/a/b/blob/HEAD/catalog-review/KNOWN_ISSUES.md"
        )
        with page.expect_download() as download:
            page.get_by_role("link", name="stars.csv", exact=True).click()
        assert (
            download.value.path().read_bytes()
            == (site / "downloads/stars.csv").read_bytes()
        )
        page.reload()
        expect(page.locator(".about")).to_be_visible()
        page.get_by_role("link", name="All stars", exact=True).click()
        expect(page.locator(".catalog")).to_be_visible()
        page.go_back()
        expect(page.locator(".about")).to_be_visible()
        page.goto(base + "/subpath/#/star/missing")
        expect(page.get_by_text("No default values are available.")).to_be_visible()
        expect(
            page.get_by_label("Show quoted and non-comparable values")
        ).to_be_checked()
        expect(
            page.get_by_label("Show papers without visible values")
        ).not_to_be_checked()
        expect(page.locator(".literature .quoted")).to_have_count(2)
        expect(page.locator(".literature tbody tr")).to_have_count(1)
        text = page.locator("body").inner_text()
        assert "private-" not in text and "T:p" not in text
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
        assert not external
        browser.close()


@pytest.mark.integration
def test_browser_without_any_visible_values_defaults_to_all_papers(tmp_path):
    catalog = synthetic_catalog(tmp_path / "catalog")
    path = catalog / "stars/2.json"
    sample = json.loads(path.read_text())
    sample["quantities"] = []
    path.write_text(json.dumps(sample))
    site = tmp_path / "site"
    build(catalog, site, "https://github.com/a/b")
    with serve(site) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chromium")
        for width, height in [(1440, 900), (390, 844)]:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if route.request.url.startswith(base + "/")
                    else route.abort()
                ),
            )
            page.goto(base + "/#/star/missing")
            expect(
                page.get_by_label("Show quoted and non-comparable values")
            ).to_be_checked()
            expect(
                page.get_by_label("Show papers without visible values")
            ).to_be_checked()
            expect(page.locator(".literature tbody tr")).to_have_count(1)
            expect(page.locator(".literature tbody tr")).to_contain_text(
                "Example et al."
            )
            expect(page.locator(".literature")).not_to_contain_text("No papers")
            page.close()
        browser.close()
