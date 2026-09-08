"""Build ``tests/livepages/`` — the replay corpus for the live-page check.

Run by hand (``.venv/bin/python apps/buyer/svc/tests/_corpus_build.py``), never by the suite,
and never over the network. It reads ``fixtures/real-catalogs/stores/*.products.jsonl.gz`` —
this repository's own point-in-time capture of ten real public catalogues, collected in 28
requests under robots.txt with a declared contact — and renders the ``application/ld+json``
block a Shopify theme's ``product-json-ld`` snippet emits for those exact products.

**What that makes these fixtures, stated precisely so nobody reads them as more.** The VALUES
are real and recorded: the title, the handle, the sku, the variant price and the ``available``
flag are the bytes the storefront served. The MARKUP is reconstructed — the corpus holds the
``/products.json`` surface, not the product page's HTML — so it is a faithful rendering of a
known theme's output around recorded data rather than a capture of a page.

``test_livecheck_corpus.py`` re-derives the values from the same gzip and fails if this
corpus has drifted from it, so the reconstruction cannot quietly stop matching what was
recorded.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
CATALOGS = REPO / "fixtures" / "real-catalogs" / "stores"
OUT = Path(__file__).resolve().parent / "livepages"

#: One real store and one real product from it. Chosen for shape, not for value: a product
#: with several variants and a plain in-stock first variant.
HOST = "gaiaherbs.com"
ORIGIN = "https://www.gaiaherbs.com"


def _first_product() -> dict[str, Any]:
    path = CATALOGS / f"{HOST}.products.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            variants = row.get("variants") or []
            if variants and variants[0].get("price") and variants[0].get("available") is True:
                return row
    raise SystemExit(f"no usable product in {path}")


def _json_ld(product: dict[str, Any], *, price: str, availability: str) -> str:
    variant = product["variants"][0]
    body = {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": product["title"],
        "url": f"{ORIGIN}/products/{product['handle']}",
        "sku": variant.get("sku") or "",
        "brand": {"@type": "Brand", "name": product.get("vendor") or ""},
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": "USD",
            "availability": f"https://schema.org/{availability}",
            "url": f"{ORIGIN}/products/{product['handle']}",
        },
    }
    return json.dumps(body, indent=2)


def _page(product: dict[str, Any], block: str) -> str:
    return (
        '<!doctype html>\n<html lang="en">\n  <head>\n'
        '    <meta charset="utf-8" />\n'
        f"    <title>{product['title']}</title>\n"
        f'    <script type="application/ld+json">\n{block}\n    </script>\n'
        "  </head>\n  <body>\n"
        f"    <h1>{product['title']}</h1>\n"
        "  </body>\n</html>\n"
    )


def main() -> None:
    product = _first_product()
    variant = product["variants"][0]
    listed = str(variant["price"])
    handle = product["handle"]
    url = f"{ORIGIN}/products/{handle}"

    OUT.mkdir(parents=True, exist_ok=True)
    pages: dict[str, dict[str, Any]] = {}

    files = {
        "agrees.html": _page(product, _json_ld(product, price=listed, availability="InStock")),
        "price-moved.html": _page(
            product,
            _json_ld(product, price=f"{float(listed) / 2:.2f}", availability="InStock"),
        ),
        "sold-out.html": _page(product, _json_ld(product, price=listed, availability="OutOfStock")),
        "pre-order.html": _page(product, _json_ld(product, price=listed, availability="PreOrder")),
        "microdata.html": (
            "<!doctype html>\n<html><body>\n"
            f'  <div itemscope itemtype="https://schema.org/Product">\n'
            f'    <span itemprop="name">{product["title"]}</span>\n'
            f'    <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">\n'
            f'      <meta itemprop="price" content="{listed}" />\n'
            f'      <meta itemprop="priceCurrency" content="USD" />\n'
            f'      <link itemprop="availability" href="https://schema.org/InStock" />\n'
            "    </div>\n  </div>\n</body></html>\n"
        ),
        "og-only.html": (
            "<!doctype html>\n<html><head>\n"
            f'  <meta property="og:title" content="{product["title"]}" />\n'
            f'  <meta property="product:price:amount" content="{listed}" />\n'
            '  <meta property="product:price:currency" content="USD" />\n'
            '  <meta property="product:availability" content="instock" />\n'
            "</head><body></body></html>\n"
        ),
        "no-structured-data.html": (
            "<!doctype html>\n<html><head><title>"
            f"{product['title']}</title></head><body>\n"
            f"  <h1>{product['title']}</h1>\n"
            f'  <p class="price">${listed}</p>\n'
            "  <p>Add to cart</p>\n</body></html>\n"
        ),
        "broken-json-ld.html": _page(product, '{"@type": "Product", "offers": {'),
    }
    for name, body in files.items():
        (OUT / name).write_text(body, encoding="utf-8")
        pages[f"{url}?fixture={name}"] = {
            "status": 200,
            "body_file": name,
            "encoding": "utf-8",
            "final_url": f"{url}?fixture={name}",
        }

    pages[f"{url}?fixture=missing"] = {"status": 404, "reason": "the page answered 404"}
    pages[f"{url}?fixture=timeout"] = {
        "status": 0,
        "reason": "the page could not be fetched (timed out after 20.0s)",
    }
    pages[f"{url}?fixture=robots"] = {
        "status": 0,
        "reason": "this store's robots.txt disallows this crawler for that page",
    }

    manifest = {
        "corpus_version": "1.0.0",
        "what_this_is": (
            "Replay pages for the live-page check. The VALUES are recorded from "
            "fixtures/real-catalogs (a point-in-time capture of a real public catalogue); the "
            "MARKUP is a reconstruction of the schema.org block a Shopify theme emits, because "
            "that corpus holds /products.json and not product-page HTML."
        ),
        "derived_from": {
            "file": f"fixtures/real-catalogs/stores/{HOST}.products.jsonl.gz",
            "host": HOST,
            "product_id": product["id"],
            "handle": handle,
            "title": product["title"],
            "vendor": product.get("vendor"),
            "variant_id": variant["id"],
            "variant_sku": variant.get("sku"),
            "variant_price": listed,
            "variant_available": variant.get("available"),
        },
        "no_network": (
            "Nothing here was fetched by a test and nothing here is fetched by a test. D3/C9: "
            "the suite runs offline and a test that reaches the network is a defect even when "
            "it passes."
        ),
        "pages": pages,
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(files)} pages + manifest to {OUT}")


if __name__ == "__main__":
    main()
