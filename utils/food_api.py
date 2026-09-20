"""Food & nutrition: Open Food Facts (no key) + TheMealDB recipes (key).

Open Food Facts — open barcode/nutrition database, no auth: look up a
product by barcode or search by name and read its nutrition facts.
TheMealDB — recipe search by main ingredient (free THEMEALDB_API_KEY);
name-based search works without a key (limited).

Usage:
  food_lookup_barcode(code)     e.g. '7622210449283'
  food_search(name)             e.g. 'almond milk'
  recipe_by_ingredient(ing)     e.g. 'chicken' -> a meal idea
"""
import os
import requests

_TIMEOUT = 12
_UA = "JARVIS-assistant/1.0 (personal assistant)"
_OFF = "https://world.openfoodfacts.org/api/v2/product"
_THEMEALDB = "https://www.themealdb.com/api/json/v1/1"
_TMDB_KEY = (os.getenv("THEMEALDB_API_KEY") or "1").strip()


# ---- Open Food Facts (no auth) ---- #

def _nutrition_line(nut):
    if not nut or not isinstance(nut, dict):
        return None
    vals = {"energy": nut.get("energy-kcal_100g"),
            "protein": nut.get("proteins_100g"),
            "carbs": nut.get("carbohydrates_100g"),
            "fat": nut.get("fat_100g"),
            "sugar": nut.get("sugars_100g"),
            "salt": nut.get("salt_100g")}
    parts = []
    if vals["energy"] is not None:
        parts.append(f"{vals['energy']:.0f} kcal")
    if vals["protein"] is not None:
        parts.append(f"{vals['protein']:.1f}g protein")
    if vals["carbs"] is not None:
        parts.append(f"{vals['carbs']:.1f}g carbs")
    if vals["fat"] is not None:
        parts.append(f"{vals['fat']:.1f}g fat")
    if vals["sugar"] is not None:
        parts.append(f"{vals['sugar']:.1f}g sugar")
    return "per 100g: " + ", ".join(parts) if parts else None


def _brand_product(data):
    p = data.get("product") or {}
    nut = p.get("nutriments")
    line = _nutrition_line(nut)
    name = (p.get("product_name") or "").strip() or "product"
    brand = (p.get("brands") or "").strip()
    label = brand + " " + name if brand else name
    return f"{label} — {line}" if (line and label) else (
        label or "no nutrition data found")


def food_lookup_barcode(code):
    """Look up nutrition by barcode (no auth)."""
    try:
        r = requests.get(f"{_OFF}/{code}", headers={"User-Agent": _UA},
                         timeout=_TIMEOUT)
        data = r.json()
    except Exception as e:
        return f"Food lookup unavailable: {e}"
    if data.get("status") != 1 or not data.get("product"):
        return f"No Open Food Facts entry for barcode {code}, Sir."
    return _brand_product(data)


def food_search(name, limit=3):
    """Search Open Food Facts by product name (no auth)."""
    try:
        r = requests.get(
            "https://world.openfoodfacts.org/cgi/search.pl",
            params={"search_terms": name, "search_simple": 1,
                    "action": "process", "json": 1, "page_size": limit * 4},
            headers={"User-Agent": _UA}, timeout=_TIMEOUT)
        prods = (r.json().get("products") or [])
    except Exception as e:
        return f"Food search unavailable: {e}"
    # keep only products with a recognizable name/brand
    named = [p for p in prods
             if (p.get("product_name") or "").strip()
             or (p.get("brands") or "").strip()]
    named = named[:limit] if named else prods[:limit]
    if not named:
        return f"No Open Food Facts results for '{name}', Sir."
    return "; ".join(_brand_product(p) for p in named)


# ---- TheMealDB (recipes; name search no-key, full needs key) ---- #

def _recipe_line(m):
    def val(k):
        i = 1
        vals = []
        while True:
            v = m.get(f"strIngredient{i}")
            q = m.get(f"strMeasure{i}")
            if not v or not str(v).strip():
                break
            vals.append(f"{(q or '').strip()} {v}".strip())
            i += 1
        return vals
    name = m.get("strMeal") or "dish"
    area = m.get("strArea") or ""
    ing = val("")
    return (f"{name}" + (f" ({area})" if area else "") + ": " +
            "; ".join(ing[:6]))


def recipe_by_ingredient(ingredient, limit=3):
    """Find meal ideas by main ingredient."""
    try:
        r = requests.get(f"{_THEMEALDB}/filter.php",
                         params={"i": ingredient, "apiKey": _TMDB_KEY},
                         timeout=_TIMEOUT)
        meals = (r.json().get("meals") or [])
    except Exception as e:
        return f"Recipe lookup unavailable: {e}"
    if not meals:
        return f"No recipes found with '{ingredient}', Sir."
    return (f"Meals with {ingredient}: " +
            ", ".join(m.get("strMeal") for m in meals[:limit]) + ".")


def recipe_detail(name_or_id=""):
    """Get a recipe's ingredients + instructions (best effort)."""
    try:
        r = requests.get(f"{_THEMEALDB}/search.php",
                         params={"s": name_or_id or "chicken",
                                 "apiKey": _TMDB_KEY}, timeout=_TIMEOUT)
        meal = ((r.json().get("meals") or [None]) or [None])[0]
    except Exception as e:
        return f"Recipe lookup unavailable: {e}"
    if not meal:
        return f"No recipe found for '{name_or_id}', Sir."
    return _recipe_line(meal)


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else "chicken"
    mode = sys.argv[1] if len(sys.argv) > 1 else "ing"
    print(recipe_by_ingredient(arg))
    print(food_search("almond milk"))
    print(food_lookup_barcode("7622210449283"))
