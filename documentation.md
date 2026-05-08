# Singapore Internship Aggregator – Full Documentation

> **Status:** Pre-implementation reference document.  
> **Last updated:** 2026-05-05  
> **Target audience:** Junior to mid-level developers.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Tech Stack](#2-tech-stack)
3. [APIs & Data Sources](#3-apis--data-sources)
4. [Other Tools & Rationale](#4-other-tools--rationale)
5. [Architecture Diagram](#5-architecture-diagram)
6. [Database Schema](#6-database-schema)
7. [Setup Instructions](#7-setup-instructions)
8. [Legal & Ethical Notes](#8-legal--ethical-notes)
9. [Deployment Guide](#9-deployment-guide)

---

## 1. Project Overview

This application aggregates **internship job postings** from four Singapore-based job portals into a single searchable web page:

| Source | Method |
|--------|--------|
| `sg.jobstreet.com` | `python-jobspy` library |
| `www.mycareersfuture.gov.sg` | Direct public REST API |
| `www.internsg.com` | Playwright + stealth plugin |
| `jobs.careers.gov.sg` | Playwright (dynamic table + detail pages) |

Scrapers run every 6 hours via APScheduler. Results are stored in SQLite and served through a FastAPI backend to a vanilla JS frontend.

---

## 2. Tech Stack

### Backend
| Layer | Technology | Version | Why |
|-------|-----------|---------|-----|
| Language | Python | 3.10+ | Strong async support, rich scraping ecosystem |
| Web framework | FastAPI | 0.111+ | Async-native, auto-generates OpenAPI docs, faster than Flask for I/O-bound work |
| Database | SQLite via `sqlite3` (stdlib) | 3.x | Zero-config, file-based, sufficient for a single-node aggregator with <100k rows |
| HTTP client | `httpx` | 0.27+ | Async-native drop-in for `requests`; supports HTTP/2; used for API calls |
| HTML parser | `beautifulsoup4` + `lxml` | latest | Fast, battle-tested, good for static HTML fragments |
| Browser automation | `playwright` (Python) | 1.44+ | Modern async API, cross-browser, better maintained than Selenium |
| Stealth | `playwright-stealth` | 1.0+ | Patches `navigator.webdriver` and dozens of other fingerprinting signals |
| Job scraping | `python-jobspy` | 1.1+ | Pre-built JobStreet adapter, handles pagination and type filtering |
| Scheduling | `apscheduler` | 3.10+ | Persistent job store, cron-like triggers, runs inside the same process |
| Logging | stdlib `logging` | — | No extra dependency; rotating file handler keeps logs manageable |

### Frontend
| Layer | Technology | Why |
|-------|-----------|-----|
| Markup | HTML5 | No build step required |
| Styling | Pure CSS (custom properties, Grid, Flexbox) | No framework overhead; fully responsive |
| Interactivity | Vanilla JavaScript (ES2020) | No framework needed for search/filter on a single page |
| Fonts | Google Fonts (Inter) | Clean, modern feel |

### DevOps
| Tool | Why |
|------|-----|
| Docker + Compose | Reproducible environment; ships Playwright browsers inside the image |
| Render / PythonAnywhere | Free-tier hosting options with persistent disk |

---

## 3. APIs & Data Sources

### 3.1 MyCareersFuture – Hidden Public API

**How to discover it (step-by-step for junior developers):**

1. Open **Chrome** (or Edge) and navigate to `https://www.mycareersfuture.gov.sg`.
2. Press `F12` → click the **Network** tab → check **Preserve log**.
3. In the search box on the website type `internship` and press **Enter**.
4. In DevTools, click the **Fetch/XHR** filter button.
5. Scroll through requests. You will see calls to `api.mycareersfuture.gov.sg`.
6. Click one of those requests → **Preview** tab → you'll see structured JSON.
7. Switch to the **Headers** tab to read the exact URL and query parameters.

**Discovered endpoint:**
```
GET https://api.mycareersfuture.gov.sg/v2/jobs
    ?search=internship
    &limit=100
    &page=0
    &sortBy=createdDate
    &employmentTypes=Internship
```

**Key response fields:**
```json
{
  "total": 1240,
  "results": [
    {
      "uuid": "...",
      "title": "Software Engineering Intern",
      "postedCompany": { "name": "Acme Pte Ltd" },
      "addressFormatted": "Ang Mo Kio, Singapore",
      "salary": { "minimum": 800, "maximum": 1200 },
      "metadata": { "createdAt": "2026-04-28T08:00:00Z" },
      "externalLink": "https://www.mycareersfuture.gov.sg/job/..."
    }
  ]
}
```

**No authentication required.** Pagination: increment `page` (0-based) until `results` is empty.

---

### 3.2 JobStreet – python-jobspy

`python-jobspy` reverse-engineers JobStreet's internal GraphQL/REST API.  
Usage:
```python
from jobspy import scrape_jobs
jobs = scrape_jobs(
    site_name=["jobstreet"],
    search_term="internship",
    location="Singapore",
    job_type="internship",
    results_wanted=100,
)
```
Returns a pandas `DataFrame`. No browser needed.

---

### 3.3 InternSG.com – Playwright + Stealth

`internsg.com` renders listings via JavaScript. A plain `requests` call returns an empty shell.  
**Detection mitigations applied:**
- `playwright-stealth` patches 20+ browser fingerprinting properties.
- Randomised user-agent string per session.
- Human-like delays (1–4 s) between page navigations.
- Chromium launched in headless mode with realistic viewport.

Pagination: the site uses a "Load More" button or numbered pages — both are handled.

---

### 3.4 jobs.careers.gov.sg – Playwright

Singapore government public-sector job board. Listings are in a dynamic table (Angular/React rendered). Strategy:
1. Load the main page, filter by "Internship" or search "intern".
2. Collect all job-detail URLs from the table.
3. Open each URL in sequence, scrape full details.
4. Exponential-backoff retry on timeout.

---

## 4. Other Tools & Rationale

| Tool | Rationale |
|------|-----------|
| `httpx` over `requests` | `requests` is synchronous; `httpx` supports `async/await`, matching the FastAPI async event loop |
| `lxml` parser | 3–5× faster than `html.parser` for large HTML blobs |
| `python-dotenv` | Keeps config (ports, intervals) out of committed code |
| `uvicorn` | ASGI server for FastAPI; single-worker is fine for this read-heavy app |
| Playwright over Selenium | Async API, auto-wait, built-in network interception, no WebDriver binary management |
| `playwright-stealth` over undetected-chromedriver | Works with Playwright's async API; undetected-chromedriver is Selenium-only |

---

## 5. Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                     Docker Container                     │
│                                                          │
│  ┌─────────────┐    ┌──────────────────────────────┐    │
│  │ APScheduler │───▶│        Orchestrator           │    │
│  │ (every 6h)  │    │     (orchestrator.py)         │    │
│  └─────────────┘    └──────┬───────────────────┬───┘    │
│                            │                   │         │
│              ┌─────────────┼────────┐          │         │
│              ▼             ▼        ▼           ▼         │
│  ┌──────────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐│
│  │ scraper_     │ │scraper_  │ │scraper_  │ │scraper_  ││
│  │ jobstreet.py │ │mcf.py    │ │internsg  │ │careers   ││
│  │ (jobspy)     │ │(httpx)   │ │.py       │ │_gov.py   ││
│  │              │ │          │ │(Playwright│ │(Playwright││
│  └──────┬───────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘│
│         └──────────────┴────────────┴────────────-┘      │
│                              │                            │
│                              ▼                            │
│                    ┌─────────────────┐                   │
│                    │   database.py   │                   │
│                    │   (SQLite)      │                   │
│                    └────────┬────────┘                   │
│                             │                            │
│                    ┌────────▼────────┐                   │
│                    │    api.py       │                   │
│                    │   (FastAPI)     │                   │
│                    └────────┬────────┘                   │
│                             │                            │
│                    ┌────────▼────────┐                   │
│                    │  static/        │                   │
│                    │  (HTML/CSS/JS)  │                   │
│                    └─────────────────┘                   │
└─────────────────────────────────────────────────────────-┘
```

---

## 6. Database Schema

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    company     TEXT,
    location    TEXT,
    description TEXT,
    url         TEXT    UNIQUE NOT NULL,   -- deduplication key
    source      TEXT    NOT NULL,          -- 'jobstreet' | 'mycareersfuture' | 'internsg' | 'careers_gov'
    date_posted TEXT,                      -- ISO-8601 string, nullable
    salary      TEXT,
    scraped_at  TEXT    NOT NULL           -- ISO-8601 UTC timestamp
);

CREATE INDEX IF NOT EXISTS idx_jobs_source     ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_date       ON jobs(date_posted DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_scraped_at ON jobs(scraped_at  DESC);
```

---

## 7. Setup Instructions

### Prerequisites (install before anything else)

1. **Python 3.10+**  
   `python --version` → must show 3.10 or higher.

2. **pip / venv**  
   ```bash
   python -m venv venv
   source venv/bin/activate        # Linux/macOS
   venv\Scripts\activate           # Windows PowerShell
   ```

3. **Install Python dependencies**  
   ```bash
   pip install -r requirements.txt
   ```

4. **Install Playwright browsers** (downloads ~170 MB Chromium binary)  
   ```bash
   playwright install chromium
   playwright install-deps          # Linux only – installs OS-level libs
   ```

5. **Copy env file**  
   ```bash
   cp .env.example .env
   # Edit .env if you want to change port or schedule interval
   ```

6. **Run locally**  
   ```bash
   # Run scrapers once immediately
   python orchestrator.py --run-now

   # Start the web server (also starts scheduler)
   python api.py
   # Open http://localhost:8000
   ```

### Docker (recommended for production)

```bash
docker-compose up --build
# Open http://localhost:8000
```

---

## 8. Legal & Ethical Notes

| Site | robots.txt status | ToS notes | Our approach |
|------|------------------|-----------|-------------|
| `sg.jobstreet.com` | Disallows aggressive bots; allows crawlers that respect `Crawl-delay` | Commercial redistribution prohibited | Non-commercial, personal aggregator; 3 s delay between pages; respect `Crawl-delay` |
| `mycareersfuture.gov.sg` | API is publicly documented; no disallow on the API path | Government open data | Calling the official public API — fully permitted |
| `internsg.com` | `Crawl-delay: 10` in robots.txt | Non-commercial scraping generally tolerated | 10 s delay minimum; no storing of personal data |
| `jobs.careers.gov.sg` | Government portal, no explicit disallow for crawlers | Public sector open listings | 5 s delay; no personal data stored |

**General notes embedded in all code files:**
- For personal, non-commercial use only.
- Do not increase concurrency or remove delays — doing so may cause service disruption.
- Do not re-distribute scraped data commercially.
- Review each site's ToS before deploying publicly.

---

## 9. Deployment Guide

### Option A – Render (recommended free tier)

1. Push repo to GitHub.
2. Create a new **Web Service** on Render, point to repo.
3. Set **Build Command:** `pip install -r requirements.txt && playwright install chromium`
4. Set **Start Command:** `python api.py`
5. Add environment variables from `.env.example`.
6. Add a **Persistent Disk** (at least 512 MB) mounted at `/app/data` and set `DB_PATH=/app/data/jobs.db`.

### Option B – PythonAnywhere

1. Upload files via the Files tab.
2. Create a virtualenv, install requirements.
3. Run `playwright install chromium` in a Bash console.
4. Set up a scheduled task for `python orchestrator.py --run-now` every 6 hours.
5. Configure a WSGI file pointing to `api:app`.

### Option C – Local with Docker Compose

```bash
docker-compose up -d
docker-compose logs -f
```

Scrapers run on container start + every 6 hours thereafter.
