#!/usr/bin/env python3
"""Crawl a website two levels deep and save clean PNG + PDF captures of every page.

One folder per website and date, holding every page plus the evidence log:
  <site>__<YYYY-MM-DD>/
    <page-slug>__<YYYY-MM-DD_HH-MM-SS>.png
    <page-slug>__<YYYY-MM-DD_HH-MM-SS>.pdf
    capture-log__<YYYY-MM-DD_HH-MM-SS>.csv     one row per page attempted
    run-manifest__<YYYY-MM-DD_HH-MM-SS>.json   run details + hashes

Written locally under ./captures/ and, unless --no-drive is passed, uploaded to
Google Drive under <drive-root>/<site>__<YYYY-MM-DD>/.
"""

from __future__ import annotations

import argparse
import getpass
import os
import platform
import socket
import subprocess
import re
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from evidence_log import CaptureLog, file_hash

from playwright.sync_api import Page, sync_playwright
from playwright._repo_version import version as playwright_version
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


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)


def tool_revision() -> str:
    """Git commit of this script, so the log records exactly which code made the captures."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def crawl(
    start_url: str,
    folder: Path,
    log: CaptureLog,
    max_depth: int,
    max_pages: int,
    delay: float,
    include_subdomains: bool,
    uploader=None,
    drive_folder_id: str | None = None,
) -> dict:
    root_host = urlparse(start_url).netloc.lower()
    # (url, depth, page it was found on)
    queue: deque[tuple[str, int, str]] = deque([(start_url, 0, "")])
    seen = {start_url}
    captured = 0

    with sync_playwright() as pw:
        # Optional: point at an existing Chrome/Chromium instead of Playwright's bundled one.
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        log.run_info["browser"] = f"Chromium {browser.version}"
        context = browser.new_context(viewport={"width": 1440, "height": 900},
                                      device_scale_factor=1, user_agent=USER_AGENT)
        page = context.new_page()

        while queue and captured < max_pages:
            url, depth, found_on = queue.popleft()
            print(f"[depth {depth}] {url}", flush=True)
            base = dict(depth=depth, found_on=found_on, requested_url=url)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                status = response.status if response else None
                final_url = normalize(page.url)
                if status is not None and status >= 400:
                    print(f"  skipped: HTTP {status}")
                    log.add(**base, status="skipped", final_url=final_url, http_status=status,
                            note=f"HTTP {status}")
                    continue
                content_type = response.headers.get("content-type", "") if response else "text/html"
                if "html" not in content_type:
                    print("  skipped: not an HTML page")
                    log.add(**base, status="skipped", final_url=final_url, http_status=status,
                            note=f"not HTML ({content_type})")
                    continue
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeout:
                    pass

                # Collect links before cleanup mutates the DOM.
                links = extract_links(page) if depth < max_depth else []
                title = page.title()

                clean_page(page)

                stem = f"{slugify_url(final_url)}__{datetime.now():%Y-%m-%d_%H-%M-%S}"
                png_path = folder / f"{stem}.png"
                pdf_path = folder / f"{stem}.pdf"
                # Two URLs can share a slug within one second (e.g. only differing by
                # punctuation); never overwrite earlier evidence.
                n = 2
                while png_path.exists() or pdf_path.exists():
                    png_path = folder / f"{stem}_{n}.png"
                    pdf_path = folder / f"{stem}_{n}.pdf"
                    n += 1

                page.screenshot(path=str(png_path), full_page=True)
                page.emulate_media(media="screen")
                page.pdf(
                    path=str(pdf_path),
                    format="A4",
                    print_background=True,
                    margin={"top": "10mm", "bottom": "10mm", "left": "8mm", "right": "8mm"},
                )
                page.emulate_media(media=None)
                captured += 1
                print(f"  saved {png_path.name} + .pdf")

                entry = dict(**base, status="captured", final_url=final_url, http_status=status,
                             page_title=title, png_file=png_path.name, png_sha256=file_hash(png_path),
                             pdf_file=pdf_path.name, pdf_sha256=file_hash(pdf_path))
                if uploader:
                    try:
                        png_id, png_ok = uploader.upload(png_path, drive_folder_id, "image/png")
                        pdf_id, pdf_ok = uploader.upload(pdf_path, drive_folder_id, "application/pdf")
                        entry.update(drive_png_id=png_id, drive_pdf_id=pdf_id,
                                     drive_checksum_match="yes" if png_ok and pdf_ok else "NO")
                        print("  uploaded to Drive" + ("" if png_ok and pdf_ok else " – CHECKSUM MISMATCH"))
                    except Exception as exc:
                        entry.update(note=f"Drive upload failed: {exc}")
                        print(f"  Drive upload failed: {exc}", file=sys.stderr)
                log.add(**entry)

                for link in links:
                    link = normalize(urljoin(final_url, link))
                    if link not in seen and is_crawlable(link, root_host, include_subdomains):
                        seen.add(link)
                        queue.append((link, depth + 1, final_url))
            except Exception as exc:  # keep crawling if one page fails
                print(f"  error: {exc}", file=sys.stderr)
                log.add(**base, status="error", note=str(exc).splitlines()[0][:500])
            time.sleep(delay)

        browser.close()

    return {
        "pages_captured": captured,
        "stopped_by_page_cap": bool(queue),
        # Pages found but never visited because the cap was reached – recorded so the
        # log shows exactly what was *not* captured.
        "not_visited_due_to_cap": [u for u, _, _ in queue],
    }


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
                        help="Name of the Drive folder that holds all site folders (default 'Web Captures')")
    parser.add_argument("--credentials", type=Path, default=Path("credentials.json"),
                        help="OAuth client secret JSON from Google Cloud Console")
    args = parser.parse_args()

    start_url = normalize(args.url)
    started = datetime.now().astimezone()
    run_stamp = f"{started:%Y-%m-%d_%H-%M-%S}"
    folder_name = f"{slugify_url(start_url)}__{started:%Y-%m-%d}"
    folder = args.out / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    uploader = drive_folder_id = None
    if not args.no_drive:
        from drive_upload import DriveUploader

        uploader = DriveUploader(args.credentials, args.drive_root)
        drive_folder_id = uploader.ensure_folder(folder_name)

    log = CaptureLog(
        folder / f"capture-log__{run_stamp}.csv",
        folder / f"run-manifest__{run_stamp}.json",
        run_info={
            "start_url": start_url,
            "started_at": started.isoformat(timespec="seconds"),
            "settings": {"depth": args.depth, "max_pages": args.max_pages, "delay_s": args.delay,
                         "include_subdomains": args.include_subdomains},
            "operator": getpass.getuser(),
            "hostname": socket.gethostname(),
            "os": platform.platform(),
            "tool_git_commit": tool_revision(),
            "playwright": playwright_version,
            "user_agent": USER_AGENT,
            "viewport": "1440x900",
            "drive_folder": None if args.no_drive else f"{args.drive_root}/{folder_name}",
        },
    )

    summary = {"completed": False}
    try:
        summary = crawl(start_url, folder, log, args.depth, args.max_pages, args.delay,
                        args.include_subdomains, uploader, drive_folder_id)
        summary["completed"] = True
    finally:
        # Always finish and upload the log, even if the crawl was interrupted.
        log.close(**summary)
        if uploader:
            for path, mime in ((log.csv_path, "text/csv"), (log.manifest_path, "application/json")):
                try:
                    uploader.upload(path, drive_folder_id, mime)
                except Exception as exc:
                    print(f"Log upload failed ({path.name}): {exc}", file=sys.stderr)

    print(f"\nDone: {summary.get('pages_captured', 0)} pages captured into {folder.resolve()}")
    if summary.get("stopped_by_page_cap"):
        print(f"Page cap reached: {len(summary['not_visited_due_to_cap'])} found pages were not visited "
              f"(listed in {log.manifest_path.name}).")
    print(f"Log: {log.csv_path.name}")


if __name__ == "__main__":
    main()
