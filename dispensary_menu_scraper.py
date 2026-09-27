#!/usr/bin/env python3
"""
Orlando FL dispensary menu scraper.

Pulls live menu data (deals, prices, quantity in g/mg, THC %/mg, terpenes)
from four Orlando-area medical dispensaries:

  AYR        6325 N Orange Blossom Trail   -> Dutchie GraphQL  (api.dutchie.com)
  Trulieve   4544 N Orange Blossom Trail   -> Magento GraphQL  (pwaprodbackend.trulieve.com)
  Fluent     1901 N Orange Ave (Ivanhoe)   -> Sweed POS REST   (web-ui-production.sweedpos.com)
  MUV        101 S Garland Ave             -> Sweed POS proxy  (sweed.app)

All endpoints are anonymous (no API keys). Each was verified against the live
APIs. Output: one JSON file + one flat CSV per run in the output directory.

Usage:
  python3 dispensary_menu_scraper.py                    # all four, JSON + CSV
  python3 dispensary_menu_scraper.py --only ayr fluent  # subset
  python3 dispensary_menu_scraper.py --deals-only       # only discounted items
  python3 dispensary_menu_scraper.py --deep-terps       # Fluent: per-terpene lab data (1 req/variant)
  python3 dispensary_menu_scraper.py --outdir menus     # default: ./dispensary_menus
"""

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
REQUEST_GAP = 0.5   # polite delay between paged requests


# --------------------------------------------------------------------------
# Normalized record shape
# --------------------------------------------------------------------------
def new_record(dispensary, location):
    return {
        "dispensary": dispensary,
        "location": location,
        "name": None,
        "brand": None,
        "category": None,
        "subcategory": None,
        "strain_type": None,
        "variant_label": None,      # e.g. "3.5g", "100mg [10-Pack]", "1/8oz"
        "size_value": None,         # numeric
        "size_unit": None,          # "g" | "mg" | "ea"
        "pack_count": None,         # units per package (multi-packs)
        "price": None,              # regular price, USD
        "deal_price": None,         # discounted price, USD (None if no deal)
        "deal_label": None,         # promo/deal name(s)
        "thc": None,
        "thc_unit": None,           # "%" or "mg"
        "cbd": None,
        "total_terpenes": None,     # percent
        "terpenes": [],             # named list; entries may carry values
        "in_stock": None,
    }


# --------------------------------------------------------------------------
# AYR -- Dutchie GraphQL
# --------------------------------------------------------------------------
AYR_DISPENSARY_ID = "61f280865b7e780094b5918b"   # confirmed: 6325 N Orange Blossom Trail
AYR_LOCATION = "AYR - Orlando (N. Orange Blossom Trail), 6325 N Orange Blossom Trl, Orlando FL 32810"

AYR_QUERY = """
query Q($filter: productsFilterInput!, $page: Int, $perPage: Int) {
  filteredProducts(filter: $filter, page: $page, perPage: $perPage) {
    products {
      id Name type subcategory strainType
      brand { name }
      Options rawOptions weight
      measurements { netWeight { unit values } volume { unit values } }
      THCContent { unit range }
      CBDContent { unit range }
      totalTerpenes { unit range }
      cannabinoidsV2 { value unit cannabinoid { name } }
      terpenes { value unit unitSymbol libraryTerpene { name aromas } }
      recPrices recSpecialPrices medicalPrices medicalSpecialPrices Prices
      special
      specialData { bogoSpecials { specialId specialName specialType itemsForAPrice { enabled value } } }
    }
    queryInfo { totalCount totalPages }
  }
}
"""


def ayr_fetch(session, timeout):
    """Dutchie requires Chrome UA + non-empty Origin AND non-empty Referer
    to pass its Cloudflare bot rule; all three are mandatory."""
    url = "https://api.dutchie.com/graphql"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Origin": "https://dutchie.com",
        "Referer": "https://dutchie.com/",
    }
    records = []
    page = 1
    while True:
        payload = {
            "query": AYR_QUERY,
            "variables": {
                "filter": {"dispensaryId": AYR_DISPENSARY_ID, "pricingType": "med"},
                "page": page,
                "perPage": 100,
            },
        }
        r = session.post(url, headers=headers, json=payload, timeout=timeout)
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(
                "AYR: api.dutchie.com returned a non-JSON response (likely a Cloudflare "
                "block page). The UA/Origin/Referer headers must be set, and rapid "
                "repeat requests can trigger the bot rule -- wait a bit and retry.")
        if data.get("errors"):
            raise RuntimeError(f"AYR GraphQL errors: {data['errors']}")
        fp = data["data"]["filteredProducts"]
        total_pages = fp["queryInfo"]["totalPages"]
        for p in fp["products"]:
            records.extend(_ayr_row(p))
        if page >= total_pages:
            break
        page += 1
        time.sleep(REQUEST_GAP)
    return records


def _dutchie_unit(unit):
    return {"PERCENTAGE": "%", "MILLIGRAMS": "mg", "MILLILITERS": "ml"}.get(unit, (unit or "").lower())


def _ayr_row(p):
    names = p.get("medicalPrices") or []          # FL is medical; recPrices == med here
    deals = p.get("medicalSpecialPrices") or []
    weights = ((p.get("measurements") or {}).get("netWeight") or {})
    w_values = weights.get("values") or []
    w_unit = _dutchie_unit(weights.get("unit"))   # mg
    labels = p.get("Options") or p.get("rawOptions") or []

    bogo = ((p.get("specialData") or {}).get("bogoSpecials") or [])
    deal_names = [b.get("specialName") for b in bogo if b.get("specialName")]

    thc = p.get("THCContent") or {}
    cbd = p.get("CBDContent") or {}
    terps_total = p.get("totalTerpenes") or {}
    terp_list = []
    for t in p.get("terpenes") or []:
        entry = {"name": (t.get("libraryTerpene") or {}).get("name"), "value": t.get("value"),
                 "unit": _dutchie_unit(t.get("unit")) or t.get("unitSymbol")}
        if entry["name"]:
            terp_list.append(entry)

    thc_unit = _dutchie_unit(thc.get("unit"))
    rows = []
    n = max(len(names), len(deals), len(w_values), len(labels)) or 1
    for i in range(n):
        rec = new_record("AYR", AYR_LOCATION)
        rec.update({
            "name": p.get("Name"),
            "brand": (p.get("brand") or {}).get("name"),
            "category": p.get("type"),
            "subcategory": p.get("subcategory"),
            "strain_type": p.get("strainType"),
            "variant_label": labels[i] if i < len(labels) else (labels[0] if labels else None),
            "size_value": (w_values[i] if i < len(w_values) else (w_values[0] if w_values else p.get("weight"))),
            "size_unit": w_unit or "mg",
            "price": (names[i] if i < len(names) else (names[0] if names else None)),
            "deal_price": (deals[i] if i < len(deals) else (deals[0] if deals else None)),
            "deal_label": " | ".join(deal_names) if deal_names else None,
            "thc": (thc.get("range") or [None])[0],
            "thc_unit": thc_unit,
            "cbd": (cbd.get("range") or [None])[0],
            "total_terpenes": (terps_total.get("range") or [None])[0],
            "terpenes": terp_list,
        })
        rows.append(rec)
    return rows


# --------------------------------------------------------------------------
# Trulieve -- Magento GraphQL
# --------------------------------------------------------------------------
TRULIEVE_LOCATION = "Trulieve Orlando, 4544 N Orange Blossom Trl, Orlando FL 32804"
TRULIEVE_STORE_CODE = "orlando"   # 'Store' header scopes prices/deals to this location
# category ids: 207 Flower (208 whole, 212 minis, 215 shake, 268 pre-rolls),
# 155 Vapes, 6 Concentrates, 8 Edibles, 9 Oral, 18 Topicals
TRULIEVE_CATEGORIES = ["207", "155", "6", "8", "9", "18"]

TRULIEVE_QUERY = """
query Menu($filter: ProductAttributeFilterInput, $page: Int, $size: Int) {
  products(filter: $filter, currentPage: $page, pageSize: $size) {
    total_count
    page_info { page_size current_page total_pages }
    items {
      id name sku url_key stock_status available_quantity last_chance
      opps_promo { promo_id promo_name }
      special_price
      price_range { minimum_price {
        regular_price { value currency }
        final_price { value currency }
        discount { amount_off percent_off }
      } }
      ... on PhysicalProductInterface { weight }
      custom_attributes_product { code label value }
      ... on ConfigurableProduct {
        variants {
          product {
            sku special_price stock_status available_quantity
            thc_percentage
            dominant_terpene dominant_terpene_percentage
            secondary_terpene secondary_terpene_percentage
            terciary_terpene terciary_terpene_percentage
            total_terpene_percentage
            custom_attributes_product { code label value }
          }
          attributes { code label value_index }
        }
      }
    }
  }
}
"""


def trulieve_fetch(session, timeout):
    url = "https://pwaprodbackend.trulieve.com/graphql"
    headers = {"Content-Type": "application/json", "Store": TRULIEVE_STORE_CODE}
    records = []
    for cat in TRULIEVE_CATEGORIES:
        page = 1
        while True:
            payload = {
                "query": TRULIEVE_QUERY,
                "variables": {"filter": {"category_id": {"eq": cat}}, "page": page, "size": 50},
            }
            r = session.post(url, headers=headers, json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if data.get("errors"):
                raise RuntimeError(f"Trulieve GraphQL errors: {data['errors']}")
            products = data["data"]["products"]
            for item in products["items"]:
                records.append(_trulieve_row(item))
            if page >= (products["page_info"]["total_pages"] or 1):
                break
            page += 1
            time.sleep(REQUEST_GAP)
    return records


def _trulieve_attrs(item):
    return {a["code"]: a.get("value") for a in item.get("custom_attributes_product") or []}


def _trulieve_row(item):
    mp = item["price_range"]["minimum_price"]
    price = mp["regular_price"]["value"]
    final = mp["final_price"]["value"]
    discount = mp.get("discount") or {}
    deal_price = final if final != price else None
    promo_name = (item.get("opps_promo") or {}).get("promo_name")

    attrs = _trulieve_attrs(item)
    units = attrs.get("units")            # display string: "3.5g", "100mg", ...
    size_value, size_unit = None, None
    if units:
        m = re.match(r"([0-9.]+)\s*(g|mg|ea|each)$", units.strip(), re.I)
        if m:
            size_value = float(m.group(1))
            size_unit = m.group(2).lower().replace("each", "ea")

    # THC / terpenes live on lab-batch variants, not the parent
    thc_vals, terps = [], []
    for v in item.get("variants") or []:
        vattrs = _trulieve_attrs(v["product"])
        if vattrs.get("thc_percentage"):
            try:
                thc_vals.append(float(vattrs["thc_percentage"]))
            except (TypeError, ValueError):
                pass
        vp = v["product"]
        named = [
            {"name": vp.get("dominant_terpene"), "value": vp.get("dominant_terpene_percentage")},
            {"name": vp.get("secondary_terpene"), "value": vp.get("secondary_terpene_percentage")},
            {"name": vp.get("terciary_terpene"), "value": vp.get("terciary_terpene_percentage")},
        ]
        named = [t for t in named if t["name"]]
        if named:
            terps = named

    rec = new_record("Trulieve", TRULIEVE_LOCATION)
    rec.update({
        "name": attrs.get("product_display_name") or item.get("name"),
        "brand": attrs.get("brand"),
        "category": None,  # Trulieve doesn't expose category name on items
        "subcategory": attrs.get("strain"),
        "strain_type": attrs.get("strain_type"),
        "variant_label": units,
        "size_value": size_value,
        "size_unit": size_unit if size_unit != "g" else "g",
        "price": price,
        "deal_price": deal_price,
        "deal_label": promo_name,
        "thc": (round(sum(thc_vals) / len(thc_vals), 2) if thc_vals else None),
        "thc_unit": "%" if thc_vals else None,
        "total_terpenes": next(iter([v["product"].get("total_terpene_percentage")
                                     for v in item.get("variants") or []
                                     if v["product"].get("total_terpene_percentage")]), None),
        "terpenes": terps,
        "in_stock": item.get("stock_status") == "IN_STOCK",
    })
    return rec


# --------------------------------------------------------------------------
# Fluent -- Sweed POS REST (POST + StoreId header)
# --------------------------------------------------------------------------
FLUENT_LOCATION = "FLUENT Orlando - Orange Ave (Ivanhoe), 1901 N Orange Ave, Orlando FL 32804"
FLUENT_STORE_ID = 662    # resolved from shop.getfluent.com/orlando-downtown SSR payload
FLUENT_API = "https://web-ui-production.sweedpos.com/_api"


def _sweed_product_rows(products, dispensary, location):
    """Sweed POS field mapping (shared by Fluent and MUV)."""
    rows = []
    for p in products:
        for v in p.get("variants") or []:
            rec = new_record(dispensary, location)
            lab = v.get("labTests") or {}
            thc = lab.get("thc") or {}
            cbd = lab.get("cbd") or {}
            terp = lab.get("terpenes") or {}
            unit_size = v.get("unitSize") or {}
            unit_abbr = (unit_size.get("unitAbbr") or "").lower()
            unit = {"G": "g", "MG": "mg", "EA": "ea"}.get(unit_abbr, unit_abbr.lower() or None)
            promos = v.get("promos") or []
            rec.update({
                "name": p.get("name"),
                "brand": (p.get("brand") or {}).get("name"),
                "category": (p.get("category") or {}).get("name"),
                "subcategory": ((p.get("subcategory") or {}).get("name")
                                or ((p.get("strain") or {}).get("name"))),
                "strain_type": ((p.get("strain") or {}).get("prevalence") or {}).get("name"),
                "variant_label": v.get("name"),
                "size_value": unit_size.get("value"),
                "size_unit": unit,
                "pack_count": v.get("unitsInPackage"),
                "price": v.get("price"),
                "deal_price": v.get("promoPrice"),
                "deal_label": " | ".join(filter(None, [pr.get("shortName") or pr.get("name")
                                                       for pr in promos])) or None,
                "thc": (thc.get("value") or [None])[0],
                "thc_unit": "%" if (thc.get("unitAbbr") == "%") else ("mg" if thc.get("unitAbbr") == "MG" else thc.get("unitAbbr")),
                "cbd": (cbd.get("value") or [None])[0],
                "total_terpenes": (terp.get("value") or [None])[0],
                "terpenes": [{"name": t.get("name")} for t in ((p.get("strain") or {}).get("terpenes") or [])],
                "in_stock": (v.get("availableQty") or 0) > 0,
            })
            rows.append(rec)
    return rows


def _sweed_paged(session, url, headers, base_body, timeout):
    """POST pagination for Sweed GetProductList. Returns (rows, promos)."""
    all_products, promos, page = [], [], 1
    while True:
        body = dict(base_body, page=page)
        r = session.post(url, headers=headers, json=body, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        lst = data.get("list") or []
        all_products.extend(lst)
        promos = data.get("promos") or promos
        total = data.get("total") or 0
        if not lst or len(all_products) >= total:
            break
        page += 1
        time.sleep(REQUEST_GAP)
    return all_products, promos


def fluent_fetch(session, timeout, deep_terps=False):
    headers = {"Content-Type": "application/json", "StoreId": str(FLUENT_STORE_ID)}
    body = {"filters": {}, "page": 1, "pageSize": 250, "platformOs": "web",
            "searchTerm": "", "sortingMethodId": 7, "sourcePage": 1}
    products, promos = _sweed_paged(session, f"{FLUENT_API}/Products/GetProductList",
                                    headers, body, timeout)
    records = _sweed_product_rows(products, "Fluent", FLUENT_LOCATION)

    # optional: full measured terpene breakdown via lab endpoint
    if deep_terps:
        by_variant = {}
        for p in products:
            for v in p.get("variants") or []:
                by_variant[v["id"]] = v
        for rec in records:
            pass  # records are variant-rows; map back via label order
        idx = 0
        for p in products:
            for v in p.get("variants") or []:
                rec = records[idx] if idx < len(records) else None
                idx += 1
                if rec is None:
                    continue
                try:
                    r = session.post(f"{FLUENT_API}/Products/GetExtendedLabdata",
                                     headers=headers, json={"variantId": v["id"]}, timeout=timeout)
                    r.raise_for_status()
                    lab = r.json()
                    extra = []
                    for t in (lab.get("terpenes") or {}).get("values") or []:
                        if t.get("code") != "Total_Terpenes":
                            extra.append({"name": t.get("name"), "value": t.get("min"), "unit": "%"})
                    if extra:
                        rec["terpenes"] = extra
                except requests.RequestException:
                    continue
                time.sleep(REQUEST_GAP)
    return records, promos


# --------------------------------------------------------------------------
# MUV -- Sweed POS via public proxy (store id must be in the BODY)
# --------------------------------------------------------------------------
MUV_LOCATION = "MÜV Apopka, 3309 E State Road 436, Apopka FL"
MUV_STORE_ID = "280"   # Garland (320) has an empty upstream catalog; Apopka per user request
MUV_PROMOS_URL = "https://sweed.app/_api/proxy/Products/GetPromotionsList"
MUV_PRODUCTS_URL = "https://sweed.app/_api/proxy/Products/GetProductList"


def muv_fetch(session, timeout):
    headers = {"Content-Type": "application/json", "Storeid": MUV_STORE_ID,
               "SaleType": "Medical"}
    # store-level deals list
    promos = []
    try:
        r = session.post(MUV_PROMOS_URL, headers=headers,
                         json={"platformOs": "Web", "saleType": "Medical"}, timeout=timeout)
        r.raise_for_status()
        promos = r.json() or []
    except requests.RequestException as e:
        print(f"  [muv] promos fetch failed: {e}", file=sys.stderr)

    body = {"filters": {}, "page": 1, "pageSize": 250, "sorting": 0, "searchTerm": "",
            "saleType": "Medical", "platformOs": "web", "Storeid": MUV_STORE_ID}
    products, promos2 = _sweed_paged(session, MUV_PRODUCTS_URL, headers, body, timeout)
    promos = promos2 or promos
    records = _sweed_product_rows(products, "MUV", MUV_LOCATION)
    return records, promos


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
CSV_FIELDS = ["dispensary", "location", "name", "brand", "category", "subcategory",
              "strain_type", "variant_label", "size_value", "size_unit", "pack_count",
              "price", "deal_price", "deal_label", "thc", "thc_unit", "cbd",
              "total_terpenes", "terpenes", "in_stock"]


def write_outputs(outdir, results, run_stamp):
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / f"dispensary_menus_{run_stamp}.json"
    csv_path = outdir / f"dispensary_menus_{run_stamp}.csv"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    all_rows = []
    for disp in results["dispensaries"]:
        all_rows.extend(disp["products"])
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for row in all_rows:
            flat = dict(row)
            flat["terpenes"] = "; ".join(
                f"{t.get('name')}{':' + str(t.get('value')) if t.get('value') is not None else ''}"
                for t in row.get("terpenes") or [])
            flat.pop("location", None)
            w.writerow({k: flat.get(k) for k in CSV_FIELDS})
    return json_path, csv_path


def main():
    ap = argparse.ArgumentParser(description="Orlando dispensary menu scraper")
    ap.add_argument("--only", nargs="*", choices=["ayr", "trulieve", "fluent", "muv"],
                    default=["ayr", "trulieve", "fluent", "muv"])
    ap.add_argument("--outdir", default="dispensary_menus")
    ap.add_argument("--deals-only", action="store_true",
                    help="keep only products with a deal price")
    ap.add_argument("--deep-terps", action="store_true",
                    help="Fluent: fetch full per-terpene lab data (1 extra request per variant)")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    session = requests.Session()
    session.headers["Accept"] = "application/json"
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    results = {"fetched_at_utc": datetime.now(timezone.utc).isoformat(), "dispensaries": []}

    runners = {
        "ayr": ("AYR", lambda: (ayr_fetch(session, args.timeout), [])),
        "trulieve": ("Trulieve", lambda: (trulieve_fetch(session, args.timeout), [])),
        "fluent": ("Fluent", lambda: fluent_fetch(session, args.timeout, args.deep_terps)),
        "muv": ("MUV", lambda: muv_fetch(session, args.timeout)),
    }

    for key in args.only:
        label, runner = runners[key]
        print(f"== {label} ==")
        try:
            records, promos = runner()
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            results["dispensaries"].append({"dispensary": label, "error": str(e),
                                            "promos": [], "products": []})
            continue

        if args.deals_only:
            records = [r for r in records if r.get("deal_price") is not None]

        deals = sum(1 for r in records if r.get("deal_price") is not None)
        print(f"  {len(records)} products ({deals} on deal), {len(promos)} store promos")
        if key == "muv" and not records:
            print("  NOTE: MUV store %s returned an empty catalog (the public site shows "
                  "the same). Other MUV stores work with the same recipe -- change "
                  "MUV_STORE_ID (e.g. 319 Orlando-Colonial)." % MUV_STORE_ID, file=sys.stderr)
        results["dispensaries"].append({"dispensary": label, "promos": promos,
                                        "products": records})

    json_path, csv_path = write_outputs(Path(args.outdir), results, run_stamp)
    n = sum(len(d.get("products", [])) for d in results["dispensaries"])
    print(f"\nWrote {n} product rows:\n  {json_path}\n  {csv_path}")


if __name__ == "__main__":
    main()