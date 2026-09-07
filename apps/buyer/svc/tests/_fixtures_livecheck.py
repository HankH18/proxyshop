"""Fixtures for the live-page check. Offline by construction — nothing here opens a socket.

Loaded into ``conftest`` by ``proxyshop_support.fixture_loader``; see this directory's frozen
``conftest.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

CORPUS = Path(__file__).resolve().parent / "livepages"

#: The store the corpus was recorded from, and the domain the PLATFORM registers for it. Both
#: sides of the check's one security decision are named here so a test that changes either is
#: obviously changing it.
STORE_ID = "store-gaia"
STORE_DOMAIN = "www.gaiaherbs.com"


def corpus_manifest() -> dict[str, Any]:
    return json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))


def page_url(fixture: str) -> str:
    """The corpus URL for one recorded page."""
    handle = corpus_manifest()["derived_from"]["handle"]
    return f"https://{STORE_DOMAIN}/products/{handle}?fixture={fixture}"


@pytest.fixture()
def livecheck_corpus() -> dict[str, Any]:
    return corpus_manifest()


@pytest.fixture()
def recorded_fetcher() -> Any:
    from buyer_svc.livecheck import RecordedPageFetcher

    return RecordedPageFetcher(CORPUS)


@pytest.fixture(autouse=True)
def _reset_livecheck_seams() -> Any:
    """Drop the process-level queue, ledger and registries around every test in this package.

    Autouse and unconditional. The seams are module-level for the reason
    ``buyer_svc.pitch.writer`` states (``/render`` holds no ``Request``), which means they are
    shared by every test in the session — a queue left armed by one test would be drained by
    another, and a registry left bound would make a test that never configured one pass for
    the wrong reason. Both directions are failures this fixture removes.
    """
    from buyer_svc.livecheck import deferred, targets

    deferred._reset_for_tests()
    targets.set_registered_domains(None)
    targets.set_product_pages(None)
    yield
    deferred._reset_for_tests()
    targets.set_registered_domains(None)
    targets.set_product_pages(None)


def shortlist_body(
    *,
    fixture: str = "agrees.html",
    unit_price: float = 29.97,
    message: str = "In stock and ready to ship. Two year warranty on every bottle.",
    store_id: str = STORE_ID,
    product_ref: str = "prod_gaia_reflux",
    product_url: str | None = None,
    auction_id: str = "auc-live-1",
) -> dict[str, Any]:
    """One shortlist, exchange-shaped, with one sponsored slot.

    ``product_url`` is the field a bid does NOT carry today (see
    ``buyer_svc.livecheck.targets``' measured correction). It is settable here so the
    hostile-url refusals are driven through the same code path a future contract change would
    take.
    """
    slot: dict[str, Any] = {
        "slot": "headline",
        "bid_ref": "bid-gaia-1",
        "store_id": store_id,
        "fit_score": 0.91,
        "trust_summary": {"score": 0.82, "sample_size": 41},
        "provenance_labels": ["store-confirmed"],
        "store_domain": STORE_DOMAIN,
        "product": {"product_ref": product_ref, "variant_ref": "var_gaia_45ct"},
        "price": {
            "unit_price": unit_price,
            "total_price": unit_price,
            "currency": "USD",
            "discount": None,
            "expires_at": None,
        },
        "commitments": [
            {
                "key": "free_returns",
                "value": True,
                "unit": None,
                "provenance": {
                    "source": "store_confirmed",
                    "ref": "bid-gaia-1#free_returns",
                    "authority_rank": 2,
                },
            }
        ],
        "message": message,
    }
    if product_url is not None:
        slot["product_url"] = product_url
    elif fixture:
        slot["product_url"] = page_url(fixture)
    return {"auction_id": auction_id, "slots": [slot]}


class RecordingSink:
    """A ledger sink that keeps what it was handed. One positional argument, exactly once."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def append(self, event: Any) -> None:
        self.events.append(event)
