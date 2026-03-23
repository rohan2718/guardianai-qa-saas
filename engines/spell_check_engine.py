"""
engines/spell_check_engine.py — GuardianAI Spell Check Engine
=============================================================
Uses Groq API (llama-3.3-70b-versatile) to detect spelling errors
from page visible text content.

Integration:
  - Called from engines/deep_qa_engine.py inside test_page()
  - Results stored in DeepQAPageResult.spell_check
  - Persisted in PageResult.ui_summary JSONB under ["deep_qa"]["spell_check"]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
SPELL_TIMEOUT_S = 15

_SKIP_TAGS = {"script", "style", "noscript", "head", "meta", "link", "title"}

# Initialise Groq client — mirrors ai_analyzer.py pattern
try:
    from groq import Groq as _Groq
    _groq_client = _Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
except ImportError:
    _groq_client = None
    logger.debug("[spell_check] groq package not installed")


# ── Text Extraction ────────────────────────────────────────────────────────────

async def extract_page_text(page) -> str:
    """
    Extract all visible text from the page using a DOM TreeWalker.
    Skips script/style/noscript tags, hidden elements, cookie banners.
    Returns up to 3000 characters of clean joined text.
    """
    try:
        raw_text = await page.evaluate("""() => {
            const skipTags = new Set(['SCRIPT','STYLE','NOSCRIPT','HEAD','META','LINK']);
            const skipPatterns = [/cookie/i, /consent/i, /gdpr/i, /onetrust/i];
            const texts = [];

            const walker = document.createTreeWalker(
                document.body,
                NodeFilter.SHOW_TEXT,
                {
                    acceptNode: function(node) {
                        const el = node.parentElement;
                        if (!el) return NodeFilter.FILTER_REJECT;
                        if (skipTags.has(el.tagName)) return NodeFilter.FILTER_REJECT;
                        const s = window.getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden')
                            return NodeFilter.FILTER_REJECT;
                        // Skip cookie banners
                        const haystack = (el.id || '') + ' ' + (el.className || '');
                        if (skipPatterns.some(p => p.test(haystack)))
                            return NodeFilter.FILTER_REJECT;
                        return NodeFilter.FILTER_ACCEPT;
                    }
                }
            );

            let node;
            while ((node = walker.nextNode())) {
                const t = (node.textContent || '').trim();
                if (t.length > 2) texts.push(t);
            }
            return texts.join(' ');
        }""")

        if not raw_text:
            return ""

        # Normalise whitespace and truncate
        cleaned = re.sub(r"\s+", " ", raw_text).strip()
        return cleaned[:3000]

    except Exception as e:
        logger.warning(f"[spell_check] extract_page_text failed: {e}")
        return ""


# ── Groq Spell Checker ─────────────────────────────────────────────────────────

def check_spelling_with_groq(text: str, page_url: str) -> dict:
    """
    Calls Groq API synchronously to detect spelling errors.
    Returns structured dict with errors list, total, and grade.
    Safe default on any failure.
    """
    _safe_default = {"errors": [], "total": 0, "grade": "Unknown", "error": "API unavailable"}

    if not _groq_client:
        return {"errors": [], "total": 0, "grade": "N/A",
                "skipped": True, "reason": "Groq package not installed"}

    if not text or len(text.strip()) < 20:
        return {"errors": [], "total": 0, "grade": "Good", "skipped": True,
                "reason": "Insufficient text to check"}

    prompt = (
        "You are a professional proofreader. Review the following web page text and identify "
        "ONLY clear spelling mistakes (not grammar, not style, not abbreviations, not proper nouns). "
        "For each spelling error found, return: the misspelled word, the correct spelling, "
        "and the sentence context (max 60 chars). "
        'Return JSON ONLY in this exact format: {"errors": [{"wrong": "...", "correct": "...", '
        '"context": "..."}], "total": N, "grade": "Good|Needs Review|Poor"}. '
        "Grade rules: Good = 0 errors, Needs Review = 1-4 errors, Poor = 5+ errors. "
        "If no errors found return {\"errors\": [], \"total\": 0, \"grade\": \"Good\"}. "
        "Do NOT include any text outside the JSON. "
        f"Text to check:\n\n{text}"
    )

    try:
        response = _groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.05,
            max_tokens=800,
        )
        raw = response.choices[0].message.content.strip()

        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        parsed = json.loads(raw)

        # Validate structure
        if not isinstance(parsed.get("errors"), list):
            parsed["errors"] = []
        parsed["total"] = len(parsed["errors"])

        # Clamp and validate grade
        grade = parsed.get("grade", "Good")
        if grade not in ("Good", "Needs Review", "Poor"):
            n = parsed["total"]
            grade = "Good" if n == 0 else ("Needs Review" if n < 5 else "Poor")
        parsed["grade"] = grade

        logger.info(
            f"[spell_check] {page_url} → {parsed['total']} errors, grade={grade}"
        )
        return parsed

    except json.JSONDecodeError as e:
        logger.warning(f"[spell_check] JSON parse failed for {page_url}: {e}")
        return _safe_default
    except Exception as e:
        logger.error(f"[spell_check] Groq call failed for {page_url}: {e}")
        return _safe_default


# ── Main Entry Point ───────────────────────────────────────────────────────────

async def run_spell_check(page, page_url: str) -> dict:
    """
    Main entry point called from deep_qa_engine.test_page().
    Extracts visible text and runs Groq spell check with timeout protection.

    Returns dict with keys: errors, total, grade, skipped (optional), reason (optional).
    Never raises — always returns safe default on any error.
    """
    if not GROQ_API_KEY:
        return {
            "errors": [],
            "total":  0,
            "grade":  "N/A",
            "skipped": True,
            "reason": "Groq API key not configured",
        }

    try:
        # 1. Extract text (async, in-browser)
        text = await extract_page_text(page)
        if not text:
            return {
                "errors": [],
                "total":  0,
                "grade":  "Good",
                "skipped": True,
                "reason": "No extractable text on page",
            }

        # 2. Call Groq in a thread so we can enforce a hard timeout
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = loop.run_in_executor(
                executor,
                check_spelling_with_groq,
                text,
                page_url,
            )
            try:
                result = await asyncio.wait_for(future, timeout=SPELL_TIMEOUT_S)
                return result
            except (asyncio.TimeoutError, FuturesTimeout):
                logger.warning(
                    f"[spell_check] Groq timed out after {SPELL_TIMEOUT_S}s for {page_url}"
                )
                return {
                    "errors": [],
                    "total":  0,
                    "grade":  "Unknown",
                    "skipped": True,
                    "reason": "Spell check timed out",
                }

    except Exception as e:
        logger.error(f"[spell_check] run_spell_check failed for {page_url}: {e}")
        return {
            "errors": [],
            "total":  0,
            "grade":  "Unknown",
            "skipped": True,
            "reason": f"Internal error: {str(e)[:100]}",
        }