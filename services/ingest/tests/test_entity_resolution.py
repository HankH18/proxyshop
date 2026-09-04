"""Entity resolution: GTIN identity, threshold agreement, blocking, SAME_AS edges (T-022).

The frozen acceptance test grades one pair at two thresholds. That is the *goal*, not the
risk surface: entity resolution is a matching problem, and matching problems fail on the
inputs nobody built the happy path for — a check digit that does not check, one GTIN written
two ways, a title made of invisible characters, a score sitting exactly on the boundary, a
NaN that makes a comparator false in both directions, and a pair that matches transitively
through a third record that should never have joined them.

So the tests below are organised by *what could be wrong*:

``TestGtinNormalisation``     what counts as an identifier at all
``TestMatchInvariants``       the one rule everything else is a theorem of
``TestConflictsAndAbsence``   disagreement vetoes, absence abstains
``TestUnicodeEvasion``        a title that renders identically must resolve identically
``TestFixtureSetQuality``     T-022 acceptance 1, graded against committed ground truth
``TestBlockingAndResolution`` which pairs get asked about, and the transitive false merge
``TestSameAsEdges``           the edges actually reach the graph

``fixtures/er/pairs.json`` is ground truth read in one direction: the resolver is graded
against those labels and never writes them.
"""

from __future__ import annotations

import json
import math
import pathlib
from typing import Any

import pytest

#: The committed ER evaluation set (T-022 acceptance 1).
PAIRS_FILE = pathlib.Path(__file__).resolve().parents[3] / "fixtures" / "er" / "pairs.json"


def _pairs() -> dict[str, Any]:
    return json.loads(PAIRS_FILE.read_text(encoding="utf-8"))


def _record(product_id: str, gtin: str = "", name: str = "", brand: str = "", **extra: Any):
    record = {
        "product_id": product_id,
        "gtin": gtin,
        "canonical_name": name,
        "brand": brand,
    }
    record.update(extra)
    return record


class TestGtinNormalisation:
    """What is and is not a trade-item identifier."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("00012345678905", "00012345678905"),  # GTIN-14, already canonical
            ("0012345678905", "00012345678905"),  # EAN-13
            ("012345678905", "00012345678905"),  # UPC-A
            ("0-12345-67890-5", "00012345678905"),  # printed barcode separators
            ("  00012345678905  ", "00012345678905"),  # surrounding whitespace
            ("０００１２３４５６７８９０５", "00012345678905"),  # full-width digits, NFKC-folded
            ("96385074", "00000096385074"),  # EAN-8
        ],
    )
    def test_every_encoding_of_one_trade_item_reduces_to_one_key(self, raw, expected) -> None:
        """UPC-A, EAN-13 and GTIN-14 are one number written three ways.

        This is the property the whole identity rule rests on: two storefronts publishing the
        same product in different GTIN renderings must produce the same canonical key, or the
        "GTIN matches always link" guarantee is true only when both stores happened to pick
        the same encoding.
        """
        from ingest.er import normalize_gtin

        assert normalize_gtin(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "why"),
        [
            ("00099999999999", "check digit should be 0, not 9"),
            ("00088888888888", "check digit should be 0, not 8"),
            ("00012345678904", "one digit off a real GTIN"),
            ("", "absent"),
            ("   ", "whitespace"),
            (None, "null"),
            (True, "a boolean is not an identifier"),
            ("abc", "not numeric"),
            ("gid://shopify/Product/8123456", "a Shopify GID is not a GTIN"),
            ("12345678905", "11 digits is not a GTIN length"),
            ("0123456789012345", "16 digits is not a GTIN length"),
            ("00000000000000", "the unset-field placeholder"),
            ("0000000000000", "the same placeholder, EAN-13 width"),
        ],
    )
    def test_unusable_values_are_refused_rather_than_trusted(self, raw, why) -> None:
        """Every way a GTIN can fail to be an identifier answers ``""``.

        The check-digit cases matter most. ``normalize_gtin`` feeds an *identity* rule that
        links with confidence 1.0 and no appeal to the names, so a value that GS1's own
        arithmetic rejects must not reach it: two unrelated products sharing one mistyped
        barcode would otherwise be merged outright.
        """
        from ingest.er import is_valid_gtin, normalize_gtin

        assert normalize_gtin(raw) == "", why
        assert is_valid_gtin(raw) is False, why

    def test_the_all_zero_placeholder_passes_its_check_digit_and_is_still_refused(self) -> None:
        """The one bad value a check digit cannot catch.

        ``00000000000000`` is arithmetically valid — an empty weighted sum has check digit
        zero — and stores emit it constantly as an unset field's default. Accepting it would
        link every product that never filled in a GTIN to every other one.
        """
        from ingest.er import gtin_check_digit, normalize_gtin

        assert gtin_check_digit("0" * 13) == 0, "the placeholder is check-digit valid"
        assert normalize_gtin("0" * 14) == "", "and is refused anyway"


class TestMatchInvariants:
    """``linked == (confidence >= threshold)``, and what follows from it."""

    def test_equal_gtins_link_at_every_permitted_threshold(self) -> None:
        """ "Always" means at 1.0 as well as at 0.9 — the whole point of an identity rule."""
        from ingest.er import match

        left = _record("p-1", "00012345678905", "Trail Runner Shoe", "Cascade")
        right = _record("p-2", "012345678905", "Zapatilla de trail para hombre", "Cascade")
        for threshold in (0.01, 0.5, 0.86, 0.9, 0.95, 0.999, 1.0):
            decision = match(left, right, threshold)
            assert decision.linked is True, threshold
            assert decision.confidence == 1.0
            assert decision.method == "gtin"

    def test_the_decision_and_the_confidence_can_never_disagree(self) -> None:
        """Swept over every code path, not asserted on one example.

        A matcher that computes a score and then decides separately has two opinions, and the
        second one is the one nobody tested. Here the comparison IS the decision, so the
        sweep is checking that no branch has quietly grown its own verdict.
        """
        from ingest.er import match

        names = ["Trail Runner Shoe", "Merino Wool Sock", "", "Ceramic Burr Grinder"]
        brands = ["", "Cascade", "Kettleworks"]
        gtins = ["", "00012345678905", "00000000000017", "00099999999999"]
        checked = 0
        for left_name in names:
            for right_name in names:
                for left_brand in brands:
                    for right_brand in brands:
                        for left_gtin in gtins:
                            for right_gtin in gtins:
                                for threshold in (0.4, 0.86, 0.9, 1.0):
                                    decision = match(
                                        _record("a", left_gtin, left_name, left_brand),
                                        _record("b", right_gtin, right_name, right_brand),
                                        threshold,
                                    )
                                    assert decision.linked == (decision.confidence >= threshold), (
                                        decision
                                    )
                                    assert 0.0 <= decision.confidence <= 1.0, decision
                                    assert not math.isnan(decision.confidence), decision
                                    checked += 1
        assert checked == 4 * 4 * 3 * 3 * 4 * 4 * 4

    def test_the_threshold_boundary_is_inclusive(self) -> None:
        """A pair scoring exactly the threshold links; a hair above it does not.

        Which side of the boundary is inclusive is not a free choice: the frozen goal says a
        pair *refused* at 0.9 scores below 0.9, which is only true if ``score >= threshold``
        links. Pinned here so a later refactor cannot flip it silently.
        """
        from ingest.er import match

        left = _record("p-1", "", "Trail Runner Shoe", "Cascade")
        right = _record("p-2", "", "Trail Runner Shoe", "Cascade")
        score = match(left, right, 0.5).confidence
        assert match(left, right, score).linked is True, "score == threshold must link"
        assert match(left, right, min(1.0, score + 1e-9)).linked is False

    def test_resolution_is_symmetric_as_a_value_not_merely_as_a_verdict(self) -> None:
        """``match(a, b) == match(b, a)`` for the whole decision, including its evidence."""
        from ingest.er import match

        left = _record("p-1", "00012345678905", "Trail Runner Shoe", "Cascade")
        right = _record("p-2", "00012345678905", "Zapatilla de trail", "Cascade")
        weak_a = _record("p-3", "00099999999999", "Ceramic Burr Coffee Grinder", "Kettleworks")
        weak_b = _record("p-4", "00088888888888", "Merino Wool Hiking Sock", "Cascade")
        for one, other, threshold in ((left, right, 0.95), (weak_a, weak_b, 0.9)):
            assert match(one, other, threshold) == match(other, one, threshold)

    def test_resolution_is_deterministic(self) -> None:
        """Identical inputs, identical output — every field, every call."""
        from ingest.er import match

        left = _record("p-3", "", "Ceramic Burr Coffee Grinder", "Kettleworks")
        right = _record("p-4", "", "Merino Wool Hiking Sock", "Kettleworks")
        first = match(left, right, 0.9)
        assert all(match(left, right, 0.9) == first for _ in range(20))

    @pytest.mark.parametrize(
        ("threshold", "why"),
        [
            (0.0, "links the entire catalog into one product"),
            (-0.5, "not a probability"),
            (1.5, "links nothing, forever, with no error"),
            (90, "a 0.9 written as a percentage"),
            (float("nan"), "every comparison against it is false"),
            (float("inf"), "links nothing"),
            (None, "not a number"),
            (True, "a boolean is not a threshold"),
        ],
    )
    def test_a_threshold_that_would_fail_silently_fails_loudly(self, threshold, why) -> None:
        """Each rejected value degrades the resolver *without* raising, if it is honoured.

        ``0.0`` links everything and ``90`` links nothing; both leave a green test suite and a
        wrong graph. Refusing them at the call is the only place the mistake is visible.
        """
        from ingest.er import match

        with pytest.raises(ValueError):
            match(_record("a", "", "x", "y"), _record("b", "", "x", "y"), threshold)

    def test_free_text_names_alone_can_never_reach_a_usable_threshold(self) -> None:
        """DESIGN §Data models: never match products by free-text name alone.

        Enforced by arithmetic rather than by review: with no corroborating signal the score
        is capped at ``NAME_WEIGHT``, which is below the default threshold by construction.
        """
        from ingest.er import DEFAULT_MATCH_THRESHOLD, NAME_WEIGHT, match

        assert NAME_WEIGHT < DEFAULT_MATCH_THRESHOLD
        decision = match(
            _record("p-1", "", "Trail Runner Shoe", ""),
            _record("p-2", "", "Trail Runner Shoe", ""),
            DEFAULT_MATCH_THRESHOLD,
        )
        assert decision.linked is False
        assert decision.confidence == pytest.approx(NAME_WEIGHT)

    def test_only_an_identity_match_is_a_certainty(self) -> None:
        """Nothing on the similarity path reaches 1.0, so ``threshold=1.0`` is identity-only."""
        from ingest.er import MAX_SIMILARITY_CONFIDENCE, match

        perfect = match(
            _record("p-1", "", "Trail Runner Shoe", "Cascade"),
            _record("p-2", "", "Trail Runner Shoe", "Cascade"),
            1.0,
        )
        assert perfect.confidence == MAX_SIMILARITY_CONFIDENCE < 1.0
        assert perfect.linked is False


class TestConflictsAndAbsence:
    """Disagreement vetoes; absence abstains. They are not the same thing."""

    def test_two_valid_different_gtins_beat_identical_names_and_brands(self) -> None:
        """A check-digit-valid GTIN belonging to another product is the case this exists for."""
        from ingest.er import match

        decision = match(
            _record("p-1", "00012345678905", "Trail Runner Shoe", "Cascade"),
            _record("p-2", "00000000000017", "Trail Runner Shoe", "Cascade"),
            0.86,
        )
        assert decision.linked is False
        assert decision.confidence == 0.0
        assert decision.method == "gtin_conflict"

    def test_an_absent_gtin_does_not_conflict_with_a_present_one(self) -> None:
        """Half the catalog publishes no GTIN; that must not veto every pair it appears in."""
        from ingest.er import match

        decision = match(
            _record("p-1", "00012345678905", "Trail Runner Shoe", "Cascade"),
            _record("p-2", "", "Trail Runner Shoe", "Cascade"),
            0.86,
        )
        assert decision.linked is True
        assert decision.method == "similarity"

    def test_a_placeholder_gtin_neither_links_nor_conflicts(self) -> None:
        """It is not an identifier, so the pair is decided on its other evidence."""
        from ingest.er import match

        same_product = match(
            _record("p-1", "00000000000000", "Trail Runner Shoe", "Cascade"),
            _record("p-2", "00000000000000", "Trail Runner Shoe", "Cascade"),
            0.86,
        )
        assert same_product.method == "similarity", "not an identity match"
        assert same_product.linked is True, "but the names and brands still agree"

        different_product = match(
            _record("p-3", "00000000000000", "Merino Wool Hiking Sock", "Cascade"),
            _record("p-4", "00000000000000", "Ceramic Burr Grinder", "Kettleworks"),
            0.86,
        )
        assert different_product.linked is False

    def test_different_brands_veto_and_a_brand_suffix_does_not(self) -> None:
        """ "Cascade" and "Cascade Outdoors" are one maker; "Cascade" and "Kettleworks" are not."""
        from ingest.er import match

        suffix = match(
            _record("p-1", "", "Merino Wool Hiking Sock", "Cascade"),
            _record("p-2", "", "Merino Wool Hiking Socks", "Cascade Outdoors"),
            0.86,
        )
        assert suffix.linked is True

        conflict = match(
            _record("p-3", "", "Stainless Steel Water Bottle", "Cascade"),
            _record("p-4", "", "Stainless Steel Water Bottle", "Kettleworks"),
            0.86,
        )
        assert conflict.linked is False
        assert conflict.method == "brand_conflict"

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n", None])
    def test_records_without_a_name_are_no_evidence_rather_than_a_match(self, blank) -> None:
        """Two nameless records of one brand must not resolve to one product."""
        from ingest.er import match

        decision = match(
            _record("p-1", "", blank or "", "Cascade"),
            _record("p-2", "", blank or "", "Cascade"),
            0.86,
        )
        assert decision.linked is False
        assert decision.method == "no_evidence"

    def test_a_nan_embedding_is_ignored_rather_than_propagated(self) -> None:
        """A NaN confidence is false in *both* comparison directions.

        That is what lets input order decide a comparator's output, so a non-finite vector is
        refused at the door instead of being allowed to reach the score.
        """
        from ingest.er import match

        poisoned = _record("p-1", "", "Trail Runner Shoe", "Cascade", embedding=[float("nan")] * 4)
        clean = _record("p-2", "", "Trail Runner Shoe", "Cascade", embedding=[1.0, 0.0, 0.0, 0.0])
        decision = match(poisoned, clean, 0.86)
        assert not math.isnan(decision.confidence)
        assert "embedding" not in dict(decision.evidence)
        assert decision == match(clean, poisoned, 0.86)

    def test_a_mismatched_embedding_width_is_ignored_rather_than_raising(self) -> None:
        """A provider swap mid-catalog must not abort a resolution run."""
        from ingest.er import match

        decision = match(
            _record("p-1", "", "Trail Runner Shoe", "Cascade", embedding=[1.0, 0.0]),
            _record("p-2", "", "Trail Runner Shoe", "Cascade", embedding=[1.0, 0.0, 0.0]),
            0.86,
        )
        assert "embedding" not in dict(decision.evidence)

    def test_a_real_embedding_corroborates(self) -> None:
        """The signal is wired up, so a semantic provider adds recall rather than nothing."""
        from ingest.er import match

        aligned = [1.0, 0.0, 0.0, 0.0]
        decision = match(
            _record("p-1", "", "Trail Runner Shoe", "", embedding=aligned),
            _record("p-2", "", "Trail Runner Shoe", "", embedding=aligned),
            0.86,
        )
        assert dict(decision.evidence)["embedding"] == pytest.approx(1.0)
        assert decision.linked is True, "an embedding is corroboration, so this is not name-alone"


class TestUnicodeEvasion:
    """A title that renders identically must resolve identically."""

    def test_a_cyrillic_homoglyph_does_not_dodge_resolution(self) -> None:
        """``Тrail`` and ``Trail`` are the same word to a reader and must be to the resolver."""
        from ingest.er import match

        decision = match(
            _record("p-1", "", "Trail Runner Shoe", "Cascade"),
            _record("p-2", "", "Тrail Runner Shoe", "Сascade"),
            0.86,
        )
        assert decision.linked is True
        assert dict(decision.evidence)["name"] == pytest.approx(1.0)

    def test_a_title_made_of_invisible_characters_is_no_title(self) -> None:
        """The hazard: zero-width characters are not whitespace to ``strip()`` or to ``\\s``.

        Two records titled with one zero-width space each would otherwise fold to the same
        non-empty string and score a *perfect* name match against each other.
        """
        from ingest.er import fold, match

        invisible = "​‍­﻿"
        assert fold(invisible) == ""
        decision = match(
            _record("p-1", "", invisible, "Cascade"),
            _record("p-2", "", "﻿​", "Cascade"),
            0.86,
        )
        assert decision.linked is False
        assert decision.method == "no_evidence"

    def test_a_soft_hyphen_inside_a_word_does_not_split_it(self) -> None:
        """A shy hyphen is a rendering hint, not a letter."""
        from ingest.er import text_similarity

        assert text_similarity("Tra­il Runner", "Trail Runner") == 1.0

    def test_accents_and_case_do_not_make_two_products(self) -> None:
        from ingest.er import text_similarity

        assert text_similarity("Café Press Carafe", "CAFE PRESS CARAFE") == 1.0


class TestFixtureSetQuality:
    """T-022 acceptance 1, graded against committed ground truth."""

    def test_the_fixture_set_is_labelled_ground_truth_with_both_classes(self) -> None:
        """A precision measurement over an all-positive set measures nothing."""
        data = _pairs()
        labels = [pair["same"] for pair in data["pairs"]]
        assert len(labels) >= 12
        assert labels.count(True) >= 5 and labels.count(False) >= 5
        assert all(pair["why"].strip() for pair in data["pairs"])

    def test_precision_and_recall_clear_the_configured_floors(self) -> None:
        """Acceptance 1: precision ≥ the configured floor, on the approved fixture set.

        Recall is graded too. A resolver that links nothing has perfect precision, so a
        precision gate on its own is satisfied by doing no work at all.
        """
        from ingest.er import match

        data = _pairs()
        threshold = data["threshold"]
        true_positive = false_positive = false_negative = 0
        misses = []
        for pair in data["pairs"]:
            decision = match(pair["left"], pair["right"], threshold)
            if decision.linked and pair["same"]:
                true_positive += 1
            elif decision.linked and not pair["same"]:
                false_positive += 1
                misses.append(("false link", pair["why"], decision.confidence))
            elif not decision.linked and pair["same"]:
                false_negative += 1
                misses.append(("missed link", pair["why"], decision.confidence))

        precision = true_positive / max(1, true_positive + false_positive)
        recall = true_positive / max(1, true_positive + false_negative)
        assert precision >= data["precision_floor"], misses
        assert recall >= data["recall_floor"], misses

    def test_every_gtin_pair_in_the_fixture_set_links(self) -> None:
        """Acceptance 1's other half: GTIN matches always link, whatever the names say."""
        from ingest.er import match, normalize_gtin

        checked = 0
        for pair in _pairs()["pairs"]:
            left_gtin = normalize_gtin(pair["left"]["gtin"])
            right_gtin = normalize_gtin(pair["right"]["gtin"])
            if not left_gtin or left_gtin != right_gtin:
                continue
            decision = match(pair["left"], pair["right"], 1.0)
            assert decision.linked is True and decision.confidence == 1.0, pair["why"]
            checked += 1
        assert checked >= 3, "the fixture set must exercise the identity rule"


class TestBlockingAndResolution:
    """Which pairs get asked about, and the transitive merge nobody scored."""

    def test_blocking_proposes_the_pairs_that_matter_without_comparing_everything(self) -> None:
        """A shared key means "worth scoring", never "matched"."""
        from ingest.er import candidate_pairs

        records = [
            _record("p-1", "00012345678905", "Trail Runner Shoe", "Cascade"),
            _record("p-2", "012345678905", "Zapatilla de trail para hombre", "Cascade"),
            _record("p-3", "", "Ceramic Burr Coffee Grinder", "Kettleworks"),
        ]
        pairs, dropped = candidate_pairs(records)
        assert (0, 1) in pairs, "the GTIN block must propose the identity pair"
        assert (0, 2) not in pairs and (1, 2) not in pairs, "no shared key, no comparison"
        assert dropped == []

    def test_an_over_large_block_is_dropped_and_named(self) -> None:
        """An invisible refusal is a bug report nobody files."""
        from ingest.er import candidate_pairs

        records = [_record(f"p-{i}", "", "Trail Runner Shoe", "Cascade") for i in range(10)]
        pairs, dropped = candidate_pairs(records, max_block_size=4)
        assert pairs == []
        assert dropped, "the dropped block keys must be reported"
        assert all(key.startswith("tok:") for key in dropped)

    def test_resolution_is_invariant_under_the_order_of_its_input(self) -> None:
        """A set's iteration order is not a ranking and must not decide the output."""
        from ingest.er import resolve

        records = [
            _record("p-1", "00012345678905", "Trail Runner Shoe", "Cascade"),
            _record("p-2", "012345678905", "Zapatilla de trail", "Cascade"),
            _record("p-3", "", "Ceramic Burr Coffee Grinder", "Kettleworks"),
            _record("p-4", "", "Coffee Grinder, Ceramic Burr", "Kettleworks"),
        ]
        forward = resolve(records, 0.86)
        backward = resolve(list(reversed(records)), 0.86)
        assert [(e.left_id, e.right_id, e.confidence) for e in forward.edges] == [
            (e.left_id, e.right_id, e.confidence) for e in backward.edges
        ]

    def test_edges_are_emitted_in_one_canonical_direction_only(self) -> None:
        """``link_same_as`` writes a directed edge; two calls would double every count."""
        from ingest.er import resolve

        report = resolve(
            [
                _record("p-2", "00012345678905", "Trail Runner Shoe", "Cascade"),
                _record("p-1", "012345678905", "Zapatilla de trail", "Cascade"),
            ],
            0.86,
        )
        assert len(report.edges) == 1
        assert report.edges[0].left_id < report.edges[0].right_id

    def test_a_record_is_never_linked_to_itself(self) -> None:
        """A self-loop is not a resolution result."""
        from ingest.er import resolve

        record = _record("p-1", "00012345678905", "Trail Runner Shoe", "Cascade")
        assert resolve([record, dict(record)], 0.86).edges == ()

    def test_a_transitive_path_cannot_merge_two_different_trade_items(self) -> None:
        """The defect a pairwise veto alone does not close.

        ``A`` and ``C`` carry two different valid GTINs, so ``match`` refuses them outright.
        But ``B`` has no GTIN and matches both, so ``A ~ B`` and ``B ~ C`` each clear the
        threshold and any traversal of those two edges reaches the merge the GTIN veto exists
        to forbid. The consistency constraint declines the second edge and says so.
        """
        from ingest.er import match, resolve

        a = _record("A", "00012345678905", "Trail Runner Shoe", "Cascade")
        b = _record("B", "", "Trail Runner Shoe", "Cascade")
        c = _record("C", "00000000000017", "Trail Runner Shoe", "Cascade")

        assert match(a, b, 0.86).linked is True
        assert match(b, c, 0.86).linked is True
        assert match(a, c, 0.86).linked is False, "the pairwise veto still fires"

        report = resolve([a, b, c], 0.86)
        linked = {(edge.left_id, edge.right_id) for edge in report.edges}
        assert ("A", "C") not in linked
        assert len(linked) == 1, f"only one of A~B and B~C may be admitted: {linked}"
        assert report.withheld, "the refused edge must be reported, not silently dropped"

    def test_the_report_names_what_it_did_not_do(self) -> None:
        from ingest.er import resolve

        report = resolve([_record("p-1", "", "Trail Runner Shoe", "Cascade")], 0.86)
        assert report.records == 1
        assert report.compared == 0
        assert report.edges == () and report.withheld == () and report.dropped_blocks == ()


class TestSameAsEdges:
    """The edges reach the graph, with provenance."""

    def test_link_resolved_writes_one_provenanced_edge_per_linked_pair(self) -> None:
        """Verified against a recording double, so the call shape is pinned without Neo4j.

        This is not a substitute for the datastore test below — it proves what ``er`` asks the
        graph to do, not that the graph did it.
        """
        from ingest.er import link_resolved
        from ingest.graph import Source

        calls: list[dict[str, Any]] = []

        class RecordingSession:
            def run(self, *args: Any, **kwargs: Any) -> Any:
                calls.append({"args": args, "kwargs": kwargs})
                raise AssertionError("link_resolved must go through link_same_as, not raw Cypher")

        source = Source(
            source_id="src-er-test",
            url="https://proxyshop.internal/er",
            content_hash="sha256:er",
            observed_at="2026-01-01T00:00:00+00:00",
            extractor_version="er-test",
            confidence=0.9,
            source_class="network",
        )
        written: list[tuple[str, str, float]] = []

        import ingest.er.linking as linking

        original = linking.link_same_as
        try:
            linking.link_same_as = lambda session, *, product_id, other_id, confidence, source: (
                written.append((product_id, other_id, confidence))
            )
            report = link_resolved(
                RecordingSession(),
                [
                    _record("p-1", "00012345678905", "Trail Runner Shoe", "Cascade"),
                    _record("p-2", "012345678905", "Zapatilla de trail", "Cascade"),
                ],
                source=source,
                threshold=0.86,
            )
        finally:
            linking.link_same_as = original

        assert calls == [], "no raw Cypher; the graph's own writer is the only path"
        assert written == [("p-1", "p-2", 1.0)]
        assert len(report.edges) == 1

    def test_the_source_class_the_graph_reserves_for_derived_facts_is_accepted(self) -> None:
        """SAME_AS is derived by this system, not observed on a storefront."""
        from ingest.graph import SOURCE_CLASSES

        assert "network" in SOURCE_CLASSES

    @pytest.mark.docker
    @pytest.mark.graph
    def test_a_resolved_pair_becomes_a_same_as_edge_in_the_graph(
        self, graph_schema_session: Any, graph_source: Any
    ) -> None:
        """End to end: two stores' products in, one provenanced ``SAME_AS`` edge out.

        The unit tests above prove ``er`` computes the right answer; this proves the answer
        lands in Neo4j as the edge DESIGN §Data models names, carrying its confidence, with
        no unsourced-edge violation behind it.
        """
        from ingest.er import link_resolved
        from ingest.graph import Product, provenance_violations, upsert_product

        left = _record("prod_er_a", "00012345678905", "Trail Runner Shoe", "Cascade")
        right = _record("prod_er_b", "012345678905", "Zapatilla de trail", "Cascade")
        for record in (left, right):
            upsert_product(
                graph_schema_session,
                Product(
                    product_id=record["product_id"],
                    canonical_name=record["canonical_name"],
                    brand=record["brand"],
                ),
                source=graph_source,
            )

        report = link_resolved(
            graph_schema_session, [left, right], source=graph_source, threshold=0.86
        )
        assert len(report.edges) == 1

        rows = graph_schema_session.run(
            "MATCH (a:Product)-[r:SAME_AS]->(b:Product) "
            "RETURN a.product_id AS a, b.product_id AS b, r.confidence AS confidence, "
            "r.source_id AS source_id"
        ).data()
        assert len(rows) == 1, f"exactly one directed edge, not a mirrored pair: {rows}"
        assert {rows[0]["a"], rows[0]["b"]} == {"prod_er_a", "prod_er_b"}
        assert rows[0]["confidence"] == pytest.approx(1.0)
        assert rows[0]["source_id"] == graph_source.source_id

        # Idempotent: resolving again must not add a second edge.
        link_resolved(graph_schema_session, [left, right], source=graph_source, threshold=0.86)
        again = graph_schema_session.run(
            "MATCH ()-[r:SAME_AS]->() RETURN count(r) AS total"
        ).single()["total"]
        assert again == 1

        assert provenance_violations(graph_schema_session) == []


class TestRouterIsMountedByTheFrozenEntrypoint:
    """``match`` has a call site in the running service, not only in a test."""

    def test_the_entrypoint_discovers_and_mounts_the_er_router(self) -> None:
        """``ingest.main.create_app`` globs ``<feature>/routes.py``; ER is one of them."""
        import importlib

        main = importlib.import_module("ingest.main")
        assert "ingest.er.routes" in main.discover_router_modules()
        assert "ingest.er.routes" in main.create_app().state.mounted_routers

    def test_the_match_endpoint_reaches_the_matcher(self) -> None:
        """One HTTP call, one real decision — the production path, not a re-implementation."""
        import importlib

        from fastapi.testclient import TestClient

        main = importlib.import_module("ingest.main")
        client = TestClient(main.create_app())
        response = client.post(
            "/er/match",
            json={
                "left": _record("p-1", "00012345678905", "Trail Runner Shoe", "Cascade"),
                "right": _record("p-2", "012345678905", "Zapatilla de trail", "Cascade"),
                "threshold": 0.95,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["linked"] is True
        assert body["confidence"] == 1.0
        assert body["method"] == "gtin"
        assert body["normalized_gtin"] == {
            "left": "00012345678905",
            "right": "00012345678905",
        }

    def test_the_endpoint_refuses_a_threshold_that_would_link_everything(self) -> None:
        """0.0 is rejected at the edge, so the caller is told which number was wrong."""
        import importlib

        from fastapi.testclient import TestClient

        main = importlib.import_module("ingest.main")
        client = TestClient(main.create_app())
        response = client.post(
            "/er/match",
            json={
                "left": _record("p-1", "", "Trail Runner Shoe", "Cascade"),
                "right": _record("p-2", "", "Trail Runner Shoe", "Cascade"),
                "threshold": 0.0,
            },
        )
        assert response.status_code == 422

    def test_the_resolve_endpoint_returns_the_edges_and_what_it_skipped(self) -> None:
        import importlib

        from fastapi.testclient import TestClient

        main = importlib.import_module("ingest.main")
        client = TestClient(main.create_app())
        response = client.post(
            "/er/resolve",
            json={
                "records": [
                    _record("p-1", "00012345678905", "Trail Runner Shoe", "Cascade"),
                    _record("p-2", "012345678905", "Zapatilla de trail", "Cascade"),
                    _record("p-3", "", "Ceramic Burr Coffee Grinder", "Kettleworks"),
                ],
                "threshold": 0.86,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["edges"] == [
            {"left_id": "p-1", "right_id": "p-2", "confidence": 1.0, "method": "gtin"}
        ]
        assert body["records"] == 3
        assert body["dropped_blocks"] == []

    def test_the_published_config_names_the_thresholds_a_caller_would_hard_code(self) -> None:
        import importlib

        from fastapi.testclient import TestClient
        from ingest.er import DEFAULT_MATCH_THRESHOLD, NAME_WEIGHT

        main = importlib.import_module("ingest.main")
        client = TestClient(main.create_app())
        body = client.get("/er/config").json()
        assert body["default_threshold"] == DEFAULT_MATCH_THRESHOLD
        assert body["name_weight"] == NAME_WEIGHT
        assert body["name_weight"] < body["default_threshold"], (
            "DESIGN: name agreement alone must not be able to clear the default threshold"
        )
