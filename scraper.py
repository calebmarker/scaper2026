#!/usr/bin/env python3
"""Crawl a website two levels deep and save clean PNG + PDF captures of every page.

Each page gets:
  <url-slug>__<YYYY-MM-DD_HH-MM-SS>.png
  <url-slug>__<YYYY-MM-DD_HH-MM-SS>.pdf

Files are written locally to ./captures/<url-slug>__<YYYY-MM-DD>/ and, unless
--no-drive is passed, uploaded to Google Drive under:
  <drive-root>/<url-slug>__<YYYY-MM-DD>/
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

# Extensions that are files, not pages – never visit these.
SKIP_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".zip",
    ".gz", ".tar", ".mp3", ".mp4", ".mov", ".avi", ".webm", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".json", ".xml", ".css", ".js",
    ".dmg", ".exe", ".woff", ".woff2", ".ttf",
}

# Common "accept cookies" button labels, tried in order.
CONSENT_LABELS = [
    "Accept all", "Accept All", "Accept all cookies", "Accept", "I agree",
    "Agree", "Allow all", "Allow all cookies", "Got it", "OK", "Continue",
]

# Hide overlays that ruin screenshots: cookie banners, chat widgets, popups.
CLEANUP_CSS = """
[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i],
[id*="gdpr" i], [class*="gdpr" i], [class*="newsletter-popup" i],
[id*="onetrust" i], [class*="onetrust" i], #CybotCookiebotDialog,
[class*="intercom" i], [id*="intercom" i], [id*="hubspot-messages" i],
[class*="drift-" i], iframe[title*="chat" i] {
  display: none !important;
}
html, body { overflow: visible !important; }
"""

# Sticky/fixed headers repeat across a full-page screenshot and every PDF page;
# make them static so they appear once at their natural position.
UNSTICK_JS = """
() => {
  for (const el of document.querySelectorAll('body *')) {
    const pos = getComputedStyle(el).position;
    if (pos === 'fixed' || pos === 'sticky') el.style.setProperty('position', 'static', 'important');
  }
}
"""

# Scroll to the bottom in steps so lazy-loaded images actually load.
AUTOSCROLL_JS = """
async () => {
  await new Promise(resolve => {
    let total = 0, step = 600, ticks = 0;
    const timer = setInterval(() => {
      window.scrollBy(0, step);
      total += step; ticks += 1;
      if (total >= document.body.scrollHeight || ticks > 60) {
        clearInterval(timer);
        window.scrollTo(0, 0);
        resolve();
      }
    }, 120);
  });
}
"""


@dataclass
class Capture:
    url: str
    depth: int
    folder_name: str
    png: Path
    pdf: Path


def normalize(url: str) -> str:
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return parsed._replace(path=path, netloc=parsed.netloc.lower()).geturl()


def slugify_url(url: str, max_len: int = 120) -> str:
    """https://www.example.com/blog/post?id=3 -> www.example.com_blog_post_id-3"""
    parsed = urlparse(url)
    raw = parsed.netloc + parsed.path
    if parsed.query:
        raw += "_" + parsed.query
    slug = re.sub(r"[^A-Za-z0-9.\-]+", "_", raw.replace("=", "-")).strip("_")
    return slug[:max_len] or "page"


def is_crawlable(url: str, root_host: str, include_subdomains: bool) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.netloc.lower()
    if include_subdomains:
        base = root_host.removeprefix("www.")
        if host != base and not host.endswith("." + base):
            return False
    elif host != root_host:
        return False
    return Path(parsed.path).suffix.lower() not in SKIP_EXTENSIONS


def dismiss_consent(page: Page) -> None:
    for label in CONSENT_LABELS:
        try:
            button = page.get_by_role("button", name=label, exact=True)
            if button.count() and button.first.is_visible():
                button.first.click(timeout=1500)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def clean_page(page: Page) -> None:
    dismiss_consent(page)
    page.add_style_tag(content=CLEANUP_CSS)
    page.evaluate(AUTOSCROLL_JS)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PlaywrightTimeout:
        pass
    page.evaluate(UNSTICK_JS)
    page.wait_for_timeout(300)


def extract_links(page: Page) -> list[str]:
    return page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")


def crawl(
    start_url: str,
    out_dir: Path,
    max_depth: int,
    max_pages: int,
    delay: float,
    include_subdomains: bool,
    on_capture=None,
) -> list[Capture]:
    start_url = normalize(start_url)
    root_host = urlparse(start_url).netloc.lower()
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    seen = {start_url}
    captures: list[Capture] = []

    with sync_playwright() as pw:
        # Optional: point at an existing Chrome/Chromium instead of Playwright's bundled one.
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        while queue and len(captures) < max_pages:
            url, depth = queue.popleft()
            print(f"[depth {depth}] {url}", flush=True)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if response is not None and response.status >= 400:
                    print(f"  skipped: HTTP {response.status}")
                    continue
                if "html" not in (response.headers.get("content-type", "") if response else "html"):
                    print("  skipped: not an HTML page")
                    continue
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeout:
                    pass

                # Collect links before cleanup mutates the DOM.
                links = extract_links(page) if depth < max_depth else []
                final_url = normalize(page.url)

                clean_page(page)

                now = datetime.now()
                slug = slugify_url(final_url)
                folder_name = f"{slug}__{now:%Y-%m-%d}"
                stem = f"{slug}__{now:%Y-%m-%d_%H-%M-%S}"
                folder = out_dir / folder_name
                folder.mkdir(parents=True, exist_ok=True)
                png_path = folder / f"{stem}.png"
                pdf_path = folder / f"{stem}.pdf"

                page.screenshot(path=str(png_path), full_page=True)
                page.emulate_media(media="screen")
                page.pdf(
                    path=str(pdf_path),
                    format="A4",
                    print_background=True,
                    margin={"top": "10mm", "bottom": "10mm", "left": "8mm", "right": "8mm"},
                )
                page.emulate_media(media=None)

                capture = Capture(final_url, depth, folder_name, png_path, pdf_path)
                captures.append(capture)
                print(f"  saved {png_path.name} + .pdf")
                if on_capture:
                    on_capture(capture)

                for link in links:
                    link = normalize(urljoin(final_url, link))
                    if link not in seen and is_crawlable(link, root_host, include_subdomains):
                        seen.add(link)
                        queue.append((link, depth + 1))
            except Exception as exc:  # keep crawling if one page fails
                print(f"  error: {exc}", file=sys.stderr)
            time.sleep(delay)

        browser.close()
    return captures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="Start URL, e.g. https://example.com")
    parser.add_argument("--depth", type=int, default=2,
                        help="Link levels to follow from the start page (default 2: start -> links -> their links)")
    parser.add_argument("--max-pages", type=int, default=200, help="Safety cap on pages captured (default 200)")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between pages (default 1.0)")
    parser.add_argument("--include-subdomains", action="store_true", help="Also follow links to subdomains")
    parser.add_argument("--out", type=Path, default=Path("captures"), help="Local output directory")
    parser.add_argument("--no-drive", action="store_true", help="Save locally only; skip Google Drive upload")
    parser.add_argument("--drive-root", default="Web Captures",
                        help="Name of the Drive folder that holds all page folders (default 'Web Captures')")
    parser.add_argument("--credentials", type=Path, default=Path("credentials.json"),
                        help="OAuth client secret JSON from Google Cloud Console")
    args = parser.parse_args()

    on_capture = None
    if not args.no_drive:
        from drive_upload import DriveUploader

        uploader = DriveUploader(args.credentials, args.drive_root)

        def on_capture(capture: Capture) -> None:
            folder_id = uploader.ensure_folder(capture.folder_name)
            uploader.upload(capture.png, folder_id, "image/png")
            uploader.upload(capture.pdf, folder_id, "application/pdf")
            print(f"  uploaded to Drive: {args.drive_root}/{capture.folder_name}/")

    captures = crawl(args.url, args.out, args.depth, args.max_pages, args.delay,
                     args.include_subdomains, on_capture)
    print(f"\nDone: {len(captures)} pages captured into {args.out.resolve()}")


if __name__ == "__main__":
    main()
