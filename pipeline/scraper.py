"""
Meal delivery scraper.
- Snap Kitchen: full meal data via their public API
- BistroMD: product catalog via Shopify GraphQL
- All services: Trustpilot ratings via Playwright
- Others: pricing from public plan pages; nutrition from manual_nutrition.json
"""

import json, re, os, time, requests
from dataclasses import dataclass, asdict
from typing import Optional
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

RAW_DIR   = os.path.join(os.path.dirname(__file__), "data", "raw")
MANUAL_FILE = os.path.join(os.path.dirname(__file__), "data", "manual_nutrition.json")
os.makedirs(RAW_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class Meal:
    name: str
    calories: Optional[float]
    protein_g: Optional[float]
    carbs_g: Optional[float]
    fat_g: Optional[float]
    price_usd: Optional[float]
    diet_tags: list
    source_url: str


@dataclass
class ServiceMeta:
    name: str
    slug: str
    base_url: str
    shipping_cost_usd: float
    min_meals_per_week: int
    advertised_price_from: float
    trustpilot_url: str


SERVICES = [
    ServiceMeta("Factor",       "factor",      "https://www.factor75.com",         10.99, 4,  11.00, "https://www.trustpilot.com/review/www.factor75.com"),
    ServiceMeta("HelloFresh",   "hellofresh",  "https://www.hellofresh.com",         9.99, 2,   9.99, "https://www.trustpilot.com/review/www.hellofresh.com"),
    ServiceMeta("Trifecta",     "trifecta",    "https://www.trifectanutrition.com",  0.00, 5,  14.48, "https://www.trustpilot.com/review/www.trifectanutrition.com"),
    ServiceMeta("Sunbasket",    "sunbasket",   "https://sunbasket.com",              9.99, 2,  10.99, "https://www.trustpilot.com/review/sunbasket.com"),
    ServiceMeta("Green Chef",   "greenchef",   "https://www.greenchef.com",          9.99, 2,  12.99, "https://www.trustpilot.com/review/www.greenchef.com"),
    ServiceMeta("BistroMD",     "bistromd",    "https://www.bistromd.com",          19.95, 5,   9.99, "https://www.trustpilot.com/review/www.bistromd.com"),
    ServiceMeta("Snap Kitchen", "snapkitchen", "https://www.snapkitchen.com",        0.00, 6,  10.83, "https://www.trustpilot.com/review/www.snapkitchen.com"),
]


def parse_num(val) -> Optional[float]:
    if val is None:
        return None
    try:
        s = re.sub(r"[^\d.]", "", str(val))
        return float(s) if s else None
    except Exception:
        return None


# ── Snap Kitchen (public API) ──────────────────────────────────────────────────

def scrape_snapkitchen(service: ServiceMeta) -> list[Meal]:
    url = "https://www.snapkitchen.com/api/menu-spa/init?orgId=6b79e860-8e40-47d5-5b24-08da31cd430b"
    print(f"  Fetching Snap Kitchen API...")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        data = r.json()
    except Exception as e:
        print(f"  [error] {e}")
        return []

    def parse_macro(text, key):
        if not text:
            return None
        m = re.search(rf"{key}:\s*([\d.]+)", text, re.IGNORECASE)
        return float(m.group(1)) if m else None

    meals, seen = [], set()
    for cat in data.get("orderedProductCategories", []):
        for p in cat.get("products", []):
            pid = p["id"]
            if pid in seen:
                continue
            seen.add(pid)
            macro = p.get("macroDisplay", "")
            tags = [t["name"] for t in p.get("menuTags", []) if t.get("name")]
            price = parse_num(p.get("price")) or None
            if price == 0.0:
                price = None
            meals.append(Meal(
                name=p["name"],
                calories=parse_macro(macro, "CAL"),
                protein_g=parse_macro(macro, "Protein"),
                carbs_g=parse_macro(macro, "Carbs"),
                fat_g=parse_macro(macro, "Fat"),
                price_usd=price,
                diet_tags=tags,
                source_url=url,
            ))

    print(f"  {len(meals)} meals extracted")
    return meals


# ── BistroMD (Shopify GraphQL) ─────────────────────────────────────────────────

def scrape_bistromd(service: ServiceMeta) -> list[Meal]:
    print(f"  Querying BistroMD Shopify GraphQL...")
    shopify_headers = {**HEADERS,
        "X-Shopify-Storefront-Access-Token": "f2d6831869006ff15f510c6028783e71",
        "Content-Type": "application/json",
    }
    all_products, cursor = [], None
    while True:
        after = f', after: "{cursor}"' if cursor else ""
        query = f"""
        {{
          products(first: 250{after}) {{
            pageInfo {{ hasNextPage endCursor }}
            edges {{
              node {{
                title
                tags
                variants(first: 1) {{
                  edges {{ node {{ price {{ amount }} }} }}
                }}
              }}
            }}
          }}
        }}
        """
        try:
            r = requests.post("https://bistro-md.myshopify.com/api/2025-07/graphql.json",
                              headers=shopify_headers, json={"query": query}, timeout=15)
            data = r.json().get("data", {}).get("products", {})
        except Exception as e:
            print(f"  [shopify error] {e}")
            break
        edges = data.get("edges", [])
        all_products.extend(e["node"] for e in edges)
        if not data.get("pageInfo", {}).get("hasNextPage"):
            break
        cursor = data["pageInfo"]["endCursor"]

    # BistroMD products have no nutrition in Shopify — we get names + tags only.
    # Nutrition comes from manual_nutrition.json; here we just store the catalog.
    meals = []
    for p in all_products:
        # Shopify prices here are add-ons/bundles, not per-meal delivery prices
        price = None
        meals.append(Meal(
            name=p["title"],
            calories=None,
            protein_g=None,
            carbs_g=None,
            fat_g=None,
            price_usd=price,
            diet_tags=p.get("tags", []),
            source_url="https://bistro-md.myshopify.com",
        ))

    print(f"  {len(meals)} products extracted (nutrition from manual seed)")
    return meals


# ── Trustpilot (Playwright) ────────────────────────────────────────────────────

def scrape_trustpilot_batch(services: list[ServiceMeta]) -> dict:
    """Returns {slug: {rating, review_count, url}}."""
    results = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )

        for service in services:
            print(f"  Trustpilot: {service.name}...")
            result = {"rating": None, "review_count": None, "url": service.trustpilot_url}
            page = context.new_page()
            captured = []

            def on_response(response, svc=service):
                ctype = response.headers.get("content-type", "")
                if "json" not in ctype:
                    return
                try:
                    data = response.json()
                    captured.append((response.url, data))
                except Exception:
                    pass

            page.on("response", on_response)
            try:
                page.goto(service.trustpilot_url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(5000)

                # Check intercepted API calls first
                for url, data in captured:
                    if "trustpilot" in url and "review" in url.lower():
                        rating = (data.get("score") or {}).get("trustScore")
                        count = (data.get("numberOfReviews") or {}).get("total")
                        if rating:
                            result["rating"] = rating
                            result["review_count"] = count
                            break

                # DOM selector (primary — most reliable on Trustpilot)
                if result["rating"] is None:
                    try:
                        els = page.query_selector_all("[data-rating-typography]")
                        if els:
                            result["rating"] = parse_num(els[0].inner_text())
                    except Exception:
                        pass

                # JSON-LD fallback (handles @graph wrapper)
                if result["rating"] is None:
                    scripts = page.eval_on_selector_all(
                        "script[type='application/ld+json']",
                        "els => els.map(e => e.textContent)"
                    )
                    for raw in scripts:
                        try:
                            d = json.loads(raw)
                            # Unwrap @graph if present
                            items = d.get("@graph", []) if isinstance(d, dict) and "@graph" in d else []
                            if not items:
                                items = d if isinstance(d, list) else [d]
                            for item in items:
                                agg = item.get("aggregateRating", {})
                                if agg.get("ratingValue"):
                                    result["rating"] = float(agg["ratingValue"])
                                    result["review_count"] = agg.get("reviewCount")
                                    break
                        except Exception:
                            pass

            except Exception as e:
                print(f"    [error] {e}")
            finally:
                page.close()

            tp = result.get("rating")
            ct = result.get("review_count")
            print(f"    → {tp} / 5.0  ({ct} reviews)")
            results[service.slug] = result
            time.sleep(2)

        browser.close()

    return results


# ── Public pricing scraper ─────────────────────────────────────────────────────

PLAN_PAGES = {
    "factor":     "https://www.factor75.com/plans",
    "hellofresh": "https://www.hellofresh.com/plans",
    "greenchef":  "https://www.greenchef.com/plans",
    "trifecta":   "https://www.trifectanutrition.com/meal-plan-delivery",
    "sunbasket":  "https://sunbasket.com/fresh-and-ready",
    "bistromd":   "https://www.bistromd.com/",
    "snapkitchen":"https://www.snapkitchen.com/",
}

def scrape_pricing(slug: str) -> dict:
    url = PLAN_PAGES.get(slug, "")
    if not url:
        return {}
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        text = re.sub(r"<[^>]+>", " ", r.text)
        prices = re.findall(r"\$\s*([\d]+\.[\d]{2})(?:\s*(?:/|per)\s*meal)?", text)
        prices = [float(p) for p in prices if 5.0 < float(p) < 25.0]
        if prices:
            return {"min_price": min(prices), "max_price": max(prices), "prices_found": sorted(set(prices))}
    except Exception:
        pass
    return {}


# ── Orchestrator ───────────────────────────────────────────────────────────────

def run(slugs=None):
    targets = [s for s in SERVICES if slugs is None or s.slug in slugs]
    manual = {}
    if os.path.exists(MANUAL_FILE):
        with open(MANUAL_FILE) as f:
            manual = json.load(f)

    # 1. Trustpilot for all targets
    print("\n=== Trustpilot ratings ===")
    tp_data = scrape_trustpilot_batch(targets)

    # 2. Per-service meal + pricing data
    for service in targets:
        print(f"\n=== {service.name} ===")

        if service.slug == "snapkitchen":
            meals = scrape_snapkitchen(service)
        elif service.slug == "bistromd":
            meals = scrape_bistromd(service)
        else:
            meals = []  # nutrition from manual seed

        print(f"  Scraping pricing page...")
        pricing = scrape_pricing(service.slug)
        if pricing:
            print(f"  Prices found: {pricing['prices_found'][:6]}")

        output = {
            "service": asdict(service),
            "trustpilot": tp_data.get(service.slug, {}),
            "meals": [asdict(m) for m in meals],
            "meal_count": len(meals),
            "pricing_scraped": pricing,
            "manual_nutrition": manual.get(service.slug, {}),
            "data_source": "api" if service.slug == "snapkitchen" else ("shopify+manual" if service.slug == "bistromd" else "manual"),
        }

        out_path = os.path.join(RAW_DIR, f"{service.slug}.json")
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2)
        print(f"  Saved → {out_path}")


if __name__ == "__main__":
    import sys
    run(sys.argv[1:] or None)
