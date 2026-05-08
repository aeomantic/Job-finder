"""
query_generator.py – Generate targeted job search queries from resume.pdf.

Reads the candidate's resume once per scrape run and asks a fast, cheap LLM
(Groq llama-3.1-8b-instant) to produce 3-6 highly specific search strings
tailored to their technical profile.

Priority order for query source:
  1. SEARCH_TERMS_OVERRIDE env var  – comma-separated, skips the LLM entirely
  2. LLM generation from resume.pdf – requires GROQ_API_KEY
  3. DEFAULT_QUERIES                – hard-coded fallback, always available

Public entry point
------------------
  generate_search_queries(resume_pdf_path, groq_key) -> list[str]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GROQ_MODEL = "llama-3.1-8b-instant"
MIN_QUERIES = 3
MAX_QUERIES = 6

# Used when resume is missing, Groq is unavailable, or the LLM returns garbage.
# Covers the most common CS / IT internship roles in Singapore.
DEFAULT_QUERIES: list[str] = [
    "software engineer intern",
    "backend developer intern",
    "data science intern",
    "AI machine learning intern",
    "full stack developer intern",
]

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a recruitment specialist for Singapore tech internships.
Your sole task is to convert a candidate's resume into a short list of precise,
high-signal job search strings.
Respond ONLY with a valid JSON array of strings.
Do NOT include markdown fences, explanations, or any text outside the JSON array.\
"""

_USER_PROMPT_TMPL = """\
CANDIDATE RESUME (truncated to 3 000 characters):
---
{resume_text}
---

Generate between {min_q} and {max_q} targeted job search strings for Singapore
tech internship portals (MyCareersFuture, JobStreet, InternSG, Careers@Gov).

Rules – follow them strictly:
  1. Each string must be 2–5 words (e.g. "Backend Java Intern").
  2. Prioritise the candidate's STRONGEST and most SPECIFIC technical skills,
     languages, and frameworks. Do not repeat vague terms.
  3. Every string must clearly indicate an internship or entry-level role.
  4. Exclude all non-technical disciplines (HR, Finance, Healthcare, Law, etc.).
  5. Do NOT add markdown fences or any text outside the JSON array.

CORRECT output example:
["Backend Java Intern", "Spring Boot Developer Intern", "AI Engineer Internship", \
"Supabase Full Stack Intern"]

Output ONLY the JSON array now:\
"""


# ---------------------------------------------------------------------------
# Async implementation
# ---------------------------------------------------------------------------

def _clean_llm_json(raw: str) -> str:
    """Strip markdown code fences from LLM output before JSON parsing."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\n?```\s*$", "", raw)
    return raw.strip()


async def _call_groq(resume_text: str, groq_key: str) -> list[str]:
    """
    Call the Groq API and return a validated list of search query strings.
    Raises on any error so the caller can fall back gracefully.
    """
    from groq import AsyncGroq

    client   = AsyncGroq(api_key=groq_key)
    user_msg = _USER_PROMPT_TMPL.format(
        resume_text = resume_text[:3_000],
        min_q       = MIN_QUERIES,
        max_q       = MAX_QUERIES,
    )

    resp = await client.chat.completions.create(
        model    = GROQ_MODEL,
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        temperature = 0.3,
        max_tokens  = 200,
    )

    raw = resp.choices[0].message.content.strip()
    logger.debug("[query_generator] Raw LLM response: %r", raw)

    raw     = _clean_llm_json(raw)
    queries: list = json.loads(raw)

    if not isinstance(queries, list):
        raise ValueError(f"Expected a JSON array, got: {type(queries).__name__}")

    # Sanitise: keep only non-empty strings, cap at MAX_QUERIES
    queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()][:MAX_QUERIES]

    if len(queries) < MIN_QUERIES:
        raise ValueError(
            f"LLM returned only {len(queries)} quer{'y' if len(queries) == 1 else 'ies'} "
            f"(minimum is {MIN_QUERIES}): {queries}"
        )

    return queries


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_search_queries(
    resume_pdf_path: str,
    groq_key: Optional[str] = None,
) -> list[str]:
    """
    Return a list of targeted job search strings for this candidate.

    Resolution order:
      1. SEARCH_TERMS_OVERRIDE env var (comma-separated) – no LLM call needed.
      2. Groq LLM generation from resume_pdf_path.
      3. DEFAULT_QUERIES fallback.

    Args:
        resume_pdf_path: Path to the candidate's resume PDF.
        groq_key:        Groq API key; reads GROQ_API_KEY env var if omitted.

    Returns:
        A list of 3–6 search query strings.
    """
    # ── Priority 1: manual override via env var ──────────────────────────
    override = os.getenv("SEARCH_TERMS_OVERRIDE", "").strip()
    if override:
        terms = [t.strip() for t in override.split(",") if t.strip()]
        if terms:
            logger.info("[query_generator] Using SEARCH_TERMS_OVERRIDE: %s", terms)
            return terms

    # ── Priority 2: LLM generation ───────────────────────────────────────
    api_key = groq_key or os.getenv("GROQ_API_KEY", "")
    if not api_key:
        logger.warning(
            "[query_generator] GROQ_API_KEY not set – falling back to default queries: %s",
            DEFAULT_QUERIES,
        )
        return DEFAULT_QUERIES

    if not os.path.exists(resume_pdf_path):
        logger.warning(
            "[query_generator] Resume not found at '%s' – falling back to default queries.",
            resume_pdf_path,
        )
        return DEFAULT_QUERIES

    # Load resume text using the shared helper in evaluator (caches the PDF parse)
    try:
        from evaluator import load_resume_text
        resume_text = load_resume_text(resume_pdf_path)
    except Exception as exc:
        logger.warning("[query_generator] Could not load resume text (%s) – using defaults.", exc)
        return DEFAULT_QUERIES

    if not resume_text:
        logger.warning("[query_generator] Resume text is empty – using default queries.")
        return DEFAULT_QUERIES

    logger.info(
        "[query_generator] Resume text extracted: %d chars. First 200: %r",
        len(resume_text),
        resume_text[:200],
    )
    logger.debug("[query_generator] Full resume text passed to LLM:\n%s", resume_text[:3_000])

    try:
        queries = asyncio.run(_call_groq(resume_text, api_key))
        logger.info("[query_generator] Generated %d search queries: %s", len(queries), queries)
        return queries
    except Exception as exc:
        logger.warning(
            "[query_generator] LLM query generation failed (%s) – falling back to: %s",
            exc, DEFAULT_QUERIES,
        )
        return DEFAULT_QUERIES
