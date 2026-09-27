# Dispensary Menu Scraper

Pulls live menu data from four Orlando-area Florida medical dispensaries and
finds the best pre-roll deals by price-per-gram.

## Dispensaries covered

| Dispensary | Address | Menu platform |
|---|---|---|
| AYR | 6325 N Orange Blossom Trail, Orlando | Dutchie GraphQL |
| Trulieve | 4544 N Orange Blossom Trail, Orlando | Magento GraphQL |
| FLUENT | 1901 N Orange Ave (Ivanhoe Village), Orlando | Sweed POS |
| MÜV | 3309 E State Road 436, Apopka | Sweed POS (proxy) |

All APIs are queried anonymously — no API keys required. Each endpoint recipe
was verified against the live APIs.

## Usage

```bash
pip install requests

# Scrape all four menus -> timestamped JSON + CSV in ./dispensary_menus/
python3 dispensary_menu_scraper.py

python3 dispensary_menu_scraper.py --only ayr fluent   # subset
python3 dispensary_menu_scraper.py --deals-only        # only discounted items
python3 dispensary_menu_scraper.py --deep-terps        # Fluent: full per-terpene lab data
python3 dispensary_menu_scraper.py --outdir menus

# Pre-roll analysis: best price-per-gram, potency ranking
python3 pre_roll_deals.py [menus_file.json]
```

## Output

Each run writes `dispensary_menus_<timestamp>.json` and `.csv`. Every product
row includes: name, brand, category, strain type, size (value + g/mg/ea +
pack count), regular price, deal price, deal label, THC (unit-aware: % for
flower/vape, mg for edibles), CBD, total terpenes, named terpenes, and stock
status.

## Notes

- **AYR / Dutchie** sits behind a Cloudflare bot rule; rapid repeat runs can
  get temporarily blocked (~90 s). Space runs a few minutes apart.
- **Trulieve** store-scoped pricing uses the `Store: orlando` GraphQL header;
  some discounts are cart-rule based and appear only in the final price.
- **MÜV** defaults to the Apopka store (id 280) because the Garland location
  had an empty upstream catalog. Every other MÜV store works with the same
  recipe — change `MUV_STORE_ID` (Garland = 320, Orlando-Colonial = 319).
- Deals rotate daily — rerun the scraper to refresh.