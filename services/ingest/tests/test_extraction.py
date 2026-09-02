"""T-021 — policy pages and marketing claims land in the graph with provenance.

    PROXYSHOP_WORKER=<n> ./.venv/bin/python -m pytest services/ingest/tests/test_extraction.py -q

The ticket's three acceptance criteria, and where each is graded here:

1. *Given fixture pages, extracted Claims match approved expectation file* —
   ``test_fixture_pages_extract_exactly_the_approved_claims`` against
   ``fixtures/pages/expected_claims.json``, which is ground truth read in one direction
   only.
2. *Every claim/attribute has a SUPPORTED_BY Source with snapshot ref; low-confidence
   extraction is quarantined, not upserted* — the provenance and quarantine sections.
3. *Re-run on unchanged pages performs no LLM calls (double counts)* — the differential
   section, which counts an LLM double's calls rather than inferring anything.

Every guard test is paired with a positive control: without one, an extractor that refuses
everything would pass the quarantine tests and one that extracts nothing would pass the
provenance tests.
"""

from __future__ import annotations

import json

import pytest
from ingest.adapters import CrawlBudget, FetchPolicy
from ingest.adapters import content_hash as adapter_content_hash
from ingest.extraction import (
    CLAIM_TYPES,
    DEFAULT_CONFIDENCE_FLOOR,
    EXTRACTION_CONTRACT,
    EXTRACTOR_VERSION,
    FENCE_CLOSE,
    FENCE_OPEN,
    ClaimProvenance,
    ExtractionLedger,
    LLMClaimExtractor,
    PolicyDocument,
    PolicyPageIngestor,
    RuleExtractor,
    SchemaViolation,
    build_prompt,
    content_hash,
    extract_claims,
    extract_policy_page,
    infer_claim_type,
    injection_markers,
    needs_extraction,
    normalise_provenance,
    page_kind_for,
    page_text,
    parse_model_claims,
    partition_by_confidence,
    policy_page_id,
    snapshot_ref,
    to_upserts,
)
from ingest.graph.model import SOURCE_CLASSES, AttributeValue, PolicyPage, Source

# NOTE: the policy-storefront fixtures (`policy_store`, `policy_store_factory`,
# `policy_pages`, `policy_expectation`) come from `_fixtures_extraction.py`, which this
# directory's conftest auto-loads. They are requested as fixtures rather than imported:
# under pytest's `importlib` import mode a test module cannot import a sibling helper by
# name, so everything a test needs from that module reaches it through the fixture.

#: Loopback is outside the default fetch policy's permitted networks, on purpose. Every
#: test that talks to an in-process server therefore loosens the policy at the call site,
#: the way `test_signed_fetch.py` does, rather than hiding it in a fixture.
LOOPBACK = FetchPolicy(allowed_ports=None, extra_allowed_networks=("127.0.0.0/8",))

POLICY_PAGE_TEXT = (
    "Shipping & Returns\n"
    "We ship every order within 2 business days from our Portland warehouse.\n"
    "Standard shipping is free on orders over $50; expedited shipping costs $12.\n"
    "Returns are accepted within 30 days of delivery for unworn items.\n"
    "All footwear carries a 1 year manufacturing guarantee.\n"
    "We may occasionally be able to ship faster during quiet weeks.\n"
)

SCRAPED_SOURCE = {
    "source": "scraped",
    "url": "https://store.example.com/policies/shipping",
    "ref": "snapshot://store.example.com/policies/shipping@sha256:0f1e2d3c",
    "content_hash": "sha256:0f1e2d3c",
    "observed_at": "2026-01-01T00:00:00Z",
    "authority_rank": 1,
}


def _crawl(ingestor: PolicyPageIngestor, base_url: str):
    """One policy crawl of an in-process storefront, with the loopback policy loosened."""
    return ingestor.run(
        store_id="store-t021",
        base_url=base_url,
        policy=LOOPBACK,
        budget=CrawlBudget(max_pages=40, max_seconds=30.0),
    )


# ---------------------------------------------------------------------------------------
# Acceptance 3 — content-hash-driven re-extraction
# ---------------------------------------------------------------------------------------


def test_the_content_hash_is_the_adapters_hash_not_a_second_convention() -> None:
    """A digest the crawler stored and one the extractor computes must be comparable."""
    assert content_hash is adapter_content_hash
    digest = content_hash(POLICY_PAGE_TEXT)
    assert digest.startswith("sha256:"), f"the digest must name its algorithm: {digest!r}"
    assert content_hash(POLICY_PAGE_TEXT) == digest, "content_hash must be deterministic"
    assert content_hash(POLICY_PAGE_TEXT.encode()) == digest, "str and bytes must agree"


def test_unchanged_content_needs_no_extraction_and_anything_else_does() -> None:
    """The whole differential contract, as a table."""
    digest = content_hash(POLICY_PAGE_TEXT)
    changed = POLICY_PAGE_TEXT + "We now also ship to Canada.\n"

    assert needs_extraction(digest, POLICY_PAGE_TEXT) is False
    assert needs_extraction(digest, changed) is True
    assert needs_extraction(None, POLICY_PAGE_TEXT) is True, "never seen means do the work"
    assert needs_extraction("", POLICY_PAGE_TEXT) is True, "an empty hash is not a match"
    assert needs_extraction(content_hash(changed), changed) is False


def test_the_change_decision_opens_no_socket_and_calls_no_model(monkeypatch) -> None:
    """C6: deciding whether to re-extract is a local comparison, not a fetch."""
    import socket

    def _boom(*args, **kwargs):
        raise AssertionError("the change decision must not open a network connection")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    assert needs_extraction(content_hash(POLICY_PAGE_TEXT), POLICY_PAGE_TEXT) is False


def test_a_second_run_over_an_unchanged_page_makes_zero_model_calls(llm_double) -> None:
    """Acceptance 3, counted on the double rather than inferred."""
    extractor = LLMClaimExtractor(llm_double)
    llm_double.when(
        FENCE_OPEN,
        json.dumps(
            {
                "claims": [
                    {
                        "key": "returns.window_days",
                        "value": 30,
                        "unit": "days",
                        "claim_type": "return_policy",
                        "confidence": 0.9,
                    }
                ]
            }
        ),
    )
    ledger = ExtractionLedger()
    document = PolicyDocument(
        url="https://store.example.com/policies/shipping",
        kind="shipping",
        text=POLICY_PAGE_TEXT,
        content_hash=content_hash(POLICY_PAGE_TEXT),
        snapshot_ref=snapshot_ref(
            "https://store.example.com/policies/shipping", content_hash(POLICY_PAGE_TEXT)
        ),
        observed_at="2026-01-01T00:00:00Z",
    )

    first = extract_policy_page(document, ledger=ledger, extractor=extractor)
    # Positive control: without this, an extractor that never runs would "pass" below.
    assert first.model_calls == 1 and len(llm_double.calls) == 1
    assert first.reused is False
    assert first.claims, "the first run must actually extract something"

    second = extract_policy_page(document, ledger=ledger, extractor=extractor)
    assert second.model_calls == 0, "an unchanged page must perform no LLM call"
    assert len(llm_double.calls) == 1, "the double must not have been called a second time"
    assert second.reused is True
    assert [c.key for c in second.claims] == [c.key for c in first.claims]
    assert extractor.calls == 1


def test_changed_content_re_extracts_and_calls_the_model_again(llm_double) -> None:
    """The other half of the guard: the short-circuit must not swallow a real change."""
    extractor = LLMClaimExtractor(llm_double)
    llm_double.when(FENCE_OPEN, json.dumps({"claims": []}))
    ledger = ExtractionLedger()

    def document(text: str) -> PolicyDocument:
        digest = content_hash(text)
        return PolicyDocument(
            url="https://store.example.com/policies/shipping",
            kind="shipping",
            text=text,
            content_hash=digest,
            snapshot_ref=snapshot_ref("https://store.example.com/policies/shipping", digest),
            observed_at="2026-01-01T00:00:00Z",
        )

    extract_policy_page(document(POLICY_PAGE_TEXT), ledger=ledger, extractor=extractor)
    assert len(llm_double.calls) == 1

    updated = extract_policy_page(
        document(POLICY_PAGE_TEXT + "We now also ship to Canada.\n"),
        ledger=ledger,
        extractor=extractor,
    )
    assert updated.reused is False
    assert updated.model_calls == 1
    assert len(llm_double.calls) == 2


def test_a_cached_result_answers_a_different_floor_without_re_extracting(llm_double) -> None:
    """Re-partitioning is free; only the extraction is expensive."""
    extractor = LLMClaimExtractor(llm_double)
    llm_double.when(
        FENCE_OPEN,
        json.dumps(
            {
                "claims": [
                    {"key": "returns.window_days", "value": 30, "confidence": 0.95},
                    {"key": "shipping.dispatch_window_days", "value": 2, "confidence": 0.3},
                ]
            }
        ),
    )
    ledger = ExtractionLedger()
    digest = content_hash(POLICY_PAGE_TEXT)
    document = PolicyDocument(
        url="https://store.example.com/policies/shipping",
        kind="shipping",
        text=POLICY_PAGE_TEXT,
        content_hash=digest,
        snapshot_ref=snapshot_ref("https://store.example.com/policies/shipping", digest),
        observed_at="2026-01-01T00:00:00Z",
    )

    loose = extract_policy_page(document, ledger=ledger, extractor=extractor, confidence_floor=0.0)
    assert len(loose.claims) == 2 and loose.quarantined == ()

    strict = extract_policy_page(document, ledger=ledger, extractor=extractor, confidence_floor=0.9)
    assert strict.reused is True and strict.model_calls == 0
    assert len(llm_double.calls) == 1
    assert [c.key for c in strict.claims] == ["returns.window_days"]
    assert [c.key for c in strict.quarantined] == ["shipping.dispatch_window_days"]


def test_a_stale_ledger_entry_never_answers_for_changed_content() -> None:
    """The cache is keyed by the page and *validated* by the digest."""
    ledger = ExtractionLedger()
    ledger.record("https://store.example.com/policies/shipping", "sha256:aaa", object())

    assert ledger.cached("https://store.example.com/policies/shipping", "sha256:aaa") is not None
    assert ledger.cached("https://store.example.com/policies/shipping", "sha256:bbb") is None
    assert ledger.cached("https://store.example.com/policies/other", "sha256:aaa") is None

    ledger.forget("https://store.example.com/policies/shipping")
    assert ledger.known_hash("https://store.example.com/policies/shipping") is None


# ---------------------------------------------------------------------------------------
# Acceptance 2a — every claim is SUPPORTED_BY a scraped Source with a snapshot ref
# ---------------------------------------------------------------------------------------


def test_every_extracted_claim_carries_a_scraped_source_with_a_snapshot_ref(policy_pages) -> None:
    """R8/C6 over every committed fixture page, on both sides of the floor."""
    seen = 0
    for name, body in policy_pages.items():
        url = f"https://store.example.com/policies/{name.removesuffix('.html')}"
        result = extract_claims(body, None, url=url, confidence_floor=0.0)
        assert result.claims, f"{name} must yield at least one atomic claim"
        for claim in result.all_claims:
            seen += 1
            assert claim.key.strip(), f"claim must be keyed: {claim!r}"
            assert claim.provenance.source == "scraped"
            assert claim.provenance.ref.startswith("snapshot://")
            assert claim.provenance.ref.endswith(result.content_hash)
            assert claim.provenance.observed_at.strip()
            assert claim.claim_type in CLAIM_TYPES
    assert seen >= 12, "the fixture set must actually exercise this"


def test_a_claims_source_is_a_valid_graph_source_node() -> None:
    """The ``SUPPORTED_BY`` target is the real node type, not a look-alike."""
    result = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE, confidence_floor=0.0)
    for claim in result.claims:
        source = claim.supported_by
        assert isinstance(source, Source)
        assert source.source_class == "scraped" and source.source_class in SOURCE_CLASSES
        assert source.url and source.content_hash and source.observed_at
        assert source.extractor_version
        assert source.confidence == pytest.approx(claim.confidence)
        assert source.as_properties()["source_class"] == "scraped"

    ids = {claim.supported_by.source_id for claim in result.claims}
    assert len(ids) == len(result.claims), (
        "each reading keeps its own confidence, so each needs its own Source id"
    )


def test_provenance_is_completed_from_the_snapshot_ref_when_that_is_all_there_is() -> None:
    """The pinned contract type carries only four fields; a Source node needs seven."""
    provenance = normalise_provenance(
        {
            "source": "scraped",
            "ref": "snapshot://store.example.com/policies/shipping@sha256:0f1e2d3c",
            "observed_at": "2026-01-01T00:00:00Z",
            "authority_rank": 1,
        }
    )
    assert provenance.url == "https://store.example.com/policies/shipping"
    assert provenance.content_hash == "sha256:0f1e2d3c"
    assert provenance.source_id.startswith("src_")
    assert provenance.extractor_version == EXTRACTOR_VERSION
    assert isinstance(provenance.as_source(), Source)


def test_the_pinned_contracts_provenance_is_accepted_as_a_source_descriptor() -> None:
    """DESIGN: ``Source ≡ Provenance``. The extractor takes the contract type directly."""
    contracts = pytest.importorskip("contracts")
    descriptor = contracts.Provenance(
        source="scraped",
        ref="snapshot://store.example.com/policies/shipping@sha256:0f1e2d3c",
        observed_at="2026-01-01T00:00:00Z",
        authority_rank=1,
    )
    result = extract_claims(POLICY_PAGE_TEXT, descriptor, confidence_floor=0.0)
    assert result.claims
    for claim in result.claims:
        assert claim.provenance.source == "scraped"
        assert claim.provenance.ref == descriptor.ref


def test_a_provenance_record_that_points_at_nothing_is_refused() -> None:
    """A blank Source satisfies "every fact has provenance" while providing none."""
    with pytest.raises(ValueError, match="ref"):
        ClaimProvenance(source="scraped", ref="", observed_at="2026-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="observed_at"):
        ClaimProvenance(source="scraped", ref="snapshot://x/y@sha256:aa", observed_at="")
    with pytest.raises(ValueError, match="not one of"):
        ClaimProvenance(
            source="hearsay", ref="snapshot://x/y@sha256:aa", observed_at="2026-01-01T00:00:00Z"
        )
    with pytest.raises(ValueError, match="neither a `ref` nor a `url`"):
        extract_claims(POLICY_PAGE_TEXT, {"source": "scraped"})


# ---------------------------------------------------------------------------------------
# Acceptance 2b — low-confidence extraction is quarantined, not upserted
# ---------------------------------------------------------------------------------------


def test_a_hedged_promise_scores_below_the_floor_and_is_held_back() -> None:
    """ "We *may occasionally* ship faster" is marketing, not a commitment."""
    result = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE)

    upserted = {claim.key for claim in result.claims}
    held = {claim.key for claim in result.quarantined}
    assert "shipping.speed_claim" in held, "the hedged sentence must not reach the graph"
    assert "shipping.speed_claim" not in upserted
    # Positive control: the unhedged commitments in the same page are still extracted.
    assert {"shipping.dispatch_window_days", "returns.window_days"} <= upserted

    held_claim = next(c for c in result.quarantined if c.key == "shipping.speed_claim")
    assert held_claim.quarantine_reason == "below_confidence_floor"
    assert held_claim.confidence < DEFAULT_CONFIDENCE_FLOOR
    assert held_claim.provenance.ref, "a quarantined claim keeps its provenance"


@pytest.mark.parametrize("floor", [0.0, 0.2, 0.5, 0.6, 0.89, 0.9, 0.93, 0.94, 1.0])
def test_the_partition_loses_no_claim_at_any_floor(floor: float) -> None:
    """C10's floor moves claims between two collections; it never deletes one."""
    everything = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE, confidence_floor=0.0)
    total = len(everything.claims)

    result = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE, confidence_floor=floor)
    assert len(result.claims) + len(result.quarantined) == total
    assert all(c.confidence >= floor for c in result.claims)
    assert all(c.confidence < floor for c in result.quarantined)


def test_a_floor_above_every_reading_holds_the_whole_batch_back() -> None:
    """An extractor that upserts regardless of the floor fails here."""
    baseline = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE, confidence_floor=0.0)
    assert baseline.claims and baseline.quarantined == ()

    above = max(c.confidence for c in baseline.claims) + 0.01
    strict = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE, confidence_floor=above)
    assert strict.claims == ()
    assert len(strict.quarantined) == len(baseline.claims)
    assert all(c.quarantine_reason == "below_confidence_floor" for c in strict.quarantined)


def test_every_confidence_is_a_probability() -> None:
    """A score outside [0, 1] makes the floor meaningless."""
    result = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE, confidence_floor=0.0)
    for claim in result.claims:
        assert 0.0 <= claim.confidence <= 1.0


def test_a_non_confidence_quarantine_survives_a_floor_of_zero() -> None:
    """The floor decides confidence; it does not decide validity."""
    result = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE, confidence_floor=0.0)
    poisoned = result.claims[0].quarantine("schema_violation")
    keep, held = partition_by_confidence((poisoned,), 0.0)
    assert keep == ()
    assert held == (poisoned,)


def test_extraction_is_deterministic() -> None:
    """The approved expectation file is only meaningful against a repeatable extractor."""
    a = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE, confidence_floor=0.0)
    b = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE, confidence_floor=0.0)
    assert [(c.key, c.value, c.confidence) for c in a.claims] == [
        (c.key, c.value, c.confidence) for c in b.claims
    ]


# ---------------------------------------------------------------------------------------
# Acceptance 1 — the approved expectation file
# ---------------------------------------------------------------------------------------


def test_fixture_pages_extract_exactly_the_approved_claims(
    policy_pages, policy_expectation
) -> None:
    """Acceptance 1. The expectation is ground truth; the extractor is what is graded."""
    assert policy_expectation["extractor_version"] == EXTRACTOR_VERSION, (
        "the expectation was approved against a different extractor version"
    )
    floor = policy_expectation["confidence_floor"]
    assert set(policy_expectation["pages"]) == set(policy_pages), (
        "every committed fixture page must be covered by the expectation, and vice versa"
    )

    for name, expected in policy_expectation["pages"].items():
        result = extract_claims(
            policy_pages[name],
            None,
            url=expected["url"],
            kind=expected["kind"],
            observed_at="2026-01-01T00:00:00Z",
            confidence_floor=floor,
        )
        actual = [
            {
                "key": c.key,
                "value": c.value,
                "unit": c.unit,
                "claim_type": c.claim_type,
                "confidence": c.confidence,
            }
            for c in result.claims
        ]
        assert actual == expected["claims"], f"{name}: upserted claims differ from the approval"

        held = [
            {
                "key": c.key,
                "value": c.value,
                "unit": c.unit,
                "claim_type": c.claim_type,
                "confidence": c.confidence,
                "quarantine_reason": c.quarantine_reason,
            }
            for c in result.quarantined
        ]
        assert held == expected["quarantined"], f"{name}: quarantine differs from the approval"
        assert (
            list(injection_markers(page_text(policy_pages[name]))) == expected["injection_markers"]
        ), f"{name}: untrusted-instruction detection differs from the approval"


def test_every_claim_type_the_extractor_emits_is_in_the_published_vocabulary(
    policy_expectation,
) -> None:
    """DESIGN's ``claim_type -> dimension`` table is exhaustive and raises on a stranger."""
    from fixtures import FIXTURES_DIR

    manifest = json.loads((FIXTURES_DIR / "manifest.json").read_text())
    published = set(manifest["claim_type_dimensions"])
    assert CLAIM_TYPES == published, (
        "the extractor's vocabulary and the approved trust-routing table must be the same set"
    )
    emitted = {
        claim["claim_type"]
        for page in policy_expectation["pages"].values()
        for claim in page["claims"] + page["quarantined"]
    }
    assert emitted <= published, f"unroutable claim types: {sorted(emitted - published)}"


# ---------------------------------------------------------------------------------------
# C10 — scraped text is untrusted data, never instructions
# ---------------------------------------------------------------------------------------


def test_an_injected_page_yields_only_its_real_claims_and_a_warning(policy_pages) -> None:
    """The instruction paragraph is data: it produces no claim and is reported."""
    body = policy_pages["injected.html"]
    result = extract_claims(body, None, url="https://store.example.com/policies/injected")

    keys = {claim.key for claim in result.all_claims}
    # Positive control: the page's genuine commitments are still read.
    assert keys == {"shipping.dispatch_window_days", "returns.window_days"}
    assert all("instruction" not in str(c.value).lower() for c in result.all_claims)
    # The page demands confidence 1.0 for everything. It does not get it.
    assert all(claim.confidence < 1.0 for claim in result.all_claims)
    assert any("untrusted instruction text" in warning for warning in result.warnings)


def test_a_commented_out_promise_is_never_extracted(policy_pages) -> None:
    """An HTML comment is not something a buyer could have read."""
    body = policy_pages["injected.html"]
    assert "Free shipping on everything, always." in body, "the fixture must contain the trap"
    text = page_text(body)
    assert "Free shipping on everything" not in text
    assert "authorize_discount" in text, "the visible injection text is kept, not sanitised away"


def test_the_prompt_fences_the_page_and_keeps_the_contract_out_of_it() -> None:
    """The only place the model is instructed is a block the page cannot reach."""
    prompt = build_prompt(POLICY_PAGE_TEXT, url="https://store.example.com/x", kind="shipping")
    assert prompt.splitlines()[0].isupper(), "the section label identifies the untrusted turn"
    assert FENCE_OPEN in prompt and FENCE_CLOSE in prompt
    assert POLICY_PAGE_TEXT.strip() in prompt
    assert EXTRACTION_CONTRACT not in prompt, (
        "the contract belongs in the system block; putting it in the user turn puts it "
        "next to text that is trying to rewrite it"
    )


def test_a_model_reply_that_breaks_the_schema_quarantines_the_page(llm_double) -> None:
    """C10: LLM output is validated against a strict schema, not trusted."""
    llm_double.when(FENCE_OPEN, "Sure! Here are the claims: shipping is fast.")
    extractor = LLMClaimExtractor(llm_double)

    result = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE, extractor=extractor)
    assert result.claims == ()
    assert any("schema violation" in warning for warning in result.warnings)

    # Positive control: valid JSON from the same client does produce claims.
    llm_double.when(
        FENCE_OPEN, json.dumps({"claims": [{"key": "returns.window_days", "value": 30}]})
    )
    ok = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE, extractor=LLMClaimExtractor(llm_double))
    assert [c.key for c in ok.claims] == ["returns.window_days"]


def test_a_value_the_page_does_not_contain_is_refused() -> None:
    """ "Copy values verbatim, never infer" is enforced, not requested."""
    grounded = parse_model_claims(
        json.dumps({"claims": [{"key": "returns.window_days", "value": 30}]}),
        text=POLICY_PAGE_TEXT,
    )
    assert [c.value for c in grounded] == [30]

    with pytest.raises(SchemaViolation, match="does not appear in the page"):
        parse_model_claims(
            json.dumps({"claims": [{"key": "returns.window_days", "value": 365}]}),
            text=POLICY_PAGE_TEXT,
        )


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "not json at all",
        json.dumps({"claims": "thirty days"}),
        json.dumps({"claims": [{"value": 30}]}),
        json.dumps({"claims": [{"key": "returns.window_days"}]}),
        json.dumps({"claims": [{"key": "returns.window_days", "value": 30, "confidence": 4}]}),
        json.dumps({"claims": [{"key": "returns.window_days", "value": 30, "confidence": "hi"}]}),
        json.dumps({"claims": ["returns.window_days"]}),
    ],
)
def test_every_malformed_reply_shape_is_refused(reply: str) -> None:
    """Each of these is a way a model reply has actually gone wrong somewhere."""
    with pytest.raises(SchemaViolation):
        parse_model_claims(reply, text=POLICY_PAGE_TEXT)


def test_a_json_object_wrapped_in_prose_is_still_read() -> None:
    """Refusing this would quarantine sound extractions over a preamble."""
    claims = parse_model_claims(
        'Here you go:\n{"claims": [{"key": "returns.window_days", "value": 30}]}\nHope that helps!',
        text=POLICY_PAGE_TEXT,
    )
    assert [c.key for c in claims] == ["returns.window_days"]


def test_the_model_may_not_invent_a_claim_type_the_trust_table_has_no_row_for() -> None:
    """An unmapped ``claim_type`` would raise at manifest load; it is folded here instead."""
    assert infer_claim_type("returns.window_days", "totally_made_up") == "return_policy"
    assert infer_claim_type("returns.window_days", "warranty") == "warranty"
    assert infer_claim_type("net_weight") == "specifications"
    for key in ("a", "zzz", ""):
        assert infer_claim_type(key) in CLAIM_TYPES


def test_the_recorded_extraction_fixture_replays_through_the_validator() -> None:
    """T-014 recorded a real extraction reply for this ticket; it must parse."""
    recordings = pytest.importorskip("llm.recordings")
    if "extraction_claims" not in recordings.available_recordings():
        pytest.skip("no extraction recording is committed")
    table = recordings.load_recording("extraction_claims")
    parsed = 0
    for (_system, prompt), reply in table.items():
        claims = parse_model_claims(reply, text=prompt)
        parsed += 1
        for claim in claims:
            assert claim.claim_type in CLAIM_TYPES
            assert 0.0 <= claim.confidence <= 1.0
    assert parsed, "the recording must contain at least one prompt"


# ---------------------------------------------------------------------------------------
# Graph writes — only the upsertable half becomes work
# ---------------------------------------------------------------------------------------


def test_the_write_batch_states_the_page_before_the_claims_it_makes() -> None:
    """``link_states`` refuses an edge to a PolicyPage that does not exist yet."""
    digest = content_hash(POLICY_PAGE_TEXT)
    document = PolicyDocument(
        url="https://store.example.com/policies/shipping",
        kind="shipping",
        text=POLICY_PAGE_TEXT,
        content_hash=digest,
        snapshot_ref=snapshot_ref("https://store.example.com/policies/shipping", digest),
        observed_at="2026-01-01T00:00:00Z",
    )
    result = extract_claims(POLICY_PAGE_TEXT, document.provenance())
    ops = to_upserts(document, result)

    assert ops[0].kind == "policy_page"
    assert isinstance(ops[0].node, PolicyPage)
    assert ops[0].node.page_id == policy_page_id(document.url) == document.page_id
    assert ops[0].node.snapshot_ref == document.snapshot_ref

    states = ops[1:]
    assert len(states) == len(result.claims)
    assert {op.kind for op in states} == {"states"}
    for op in states:
        assert isinstance(op.node, AttributeValue)
        assert op.context["page_id"] == document.page_id
        assert isinstance(op.source, Source) and op.source.source_class == "scraped"


def test_quarantined_claims_are_not_graph_writes() -> None:
    """C10's whole point: below the floor is *not upserted*, not merely flagged."""
    digest = content_hash(POLICY_PAGE_TEXT)
    document = PolicyDocument(
        url="https://store.example.com/policies/shipping",
        kind="shipping",
        text=POLICY_PAGE_TEXT,
        content_hash=digest,
        snapshot_ref=snapshot_ref("https://store.example.com/policies/shipping", digest),
        observed_at="2026-01-01T00:00:00Z",
    )
    result = extract_claims(POLICY_PAGE_TEXT, document.provenance())
    assert result.quarantined, "the fixture must actually quarantine something"

    written_keys = {op.node.key for op in to_upserts(document, result) if op.kind == "states"}
    for claim in result.quarantined:
        assert claim.key not in written_keys or claim.key in {c.key for c in result.claims}

    # And a batch with nothing above the floor is no work at all.
    nothing = extract_claims(POLICY_PAGE_TEXT, document.provenance(), confidence_floor=1.0)
    assert to_upserts(document, nothing) == []


def test_an_attribute_value_carries_the_reading_in_its_typed_slot() -> None:
    """The candidate query compares numbers as numbers, so a number must land as one."""
    result = extract_claims(POLICY_PAGE_TEXT, SCRAPED_SOURCE, confidence_floor=0.0)
    by_key = {claim.key: claim.as_attribute() for claim in result.claims}

    assert by_key["returns.window_days"].value_number == 30.0
    assert by_key["returns.window_days"].value_string is None
    assert by_key["returns.window_days"].unit == "days"
    assert by_key["shipping.speed_claim"].value_string == "ship faster"
    assert by_key["shipping.speed_claim"].value_number is None


# ---------------------------------------------------------------------------------------
# The fetch path — through T-020's guarded transport, never around it
# ---------------------------------------------------------------------------------------


def test_the_ingestor_reads_a_storefronts_policy_pages_and_extracts_them(policy_store) -> None:
    """The end-to-end shape: fetch, hash, extract, provenance — no graph required."""
    base_url, stub = policy_store
    report = _crawl(PolicyPageIngestor(), base_url)

    assert "/robots.txt" in stub.paths_fetched(), "robots.txt must be read before the pages"
    assert stub.paths_fetched().index("/robots.txt") == 0
    kinds = {document.kind for document in report.documents}
    assert {"shipping", "returns", "warranty"} <= kinds
    assert report.claims, "the crawl must produce claims"
    for claim in report.claims:
        assert claim.provenance.source == "scraped"
        assert claim.provenance.ref.startswith(f"snapshot://{base_url.split('//')[1]}")
    assert report.quarantined, "the hedged sentences must be held back, not dropped"


def test_a_second_crawl_of_an_unchanged_store_extracts_nothing_new(policy_store) -> None:
    """Acceptance 3, over the real fetch path."""
    base_url, _stub = policy_store
    ingestor = PolicyPageIngestor(extractor=RuleExtractor())

    first = _crawl(ingestor, base_url)
    assert first.reused == (), "nothing can be reused on a first crawl"
    assert ingestor.extractor.calls == len(first.documents)

    second = _crawl(ingestor, base_url)
    assert set(second.reused) == {d.url for d in second.documents}
    assert ingestor.extractor.calls == len(first.documents), (
        "an unchanged storefront must not run the extractor again"
    )
    assert second.model_calls == 0
    assert [c.key for c in second.claims] == [c.key for c in first.claims]
    assert ingestor.to_upserts(second) == ingestor.to_upserts(first)


def test_a_changed_policy_page_is_the_only_one_re_extracted(policy_store) -> None:
    """Differential means per page, not per store."""
    base_url, stub = policy_store
    ingestor = PolicyPageIngestor(extractor=RuleExtractor())
    first = _crawl(ingestor, base_url)
    baseline_calls = ingestor.extractor.calls

    stub.replace(
        "/policies/refund-policy",
        "<html><body><main><p>Returns are accepted within 60 days of delivery.</p>"
        "</main></body></html>",
    )
    second = _crawl(ingestor, base_url)

    assert ingestor.extractor.calls == baseline_calls + 1, "exactly one page changed"
    assert len(second.reused) == len(first.documents) - 1
    changed = next(
        r for d, r in zip(second.documents, second.results, strict=False) if not r.reused
    )
    assert any(c.key == "returns.window_days" and c.value == 60 for c in changed.claims)


def test_robots_disallow_keeps_the_fetcher_off_a_policy_page(policy_store_factory) -> None:
    """C6: robots.txt is honoured on the policy path too, not only the catalog path."""
    base_url, stub = policy_store_factory(robots="User-agent: *\nDisallow: /policies/\nAllow: /\n")
    report = _crawl(PolicyPageIngestor(), base_url)

    assert not any(path.startswith("/policies/") for path in stub.paths_fetched())
    assert any("robots.txt disallows" in warning for warning in report.warnings)
    # Positive control: the allowed page was still read.
    assert [d.kind for d in report.documents] == ["warranty"]


def test_an_unreachable_robots_file_fails_closed(policy_store_factory) -> None:
    """A 500 on robots.txt is not permission to crawl."""
    base_url, stub = policy_store_factory(robots_status=500)
    report = _crawl(PolicyPageIngestor(), base_url)

    assert report.documents == ()
    assert stub.paths_fetched() == ["/robots.txt"]
    assert any("disallow-all" in warning for warning in report.warnings)


def test_the_crawler_identifies_itself_on_every_policy_request(policy_store) -> None:
    """C6: an identified bot, on this path as much as any other."""
    from ingest.adapters import USER_AGENT

    base_url, stub = policy_store
    _crawl(PolicyPageIngestor(), base_url)

    assert stub.requests, "the crawl must have made requests"
    assert USER_AGENT.lower().startswith("proxyshopbot")


# ---------------------------------------------------------------------------------------
# Production wiring — the frozen entrypoint mounts this
# ---------------------------------------------------------------------------------------


def test_the_frozen_entrypoint_mounts_the_extraction_router() -> None:
    """``ingest.main.create_app`` globs ``*/routes.py``; this is where that lands."""
    import importlib

    main = importlib.import_module("ingest.main")
    assert "ingest.extraction.routes" in main.discover_router_modules()

    app = main.create_app()
    assert "ingest.extraction.routes" in app.state.mounted_routers
    paths = set(app.openapi()["paths"])
    assert {"/extraction/policy-pages", "/extraction/config"} <= paths


def test_the_endpoint_extracts_a_page_and_then_short_circuits_on_it(policy_pages) -> None:
    """The differential guarantee, observed across two HTTP requests."""
    import importlib

    from fastapi.testclient import TestClient

    routes = importlib.import_module("ingest.extraction.routes")
    routes.ledger.forget("https://store.example.com/policies/shipping")

    with TestClient(importlib.import_module("ingest.main").create_app()) as client:
        payload = {
            "url": "https://store.example.com/policies/shipping",
            "body": policy_pages["shipping.html"],
        }
        first = client.post("/extraction/policy-pages", json=payload)
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["reused"] is False
        assert body["claims"], "the first call must extract"
        assert body["quarantined"], "and must report what it held back"
        for claim in body["claims"]:
            assert claim["provenance"]["source"] == "scraped"
            assert claim["provenance"]["ref"].startswith("snapshot://")
            assert claim["supported_by"]["source_class"] == "scraped"

        replay = client.post(
            "/extraction/policy-pages", json={**payload, "known_hash": body["content_hash"]}
        )
        assert replay.status_code == 200, replay.text
        again = replay.json()
        assert again["reused"] is True
        assert again["model_calls"] == 0
        assert [c["key"] for c in again["claims"]] == [c["key"] for c in body["claims"]]

        config = client.get("/extraction/config").json()
        assert config["confidence_floor"] == DEFAULT_CONFIDENCE_FLOOR
        assert set(config["claim_types"]) == CLAIM_TYPES


def test_the_endpoint_refuses_a_page_with_no_readable_text() -> None:
    """A 422 beats silently returning an empty extraction."""
    import importlib

    from fastapi.testclient import TestClient

    with TestClient(importlib.import_module("ingest.main").create_app()) as client:
        response = client.post(
            "/extraction/policy-pages",
            json={"url": "https://store.example.com/policies/empty", "body": "<html></html>"},
        )
        assert response.status_code == 422


def test_page_kinds_are_classified_from_the_url() -> None:
    """``PolicyPage.kind`` is what a downstream reader filters on."""
    assert page_kind_for("https://s.example.com/policies/shipping-policy") == "shipping"
    assert page_kind_for("https://s.example.com/policies/refund-policy") == "returns"
    assert page_kind_for("https://s.example.com/pages/warranty") == "warranty"
    assert page_kind_for("https://s.example.com/pages/about-us") == "policy"


# ---------------------------------------------------------------------------------------
# The graph write, against a real Neo4j — the provenance audit reads the graph, not the
# call sites, so this is the only place "SUPPORTED_BY" is actually proved.
# ---------------------------------------------------------------------------------------


@pytest.mark.docker
@pytest.mark.graph
def test_extracted_claims_land_in_the_graph_with_complete_provenance(
    graph_schema_session,
) -> None:
    """Acceptance 2, end to end: the writes apply and the graph audit finds no gap."""
    from ingest.extraction import apply_policy_upserts
    from ingest.graph import provenance_violations

    session = graph_schema_session
    digest = content_hash(POLICY_PAGE_TEXT)
    url = "https://store.example.com/policies/shipping"
    document = PolicyDocument(
        url=url,
        kind="shipping",
        text=POLICY_PAGE_TEXT,
        content_hash=digest,
        snapshot_ref=snapshot_ref(url, digest),
        observed_at="2026-01-01T00:00:00Z",
    )
    result = extract_claims(POLICY_PAGE_TEXT, document.provenance())
    assert result.claims and result.quarantined, "the fixture must exercise both sides"

    written = apply_policy_upserts(session, to_upserts(document, result))
    assert written, "the batch must actually write something"
    assert provenance_violations(session) == [], (
        "every material fact this ticket writes must carry a SUPPORTED_BY Source"
    )

    stated = session.run(
        "MATCH (p:PolicyPage {page_id: $page_id})-[:STATES]->(a:AttributeValue) "
        "RETURN a.key AS key ORDER BY key",
        page_id=document.page_id,
    ).data()
    assert [row["key"] for row in stated] == sorted(c.key for c in result.claims)

    held_back = {c.key for c in result.quarantined}
    assert held_back and not (held_back & {row["key"] for row in stated}), (
        "a quarantined claim must not appear in the graph"
    )

    sources = session.run(
        "MATCH (a:AttributeValue)-[:SUPPORTED_BY]->(s:Source) "
        "RETURN DISTINCT s.source_class AS klass, s.url AS url"
    ).data()
    assert sources and all(row["klass"] == "scraped" and row["url"] == url for row in sources)

    # Idempotent: replaying the same unchanged batch writes the same nodes, not duplicates.
    again = apply_policy_upserts(session, to_upserts(document, result))
    assert again == written
    count = session.run("MATCH (a:AttributeValue) RETURN count(a) AS n").single()["n"]
    assert count == len(result.claims)
