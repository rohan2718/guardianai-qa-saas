"""
engines/broken_links_engine.py — GuardianAI Enhanced Broken Link Detection
===========================================================================
Performs comprehensive internal + external link validation.
Uses concurrent async HEAD requests for speed.
Classifies broken links by type: navigation, asset, external, redirect-chain.

Integration:
  - Called from crawler.py classify_links()
  - Results stored in page_obj["broken_links_detail"]
  - Used by scoring_engine.compute_functional_score()
"""

import asyncio
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# HTTP status codes that indicate the link works (auth-wall is not broken)
IGNORABLE_STATUS = frozenset({401, 403, 405, 406, 407, 429})

# Extensions that are assets, not navigation links
ASSET_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".webm", ".ogg", ".mp3", ".wav",
    ".css", ".js", ".json", ".xml", ".pdf",
    ".zip", ".gz", ".tar", ".exe", ".dmg",
})

MAX_EXTERNAL_CHECKS = 20   # Cap external link checks per page
MAX_INTERNAL_CHECKS = 50   # Cap internal link checks per page
LINK_TIMEOUT_MS     = 8000


def _classify_url(base_url: str, url: str) -> str:
    """Returns 'internal', 'external', or 'asset'."""
    try:
        parsed_base = urlparse(base_url)
        parsed_url  = urlparse(url)
        path_lower  = parsed_url.path.lower()
        _, ext      = __import__("os.path", fromlist=["splitext"]).splitext(path_lower)

        if ext in ASSET_EXTENSIONS:
            return "asset"
        if parsed_base.netloc == parsed_url.netloc:
            return "internal"
        return "external"
    except Exception:
        return "unknown"


async def _check_link(context, url: str, timeout_ms: int = LINK_TIMEOUT_MS) -> dict:
    """
    Checks a single URL via HEAD request (falls back to GET if HEAD blocked).
    Returns structured result dict.
    """
    result = {
        "url":     url,
        "status":  None,
        "error":   None,
        "method":  "HEAD",
        "broken":  False,
        "redirect_url": None,
    }

    try:
        response = await context.request.fetch(
            url,
            method="HEAD",
            timeout=timeout_ms,
            headers={
                "User-Agent": "GuardianAI-LinkChecker/2.0",
                "Accept": "text/html,application/xhtml+xml,*/*",
            },
            max_redirects=5,
        )
        result["status"] = response.status

        # Track final URL after redirects
        if response.url != url:
            result["redirect_url"] = response.url

        try:
            await response.dispose()
        except Exception:
            pass

        if response.status in IGNORABLE_STATUS:
            result["broken"] = False
        elif response.status >= 400:
            result["broken"] = True
        else:
            result["broken"] = False

        return result

    except Exception as e:
        err_lower = str(e).lower()

        # Try GET fallback if HEAD was blocked/refused
        if "method not allowed" in err_lower or "405" in err_lower:
            try:
                response = await context.request.fetch(
                    url,
                    method="GET",
                    timeout=timeout_ms,
                    headers={"User-Agent": "GuardianAI-LinkChecker/2.0"},
                    max_redirects=5,
                )
                result["status"] = response.status
                result["method"] = "GET"
                result["broken"] = response.status >= 400 and response.status not in IGNORABLE_STATUS
                try:
                    await response.dispose()
                except Exception:
                    pass
                return result
            except Exception as e2:
                result["error"]  = str(e2)[:120]
                result["broken"] = True
                return result

        # Timeout or network error = treat as broken
        if "timeout" in err_lower or "err_" in err_lower or "connection" in err_lower:
            result["error"]  = f"Network error: {str(e)[:100]}"
            result["broken"] = True
        else:
            result["error"]  = str(e)[:120]
            result["broken"] = True

        return result


async def check_all_links(page, base_url: str, context,
                           check_external: bool = True) -> dict:
    """
    Main entry point. Extracts all links from page and validates them.

    Returns:
    {
        "internal_broken":  [{ url, status, error }],
        "external_broken":  [{ url, status, error }],
        "internal_links":   [str],
        "external_links":   [str],
        "redirect_chains":  [{ url, redirect_to }],
        "total_checked":    int,
        "total_broken":     int,
    }
    """
    try:
        all_hrefs = await page.evaluate("""() => {
            const seen = new Set();
            const links = [];
            document.querySelectorAll('a[href]').forEach(a => {
                try {
                    const href = a.href;
                    if (!href) return;
                    if (href.startsWith('mailto:') || href.startsWith('tel:') ||
                        href.startsWith('javascript:') || href.startsWith('#') ||
                        href.startsWith('data:')) return;
                    if (seen.has(href)) return;
                    seen.add(href);
                    links.push({
                        url:  href,
                        text: (a.textContent || '').trim().substring(0, 60),
                    });
                } catch(e) {}
            });
            return links;
        }""")
    except Exception as e:
        logger.warning(f"Link extraction failed: {e}")
        return {
            "internal_broken":  [],
            "external_broken":  [],
            "internal_links":   [],
            "external_links":   [],
            "redirect_chains":  [],
            "total_checked":    0,
            "total_broken":     0,
        }

    internal_links = []
    external_links = []
    asset_links    = []

    for item in all_hrefs:
        url  = item["url"]
        kind = _classify_url(base_url, url)

        if kind == "internal":
            internal_links.append(url)
        elif kind == "external":
            external_links.append(url)
        elif kind == "asset":
            asset_links.append(url)

    # Deduplicate and cap
    internal_to_check = list(dict.fromkeys(internal_links))[:MAX_INTERNAL_CHECKS]
    external_to_check = list(dict.fromkeys(external_links))[:MAX_EXTERNAL_CHECKS] if check_external else []

    # Skip logout / dangerous paths
    SKIP_PATHS = {"logout", "signout", "sign-out", "log-out", "delete", "remove"}

    def _is_safe(url: str) -> bool:
        path = urlparse(url).path.lower()
        return not any(skip in path for skip in SKIP_PATHS)

    internal_to_check = [u for u in internal_to_check if _is_safe(u)]
    external_to_check = [u for u in external_to_check if _is_safe(u)]

    # Run checks concurrently (batched to avoid overwhelming)
    async def _batch_check(urls: list, batch_size: int = 5) -> list:
        results = []
        for i in range(0, len(urls), batch_size):
            batch = urls[i:i + batch_size]
            batch_results = await asyncio.gather(
                *[_check_link(context, url) for url in batch],
                return_exceptions=True,
            )
            for res in batch_results:
                if isinstance(res, Exception):
                    results.append({"url": "unknown", "broken": True, "error": str(res)})
                else:
                    results.append(res)
        return results

    internal_results = await _batch_check(internal_to_check)
    external_results = await _batch_check(external_to_check) if external_to_check else []

    internal_broken = [r for r in internal_results if r.get("broken")]
    external_broken = [r for r in external_results if r.get("broken")]
    redirect_chains = [
        {"url": r["url"], "redirect_to": r["redirect_url"]}
        for r in (internal_results + external_results)
        if r.get("redirect_url")
    ]

    total_checked = len(internal_results) + len(external_results)
    total_broken  = len(internal_broken) + len(external_broken)

    logger.info(
        f"[broken_links] {base_url[:60]}: "
        f"{total_checked} checked, {total_broken} broken "
        f"({len(internal_broken)} internal, {len(external_broken)} external)"
    )

    return {
        "internal_broken":  internal_broken,
        "external_broken":  external_broken,
        "internal_links":   internal_to_check,
        "external_links":   external_to_check,
        "redirect_chains":  redirect_chains[:20],
        "total_checked":    total_checked,
        "total_broken":     total_broken,
        "asset_links_count": len(asset_links),
    }


def compute_broken_links_score(link_data: dict) -> dict:
    """
    Computes link health score from broken link data.
    Returns score, risk_level, and breakdown.
    """
    if not link_data:
        return {"score": 100.0, "risk_level": "Low", "breakdown": {}}

    total_internal = len(link_data.get("internal_links", []))
    total_external = len(link_data.get("external_links", []))
    internal_broken = len(link_data.get("internal_broken", []))
    external_broken = len(link_data.get("external_broken", []))

    score = 100.0

    # Internal broken links are more severe
    if total_internal > 0:
        internal_rate = internal_broken / total_internal
        score -= min(50.0, internal_rate * 100 * 0.5)
    elif internal_broken > 0:
        score -= min(50.0, internal_broken * 10.0)

    # External broken links — less severe
    if external_broken > 0:
        score -= min(20.0, external_broken * 4.0)

    # Redirect chain penalty
    redirects = len(link_data.get("redirect_chains", []))
    if redirects > 3:
        score -= min(10.0, (redirects - 3) * 2.0)

    score = max(0.0, min(100.0, score))

    if score >= 90:
        risk_level = "Low"
    elif score >= 70:
        risk_level = "Medium"
    elif score >= 40:
        risk_level = "High"
    else:
        risk_level = "Critical"

    return {
        "score": round(score, 1),
        "risk_level": risk_level,
        "breakdown": {
            "internal_links_checked": total_internal,
            "internal_broken":        internal_broken,
            "external_links_checked": total_external,
            "external_broken":        external_broken,
            "redirect_chains":        redirects,
        },
    }