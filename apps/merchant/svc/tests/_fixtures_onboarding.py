"""Fixtures for T-053's onboarding-envelope tests. Owned by T-053.

Loaded into the frozen ``apps/merchant/svc/tests/conftest.py`` by
``proxyshop_support.fixture_loader``. Every name carries the ``onboarding_`` prefix so it
cannot collide with a fixture T-050, T-051 or T-052 drops in the same directory.

Nothing here needs a socket, a database or the clock: the interview under test is a recorded
document and the envelope store is process-local.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from merchant_svc.envelope.store import EnvelopeVersions
from merchant_svc.install.routes import ADMIN_TOKEN_ENV

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

#: The single interview fixture the frozen acceptance suite also reads.
INTERVIEW_FIXTURE = REPO_ROOT / "fixtures" / "interviews" / "northwind-outfitters.json"

#: The bearer token the envelope routes are driven with in these tests.
ONBOARDING_ADMIN_TOKEN = "onboarding-test-token"


@pytest.fixture
def onboarding_fixture_document() -> dict[str, Any]:
    """The whole ``fixtures/interviews/`` document — transcript *and* golden envelope."""
    return json.loads(INTERVIEW_FIXTURE.read_text())


@pytest.fixture
def onboarding_transcript(onboarding_fixture_document: dict[str, Any]) -> Any:
    """The recorded plain-language interview."""
    return onboarding_fixture_document["transcript"]


@pytest.fixture
def onboarding_golden(onboarding_fixture_document: dict[str, Any]) -> dict[str, Any]:
    """The envelope that interview must produce."""
    return onboarding_fixture_document["expected_envelope"]


@pytest.fixture
def onboarding_envelope(onboarding_transcript: Any) -> Any:
    """Version 1 of the fixture store's envelope, straight out of the interview."""
    from merchant_svc.onboarding import envelope_from_transcript

    return envelope_from_transcript(onboarding_transcript)


@pytest.fixture
def onboarding_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[EnvelopeVersions]:
    """An empty envelope history, swapped in for the process-wide one for one test.

    The routes read the module-level ``ENVELOPES``, so replacing that name is what isolates
    a test from every other test's stores — and from the real one.
    """
    fresh = EnvelopeVersions()
    monkeypatch.setattr("merchant_svc.onboarding.routes.ENVELOPES", fresh)
    yield fresh


@pytest.fixture
def onboarding_admin_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure the administrative bearer token the envelope routes require."""
    monkeypatch.setenv(ADMIN_TOKEN_ENV, ONBOARDING_ADMIN_TOKEN)
    return ONBOARDING_ADMIN_TOKEN


@pytest.fixture
async def onboarding_client(onboarding_store: EnvelopeVersions) -> AsyncIterator[httpx.AsyncClient]:
    """An in-process client against the frozen entrypoint's app, with an empty store."""
    from merchant_svc.main import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://merchant") as client:
        yield client
