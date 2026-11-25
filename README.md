# Printer Prices TR

A home screen web app for iPhone that shows what a short watchlist of 3D
printers costs in Turkey, whether each offer is in stock, pre-order or out of
stock, and the price in US dollars at the current market rate.

## How it works

- `collector/collect.py` runs once an hour on GitHub Actions
  (`.github/workflows/collect.yml`). It reads each store in
  `collector/stores.json`, finds the watchlist printers through the store's
  sitemap (or its Shopify product feed), opens every product page and writes
  `docs/data/prices.json`.
- `docs/` is the web app, served by GitHub Pages. The Refresh button loads
  the newest `prices.json` and a live USD/TRY rate.
- Stores that answer automated reading with a bot check are not worked
  around. They are listed in the app under "Stores that could not be read".

## Watchlist

Edit `MODELS` at the top of `collector/collect.py`. Each entry is a brand, a
model name, a pattern that matches the product title, and the lowest
believable price (cheaper matches are treated as spare parts).

## Stores

Add a store to `collector/stores.json` with an `id`, `name` and `base` URL.
Optional keys: `platform: "shopify"`, `sitemaps`, `price_re` (when the page's
structured price excludes VAT), `instock_markers` and `preorder_markers`.

## Run it on a PC

    python collector/collect.py                  # full run, about 4-9 minutes
    python collector/collect.py --only robo90    # one store
    python collector/collect.py --probe          # print results, write nothing
    python -m http.server 8765 --directory docs  # then open localhost:8765

Only the Python standard library is needed (`make_icons.py` needs Pillow).

## Notes

- Prices are the stores' own current prices in lira, VAT included.
- Dollar amounts are conversions, not store prices.
- If a store cannot be read during a run, its last known offers stay on
  screen marked as old, for up to 14 days.
