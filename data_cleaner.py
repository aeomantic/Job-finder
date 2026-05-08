"""
data_cleaner.py – Deterministic metadata extraction utilities.

Replaces the LLM-based Ministral-3B sanitisation pipeline with fast,
free, rule-based extraction using dateparser and re.

No network calls, no API keys, no rate limits.

Public API
----------
  extract_date(raw_text)     → ISO-8601 date string (YYYY-MM-DD) or None
  extract_salary(raw_text)   → Human-readable SGD salary string or None
  extract_location(raw_text) → Singapore location string or None
  clean_job_metadata(raw_text) → dict combining all three fields
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

# Relative shorthand: "6d ago", "3days ago", "2 wks ago", "1 month ago"
_RELATIVE_SHORT = re.compile(
    r"\b(\d+)\s*"
    r"(d|day|days|h|hr|hrs|hour|hours|w|wk|wks|week|weeks|m|mo|month|months)"
    r"\s*ago\b",
    re.IGNORECASE,
)

_UNIT_MAP: dict[str, str] = {
    "d": "day",   "day":   "day",   "days":   "day",
    "h": "hour",  "hr":    "hour",  "hrs":    "hour",  "hour": "hour",  "hours": "hour",
    "w": "week",  "wk":    "week",  "wks":    "week",  "week": "week",  "weeks": "week",
    "m": "month", "mo":    "month", "month":  "month", "months": "month",
}

# Absolute date patterns tried before handing off to dateparser.
# Intentionally narrow to avoid false positives on long body text.
_ABS_DATE_RE = re.compile(
    r"\b(?:"
    # DD Mon YYYY  or  DD-Mon-YYYY  or  DD/Mon/YYYY
    r"\d{1,2}[\s/\-](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s/\-]\d{2,4}"
    # Mon DD, YYYY  or  Mon DD YYYY
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s,]+\d{1,2}[\s,]+\d{4}"
    # YYYY-MM-DD  (strict ISO-8601 already)
    r"|\d{4}-\d{2}-\d{2}"
    # DD/MM/YYYY  or  DD-MM-YYYY
    r"|\d{1,2}[/\-]\d{1,2}[/\-]\d{4}"
    r")\b",
    re.IGNORECASE,
)


def extract_date(raw_text: str) -> Optional[str]:
    """
    Extract and normalise a job-posting date from free-form text.

    Handles:
      - Relative shorthand : "6d ago", "3 days ago", "2 wks ago"
      - Natural language   : "just posted", "today", "yesterday"
      - Absolute dates     : "30 Apr 2026", "Apr 30, 2026", "2026-04-30"

    Strategy: regex isolates a candidate string first; dateparser is called
    only on that short substring, not the full body, to prevent false positives
    and keep latency low.

    Returns an ISO-8601 date string (YYYY-MM-DD), or None if nothing found.
    """
    if not raw_text or not raw_text.strip():
        return None

    try:
        import dateparser
    except ImportError:
        logger.warning("[data_cleaner] dateparser not installed – date extraction skipped")
        return None

    now = datetime.now(timezone.utc)
    _settings = {
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DAY_OF_MONTH":      "first",
        "RELATIVE_BASE":            now,
        "TO_TIMEZONE":              "UTC",
    }

    text = raw_text.strip()

    # ── 1. Relative shorthand ("6d ago" → "6 days ago") ──────────────────
    rel_match = _RELATIVE_SHORT.search(text)
    if rel_match:
        n        = rel_match.group(1)
        unit_raw = rel_match.group(2).lower()
        unit     = _UNIT_MAP.get(unit_raw, "day")
        parsed   = dateparser.parse(f"{n} {unit}s ago", settings=_settings)
        if parsed:
            return parsed.strftime("%Y-%m-%d")

    # ── 2. Natural-language relative terms ───────────────────────────────
    text_lower = text.lower()
    for phrase in ("just posted", "today", "yesterday"):
        if phrase in text_lower:
            parsed = dateparser.parse(phrase, settings=_settings)
            if parsed:
                return parsed.strftime("%Y-%m-%d")

    # ── 3. Absolute date pattern ──────────────────────────────────────────
    abs_match = _ABS_DATE_RE.search(text)
    if abs_match:
        parsed = dateparser.parse(abs_match.group(0), settings=_settings)
        if parsed:
            # Sanity-check: reject dates more than 2 years in either direction
            if abs((parsed - now).days) <= 730:
                return parsed.strftime("%Y-%m-%d")

    return None


# ---------------------------------------------------------------------------
# Salary extraction
# ---------------------------------------------------------------------------

# Matches: "SGD 1,500", "S$2,000", "$1500", "$4,500 – 6,000 / month"
_SALARY_RE = re.compile(
    r"(?:SGD|S\$|\$)\s*"
    r"(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})?)"
    r"(?:\s*[-–—]\s*(?:SGD|S\$|\$)?\s*(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})?))?",
    re.IGNORECASE,
)

# Pay-period indicator appearing near the salary figure
_PERIOD_RE = re.compile(
    r"\b(?:per\s+(?:month|mth|week|wk|hour|hr|day)"
    r"|/\s*(?:month|mth|week|wk|hour|hr|day|mo))\b",
    re.IGNORECASE,
)

_UNPAID_RE = re.compile(r"\bunpaid\b", re.IGNORECASE)


def _fmt_amount(raw: str) -> str:
    """Format a numeric string with thousands-separator commas."""
    try:
        return f"{int(float(raw.replace(',', '').replace(' ', ''))):,}"
    except ValueError:
        return raw


def extract_salary(raw_text: str) -> Optional[str]:
    """
    Extract an SGD salary or stipend amount from free-form text.

    Returns a human-readable string such as:
      "SGD 1,500 – 2,000 / month"
      "SGD 3,500 / month"
      "Unpaid"

    Returns None if no salary information is detected.
    """
    if not raw_text:
        return None

    if _UNPAID_RE.search(raw_text):
        return "Unpaid"

    match = _SALARY_RE.search(raw_text)
    if not match:
        return None

    low_raw  = match.group(1).replace(" ", "").replace(",", "")
    high_raw = match.group(2)

    # Reject implausibly small values (e.g. phone number fragments, "S$5")
    try:
        if int(float(low_raw)) < 100:
            return None
    except ValueError:
        return None

    # Detect pay period from the ±80-character window around the match
    ctx_start = max(0, match.start() - 30)
    ctx_end   = min(len(raw_text), match.end() + 80)
    context   = raw_text[ctx_start:ctx_end]

    period_match = _PERIOD_RE.search(context)
    if period_match:
        p = period_match.group(0).lower()
        if "month" in p or "mth" in p or "mo" in p:
            period = "/ month"
        elif "week" in p or "wk" in p:
            period = "/ week"
        elif "hour" in p or "hr" in p:
            period = "/ hour"
        elif "day" in p:
            period = "/ day"
        else:
            period = "/ month"
    else:
        period = "/ month"  # most Singapore internship stipends are monthly

    if high_raw:
        return f"SGD {_fmt_amount(low_raw)} – {_fmt_amount(high_raw)} {period}"
    return f"SGD {_fmt_amount(low_raw)} {period}"


# ---------------------------------------------------------------------------
# Location extraction
# ---------------------------------------------------------------------------

# Ordered most-specific → least-specific so the first match wins.
_SG_LOCATIONS: list[str] = [
    # One-north / Science Park cluster
    "Buona Vista", "one-north", "Biopolis", "Fusionopolis",
    # CBD & city fringe
    "Raffles Place", "Marina Bay", "Tanjong Pagar", "Bugis",
    "Clarke Quay", "Chinatown", "Little India",
    # Orchard / Central
    "Orchard", "Somerset", "Novena", "Newton",
    # East
    "Tampines", "Pasir Ris", "Bedok", "Changi", "Paya Lebar",
    # North-east
    "Serangoon", "Hougang", "Punggol", "Sengkang", "Ang Mo Kio",
    # Central-north
    "Bishan", "Toa Payoh", "Braddell",
    # North
    "Woodlands", "Sembawang", "Yishun", "Canberra", "Kranji",
    # West
    "Jurong East", "Jurong West", "Jurong Lake", "Lakeside",
    "Choa Chu Kang", "Bukit Gombak", "Bukit Batok",
    "Clementi", "Dover", "Kent Ridge", "NUS", "NTU", "Queenstown",
    "Redhill", "Commonwealth", "Tiong Bahru", "Outram",
    # Broad regions (matched last – least specific)
    "Central Business District", "CBD",
    "North-East", "North East",
    "Central Singapore", "Central Region",
    "North Singapore", "North Region",
    "East Singapore",  "East Region",
    "West Singapore",  "West Region",
    "Islandwide", "Island-wide",
    # Generic fallback
    "Singapore",
]

_LOCATION_RE = re.compile(
    r"\b(" + "|".join(re.escape(loc) for loc in _SG_LOCATIONS) + r")\b",
    re.IGNORECASE,
)

# Lookup for canonical capitalisation (avoids .title() mangling "CBD" etc.)
_LOCATION_CANONICAL: dict[str, str] = {loc.lower(): loc for loc in _SG_LOCATIONS}


def extract_location(raw_text: str) -> Optional[str]:
    """
    Extract the first recognisable Singapore location from free-form text.

    Searches known planning areas, landmark districts, and broad regional
    labels.  Returns None (not the generic "Singapore") when nothing specific
    is found, so callers can apply their own fallback.

    Returns the location in canonical capitalisation (e.g. "Raffles Place",
    "CBD", "Buona Vista").
    """
    if not raw_text:
        return None

    match = _LOCATION_RE.search(raw_text)
    if not match:
        return None

    return _LOCATION_CANONICAL.get(match.group(1).lower(), match.group(1))


# ---------------------------------------------------------------------------
# Combined helper
# ---------------------------------------------------------------------------

def clean_job_metadata(raw_text: str) -> dict:
    """
    Run all three extractors against the same raw text in a single call.

    Returns a dict whose keys are always present (values may be None):
      {
        "date_posted": str | None,   # ISO-8601 YYYY-MM-DD
        "location":    str | None,
        "salary":      str | None,
      }
    """
    return {
        "date_posted": extract_date(raw_text),
        "location":    extract_location(raw_text),
        "salary":      extract_salary(raw_text),
    }


# ---------------------------------------------------------------------------
# Structured modal parser
# ---------------------------------------------------------------------------

class JobDataParser:
    """
    Deterministic parser for InternSG job modal text blocks.

    The modal inner_text has a consistent label-then-value structure:

        Company\n
        Aramco Trading Singapore\n
        Designation\n
        Analytics Intern (6 Months Full Time)\n
        Date Listed\n
        30 Apr 2026\n

    Strategy
    --------
    1. Split on newlines and strip every line.
    2. Match each line against a compiled label regex (case-insensitive,
       anchored so partial matches like "Company Name" are not confused
       with the plain "Company" label).
    3. Return the next non-empty line as the field value.
    4. Pass extracted date strings through dateparser for ISO-8601 normalisation.
    """

    # Anchored, case-insensitive label patterns.
    # \s* inside allows for the rare "Date  Listed" double-space variant.
    _RE_COMPANY     = re.compile(r"^\s*company\s*$",          re.IGNORECASE)
    _RE_DESIGNATION = re.compile(r"^\s*designation\s*$",      re.IGNORECASE)
    _RE_DATE_LISTED = re.compile(r"^\s*date\s+listed\s*$",    re.IGNORECASE)

    # ---------------------------------------------------------------------------

    def _value_after_label(
        self,
        lines: list[str],
        label_re: re.Pattern,
    ) -> str | None:
        """
        Scan `lines` for the first line matching `label_re`, then return the
        next non-empty line as the value.

        Returns None when:
          - the label is not present in the text, or
          - the label is the last line (no value follows it).
        """
        for i, line in enumerate(lines):
            if label_re.match(line):
                for j in range(i + 1, len(lines)):
                    candidate = lines[j].strip()
                    if candidate:
                        return candidate
        return None

    # ---------------------------------------------------------------------------

    def _normalise_date(self, raw_date: str | None) -> str | None:
        """
        Convert a raw date string (e.g. "30 Apr 2026", "6d ago") to strict
        ISO-8601 (YYYY-MM-DD) via dateparser.

        Returns None when:
          - raw_date is None or empty, or
          - dateparser cannot confidently parse the string, or
          - dateparser is not installed.
        """
        if not raw_date:
            return None
        try:
            import dateparser
            parsed = dateparser.parse(
                raw_date,
                settings={
                    "RETURN_AS_TIMEZONE_AWARE": False,
                    "PREFER_DAY_OF_MONTH":      "first",
                    "PREFER_LOCALE_DATE_ORDER": "DMY",   # Singapore date convention
                },
            )
            if parsed:
                return parsed.strftime("%Y-%m-%d")
        except Exception as exc:
            logger.debug("[JobDataParser] dateparser failed for %r: %s", raw_date, exc)
        return None

    # ---------------------------------------------------------------------------

    def parse_job_text(self, raw_text: str) -> dict:
        """
        Main entry point.  Accepts the full inner_text of a job modal and
        returns a normalised dict.

        Args:
            raw_text: Raw string from Playwright's ``page.inner_text("body")``
                      or an equivalent modal selector.

        Returns:
            {
                "company_name": str | None,
                "title":        str | None,
                "date_posted":  str | None,   # ISO-8601 YYYY-MM-DD
            }
        """
        if not raw_text:
            return {"company_name": None, "title": None, "date_posted": None}

        # Strip each line; removes trailing spaces and collapses blank lines
        # into empty strings (preserved so relative line positions stay intact).
        lines: list[str] = [line.strip() for line in raw_text.splitlines()]

        raw_company = self._value_after_label(lines, self._RE_COMPANY)
        raw_title   = self._value_after_label(lines, self._RE_DESIGNATION)
        raw_date    = self._value_after_label(lines, self._RE_DATE_LISTED)

        return {
            "company_name": raw_company,
            "title":        raw_title,
            "date_posted":  self._normalise_date(raw_date),
        }


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _SAMPLE = """
Company
Aramco Trading Singapore
aramcotrading.sg
Designation
Analytics Intern (6 Months Full Time)
Date Listed
30 Apr 2026
Job Type
Entry Level / Junior Executive
Part/TempIntern/TS
Job Period
From Jul 2026, For At Least 6 Months
"""

    _SAMPLE_MISSING_DATE = """
COMPANY
Tech Startup Pte Ltd

DESIGNATION
Backend Engineering Intern
Job Type
Internship
"""

    _SAMPLE_EXTRA_WHITESPACE = """
  Company
  Shopee Singapore

  Designation
  Data Science Intern

  Date Listed
  6d ago
"""

    parser = JobDataParser()

    print("── Sample 1 (full) ──────────────────────────────────")
    print(parser.parse_job_text(_SAMPLE))

    print("\n── Sample 2 (missing date) ──────────────────────────")
    print(parser.parse_job_text(_SAMPLE_MISSING_DATE))

    print("\n── Sample 3 (extra whitespace + relative date) ──────")
    print(parser.parse_job_text(_SAMPLE_EXTRA_WHITESPACE))

    print("\n── Sample 4 (empty string) ──────────────────────────")
    print(parser.parse_job_text(""))
