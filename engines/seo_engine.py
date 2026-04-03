"""
engines/seo_engine.py — GuardianAI SEO Audit Engine
=====================================================
Performs real DOM-based SEO audits via Playwright.
Checks meta tags, headings, structured data, canonical URLs,
Open Graph, Twitter Cards, and on-page SEO fundamentals.

Integration:
  - Called from crawler.py inside the crawl loop
  - Results stored in page_obj["seo_data"]
  - SEO score contributed to composite health via scoring_engine
"""

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


async def capture_seo_data(page, url: str) -> dict:
    """
    Runs a comprehensive SEO audit in the browser context.
    Returns structured findings. Missing data = None, never estimated.
    """
    try:
        raw = await page.evaluate("""(pageUrl) => {
            const issues = [];
            const passed = [];
            const warnings = [];

            // ── 1. TITLE TAG ──────────────────────────────────────────────
            const titleEl = document.querySelector('title');
            const titleText = titleEl ? titleEl.textContent.trim() : null;
            let title_score = 0;

            if (!titleText) {
                issues.push({ category: 'title', severity: 'critical',
                    message: 'Page has no <title> tag' });
            } else if (titleText.length < 10) {
                issues.push({ category: 'title', severity: 'high',
                    message: 'Title is too short (' + titleText.length + ' chars, min 10)' });
                title_score = 30;
            } else if (titleText.length > 70) {
                warnings.push({ category: 'title', severity: 'medium',
                    message: 'Title may be truncated in SERPs (' + titleText.length + ' chars, max ~60-70)' });
                title_score = 70;
            } else {
                passed.push({ category: 'title', message: 'Title is well-formed' });
                title_score = 100;
            }

            // ── 2. META DESCRIPTION ───────────────────────────────────────
            const metaDesc = document.querySelector('meta[name="description"]');
            const descContent = metaDesc ? (metaDesc.getAttribute('content') || '').trim() : null;
            let desc_score = 0;

            if (!descContent) {
                issues.push({ category: 'meta_description', severity: 'high',
                    message: 'Missing meta description — search engines will auto-generate one' });
            } else if (descContent.length < 50) {
                warnings.push({ category: 'meta_description', severity: 'medium',
                    message: 'Meta description too short (' + descContent.length + ' chars)' });
                desc_score = 50;
            } else if (descContent.length > 165) {
                warnings.push({ category: 'meta_description', severity: 'low',
                    message: 'Meta description may be truncated (' + descContent.length + ' chars)' });
                desc_score = 75;
            } else {
                passed.push({ category: 'meta_description', message: 'Meta description well-formed' });
                desc_score = 100;
            }

            // ── 3. HEADING HIERARCHY ──────────────────────────────────────
            const h1s = document.querySelectorAll('h1');
            const h2s = document.querySelectorAll('h2');
            const h3s = document.querySelectorAll('h3');
            let heading_score = 100;

            if (h1s.length === 0) {
                issues.push({ category: 'headings', severity: 'high',
                    message: 'No H1 tag found — critical for SEO' });
                heading_score -= 40;
            } else if (h1s.length > 1) {
                warnings.push({ category: 'headings', severity: 'medium',
                    message: 'Multiple H1 tags (' + h1s.length + ') — only one H1 recommended' });
                heading_score -= 20;
            } else {
                passed.push({ category: 'headings',
                    message: 'Single H1 tag found: "' + h1s[0].textContent.trim().substring(0, 60) + '"' });
            }

            if (h2s.length === 0 && document.body.textContent.length > 500) {
                warnings.push({ category: 'headings', severity: 'low',
                    message: 'No H2 subheadings — consider adding structure' });
                heading_score -= 10;
            }

            // ── 4. IMAGES ALT TEXT ────────────────────────────────────────
            const allImgs = document.querySelectorAll('img');
            const decorativeImgs = document.querySelectorAll('img[alt=""], img[role="presentation"]');
            const missingAlt = [...allImgs].filter(img => !img.hasAttribute('alt'));
            const emptyAlt   = [...allImgs].filter(img => img.hasAttribute('alt') && img.alt.trim() === '' && !img.getAttribute('role'));
            let img_score = 100;

            if (missingAlt.length > 0) {
                issues.push({ category: 'images', severity: 'high',
                    message: missingAlt.length + ' image(s) missing alt attribute',
                    elements: missingAlt.slice(0, 5).map(i => i.src.substring(0, 80)) });
                img_score -= Math.min(50, missingAlt.length * 10);
            }
            if (emptyAlt.length > 0 && emptyAlt.length !== decorativeImgs.length) {
                warnings.push({ category: 'images', severity: 'low',
                    message: emptyAlt.length + ' image(s) have empty alt (verify these are decorative)' });
                img_score -= 10;
            }
            if (missingAlt.length === 0) {
                passed.push({ category: 'images', message: 'All images have alt attributes' });
            }

            // ── 5. CANONICAL URL ──────────────────────────────────────────
            const canonical = document.querySelector('link[rel="canonical"]');
            const canonicalUrl = canonical ? canonical.getAttribute('href') : null;

            if (!canonicalUrl) {
                warnings.push({ category: 'canonical', severity: 'medium',
                    message: 'No canonical URL specified — may cause duplicate content issues' });
            } else {
                passed.push({ category: 'canonical', message: 'Canonical URL set: ' + canonicalUrl.substring(0, 80) });
            }

            // ── 6. OPEN GRAPH TAGS ────────────────────────────────────────
            const ogTitle    = document.querySelector('meta[property="og:title"]');
            const ogDesc     = document.querySelector('meta[property="og:description"]');
            const ogImage    = document.querySelector('meta[property="og:image"]');
            const ogUrl      = document.querySelector('meta[property="og:url"]');
            const ogType     = document.querySelector('meta[property="og:type"]');

            const og_present = [ogTitle, ogDesc, ogImage, ogUrl].filter(Boolean).length;
            const og_missing = ['og:title', 'og:description', 'og:image', 'og:url']
                .filter((tag, i) => ![ogTitle, ogDesc, ogImage, ogUrl][i]);

            if (og_missing.length === 4) {
                warnings.push({ category: 'open_graph', severity: 'medium',
                    message: 'No Open Graph tags — social sharing will use defaults' });
            } else if (og_missing.length > 0) {
                warnings.push({ category: 'open_graph', severity: 'low',
                    message: 'Incomplete Open Graph: missing ' + og_missing.join(', ') });
            } else {
                passed.push({ category: 'open_graph', message: 'All core Open Graph tags present' });
            }

            // ── 7. TWITTER CARD ───────────────────────────────────────────
            const twitterCard = document.querySelector('meta[name="twitter:card"]');
            const twitterTitle = document.querySelector('meta[name="twitter:title"]');
            const twitterImage = document.querySelector('meta[name="twitter:image"]');

            if (!twitterCard) {
                warnings.push({ category: 'twitter_card', severity: 'low',
                    message: 'No Twitter Card meta tags — Twitter will use defaults' });
            } else {
                passed.push({ category: 'twitter_card',
                    message: 'Twitter Card type: ' + (twitterCard.getAttribute('content') || 'unknown') });
            }

            // ── 8. ROBOTS META ────────────────────────────────────────────
            const robotsMeta = document.querySelector('meta[name="robots"]');
            const robotsContent = robotsMeta ? (robotsMeta.getAttribute('content') || '').toLowerCase() : null;
            let indexable = true;

            if (robotsContent) {
                if (robotsContent.includes('noindex')) {
                    issues.push({ category: 'indexability', severity: 'critical',
                        message: 'Page is marked noindex — will not appear in search results' });
                    indexable = false;
                }
                if (robotsContent.includes('nofollow')) {
                    warnings.push({ category: 'indexability', severity: 'medium',
                        message: 'Page is marked nofollow — links will not be followed' });
                }
            }

            // ── 9. STRUCTURED DATA (JSON-LD) ──────────────────────────────
            const jsonLdScripts = document.querySelectorAll('script[type="application/ld+json"]');
            const structured_data = [];

            jsonLdScripts.forEach(script => {
                try {
                    const data = JSON.parse(script.textContent);
                    structured_data.push({
                        type: data['@type'] || 'Unknown',
                        context: data['@context'] || ''
                    });
                } catch(e) {
                    issues.push({ category: 'structured_data', severity: 'medium',
                        message: 'Invalid JSON-LD structured data found (parse error)' });
                }
            });

            if (structured_data.length === 0) {
                warnings.push({ category: 'structured_data', severity: 'low',
                    message: 'No JSON-LD structured data — rich snippets unavailable' });
            } else {
                passed.push({ category: 'structured_data',
                    message: structured_data.length + ' JSON-LD block(s): ' + structured_data.map(d => d.type).join(', ') });
            }

            // ── 10. PAGE LANGUAGE ─────────────────────────────────────────
            const htmlEl = document.querySelector('html');
            const langAttr = htmlEl ? htmlEl.getAttribute('lang') : null;

            if (!langAttr) {
                issues.push({ category: 'language', severity: 'medium',
                    message: 'Missing lang attribute on <html> — accessibility and SEO impact' });
            } else {
                passed.push({ category: 'language', message: 'Language set: ' + langAttr });
            }

            // ── 11. CONTENT ANALYSIS ──────────────────────────────────────
            const bodyText = document.body ? document.body.innerText : '';
            const wordCount = bodyText.split(/\s+/).filter(w => w.length > 0).length;
            const internalLinks = [...document.querySelectorAll('a[href]')]
                .filter(a => {
                    try {
                        const href = new URL(a.href, pageUrl);
                        return href.hostname === new URL(pageUrl).hostname;
                    } catch(e) { return false; }
                }).length;
            const externalLinks = [...document.querySelectorAll('a[href]')]
                .filter(a => {
                    try {
                        const href = new URL(a.href, pageUrl);
                        return href.hostname !== new URL(pageUrl).hostname && href.protocol.startsWith('http');
                    } catch(e) { return false; }
                }).length;

            if (wordCount < 100) {
                warnings.push({ category: 'content', severity: 'medium',
                    message: 'Low word count (' + wordCount + ') — may be considered thin content' });
            }

            // ── 12. VIEWPORT META ─────────────────────────────────────────
            const viewportMeta = document.querySelector('meta[name="viewport"]');
            if (!viewportMeta) {
                issues.push({ category: 'mobile', severity: 'high',
                    message: 'Missing viewport meta tag — page will not be mobile-friendly' });
            } else {
                passed.push({ category: 'mobile', message: 'Viewport meta tag present' });
            }

            // ── SEVERITY COUNTS ──────────────────────────────────────────
            const critical = [...issues, ...warnings].filter(i => i.severity === 'critical').length;
            const high     = [...issues, ...warnings].filter(i => i.severity === 'high').length;
            const medium   = [...issues, ...warnings].filter(i => i.severity === 'medium').length;
            const low      = [...issues, ...warnings].filter(i => i.severity === 'low').length;

            return {
                title:            titleText,
                title_length:     titleText ? titleText.length : 0,
                meta_description: descContent,
                desc_length:      descContent ? descContent.length : 0,
                canonical_url:    canonicalUrl,
                lang:             langAttr,
                indexable:        indexable,
                word_count:       wordCount,
                internal_links:   internalLinks,
                external_links:   externalLinks,
                h1_count:         h1s.length,
                h1_text:          h1s.length > 0 ? h1s[0].textContent.trim().substring(0, 100) : null,
                h2_count:         h2s.length,
                h3_count:         h3s.length,
                images_total:     allImgs.length,
                images_missing_alt: missingAlt.length,
                has_og:           og_present >= 3,
                og_tags: {
                    title:       ogTitle ? ogTitle.getAttribute('content') : null,
                    description: ogDesc  ? ogDesc.getAttribute('content')  : null,
                    image:       ogImage ? ogImage.getAttribute('content')  : null,
                    url:         ogUrl   ? ogUrl.getAttribute('content')    : null,
                    type:        ogType  ? ogType.getAttribute('content')   : null,
                },
                has_twitter_card: !!twitterCard,
                twitter_card_type: twitterCard ? twitterCard.getAttribute('content') : null,
                structured_data:  structured_data,
                robots_meta:      robotsContent,
                severity_counts:  { critical, high, medium, low },
                issues:           issues.slice(0, 30),
                warnings:         warnings.slice(0, 30),
                passed:           passed.slice(0, 20),
                total_issues:     issues.length,
                total_warnings:   warnings.length,
                total_passed:     passed.length,
                title_score,
                desc_score,
                heading_score:    Math.max(0, heading_score),
                img_score:        Math.max(0, img_score),
            };
        }""", url)

        return raw or {}

    except Exception as e:
        logger.error(f"SEO capture failed for {url}: {e}")
        return {
            "total_issues": None,
            "total_warnings": None,
            "_error": str(e),
        }


def compute_seo_score(seo_data: dict) -> dict:
    """
    Computes 0–100 SEO score from severity-weighted deductions.
    Returns score, grade, and top recommendations.
    """
    if not seo_data or seo_data.get("_error"):
        return {"score": None, "grade": None, "recommendations": []}

    score = 100.0
    severity = seo_data.get("severity_counts") or {}

    critical = severity.get("critical", 0)
    high     = severity.get("high", 0)
    medium   = severity.get("medium", 0)
    low      = severity.get("low", 0)

    score -= min(40.0, critical * 20.0)
    score -= min(30.0, high    * 10.0)
    score -= min(20.0, medium  *  5.0)
    score -= min(10.0, low     *  2.0)

    # Bonus: penalise if not indexable
    if not seo_data.get("indexable", True):
        score = min(score, 10.0)

    score = max(0.0, min(100.0, score))

    if score >= 90:
        grade = "Excellent"
    elif score >= 75:
        grade = "Good"
    elif score >= 50:
        grade = "Needs Improvement"
    else:
        grade = "Poor"

    # Build priority recommendations
    recommendations = []
    issues   = seo_data.get("issues", [])
    warnings = seo_data.get("warnings", [])

    for item in (issues + warnings)[:5]:
        recommendations.append({
            "severity": item.get("severity", "medium"),
            "category": item.get("category", ""),
            "action":   item.get("message", ""),
        })

    return {
        "score":           round(score, 1),
        "grade":           grade,
        "recommendations": recommendations,
    }