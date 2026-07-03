"""
generate_catalog.py — seed products.json with 500+ realistic PLACEHOLDER
products across all categories.

These are demo items (pid = DEMO-*) so the storefront looks and paginates
like a full store before you have a CJ API key. Once you have a key, add
real pids to watchlist.json and run `sync_engine.py --mode daily`; real
items keep their CJ pids and demo items can be pruned with --prune-demo.

Retail prices use the SAME risk-adjusted formula as the live pipeline
(pricing.py), so margins on screen are what you'd really charge.

Usage:
    python generate_catalog.py            # writes ../products.json (>=500 items)
    python generate_catalog.py --count 800
    python generate_catalog.py --prune-demo   # remove DEMO-* items only
"""

import argparse
import json
import random
from pathlib import Path

from pricing import PricingConfig, retail_price

HERE = Path(__file__).resolve().parent
PRODUCTS_JSON = HERE.parent / "products.json"

# category -> (adjectives, nouns, cost range, image label)
CATALOG = {
    "Electronics": (
        ["Wireless", "Bluetooth 5.3", "Noise-Cancelling", "Fast-Charging", "Mini", "4K", "Smart", "Portable", "Magnetic", "Foldable"],
        ["Earbuds", "Speaker", "Power Bank 20000mAh", "Webcam", "Dash Cam", "Projector", "Keyboard", "Mouse", "USB-C Hub", "Charging Station"],
        (4.0, 45.0)),
    "Home": (
        ["Smart", "Dimmable", "Motion-Sensor", "Rechargeable", "Ultrasonic", "Cordless", "Space-Saving", "Adjustable", "Stackable", "Anti-Slip"],
        ["LED Strip Lights", "Essential Oil Diffuser", "Night Light", "Shower Head", "Door Draft Stopper", "Wall Shelf", "Storage Bins (3-Pack)", "Humidifier", "Curtain Lights", "Sunset Lamp"],
        (2.5, 28.0)),
    "Kitchen": (
        ["Stainless Steel", "Multi-Function", "Electric", "Silicone", "Magnetic", "Collapsible", "Non-Stick", "Portable", "Mini", "Digital"],
        ["Milk Frother", "Vegetable Chopper", "Knife Sharpener", "Spice Rack", "Kitchen Scale", "Oil Sprayer", "Jar Opener", "Cutting Board Set", "Egg Cooker", "Coffee Grinder"],
        (2.0, 24.0)),
    "Office": (
        ["Ergonomic", "Adjustable", "Aluminum", "Foldable", "Bamboo", "Magnetic", "Cable-Management", "Dual-Monitor", "Anti-Fatigue", "RGB"],
        ["Laptop Stand", "Desk Organizer", "Monitor Riser", "Footrest", "Desk Pad", "Pen Holder", "Whiteboard Set", "Book Stand", "Wrist Rest", "Desk Lamp"],
        (3.0, 26.0)),
    "Beauty": (
        ["Ice Roller", "Jade", "LED", "Sonic", "Rechargeable", "Travel", "Professional", "Ceramic", "Ionic", "Heated"],
        ["Facial Roller", "Makeup Brush Set", "Eyelash Curler", "Hair Straightening Brush", "Facial Steamer", "Makeup Mirror", "Nail Kit", "Scalp Massager", "Blackhead Remover", "Lash Kit"],
        (1.8, 22.0)),
    "Fitness": (
        ["Adjustable", "Non-Slip", "Heavy-Duty", "Smart", "Resistance", "Portable", "Weighted", "High-Density", "Quick-Dry", "Compact"],
        ["Resistance Bands Set", "Jump Rope", "Yoga Mat", "Massage Gun", "Ab Roller", "Grip Strengthener", "Foam Roller", "Hand Weights (Pair)", "Posture Corrector", "Workout Gloves"],
        (2.5, 30.0)),
    "Pets": (
        ["Self-Cleaning", "Interactive", "Automatic", "No-Pull", "Reflective", "Washable", "Elevated", "Foldable", "Silicone", "Anti-Anxiety"],
        ["Slicker Brush", "Cat Toy", "Dog Harness", "Water Fountain", "Pet Hair Remover", "Feeding Mat", "Nail Grinder", "Calming Bed", "Treat Pouch", "Litter Mat"],
        (2.0, 26.0)),
    "Outdoors": (
        ["Waterproof", "Solar", "Ultralight", "Collapsible", "Insulated", "Windproof", "Rechargeable", "Heavy-Duty", "Compact", "Multi-Tool"],
        ["Camping Lantern", "Water Bottle 32oz", "Picnic Blanket", "Headlamp", "Cooler Bag", "Hammock", "Carabiner Set", "Dry Bag", "Camping Stool", "Bug Zapper"],
        (3.0, 32.0)),
    "Car": (
        ["Magnetic", "360°", "LED", "Portable", "Wireless", "Universal", "Anti-Slip", "Mini", "Dual-Port", "Retractable"],
        ["Phone Mount", "Vacuum Cleaner", "Trunk Organizer", "Seat Gap Filler", "Tire Pressure Gauge", "Sunshade", "Charger Adapter", "Cleaning Gel", "Headrest Hooks (4-Pack)", "Interior Lights"],
        (2.0, 24.0)),
    "Toys": (
        ["Magnetic", "Glow-in-the-Dark", "STEM", "Fidget", "Remote-Control", "Educational", "Squishy", "Building", "Interactive", "Mini"],
        ["Building Tiles Set", "Sensory Toy Pack", "RC Stunt Car", "Dinosaur Set", "Pop Puzzle", "Drawing Tablet", "Stacking Blocks", "Flying Orb Ball", "Kinetic Sand Kit", "Card Game"],
        (2.5, 28.0)),
    "Accessories": (
        ["Minimalist", "RFID-Blocking", "Magnetic", "Waterproof", "Braided", "Slim", "Vintage", "Adjustable", "Genuine Leather", "Titanium"],
        ["Wallet", "Phone Ring Holder (2-Pack)", "Watch Band", "Sunglasses", "Belt", "Keychain Organizer", "Crossbody Bag", "Beanie", "Card Holder", "Lanyard"],
        (1.5, 20.0)),
    "Baby": (
        ["Silicone", "BPA-Free", "Portable", "Adjustable", "Washable", "Anti-Colic", "Soft-Grip", "Foldable", "Musical", "Non-Toxic"],
        ["Feeding Set", "Bottle Warmer", "Teething Toys", "Bib Set (5-Pack)", "Stroller Organizer", "Sound Machine", "Bath Thermometer", "Diaper Caddy", "Milestone Cards", "Snack Cup"],
        (2.0, 22.0)),
}

BRANDS = ["Vaultline", "NovaGear", "Brixly", "TrueNorth", "Zentro", "PulseCo",
          "Havenly", "Corely", "Driftr", "Luxen", "Peaka", "Snugly"]

IMG_COLORS = ["1f2937", "374151", "4b5563", "0c2340", "334155", "1e293b"]


def make_product(i: int, category: str, cfg: PricingConfig, rng: random.Random) -> dict:
    adjs, nouns, (lo, hi) = CATALOG[category]
    title = f"{rng.choice(adjs)} {rng.choice(nouns)}"
    cost = round(rng.uniform(lo, hi), 2)
    retail = retail_price(cost, cfg)
    stock = rng.choice([0] * 2 + list(range(6, 400, 7)))  # ~6% out of stock
    label = title.split()[-1][:10]
    color = rng.choice(IMG_COLORS)
    return {
        "id": f"SKU-D{i:04d}",
        "pid": f"DEMO-{i:04d}",              # placeholder — replaced by real CJ pid
        "title": title,
        "brand": rng.choice(BRANDS),
        "image": f"https://placehold.co/600x600/{color}/ffffff?text={label}",
        "images": [f"https://placehold.co/600x600/{color}/ffffff?text={label}"],
        "description": f"{title} — demo listing. Replaced by real CJ product data on first daily sync.",
        "category": category,
        "rating": round(rng.uniform(3.9, 4.9), 1),
        "review_count": rng.randint(40, 9500),
        "source_cost": cost,
        "retail_price": retail,
        # honest scarcity: storefront shows "Only N left" from this real field
        "stock": stock,
        "in_stock": stock > 0,
        "trending_score": round(rng.uniform(0.3, 0.99), 2),
        "source_verified_at": None,           # None marks it as unverified demo data
        "demo": True,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=520)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--prune-demo", action="store_true",
                    help="remove DEMO-* items, keep real synced items")
    args = ap.parse_args()

    doc = json.loads(PRODUCTS_JSON.read_text())
    cfg = PricingConfig.from_store(doc["store"])
    real = [p for p in doc["products"] if not p.get("demo")]

    if args.prune_demo:
        doc["products"] = real
        PRODUCTS_JSON.write_text(json.dumps(doc, indent=1))
        print(f"Pruned demo items. {len(real)} real products remain.")
        return

    rng = random.Random(args.seed)
    cats = list(CATALOG)
    demo = [make_product(i + 1, cats[i % len(cats)], cfg, rng)
            for i in range(max(0, args.count - len(real)))]
    doc["products"] = real + demo
    PRODUCTS_JSON.write_text(json.dumps(doc, indent=1))
    by_cat = {}
    for p in doc["products"]:
        by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1
    print(f"Wrote {len(doc['products'])} products "
          f"({len(real)} real, {len(demo)} demo) across {len(by_cat)} categories:")
    for c, n in sorted(by_cat.items()):
        print(f"  {c:<12} {n}")


if __name__ == "__main__":
    main()
