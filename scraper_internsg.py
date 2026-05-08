"""
scraper_internsg.py – InternSG.com scraper using Playwright + stealth.

Page structure (discovered via browser DevTools + debug script):
  Each listing page contains a table-style layout inside .jobs-list.
  Job rows are: .jobs-list .ast-row  (row 0 is the header, skip it)

  Columns (by index):
    0 – Company name
    1 – Job title  +  <a href="/job/slug/"> link
    2 – Location
    3 – Internship period / duration
    4 – Date posted

Pagination URL pattern: /jobs/  /jobs/2/  /jobs/3/  …

Stealth: playwright-stealth patches navigator.webdriver and 20+ signals.
Delays: honours robots.txt Crawl-delay: 10 s between *listing* pages only.
        Detail pages are fetched concurrently with a shorter per-tab delay.

For personal, non-commercial use only.
"""

import asyncio
import logging
import random
from typing import Optional

from data_cleaner import extract_date, extract_salary, extract_location

logger = logging.getLogger(__name__)

SOURCE   = "internsg"
BASE_URL = "https://www.internsg.com"

# Words that carry no discriminating power when title-filtering.
# Kept deliberately small so genuine tech sub-disciplines are not stripped.
_FILTER_STOPWORDS = frozenset({
    "intern", "internship", "singapore", "sg",
    "the", "and", "or", "in", "at", "for", "a", "an", "of",
})

# ── Listing-page delays (honouring robots.txt Crawl-delay: 10) ────────────
MIN_LISTING_DELAY = 10.0
MAX_LISTING_DELAY = 14.0

# ── Detail-page concurrent fetch config ───────────────────────────────────
# Each tab introduces its own polite delay before navigating, so the
# aggregate request rate stays well within site limits even at concurrency=6.
DETAIL_CONCURRENCY = 6       # simultaneous Playwright tabs
DETAIL_DELAY_MIN   = 2.0     # per-tab pre-navigation pause (seconds)
DETAIL_DELAY_MAX   = 4.5
DETAIL_TIMEOUT_MS  = 35_000  # per-page navigation timeout

# CSS selectors tried in order when extracting description text
DESCRIPTION_SELECTORS = [
    ".job-description",
    ".entry-content",
    "article .entry-content",
    "main article",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


# ---------------------------------------------------------------------------
# URL helper
# ---------------------------------------------------------------------------

def _title_matches(title: str, search_terms: list[str]) -> bool:
    """
    Return True if the job title contains at least one meaningful keyword
    extracted from any of the search terms.

    InternSG's listing pages show ALL roles (including non-tech), so this
    post-filter is our only relevance gate for this source.
    """
    title_lower = title.lower()
    for term in search_terms:
        keywords = [
            w.lower() for w in term.split()
            if w.lower() not in _FILTER_STOPWORDS and len(w) > 2
        ]
        if any(kw in title_lower for kw in keywords):
            return True
    return False


def _listing_url(page_num: int) -> str:
    """Return the listing URL for a given page number (1-indexed)."""
    return f"{BASE_URL}/jobs/" if page_num == 1 else f"{BASE_URL}/jobs/{page_num}/"


# ---------------------------------------------------------------------------
# Listing-page parser
# ---------------------------------------------------------------------------

async def _parse_listing_page(page) -> list[dict]:
    """
    Extract job stubs from the current listing page.

    Row structure: .jobs-list .ast-row
      col 0 – Company
      col 1 – Title + link
      col 2 – Location
      col 3 – Period
      col 4 – Date posted

    Returns a list of dicts with an empty 'description' field to be filled
    later by the concurrent detail-fetching stage.
    """
    rows = await page.query_selector_all(".jobs-list .ast-row")
    jobs: list[dict] = []

    for i, row in enumerate(rows):
        if i == 0:
            continue  # skip header row

        cols = await row.query_selector_all("[class*='ast-col']")
        if len(cols) < 4:
            continue

        try:
            company     = (await cols[0].inner_text()).strip().split("\n")[0].strip()
            title       = (await cols[1].inner_text()).strip().split("\n")[0].strip()
            location    = (await cols[2].inner_text()).strip() if len(cols) > 2 else "Singapore"
            date_posted = (await cols[4].inner_text()).strip() if len(cols) > 4 else None

            link = await cols[1].query_selector("a[href*='/job/']")
            url  = await link.get_attribute("href") if link else ""
            if not url:
                link = await row.query_selector("a[href*='/job/']")
                url  = await link.get_attribute("href") if link else ""

            if not url or not title:
                continue

            if url.startswith("/"):
                url = BASE_URL + url

            # Normalise listing-table date to ISO-8601 immediately
            iso_date = extract_date(date_posted) if date_posted else None

            jobs.append({
                "title":       title,
                "company":     company,
                "location":    location,
                "description": "",   # populated during detail-fetch stage
                "url":         url,
                "source":      SOURCE,
                "date_posted": iso_date,
                "salary":      "",
            })
        except Exception as exc:
            logger.debug("[%s] Row parse error: %s", SOURCE, exc)

    return jobs


# ---------------------------------------------------------------------------
# Concurrent detail-page fetching
# ---------------------------------------------------------------------------

async def _fetch_one_description(
    context,
    job: dict,
    semaphore: asyncio.Semaphore,
    completed: list[int],   # single-element mutable counter [n]
    total: int,
) -> None:
    """
    Open a fresh page for one job, extract its description, then close the page.

    Uses the semaphore to cap concurrent open tabs at DETAIL_CONCURRENCY.
    All mutations are to the job dict in-place:
      - job["description"] is always populated from the CSS selector
      - job["salary"] and job["location"] are refined from the page body using
        deterministic regex extractors (data_cleaner).  No LLM calls are made.
    """
    from playwright_stealth import stealth_async

    async with semaphore:
        page = await context.new_page()
        try:
            await stealth_async(page)

            # Polite per-tab jitter – spread requests across the window
            await asyncio.sleep(random.uniform(DETAIL_DELAY_MIN, DETAIL_DELAY_MAX))

            await page.goto(
                job["url"],
                wait_until="domcontentloaded",
                timeout=DETAIL_TIMEOUT_MS,
            )

            # Capture full page text for metadata extraction before narrowing to
            # the description selector – the date, location, and salary often
            # appear outside the description container.
            try:
                page_text = await page.inner_text("body")
            except Exception:
                page_text = ""

            for sel in DESCRIPTION_SELECTORS:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        job["description"] = text
                        break

            # ── Deterministic metadata extraction ─────────────────────────
            # Salary and location are extracted from the first 2 000 chars of
            # the page body using regex rules (data_cleaner).  No network calls.
            if page_text:
                snippet = page_text[:2_000]

                salary = extract_salary(snippet)
                if salary and not job.get("salary"):
                    job["salary"] = salary

                location = extract_location(snippet[:1_000])
                if location and location != "Singapore":
                    job["location"] = location

                # Fallback: ISO-8601 date from page body if listing table had none
                if not job.get("date_posted"):
                    date = extract_date(snippet[:500])
                    if date:
                        job["date_posted"] = date

                logger.debug(
                    "[%s] Cleaned metadata for %s – salary=%r location=%r date=%r",
                    SOURCE, job["url"],
                    job.get("salary"), job.get("location"), job.get("date_posted"),
                )

        except Exception as exc:
            logger.warning("[%s] Detail page failed (%s): %s", SOURCE, job["url"], exc)
            job["description"] = ""

        finally:
            await page.close()

            completed[0] += 1
            n = completed[0]
            # Log every 10 completions and always on the final job
            if n % 10 == 0 or n == total:
                logger.info("[%s] Fetched description %d / %d", SOURCE, n, total)


async def _fetch_descriptions_concurrent(
    context,
    jobs: list[dict],
    concurrency: int = DETAIL_CONCURRENCY,
) -> None:
    """
    Dispatch detail-page fetches for all jobs concurrently, bounded by
    a semaphore so at most `concurrency` Playwright tabs are open at once.

    Estimated wall-clock time vs. the old sequential approach:
      Sequential (1 tab × 10-14 s/job):  110 jobs ≈ 22 minutes
      Concurrent (6 tabs × 2-4.5 s/job): 110 jobs ≈  1.5 minutes
    """
    semaphore  = asyncio.Semaphore(concurrency)
    completed  = [0]          # mutable counter shared across tasks
    total      = len(jobs)

    tasks = [
        _fetch_one_description(context, job, semaphore, completed, total)
        for job in jobs
    ]

    # return_exceptions=True ensures one page timeout never cancels the batch
    await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Main async scraper
# ---------------------------------------------------------------------------

async def _scrape_async(max_pages: int = 5, search_terms: Optional[list[str]] = None) -> list[dict]:
    try:
        from playwright.async_api import async_playwright
        from playwright_stealth import stealth_async
    except ImportError as exc:
        logger.error(
            "[%s] Missing dependency: %s. Run: pip install playwright playwright-stealth",
            SOURCE, exc,
        )
        return []

    all_jobs: list[dict] = []
    user_agent = random.choice(USER_AGENTS)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1366, "height": 768},
            locale="en-SG",
            timezone_id="Asia/Singapore",
        )

        # ── Phase 1: Listing pages (sequential, respects Crawl-delay: 10) ──
        list_page = await context.new_page()
        await stealth_async(list_page)

        for page_num in range(1, max_pages + 1):
            url = _listing_url(page_num)
            logger.info("[%s] Listing page %d / %d: %s", SOURCE, page_num, max_pages, url)

            try:
                await list_page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await list_page.wait_for_selector(".jobs-list", timeout=15_000)
                await asyncio.sleep(random.uniform(2, 3))
            except Exception as exc:
                logger.warning("[%s] Navigation error on listing page %d: %s", SOURCE, page_num, exc)
                break

            page_jobs = await _parse_listing_page(list_page)
            logger.info("[%s] Page %d: found %d job rows", SOURCE, page_num, len(page_jobs))

            if not page_jobs:
                logger.info("[%s] No rows on page %d – stopping pagination", SOURCE, page_num)
                break

            all_jobs.extend(page_jobs)

            next_link = await list_page.query_selector(
                "a[href*='/jobs/']:has-text('Next'), .next.page-numbers"
            )
            if not next_link and page_num > 1:
                logger.info("[%s] No 'Next' link – reached end of listings", SOURCE)
                break

            if page_num < max_pages:
                delay = random.uniform(MIN_LISTING_DELAY, MAX_LISTING_DELAY)
                logger.debug("[%s] Sleeping %.1f s before next listing page", SOURCE, delay)
                await asyncio.sleep(delay)

        await list_page.close()

        # ── Phase 2: Detail pages (concurrent, semaphore-bounded) ──────────
        if all_jobs:
            logger.info(
                "[%s] Fetching descriptions for %d jobs "
                "(concurrency=%d, ~%.0f s estimated)…",
                SOURCE,
                len(all_jobs),
                DETAIL_CONCURRENCY,
                (len(all_jobs) / DETAIL_CONCURRENCY) * ((DETAIL_DELAY_MIN + DETAIL_DELAY_MAX) / 2),
            )
            await _fetch_descriptions_concurrent(context, all_jobs)

        await browser.close()

    # Drop jobs whose description could not be fetched (blank after attempts)
    filled  = [j for j in all_jobs if j.get("description")]
    skipped = len(all_jobs) - len(filled)
    if skipped:
        logger.info("[%s] Dropped %d job(s) with no description", SOURCE, skipped)

    # Apply title-keyword post-filter when targeted search terms are supplied.
    # InternSG lists all roles on /jobs/ with no keyword-search URL, so this is
    # the only practical relevance gate for this source.
    if search_terms:
        before  = len(filled)
        filled  = [j for j in filled if _title_matches(j["title"], search_terms)]
        removed = before - len(filled)
        logger.info(
            "[%s] Keyword post-filter: removed %d irrelevant job(s), %d remaining",
            SOURCE, removed, len(filled),
        )

    return filled


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape(
    max_pages:    int                  = 5,
    search_terms: Optional[list[str]] = None,
) -> list[dict]:
    """
    Synchronous entry point for orchestrator.py.

    Args:
        max_pages:    Maximum listing pages to scrape (≈22 jobs per page).
        search_terms: Targeted query strings for title-keyword post-filtering.
                      When supplied, non-matching jobs are dropped before return.

    Returns:
        List of normalised job dicts with descriptions populated.
    """
    logger.info(
        "[%s] Starting scrape (max_pages=%d, filter_terms=%s)",
        SOURCE, max_pages, search_terms,
    )
    jobs = asyncio.run(_scrape_async(max_pages, search_terms))
    logger.info("[%s] Scrape complete – %d jobs", SOURCE, len(jobs))
    return jobs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = scrape(max_pages=1, search_terms=["backend intern", "software engineer intern"])
    for j in results[:5]:
        print(j["title"], "|", j["company"], "|", j["location"])
    print(f"Total: {len(results)}")
