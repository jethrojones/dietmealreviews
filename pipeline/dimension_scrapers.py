"""
Scrapers for nutritional accuracy and menu flexibility dimensions.

Nutritional Accuracy:
  - Playwright: extract review text from Trustpilot (~40 reviews/service)
  - Classify for accuracy-specific complaint language via regex
  - Score = 10 − (complaint_rate × penalty) + certification bonus

Menu Flexibility:
  - Snap Kitchen: parse allProductTagFilters from existing raw API data
  - Others: scrape plan pages + FAQ/help pages for diet options and policy language
  - Score on 8-criterion rubric
"""

import json, re, os, time
from dataclasses import dataclass, asdict
from typing import Optional
from playwright.sync_api import sync_playwright
import requests
from bs4 import BeautifulSoup

RAW_DIR    = os.path.join(os.path.dirname(__file__), "data", "raw")
DIM_DIR    = os.path.join(os.path.dirname(__file__), "data", "dimensions")
os.makedirs(DIM_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Nutritional accuracy: keyword patterns ─────────────────────────────────────

ACCURACY_PATTERNS = re.compile(
    r"\b("
    r"calorie[s]?\s*(off|wrong|incorrect|inaccurate|don.t match|differ)"
    r"|macro[s]?\s*(off|wrong|incorrect|inaccurate|don.t match|differ|not accurate)"
    r"|protein\s*(off|wrong|incorrect|listed|label)"
    r"|nutrition\s*(label|info|facts|data)\s*(wrong|off|incorrect|inaccurate|mislead)"
    r"|didn.t match\s*(label|nutrition|macros|calories)"
    r"|not what\s*(it|they)\s*(say|said|list|listed)"
    r"|false\s*(advertising|calorie|nutrition)"
    r"|mislabel"
    r"|track(ing)?\s*(difficult|impossible|unreliable)"
    r")\b",
    re.IGNORECASE,
)

# USDA Organic certification = third-party oversight bonus
USDA_ORGANIC = {"trifecta", "greenchef", "sunbasket"}


# ── Trustpilot review text scraper ────────────────────────────────────────────

def scrape_trustpilot_reviews(page, tp_url: str, max_pages: int = 2) -> list[str]:
    """Return list of review body texts from up to max_pages Trustpilot pages."""
    texts = []
    for page_num in range(1, max_pages + 1):
        url = tp_url if page_num == 1 else f"{tp_url}?page={page_num}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(4000)

            # Extract review text — Trustpilot wraps body in <p data-service-review-text-typography>
            # or in a section with class containing "review-content"
            snippets = page.eval_on_selector_all(
                "[data-service-review-text-typography], .styles_reviewContent__0Q2Tg p, "
                "section[class*='reviewContent'] p, p[class*='typography_body']",
                "els => els.map(e => e.textContent.trim()).filter(t => t.length > 30)"
            )
            texts.extend(snippets)
        except Exception as e:
            print(f"    [review page {page_num} error] {e}")
    return texts


def score_nutritional_accuracy(slug: str, reviews: list[str]) -> dict:
    """
    Score nutritional accuracy 1-10 based on review complaint analysis.
    Base score: 8 (no evidence = reasonable accuracy assumed).
    Deduct for complaint rate. Add for USDA Organic certification.
    """
    total = len(reviews)
    if total == 0:
        return {
            "score": None,
            "complaint_count": 0,
            "total_reviews_analyzed": 0,
            "complaint_rate_pct": None,
            "usda_organic": slug in USDA_ORGANIC,
            "note": "no reviews extracted",
        }

    complaints = [r for r in reviews if ACCURACY_PATTERNS.search(r)]
    rate = len(complaints) / total

    # Base 8, scale down for complaint rate
    # 0% → 8.0, 5% → 6.0, 10% → 4.0, 20%+ → 2.0
    if rate <= 0.02:
        score = 8.0
    elif rate <= 0.05:
        score = 8.0 - ((rate - 0.02) / 0.03) * 2.0   # 8→6
    elif rate <= 0.10:
        score = 6.0 - ((rate - 0.05) / 0.05) * 2.0   # 6→4
    elif rate <= 0.20:
        score = 4.0 - ((rate - 0.10) / 0.10) * 2.0   # 4→2
    else:
        score = 2.0

    # USDA Organic bonus (third-party ingredient oversight)
    organic = slug in USDA_ORGANIC
    if organic:
        score = min(10.0, score + 0.5)

    score = round(score, 2)
    return {
        "score": score,
        "complaint_count": len(complaints),
        "total_reviews_analyzed": total,
        "complaint_rate_pct": round(rate * 100, 1),
        "complaint_examples": complaints[:3],
        "usda_organic": organic,
        "note": "proxy metric — review text analysis, not lab tested",
    }


# ── Menu flexibility scraper ───────────────────────────────────────────────────

# Static policy knowledge derived from public pages / ToS / help centers.
# Binary criteria verified by reading each service's help center and cancel flow descriptions.
# Sources listed per service.
POLICY_DATA = {
    "factor": {
        "skip_freely":         True,   # Skip any week from account dashboard
        "no_skip_penalty":     True,   # No fee to skip
        "cancel_anytime":      True,   # Stated explicitly
        "cancel_online":       True,   # Account settings → pause/cancel
        "individual_meals":    True,   # Choose from weekly menu
        "no_min_term":         True,   # No lock-in
        "a_la_carte":          False,  # Subscription only
        "source": "factor75.com/faq — 'You can skip or cancel anytime through your account'",
    },
    "hellofresh": {
        "skip_freely":         True,
        "no_skip_penalty":     True,
        "cancel_anytime":      True,   # Stated but multiple reports of friction
        "cancel_online":       False,  # Must navigate deep settings; BBB complaints about difficulty
        "individual_meals":    True,   # Choose recipes each week
        "no_min_term":         True,
        "a_la_carte":          False,
        "source": "hellofresh.com/about/faq + BBB complaints re: cancellation difficulty",
    },
    "trifecta": {
        "skip_freely":         True,
        "no_skip_penalty":     True,
        "cancel_anytime":      True,
        "cancel_online":       True,   # Account dashboard
        "individual_meals":    True,   # Select meals from plan
        "no_min_term":         True,
        "a_la_carte":          False,
        "source": "trifectanutrition.com/faq",
    },
    "sunbasket": {
        "skip_freely":         True,
        "no_skip_penalty":     True,
        "cancel_anytime":      True,
        "cancel_online":       True,
        "individual_meals":    True,
        "no_min_term":         True,
        "a_la_carte":          True,   # Fresh & Ready single-order available
        "source": "sunbasket.com/faq — Fresh & Ready available without subscription",
    },
    "greenchef": {
        "skip_freely":         True,
        "no_skip_penalty":     True,
        "cancel_anytime":      True,
        "cancel_online":       False,  # HelloFresh infrastructure — same deep-settings friction
        "individual_meals":    False,  # Plan-based recipe selection, less flexibility
        "no_min_term":         True,
        "a_la_carte":          False,
        "source": "greenchef.com/faq + shared HelloFresh cancellation UX",
    },
    "bistromd": {
        "skip_freely":         True,
        "no_skip_penalty":     False,  # Delivery skip may require advance notice window
        "cancel_anytime":      True,
        "cancel_online":       False,  # Must call or email; multiple BBB complaints
        "individual_meals":    False,  # Program-based, limited swap options
        "no_min_term":         True,
        "a_la_carte":          False,
        "source": "bistromd.com/faq + BBB complaint pattern: 'had to call to cancel'",
    },
    "snapkitchen": {
        "skip_freely":         True,
        "no_skip_penalty":     True,
        "cancel_anytime":      True,
        "cancel_online":       True,
        "individual_meals":    True,   # Full à la carte menu selection
        "no_min_term":         True,
        "a_la_carte":          True,   # Can order without subscription (pickup stores)
        "source": "snapkitchen.com — subscription + walk-in store pickup available",
    },
}

# FAQ / plan page URLs to scrape for diet filter keywords
DIET_PAGES = {
    "factor":      "https://www.factor75.com/plans",
    "hellofresh":  "https://www.hellofresh.com/plans",
    "trifecta":    "https://www.trifectanutrition.com/meal-plan-delivery",
    "sunbasket":   "https://sunbasket.com/fresh-and-ready",
    "greenchef":   "https://www.greenchef.com/our-menus",
    "bistromd":    "https://www.bistromd.com/",
    "snapkitchen": None,  # Use existing API data
}

DIET_KEYWORDS = [
    "keto", "paleo", "vegan", "vegetarian", "plant.based",
    "diabetic", "diabetes.friendly", "gluten.free", "low.carb",
    "high.protein", "calorie.smart", "mediterranean", "pescatarian",
    "dairy.free", "heart.healthy", "menopause", "low.sodium",
    "macro", "balanced", "whole30", "carb.conscious",
]
DIET_RE = re.compile(r"\b(" + "|".join(DIET_KEYWORDS) + r")\b", re.IGNORECASE)


def count_diet_options_from_api(slug: str) -> int:
    """For Snap Kitchen: count tag filter categories from existing raw data."""
    raw_path = os.path.join(RAW_DIR, f"{slug}.json")
    if not os.path.exists(raw_path):
        return 0
    with open(raw_path) as f:
        raw = json.load(f)
    meals = raw.get("meals", [])
    all_tags = set()
    for m in meals:
        for t in m.get("diet_tags", []):
            all_tags.add(t.lower())
    return len(all_tags)


def scrape_diet_count(slug: str) -> int:
    """Scrape plan page and count distinct diet filter keyword mentions."""
    if slug == "snapkitchen":
        return count_diet_options_from_api(slug)
    url = DIET_PAGES.get(slug)
    if not url:
        return 0
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        text = re.sub(r"<[^>]+>", " ", r.text)
        matches = set(m.lower() for m in DIET_RE.findall(text))
        return len(matches)
    except Exception:
        return 0


def score_menu_flexibility(slug: str, diet_count: int) -> dict:
    """
    Score on 8 criteria → 1-10 scale.
    Criteria:
      1. Diet variety:  0 = 2pts, 1-3 = 1pt per category group up to 2pts
      2-8. Binary policy criteria (1pt each) = 6pts max
    Total possible = 8 → normalize to 10.
    """
    policy = POLICY_DATA.get(slug, {})

    # Diet variety: 0 → 0pts, 1-2 → 1pt, 3-5 → 1.5pts, 6+ → 2pts
    if diet_count >= 6:
        diet_pts = 2.0
    elif diet_count >= 3:
        diet_pts = 1.5
    elif diet_count >= 1:
        diet_pts = 1.0
    else:
        diet_pts = 0.0

    binary_keys = [
        "skip_freely", "no_skip_penalty", "cancel_anytime",
        "cancel_online", "individual_meals", "no_min_term",
    ]
    binary_pts = sum(1.0 for k in binary_keys if policy.get(k, False))
    ala_carte_pts = 1.0 if policy.get("a_la_carte", False) else 0.0

    total_pts = diet_pts + binary_pts + ala_carte_pts  # max = 9
    score = round(min(10.0, (total_pts / 9.0) * 10.0), 2)

    criteria = {k: policy.get(k, False) for k in binary_keys}
    criteria["a_la_carte"] = policy.get("a_la_carte", False)

    return {
        "score": score,
        "diet_options_count": diet_count,
        "diet_variety_pts": diet_pts,
        "binary_pts": binary_pts,
        "ala_carte_pts": ala_carte_pts,
        "total_pts": round(total_pts, 1),
        "criteria": criteria,
        "source": policy.get("source", ""),
    }


# ── Orchestrator ───────────────────────────────────────────────────────────────

from dataclasses import field

TRUSTPILOT_URLS = {
    "factor":      "https://www.trustpilot.com/review/www.factor75.com",
    "hellofresh":  "https://www.trustpilot.com/review/www.hellofresh.com",
    "trifecta":    "https://www.trustpilot.com/review/www.trifectanutrition.com",
    "sunbasket":   "https://www.trustpilot.com/review/sunbasket.com",
    "greenchef":   "https://www.trustpilot.com/review/www.greenchef.com",
    "bistromd":    "https://www.trustpilot.com/review/www.bistromd.com",
    "snapkitchen": "https://www.trustpilot.com/review/www.snapkitchen.com",
}

SLUGS = ["factor", "hellofresh", "trifecta", "sunbasket", "greenchef", "bistromd", "snapkitchen"]


def run(slugs=None):
    targets = slugs or SLUGS

    print("=== Diet option counts (from plan pages) ===")
    diet_counts = {}
    for slug in targets:
        count = scrape_diet_count(slug)
        diet_counts[slug] = count
        print(f"  {slug}: {count} diet keywords found")
        time.sleep(1)

    print("\n=== Menu flexibility scores ===")
    flex_results = {}
    for slug in targets:
        result = score_menu_flexibility(slug, diet_counts.get(slug, 0))
        flex_results[slug] = result
        print(f"  {slug}: {result['score']}/10  (diet:{diet_counts.get(slug,0)}  binary:{result['binary_pts']}/6  à la carte:{result['ala_carte_pts']})")

    print("\n=== Trustpilot review text (nutritional accuracy proxy) ===")
    accuracy_results = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        for slug in targets:
            tp_url = TRUSTPILOT_URLS.get(slug, "")
            if not tp_url:
                continue
            print(f"  Scraping reviews: {slug}...")
            page = context.new_page()
            try:
                reviews = scrape_trustpilot_reviews(page, tp_url, max_pages=2)
                result = score_nutritional_accuracy(slug, reviews)
                accuracy_results[slug] = result
                print(f"    {len(reviews)} reviews · {result['complaint_count']} accuracy complaints "
                      f"({result['complaint_rate_pct']}%) → score: {result['score']}")
                if result["complaint_examples"]:
                    for ex in result["complaint_examples"][:1]:
                        print(f"    Example: \"{ex[:100]}...\"")
            except Exception as e:
                print(f"    [error] {e}")
                accuracy_results[slug] = {"score": None, "note": str(e)}
            finally:
                page.close()
            time.sleep(2)
        browser.close()

    # Save combined dimension data
    for slug in targets:
        out = {
            "slug": slug,
            "nutritional_accuracy": accuracy_results.get(slug, {}),
            "menu_flexibility": flex_results.get(slug, {}),
        }
        out_path = os.path.join(DIM_DIR, f"{slug}.json")
        with open(out_path, "w") as f:
            json.dump(out, f, indent=2)

    print(f"\nSaved dimension data → {DIM_DIR}/")


if __name__ == "__main__":
    import sys
    run(sys.argv[1:] or None)
