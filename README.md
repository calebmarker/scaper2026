# scaper2026 – two-level website capture

Crawls a website starting from one URL, follows same-site links **two levels deep**
(start page → pages it links to → pages *those* link to), and saves a clean
full-page **PNG** and **PDF** of every page. Files go to Google Drive, one folder per page.

## Naming

| What | Pattern | Example |
|---|---|---|
| Drive folder | `<url-slug>__<YYYY-MM-DD>` | `www.example.com_blog__2026-10-13` |
| Files | `<url-slug>__<YYYY-MM-DD_HH-MM-SS>.png/.pdf` | `www.example.com_blog__2026-10-13_14-05-22.pdf` |

All page folders sit inside one Drive folder (default `Web Captures`, change with `--drive-root`).
Re-running on the same day reuses that day's folder; the timestamp in the filename keeps captures apart.
A local copy is also kept in `./captures/`.

## "Clean" means

- Clicks common "Accept cookies" buttons, then hides cookie/consent/GDPR banners and chat widgets
- Scrolls the whole page first so lazy-loaded images render
- Turns sticky/fixed headers into normal ones so they don't repeat down the screenshot or on every PDF page
- PDFs use the on-screen styling (not the site's print stylesheet) with backgrounds

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### Google Drive setup (one time, ~5 minutes)

1. Go to <https://console.cloud.google.com/>, create a project (any name).
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **APIs & Services → OAuth consent screen** → External, add your own Gmail as a test user.
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → download the JSON.
5. Save it as `credentials.json` in this folder.

The first run opens a browser to sign in; after that `token.json` is reused.
The script only asks for the `drive.file` scope, so it can see **only the files it creates**, not the rest of your Drive.

## Usage

```bash
python scraper.py https://example.com
```

Options:

| Flag | Default | Purpose |
|---|---|---|
| `--depth N` | `2` | Link levels to follow |
| `--max-pages N` | `200` | Safety cap – link counts grow fast at depth 2 |
| `--delay S` | `1.0` | Seconds between pages (be polite to the site) |
| `--include-subdomains` | off | Also follow `blog.example.com` etc. |
| `--drive-root NAME` | `Web Captures` | Parent Drive folder |
| `--no-drive` | off | Save locally only |
| `--out DIR` | `captures` | Local output folder |

Set `CHROMIUM_PATH=/path/to/chrome` to use an existing Chrome instead of Playwright's.

## Limits

- Pages behind a login aren't captured (it browses as a logged-out visitor).
- Sites with aggressive bot protection (Cloudflare challenges etc.) may return a challenge page instead of content.
- Only follows links on the same host; external links and file downloads (PDF, images, zips) are skipped.
- Check the site's terms of service / robots.txt before crawling it.
