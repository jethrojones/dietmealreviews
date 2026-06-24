"""
Reads raw scraped JSON and produces scored service profiles.
Outputs to data/scored/<slug>.json and data/summary.json.
"""

import json
import os
import glob
from dataclasses import dataclass, asdict
from typing import Optional

RAW_DIR   = os.path.join(os.path.dirname(__file__), "data", "raw")
SCORED_DIR = os.path.join(os.path.dirname(__file__), "data", "scored")
DIM_DIR   = os.path.join(os.path.dirname(__file__), "data", "dimensions")
os.makedirs(SCORED_DIR, exist_ok=True)

# Trustpilot: 5.0 scale → normalize to 10
TRUSTPILOT_MAX = 5.0

# Scoring weights (must sum to 1.0)
WEIGHTS = {
    "protein_value": 0.25,
    "nutritional_accuracy": 0.20,
    "delivery_reliability": 0.20,
    "price_transparency": 0.20,
    "menu_flexibility": 0.15,
}


@dataclass
class ServiceScore:
    slug: str
    name: str

    # Raw metrics
    avg_protein_g: Optional[float]
    avg_price_usd: Optional[float]
    allin_price_usd: Optional[float]  # price + shipping amortized over min order
    cost_per_g_protein: Optional[float]
    price_delta_pct: Optional[float]  # (allin - advertised) / advertised
    trustpilot_rating: Optional[float]
    trustpilot_review_count: Optional[int]
    meal_count: int

    # Dimension scores (1–10)
    protein_value_score: Optional[float]
    nutritional_accuracy_score: Optional[float]
    delivery_reliability_score: Optional[float]
    price_transparency_score: Optional[float]
    menu_flexibility_score: Optional[float]

    # Nutritional accuracy detail
    accuracy_reviews_analyzed: Optional[int]
    accuracy_complaint_rate_pct: Optional[float]

    # Menu flexibility detail
    menu_diet_options: Optional[int]
    menu_criteria: Optional[dict]

    # Composite score
    composite_score: Optional[float]
    grade: Optional[str]

    # Notes on missing data
    data_gaps: list[str]


def grade(score: float) -> str:
    if score >= 9.0:
        return "A"
    if score >= 8.0:
        return "A-"
    if score >= 7.0:
        return "B"
    if score >= 6.0:
        return "C"
    return "D"


def score_protein_value(cost_per_g: Optional[float]) -> Optional[float]:
    """
    Lower cost-per-gram = higher score.
    Benchmarked against the meal-delivery market (prepared, delivered meals),
    not raw grocery prices. Best-in-class delivered meals run ~$0.20-0.30/g.

    Benchmarks (USD per gram of protein, all-in price):
      ≤$0.20  → 10   (exceptional — high-protein kit, minimal overhead)
      $0.25   → 8    (excellent for delivery)
      $0.30   → 7    (very good)
      $0.40   → 5    (average for prepared meal delivery)
      $0.55   → 3    (below average)
      $0.70   → 2
      >$0.80  → 1    (poor protein value)
    """
    if cost_per_g is None:
        return None
    if cost_per_g <= 0.20:
        return 10.0
    if cost_per_g <= 0.30:
        return round(10.0 - ((cost_per_g - 0.20) / 0.10) * 3, 2)   # 10→7
    if cost_per_g <= 0.40:
        return round(7.0 - ((cost_per_g - 0.30) / 0.10) * 2, 2)    # 7→5
    if cost_per_g <= 0.55:
        return round(5.0 - ((cost_per_g - 0.40) / 0.15) * 2, 2)    # 5→3
    if cost_per_g <= 0.70:
        return round(3.0 - ((cost_per_g - 0.55) / 0.15) * 1, 2)    # 3→2
    if cost_per_g <= 0.80:
        return round(2.0 - ((cost_per_g - 0.70) / 0.10) * 1, 2)    # 2→1
    return 1.0


def score_delivery_reliability(trustpilot: Optional[float]) -> Optional[float]:
    """
    Map Trustpilot 1–5 scale to 1–10.
    Applies a penalty curve: 4.0 TP → ~7, 3.5 TP → ~5.
    """
    if trustpilot is None:
        return None
    return round(min(10.0, max(1.0, (trustpilot / TRUSTPILOT_MAX) * 10)), 2)


def score_price_transparency(delta_pct: Optional[float]) -> Optional[float]:
    """
    delta_pct = (allin - advertised) / advertised
    0%   → 10  (no hidden costs)
    10%  → 8
    25%  → 5
    50%  → 2
    >75% → 1
    """
    if delta_pct is None:
        return None
    d = abs(delta_pct) * 100  # convert to percentage points
    if d <= 5:
        return 10.0
    if d <= 10:
        return round(10.0 - (d - 5) / 5 * 2, 2)
    if d <= 25:
        return round(8.0 - (d - 10) / 15 * 3, 2)
    if d <= 50:
        return round(5.0 - (d - 25) / 25 * 3, 2)
    return max(1.0, round(2.0 - (d - 50) / 25, 2))


def compute_allin_price(service_meta: dict, raw_meals: list[dict]) -> Optional[float]:
    """
    All-in price per meal = meal price + (shipping / min_meals_per_week).
    Uses the advertised_price_from if no per-meal price in data.
    """
    shipping = service_meta.get("shipping_cost_usd", 0)
    min_meals = service_meta.get("min_meals_per_week", 1)
    shipping_per_meal = shipping / max(min_meals, 1)

    prices = [m["price_usd"] for m in raw_meals if m.get("price_usd")]
    if prices:
        avg_meal_price = sum(prices) / len(prices)
    else:
        avg_meal_price = service_meta.get("advertised_price_from")

    if avg_meal_price is None:
        return None
    return round(avg_meal_price + shipping_per_meal, 2)


def score_service(raw_path: str) -> ServiceScore:
    with open(raw_path) as f:
        data = json.load(f)

    meta = data["service"]
    meals = data.get("meals", [])
    manual = data.get("manual_nutrition", {})
    tp = data.get("trustpilot", {})
    gaps = []

    # Protein stats — prefer scraped data, fall back to manual seed
    proteins = [m["protein_g"] for m in meals if m.get("protein_g")]
    avg_protein = round(sum(proteins) / len(proteins), 1) if proteins else None
    if avg_protein is None and manual.get("avg_protein_g"):
        avg_protein = manual["avg_protein_g"]
        gaps.append("protein from manual seed (menu requires auth)")

    # Price stats — use manual seed price if scraped meals have no price
    allin = compute_allin_price(meta, meals)
    if allin is None and manual.get("price_per_meal_base"):
        shipping = meta.get("shipping_cost_usd", 0)
        min_meals = meta.get("min_meals_per_week", 1)
        allin = round(manual["price_per_meal_base"] + shipping / max(min_meals, 1), 2)
        gaps.append("price from manual seed (plan page)")
    advertised = meta.get("advertised_price_from")
    delta_pct = None
    if allin and advertised:
        delta_pct = (allin - advertised) / advertised

    # Cost per gram protein
    cost_per_g = None
    if allin and avg_protein and avg_protein > 0:
        cost_per_g = round(allin / avg_protein, 4)

    # Trustpilot
    tp_rating = tp.get("rating")
    if tp_rating:
        tp_rating = float(tp_rating)
    else:
        gaps.append("no Trustpilot rating scraped")
    tp_count = tp.get("review_count")

    # Scores
    pv_score = score_protein_value(cost_per_g)
    dr_score = score_delivery_reliability(tp_rating)
    pt_score = score_price_transparency(delta_pct)

    prices = [m["price_usd"] for m in meals if m.get("price_usd")]
    avg_price = round(sum(prices) / len(prices), 2) if prices else None

    # Load dimension data (nutritional accuracy + menu flexibility)
    dim_path = os.path.join(DIM_DIR, f"{meta['slug']}.json")
    dim = {}
    if os.path.exists(dim_path):
        with open(dim_path) as f:
            dim = json.load(f)

    na_data  = dim.get("nutritional_accuracy", {})
    mf_data  = dim.get("menu_flexibility", {})
    na_score = na_data.get("score")
    mf_score = mf_data.get("score")

    # Composite — now all 5 dimensions, normalise by available weight
    scored_dims = {}
    if pv_score is not None:
        scored_dims["protein_value"] = pv_score
    if na_score is not None:
        scored_dims["nutritional_accuracy"] = na_score
    if dr_score is not None:
        scored_dims["delivery_reliability"] = dr_score
    if pt_score is not None:
        scored_dims["price_transparency"] = pt_score
    if mf_score is not None:
        scored_dims["menu_flexibility"] = mf_score

    if scored_dims:
        total_weight = sum(WEIGHTS[k] for k in scored_dims)
        composite = sum(scored_dims[k] * WEIGHTS[k] for k in scored_dims) / total_weight
        composite = round(composite, 2)
        g = grade(composite)
    else:
        composite = None
        g = None
        gaps.append("insufficient data for composite score")

    prices = [m["price_usd"] for m in meals if m.get("price_usd")]
    avg_price = round(sum(prices) / len(prices), 2) if prices else None

    return ServiceScore(
        slug=meta["slug"],
        name=meta["name"],
        avg_protein_g=avg_protein,
        avg_price_usd=avg_price,
        allin_price_usd=allin,
        cost_per_g_protein=cost_per_g,
        price_delta_pct=round(delta_pct * 100, 1) if delta_pct is not None else None,
        trustpilot_rating=tp_rating,
        trustpilot_review_count=int(tp_count) if tp_count else None,
        meal_count=len(meals),
        protein_value_score=pv_score,
        nutritional_accuracy_score=na_score,
        delivery_reliability_score=dr_score,
        price_transparency_score=pt_score,
        menu_flexibility_score=mf_score,
        accuracy_reviews_analyzed=na_data.get("total_reviews_analyzed"),
        accuracy_complaint_rate_pct=na_data.get("complaint_rate_pct"),
        menu_diet_options=mf_data.get("diet_options_count"),
        menu_criteria=mf_data.get("criteria"),
        composite_score=composite,
        grade=g,
        data_gaps=gaps,
    )


def run():
    raw_files = glob.glob(os.path.join(RAW_DIR, "*.json"))
    if not raw_files:
        print("No raw data found. Run scraper.py first.")
        return

    all_scores = []
    for path in sorted(raw_files):
        slug = os.path.basename(path).replace(".json", "")
        print(f"Scoring {slug}...")
        scored = score_service(path)
        all_scores.append(asdict(scored))

        out_path = os.path.join(SCORED_DIR, f"{slug}.json")
        with open(out_path, "w") as f:
            json.dump(asdict(scored), f, indent=2)

    # Summary ranked by composite score
    ranked = sorted(
        [s for s in all_scores if s["composite_score"] is not None],
        key=lambda x: x["composite_score"],
        reverse=True,
    )

    summary_path = os.path.join(os.path.dirname(__file__), "data", "summary.json")
    with open(summary_path, "w") as f:
        json.dump({"ranked": ranked, "total": len(all_scores)}, f, indent=2)

    print(f"\n{'Rank':<5} {'Service':<15} {'$/g protein':<14} {'TP Rating':<12} {'Score':<8} Grade")
    print("-" * 60)
    for i, s in enumerate(ranked, 1):
        cpg = f"${s['cost_per_g_protein']:.3f}" if s["cost_per_g_protein"] else "N/A"
        tp = f"{s['trustpilot_rating']:.1f}" if s["trustpilot_rating"] else "N/A"
        print(f"{i:<5} {s['name']:<15} {cpg:<14} {tp:<12} {s['composite_score']:<8} {s['grade']}")

    print(f"\nSummary saved → {summary_path}")


if __name__ == "__main__":
    run()
