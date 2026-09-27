#!/usr/bin/env python3
"""
Pre-roll deal finder: price-per-gram + potency ranking from the
dispensary menu scraper's JSON output.

Usage:
  python3 pre_roll_deals.py [menus_file.json]        # default: latest file
  python3 pre_roll_deals.py --min-thc 20             # only pre-rolls >= 20% THC
  python3 pre_roll_deals.py --top 15
"""

import glob
import json
import sys

# ---------------------------------------------------------------- load rows
menu_file = sys.argv[1] if len(sys.argv) > 1 else sorted(
    glob.glob("dispensary_menus/dispensary_menus_*.json"))[-1]
d = json.load(open(menu_file))
print(f"Source: {menu_file}\n")

PR_CATEGORY = {"AYR": {"Pre-Rolls"}, "Fluent": {"Prerolls"}, "MUV": {"Pre-Rolls"}}


def is_preroll(r, dispensary):
    if dispensary in PR_CATEGORY:
        return r["category"] in PR_CATEGORY[dispensary]
    # Trulieve: category isn't exposed on items; identify by name
    n = (r["name"] or "").lower()
    return "preroll" in n or "pre-roll" in n or "pre roll" in n


def total_grams(r, dispensary):
    """Each source stores size differently."""
    if r["size_value"] is None:
        return None
    if dispensary == "AYR":
        # netWeight is stored in milligrams
        return r["size_value"] / 1000.0
    if dispensary in ("Fluent", "MUV"):
        # unitSize is per-unit; multiply by pack count
        return r["size_value"] * (r["pack_count"] or 1)
    # Trulieve: the "units" attribute is already total grams
    return r["size_value"] if r["size_unit"] == "g" else None


rows = []
for disp in d["dispensaries"]:
    name = disp["dispensary"]
    for r in disp.get("products", []):
        if not is_preroll(r, name):
            continue
        g = total_grams(r, name)
        if not g or g <= 0:
            continue
        eff = r["deal_price"] if r["deal_price"] is not None else r["price"]
        if not eff:
            continue
        thc = r["thc"] if r["thc_unit"] == "%" else None
        rows.append({
            "dispensary": name,
            "name": r["name"],
            "label": r["variant_label"],
            "grams": round(g, 2),
            "price": r["price"],
            "deal": r["deal_price"],
            "eff": eff,
            "ppg": eff / g,
            "thc": thc,
            "deal_label": r["deal_label"],
        })

# de-duplicate Trulieve multi-batch rows (same product+price)
seen, unique = set(), []
for r in rows:
    key = (r["dispensary"], r["name"], r["label"], r["eff"])
    if key not in seen:
        seen.add(key)
        unique.append(r)
rows = unique


def show(title, sorted_rows, note=""):
    print(f"\n=== {title} ===" + (f"  ({note})" if note else ""))
    print(f"{'dispensary':<10} {'product':<42} {'size':<18} "
          f"{'$reg':>6} {'$deal':>6} {'$/g':>6} {'THC%':>6}")
    for r in sorted_rows:
        deal = f"{r['deal']:.2f}" if r["deal"] is not None else "-"
        thc = f"{r['thc']:.0f}" if r["thc"] is not None else "?"
        print(f"{r['dispensary']:<10} {r['name'][:42]:<42} {str(r['label'])[:18]:<18} "
              f"{r['price']:>6.2f} {deal:>6} {r['ppg']:>6.2f} {thc:>6}")


show("BEST VALUE -- lowest price per gram", sorted(rows, key=lambda r: r["ppg"]))
show("MOST POTENT -- highest THC %", [r for r in sorted(rows, key=lambda r: -(r["thc"] or 0))
                                      if r["thc"] is not None])
best = min(rows, key=lambda r: r["ppg"])
pot = max((r for r in rows if r["thc"] is not None), key=lambda r: r["thc"])
print(f"\nBottom line: cheapest flower-per-dollar is {best['dispensary']} "
      f"\"{best['name']}\" at ${best['ppg']:.2f}/g "
      f"({'deal $' + format(best['deal'], '.2f') if best['deal'] is not None else 'reg $' + format(best['price'], '.2f')}, "
      f"{best['thc']:.0f}% THC, {best['label']}).")
print(f"Strongest pre-roll on any menu: {pot['dispensary']} \"{pot['name']}\" "
      f"at {pot['thc']:.0f}% THC, ${pot['ppg']:.2f}/g.")