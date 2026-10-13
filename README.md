# scaper2026 – two-level website capture

Crawls a website starting from one URL, follows same-site links **two levels deep**
(start page → pages it links to → pages *those* link to), and saves a clean
full-page **PNG** and **PDF** of every page, plus an **evidence log**. Everything goes to
Google Drive in one folder per website and date.

## Folder layout

```
Web Captures/                                   (--drive-root)
└── www.example.com__2026-10-13/                one folder per website + date
    ├── www.example.com__2026-10-13_14-05-22.png
    ├── www.example.com__2026-10-13_14-05-22.pdf
    ├── www.example.com_about__2026-10-13_14-05-25.png
    ├── www.example.com_about__2026-10-13_14-05-25.pdf
    ├── capture-log__2026-10-13_14-05-20.csv     evidence log (one row per page)
    └── run-manifest__2026-10-13_14-05-20.json   run details + full log in JSON
```

Running the same site twice on one day puts both runs in the same folder; each run gets its
own log, named by the run's start time. A local copy is also kept in `./captures/`.

## Evidence log

`capture-log__*.csv` (opens in Excel / Google Sheets) has one row for **every page attempted**,
including pages that were skipped or failed, so gaps are documented rather than silent:

| Column | Meaning |
|---|---|
| `seq` | Order of capture |
| `status` | `captured`, `skipped` (e.g. HTTP 404, not HTML) or `error` |
| `captured_at_local` / `captured_at_utc` | ISO 8601 timestamp, local time with offset and UTC |
| `depth`, `found_on` | Link level and the page the link was found on (chain back to the start URL) |
| `requested_url`, `final_url` | URL visited and where it ended up after redirects |
| `http_status`, `page_title` | Server response code and the page's `<title>` |
| `png_file`, `png_sha256`, `pdf_file`, `pdf_sha256` | Files and their SHA-256 fingerprints |
| `drive_png_id`, `drive_pdf_id` | Google Drive file IDs |
| `drive_checksum_match` | `yes` if Google's checksum of the upload matches the local file |
| `note` | Reason for skip/error |

`run-manifest__*.json` records the run itself: start URL, settings, start/finish time, the
operator's OS username and computer name, OS, browser and Playwright versions, user agent,
the git commit of this tool, counts, **the SHA-256 of the CSV log**, and the list of pages that
were found but **not visited because the page cap was reached**.

The log is written row by row, so if a run crashes the log still covers everything done
up to that point, and it is uploaded to Drive even when the run is interrupted.

### Verifying files later

Download the folder from Drive and run:

```bash
python verify.py path/to/www.example.com__2026-10-13
```

It re-hashes every PNG/PDF and reports any file that is `MISSING` or `ALTERED` compared with the log.

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
| `--max-pages N` | `200` | Safety cap on pages captured (2 files each) – link counts grow fast at depth 2 |
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
- The log is tamper-*evident*, not tamper-*proof*: someone with access could edit both a file and
  the log. For stronger proof, keep the manifest's SHA-256 somewhere independent (e.g. email it to
  yourself) or use a trusted timestamping service.
