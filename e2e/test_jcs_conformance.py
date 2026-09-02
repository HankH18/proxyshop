"""RFC-8785 (JCS) conformance: two Python canonicalizers and the TypeScript twin, one answer.

Three independent implementations of RFC 8785 live in this repo and every one of them is on a
path where a disagreement is a security failure rather than a formatting nit:

===================================================  =========================================
``packages/contracts/src/signing.py``                D52 — the bytes a bid signature covers.
``packages/contracts/src/ts/signing.ts``             D52 — the same bytes, from Node.
``apps/trust/src/ledger/canonical.py``               D16 — the bytes the event chain hashes.
===================================================  =========================================

They agree byte for byte today. Nothing made them keep agreeing: until this file existed the
contracts suite exercised the Python and TypeScript signers, the trust suite exercised the
ledger canonicaliser, and **no test in the repo ran an input through more than one of them**.
The next edit to any one of them could have silently re-split the protocol — a bid signed by a
Node seller that the Python exchange rejects, or an event that signs and then cannot be
recorded — with the whole suite green. That is the hole this file closes.

Why three implementations and not one
-------------------------------------
Settled by orchestrator ruling on T-106, recorded here because this file is the artifact that
enforces it: **both Python implementations stay.** Unifying them means moving one of them, and
D16 says verbatim *"Nothing outside ``apps/trust/src/ledger/**`` (T-011) defines its own
hashing"* — the sentence that forbids the only placement a shared module could take without
amending D16. The ruling is that two implementations may stand *provided* a conformance gate
makes a divergence impossible to introduce quietly. This is that gate.

What "conformance" means here, precisely
----------------------------------------
For every input: **either all sides succeed with identical bytes, or all sides refuse.** The
render-versus-refuse split is the failure mode that actually happened — ``9007199254740993``
rendered on one side (silently coerced to ``…992``, so the signature covered a quantity nobody
wrote) and raised on the other — so a case that one implementation renders and another rejects
fails this gate even though neither produced *wrong* bytes.

Refusal is compared as a boolean, never as an exception class, and that is not fastidiousness:

* ``contracts.signing.CanonicalisationError`` and ``trust.ledger.canonical.CanonicalisationError``
  are unrelated classes that merely share a name;
* the TypeScript twin's is a third;
* and ``apps/trust/src/ledger`` no longer adds a fourth. It used to: the ledger is reachable
  under two dotted names and each one executed the package separately. **T-119 bound them**,
  with the ``_bind_submodules`` block T-014 introduced for ``packages/llm``, so — measured in
  this process — ``trust.ledger.canonical`` **is** ``apps.trust.src.ledger.canonical`` (``is``
  → ``True``, one ``sys.modules`` entry, one ``CanonicalisationError`` class), and a
  cross-spelling ``except CanonicalisationError`` now catches. The two *package* objects stay
  distinct on purpose (``trust.ledger is apps.trust.src.ledger`` → ``False``); what T-119
  shares is every submodule. That binding lives in ``apps/trust/src/ledger/__init__.py``,
  outside this file's scope;
  :func:`test_both_spellings_of_the_ledger_canonicaliser_agree` is what keeps it true — the
  two spellings must not drift back apart — without depending on class identity.

Three deliberate, out-of-scope differences, excluded by name
-----------------------------------------------------------
These are real differences between the two Python implementations, they are known, and none of
them can arrive over the wire as parsed JSON — ``json.loads`` produces only ``dict``, ``list``,
``str``, ``int``, ``float``, ``bool`` and ``None``. They are excluded explicitly, with the
reason, rather than being allowed to fail the gate or quietly left out of the corpus:

1. **``datetime``** — ``trust`` normalises it to RFC-3339 milliseconds (D16 requires the ledger
   to accept the type its own events carry); ``contracts`` refuses it. A signed submission is
   parsed JSON, so a ``datetime`` object cannot reach the signer.
2. **Arbitrary ``Sequence``** — ``trust`` accepts anything sequence-shaped (a ``range``, say);
   ``contracts`` accepts only ``list``/``tuple``. Again unreachable from a parsed payload.
3. **Maximum nesting depth** — both recurse, both eventually raise ``RecursionError``, and they
   do so at different depths because they use a different number of stack frames per level.
   Pinning the exact threshold would be a flaky assertion: the depth available depends on how
   much stack the *caller* has already consumed, which differs between a pytest run, a worker
   process and a request handler. What is asserted instead is the part that is not an artifact
   — agreement well inside both limits, and refusal parity well outside them
   (:func:`test_nesting_agrees_inside_the_limit_and_refuses_outside_it`).

:func:`test_the_excluded_differences_are_still_the_only_ones` pins 1 and 2 as *still true*, so
if a future edit converges them this file is told to drop the exclusion rather than carrying a
stale apology forever.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import random
import struct
import subprocess
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

import contracts.signing as contracts_signing
import pytest
import trust.ledger.canonical as trust_canonical

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The corpus T-010 and T-011 both already pin against, reused rather than re-derived.
CORPUS_PATH = REPO_ROOT / "packages" / "contracts" / "tests" / "canonicalization_corpus.json"

#: The TypeScript bridge. `vite-node`, not `node`: `signing.ts` imports `"./schemas.js"`, the
#: TypeScript-source spelling of a `.ts` file, which Node's own type stripping cannot resolve.
VITE_NODE = REPO_ROOT / "node_modules" / ".bin" / "vite-node"
TS_DRIVER = REPO_ROOT / "e2e" / "support" / "jcs" / "canonicalize_cases.ts"

#: Deep enough to exercise recursion in both implementations, far enough inside every stack
#: budget that the assertion is about JCS rather than about CPython's frame accounting.
SAFE_NESTING_DEPTH = 40

#: Comfortably past the default recursion limit, so BOTH implementations refuse and the
#: assertion is refusal *parity* rather than a pinned threshold.
OVERFLOW_NESTING_DEPTH = 5_000

#: Seeded, so a divergence is reproducible from the failure message alone.
RANDOM_SEED = 20260902
RANDOM_CASES = 1_200
RANDOM_MAX_DEPTH = 6


# ---------------------------------------------------------------------------------------
# the implementations under test
# ---------------------------------------------------------------------------------------


class Impl(NamedTuple):
    """One canonicalizer: a name for failure messages and the function to call."""

    name: str
    canonicalise: Callable[[Any], str]


PY_IMPLS: tuple[Impl, ...] = (
    Impl("contracts.signing", contracts_signing.canonical_json),
    Impl("trust.ledger.canonical", trust_canonical.canonical_json),
)


class Outcome(NamedTuple):
    """What one implementation did with one input.

    ``ok`` is the only field the gate compares across implementations. ``text`` is compared
    only when every side said ``ok``; ``detail`` exists to make a failure readable and is never
    asserted on — the three implementations raise three unrelated exception classes with three
    different messages, and requiring those to match would be asserting on prose.
    """

    ok: bool
    text: str
    detail: str


def _run(impl: Impl, value: Any) -> Outcome:
    """Canonicalise ``value``, converting any refusal into a comparable :class:`Outcome`.

    Deliberately catches ``Exception`` rather than either module's ``CanonicalisationError``:
    the two Python classes are unrelated objects (see the module docstring), so naming one
    would silently treat the other's refusal as a crash. ``RecursionError`` is a refusal here
    too — an input neither side can serialise is one neither side may serialise.
    """
    try:
        return Outcome(True, impl.canonicalise(value), "")
    except Exception as exc:  # noqa: BLE001 - a refusal is any failure to produce bytes
        return Outcome(False, "", f"{type(exc).__name__}: {exc}"[:200])


# ---------------------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------------------


class Case(NamedTuple):
    """One conformance case.

    Args:
        cid: unique, ASCII, and stable — it is the pytest test id and the key the TypeScript
            bridge answers on.
        klass: the input class, for the coverage assertion at the bottom of this file.
        value: the Python input.
        expected: the canonical form when it is known from *outside* this repo (RFC text or the
            committed corpus). ``None`` means the assertion is agreement, not a pinned answer.
        refuse: every implementation must refuse this input.
        wire: the value survives ``json.dumps`` → ``JSON.parse`` unchanged, so the TypeScript
            twin can be handed the same input. ``False`` excludes it from the TS comparison and
            every such exclusion is commented at the construction site.
    """

    cid: str
    klass: str
    value: Any
    expected: str | None = None
    refuse: bool = False
    wire: bool = True


def _corpus_cases() -> list[Case]:
    """The committed corpus, which the Python and TypeScript signer suites already pin."""
    entries = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    return [
        Case(f"corpus-{index:02d}", "corpus", entry["input"], expected=entry["expected"])
        for index, entry in enumerate(entries)
    ]


# RFC 8785 §3.2.4 ("UTF-8 Generation"), the specification's own worked example: the input
# below is the RFC's, and the expected bytes are its printed hex dump transcribed verbatim.
# This is the one case in the file whose answer comes from the standard rather than from
# anything in this repo, which is what makes it worth pinning to the byte.
RFC_3_2_4_INPUT = """
{
  "numbers": [333333333.33333329, 1E30, 4.50,
              2e-3, 0.000000000000000000000000001],
  "string": "\\u20ac$\\u000F\\u000aA'\\u0042\\u0022\\u005c\\\\\\"\\/",
  "literals": [null, true, false]
}
"""

RFC_3_2_4_EXPECTED_HEX = (
    "7b 22 6c 69 74 65 72 61 6c 73 22 3a 5b 6e 75 6c 6c 2c 74 72"
    " 75 65 2c 66 61 6c 73 65 5d 2c 22 6e 75 6d 62 65 72 73 22 3a"
    " 5b 33 33 33 33 33 33 33 33 33 2e 33 33 33 33 33 33 33 2c 31"
    " 65 2b 33 30 2c 34 2e 35 2c 30 2e 30 30 32 2c 31 65 2d 32 37"
    " 5d 2c 22 73 74 72 69 6e 67 22 3a 22 e2 82 ac 24 5c 75 30 30"
    " 30 66 5c 6e 41 27 42 5c 22 5c 5c 5c 5c 5c 22 2f 22 7d"
)

RFC_3_2_4_EXPECTED = bytes.fromhex(RFC_3_2_4_EXPECTED_HEX.replace(" ", "")).decode("utf-8")

# RFC 8785 §3.2.3 ("Sorting of Object Properties"), the specification's own example. The RFC
# prints the sorted result as "Carriage Return", "One", "Control", "Latin Small Letter O With
# Diaeresis", "Euro Sign", "Emoji: Grinning Face", "Hebrew Letter Dalet With Dagesh" — note
# that the emoji sorts BEFORE U+FB33, which is only true under UTF-16 code-unit ordering.
RFC_3_2_3_INPUT = {
    "€": "Euro Sign",
    "\r": "Carriage Return",
    "דּ": "Hebrew Letter Dalet With Dagesh",
    "1": "One",
    "\U0001f600": "Emoji: Grinning Face",
    "\u0080": "Control",
    "ö": "Latin Small Letter O With Diaeresis",
}

RFC_3_2_3_EXPECTED = (
    '{"\\r":"Carriage Return"'
    ',"1":"One"'
    ',"\u0080":"Control"'
    ',"ö":"Latin Small Letter O With Diaeresis"'
    ',"€":"Euro Sign"'
    ',"\U0001f600":"Emoji: Grinning Face"'
    ',"דּ":"Hebrew Letter Dalet With Dagesh"}'
)

#: Keys chosen so that UTF-16 code-unit order (RFC 8785 §3.2.3) is a DIFFERENT permutation
#: from both code-point order (Python's default `sorted`) and UTF-8 byte order. Everything
#: from U+0000 to U+D7FF sorts the same way under all three; the separation comes entirely
#: from the four keys at and above U+E000 versus the two astral ones, whose UTF-16 encodings
#: begin with a surrogate in D800-DBFF and therefore sort *below* U+E000.
ORDERING_KEYS = (
    "\r",
    "1",
    "\u0080",
    "ö",
    "€",
    "\ud7ff",
    "\U0001f600",
    "\U0010ffff",
    "\ue000",
    "דּ",
    "\uffff",
)


def _ordering_cases() -> list[Case]:
    return [
        Case(
            "order-all-eleven",
            "key_order",
            {key: index for index, key in enumerate(ORDERING_KEYS)},
        ),
        # U+FFFF is the largest BMP code point and still sorts ABOVE an astral character.
        Case("order-ffff-vs-emoji", "key_order", {"\uffff": 1, "\U0001f600": 2}),
        # Private-use area against the top of the astral planes: E000 > DBFF, so the PUA key
        # sorts second under UTF-16 and first under code point.
        Case("order-pua-vs-max", "key_order", {"\ue000": 1, "\U0010ffff": 2}),
        # Prefix ordering: a shorter key sorts before a longer one that extends it, and NUL is
        # a real code unit rather than a terminator.
        Case("order-prefixes", "key_order", {"a": 1, "a\u0000": 2, "ab": 3, "a\uffff": 4}),
        # Sorting is recursive, not top-level-only.
        Case(
            "order-nested",
            "key_order",
            {"z": {"\U0001f600": 1, "\uffff": 2}, "a": [{"b": 1, "é": 2}]},
        ),
        # Digits and ASCII punctuation, where "sorted like a dictionary" would differ.
        Case("order-ascii", "key_order", {"10": 1, "9": 2, "-": 3, "_": 4, "A": 5, "a": 6}),
    ]


def _number_cases() -> list[Case]:
    """ECMAScript ``Number::toString`` boundaries — where `repr` and the RFC part company."""
    values: list[tuple[str, Any]] = [
        ("zero", 0),
        ("negative-zero", -0.0),
        ("one", 1),
        ("minus-one", -1),
        ("half", 0.5),
        ("pi", 3.141592653589793),
        ("third", 1 / 3),
        # The exponential-notation switch points. Python's `repr` moves at 1e-4 and 1e16;
        # ECMAScript moves at 1e-7 and 1e21. Every value between is a rendering disagreement
        # waiting to happen.
        ("e-4", 1e-4),
        ("e-5", 1e-5),
        ("e-6", 1e-6),
        ("e-7", 1e-7),
        ("e-8", 1e-8),
        ("e15", 1e15),
        ("e16", 1e16),
        ("e20", 1e20),
        ("e21", 1e21),
        ("e22", 1e22),
        ("minus-e21", -1e21),
        # Doubles at the edges of the representable range.
        ("min-subnormal", 5e-324),
        ("max-double", 1.7976931348623157e308),
        ("min-normal", 2.2250738585072014e-308),
        ("neg-max-double", -1.7976931348623157e308),
        # Integers ABOVE 2**53 that are nevertheless exact doubles. A safe-integer bound
        # refuses every one of these; RFC 8785 §3.1 requires them to be accepted.
        ("two-53", 2**53),
        ("two-53-plus-two", 2**53 + 2),
        ("ten-16", 10**16),
        ("two-63", 2**63),
        ("two-64", 2**64),
        ("neg-two-64", -(2**64)),
        ("two-1023", 2**1023),
        # Floats whose shortest round-tripping form is not their decimal expansion.
        ("wide-float", 1.2345678901234568e20),
        ("rfc-333", 333333333.33333329),
        ("rfc-1e30", 1e30),
        ("rfc-2e-3", 2e-3),
        ("rfc-1e-27", 1e-27),
        ("float-integral", 89.0),
        ("float-trailing-zero", 4.50),
    ]
    return [Case(f"number-{name}", "numbers", {"n": value}) for name, value in values]


def _number_refusal_cases() -> list[Case]:
    """Integers that are not doubles, and the non-finite floats. All sides must refuse.

    ``wire=False`` on every one of them: JavaScript has no integer type, so ``JSON.parse``
    silently rounds ``9007199254740993`` to ``9007199254740992`` and ``NaN``/``Infinity`` are
    not JSON at all. The input therefore cannot reach ``canonicalJson`` intact and there is
    nothing for the TypeScript twin to agree or disagree about. The TypeScript side refuses
    these at its own door — ``bigint`` in ``assertDoubleRepresentable`` — which is
    ``packages/contracts/tests/signing.test.ts``'s business, not this file's.
    """
    values: list[tuple[str, Any]] = [
        # The historical bug, exactly: rendered on one side, raised on the other.
        ("two-53-plus-one", 2**53 + 1),
        ("two-53-plus-three", 2**53 + 3),
        ("ten-17-plus-one", 10**17 + 1),
        ("neg-two-53-plus-one", -(2**53) - 1),
        # No double at all: `float()` raises OverflowError rather than returning inf.
        ("ten-400", 10**400),
        ("neg-ten-400", -(10**400)),
        ("two-1024", 2**1024),
        ("nan", math.nan),
        ("inf", math.inf),
        ("neg-inf", -math.inf),
    ]
    return [
        Case(f"refuse-number-{name}", "number_refusal", {"n": value}, refuse=True, wire=False)
        for name, value in values
    ]


def _string_cases() -> list[Case]:
    values: list[tuple[str, str]] = [
        ("empty", ""),
        ("ascii", "plain"),
        ("escapes", 'tab\there\nnewline\\backslash"quote'),
        ("all-short-escapes", '\b\f\n\r\t"\\'),
        ("controls", "\x00\x01\x1f "),
        # U+007F (DEL) is NOT escaped by JCS: the rule is "below U+0020", not "not printable".
        ("del", "\x7f"),
        # U+0080-U+009F are C1 controls and are likewise emitted literally.
        ("c1-controls", "\x80\x9f"),
        # The solidus MUST NOT be escaped, though `\/` is legal JSON input.
        ("solidus", "a/b"),
        ("non-ascii", "café — “quoted”"),
        ("astral", "😀 grinning"),
        ("bom", "\ufeff"),
        # U+2028/U+2029 are line terminators in JavaScript but ordinary characters in JSON;
        # `JSON.stringify` leaves them literal and so must both Python implementations.
        ("js-line-separators", "\u2028\u2029"),
        ("surrogate-boundary", "\ud7ff\ue000"),
        ("long", "x" * 500),
        ("combining", "é vs é"),
    ]
    return [Case(f"string-{name}", "strings", {"s": value}) for name, value in values]


def _surrogate_refusal_cases() -> list[Case]:
    """RFC 8785 §3.2.2.2: a lone surrogate MUST terminate canonicalisation with an error.

    ``wire=True`` — these DO survive the trip: ``json.dumps`` escapes a lone surrogate as
    ``\\ud800`` and ``JSON.parse`` reconstructs it, so the TypeScript twin sees exactly the
    same unpaired code unit the Python implementations see. This was a genuine cross-language
    mismatch before T-010 (``JSON.stringify`` succeeded where Python raised), which is why the
    TypeScript side is included here rather than excused.
    """
    values: list[tuple[str, Any]] = [
        ("high-alone", {"s": "\ud800"}),
        ("low-alone", {"s": "\udfff"}),
        ("high-then-text", {"s": "a\ud83dz"}),
        ("reversed-pair", {"s": "\ude00\ud83d"}),
        ("in-key", {"\ud800": 1}),
        ("nested", {"a": ["ok", {"b": "\udc00"}]}),
    ]
    return [
        Case(f"refuse-surrogate-{name}", "surrogate_refusal", value, refuse=True)
        for name, value in values
    ]


def _structural_cases() -> list[Case]:
    values: list[tuple[str, Any]] = [
        ("empty-object", {}),
        ("empty-array", []),
        ("empty-nested", {"a": {}, "b": [], "c": [{}], "d": [[]]}),
        ("literals", {"a": None, "b": True, "c": False}),
        ("array-of-mixed", [1, "two", None, True, False, 4.5, {}, []]),
        ("nested", {"a": {"b": {"c": [1, [2, [3, {"d": 4}]]]}}}),
        # A tuple is not JSON, but both implementations accept it as an array and a caller can
        # reach the signer with one, so agreement here is worth pinning.
        ("tuple", {"t": (1, 2, 3)}),
        ("bool-not-int", {"a": True, "b": 1, "c": False, "d": 0}),
        ("wide-object", {f"k{index:03d}": index for index in reversed(range(100))}),
        ("root-is-array", [{"b": 1, "a": 2}, [3, 4]]),
        ("root-is-scalar", 42),
        ("root-is-null", None),
        ("root-is-string", "top level"),
    ]
    return [Case(f"struct-{name}", "structural", value) for name, value in values]


def _structural_refusal_cases() -> list[Case]:
    """Types JSON cannot carry. ``wire=False`` throughout — none is JSON-serialisable at all."""
    values: list[tuple[str, Any]] = [
        ("int-key", {1: "one"}),
        ("float-key", {"a": {2.5: 1}}),
        ("none-key", {None: 1}),
        ("bool-key", {True: 1}),
        ("decimal", {"a": Decimal("1.5")}),
        ("bytes", {"a": b"raw"}),
        ("set", {"a": {1, 2}}),
        ("object", {"a": object()}),
        ("complex", {"a": 1 + 2j}),
    ]
    return [
        Case(f"refuse-struct-{name}", "structural_refusal", value, refuse=True, wire=False)
        for name, value in values
    ]


CURATED_CASES: tuple[Case, ...] = tuple(
    [
        *_corpus_cases(),
        Case(
            "rfc-3-2-4-worked-example",
            "rfc_sample",
            json.loads(RFC_3_2_4_INPUT),
            expected=RFC_3_2_4_EXPECTED,
        ),
        Case(
            "rfc-3-2-3-property-sorting",
            "rfc_sample",
            RFC_3_2_3_INPUT,
            expected=RFC_3_2_3_EXPECTED,
        ),
        *_ordering_cases(),
        *_number_cases(),
        *_number_refusal_cases(),
        *_string_cases(),
        *_surrogate_refusal_cases(),
        *_structural_cases(),
        *_structural_refusal_cases(),
    ]
)


# ---------------------------------------------------------------------------------------
# the random differential
# ---------------------------------------------------------------------------------------

_STRING_ATOMS = (
    "",
    "a",
    "Z",
    "0",
    "_",
    "-",
    " ",
    "/",
    '"',
    "\\",
    "\n",
    "\t",
    "\x00",
    "\x1f",
    "\x7f",
    "\x80",
    "é",
    "€",
    "中",
    "😀",
    "\U0010ffff",
    "\ue000",
    "\uffff",
    "\ud7ff",
    "\u2028",
    "דּ",
)


def _random_string(rng: random.Random) -> str:
    """A short string built only from well-formed scalars — never a lone surrogate.

    Lone surrogates are covered deliberately and exhaustively by the curated refusal cases;
    letting them in here would make most random inputs a refusal and drown the rendering
    comparison that is the point of the differential.
    """
    return "".join(rng.choice(_STRING_ATOMS) for _ in range(rng.randint(0, 5)))


def _random_number(rng: random.Random) -> float | int:
    """A number every side can render: an exact-double integer, or any finite double."""
    kind = rng.random()
    if kind < 0.25:
        return rng.randint(-(2**53) + 1, 2**53 - 1)
    if kind < 0.35:
        # Exact doubles far above the safe-integer bound — the class a magnitude bound
        # wrongly refuses, and the class this whole gate exists to keep both sides accepting.
        return int(float(rng.randint(2**53, 2**70)))
    if kind < 0.55:
        return rng.uniform(-1e6, 1e6)
    if kind < 0.70:
        return rng.choice([1, -1]) * 10.0 ** rng.randint(-30, 30) * rng.random()
    while True:
        # Arbitrary bit patterns reach the subnormals and the ragged shortest-repr cases that
        # uniform sampling never produces.
        candidate: float = struct.unpack("<d", rng.randbytes(8))[0]
        if math.isfinite(candidate):
            return candidate


def _random_value(rng: random.Random, depth: int) -> Any:
    if depth >= RANDOM_MAX_DEPTH or rng.random() < 0.45:
        leaf = rng.random()
        if leaf < 0.4:
            return _random_number(rng)
        if leaf < 0.7:
            return _random_string(rng)
        if leaf < 0.8:
            return None
        return rng.random() < 0.5
    if rng.random() < 0.5:
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {_random_string(rng): _random_value(rng, depth + 1) for _ in range(rng.randint(0, 5))}


def _random_cases() -> list[Case]:
    rng = random.Random(RANDOM_SEED)
    return [
        Case(f"random-{index:04d}", "random", _random_value(rng, 0))
        for index in range(RANDOM_CASES)
    ]


RANDOM_CASE_LIST: tuple[Case, ...] = tuple(_random_cases())
ALL_CASES: tuple[Case, ...] = CURATED_CASES + RANDOM_CASE_LIST


# ---------------------------------------------------------------------------------------
# the TypeScript bridge
# ---------------------------------------------------------------------------------------


def _code_units(text: str) -> list[int]:
    """``text`` as UTF-16 code units — the unit JavaScript strings are made of.

    Astral characters become their surrogate pair and an *unpaired* surrogate passes through as
    itself, which is the whole point: RFC 8785 §3.2.2.2 requires a lone surrogate to be REFUSED,
    so it has to reach the TypeScript side intact to be refused there.
    """
    units: list[int] = []
    for character in text:
        point = ord(character)
        if point > 0xFFFF:
            point -= 0x10000
            units.append(0xD800 + (point >> 10))
            units.append(0xDC00 + (point & 0x3FF))
        else:
            units.append(point)
    return units


def _encode_for_ts(value: Any) -> Any:
    """Encode ``value`` for the bridge: every string as code units, everything else tagged.

    Nothing in the transport relies on ``JSON.parse`` decoding a ``\\uXXXX`` escape. See the
    driver's header for the measured Node v25.9.0 fault that makes that necessary — a mis-decoded
    object key would have the gate comparing two *different* inputs and blaming a canonicalizer.
    """
    if value is None:
        return ["z"]
    if isinstance(value, bool):
        return ["b", value]
    if isinstance(value, int | float):
        return ["n", value]
    if isinstance(value, str):
        return ["s", _code_units(value)]
    if isinstance(value, list | tuple):
        return ["a", [_encode_for_ts(item) for item in value]]
    if isinstance(value, dict):
        return ["o", [[_code_units(k), _encode_for_ts(v)] for k, v in value.items()]]
    raise TypeError(f"case value is not wire-representable: {type(value).__name__}")


def _same_json_value(left: Any, right: Any) -> bool:
    """Structural equality for two parsed-JSON trees, tolerant only of int-versus-float.

    Used to prove the TypeScript side canonicalised the value Python sent. Numbers are compared
    as doubles because JavaScript has no integer type — Python's ``6552588714018611`` comes back
    as the same double either way — while strings, keys, containers and literals must match
    exactly, which is what makes this check able to catch a mis-decoded key.
    """
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same_json_value(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same_json_value(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, int | float) and isinstance(right, int | float):
        try:
            return float(left) == float(right)
        except OverflowError:
            return left == right
    return type(left) is type(right) and bool(left == right)


@pytest.fixture(scope="module")
def ts_outcomes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Outcome]:
    """Every wire-representable case, canonicalised once by the TypeScript twin.

    One subprocess for the whole module: `vite-node` costs about a second to start and nothing
    per case, so paying it per test would make the gate slower than the suite it guards.

    A missing `node_modules` is a FAILURE, not a skip. `scripts/verify.sh` refuses to run at
    all without `node_modules/.bin/vitest`, so the dependency is already unconditional here,
    and a skipped conformance case is indistinguishable from a passing one in the metrics.

    Inputs are encoded (see :func:`_encode_for_ts`) and the driver echoes each rebuilt value
    back, which this fixture verifies before a single comparison is allowed to run. Both halves
    were paid for: sending the corpus as plain JSON made Node v25.9.0's `JSON.parse` hand the
    driver an object whose key was U+005C where the document spelled `\\u001f`, and the bridge
    duly reported a "divergence" in a canonicalizer that had done nothing wrong. A bridge that
    can give the two languages different inputs is not a conformance gate.
    """
    assert VITE_NODE.exists(), (
        f"{VITE_NODE} is missing — run `npm ci --prefer-offline`. The TypeScript signer is one "
        f"of the three implementations this gate compares; without it the gate is two thirds "
        f"of itself and would report green anyway."
    )
    assert TS_DRIVER.exists(), f"{TS_DRIVER} is missing"

    wire_cases = [case for case in ALL_CASES if case.wire]
    case_file = tmp_path_factory.mktemp("jcs") / "cases.ndjson"
    # Pure ASCII by construction: after `_encode_for_ts` every string is a list of integers, so
    # the file holds no non-ASCII character and no `\uXXXX` escape at all.
    case_file.write_text(
        "\n".join(
            json.dumps({"id": case.cid, "input": _encode_for_ts(case.value)}) for case in wire_cases
        ),
        encoding="utf-8",
    )

    # The case file travels in the environment, not in argv: `vite-node` inserts its own
    # arguments, so the driver's `argv[2]` is the driver's own path and an argv protocol would
    # have it read its own source as JSON.
    # T-122 sweep: deliberately no PYTHONPATH. The child is `vite-node` running the
    # TypeScript signer — there is no Python interpreter here for `.pkgroot` to serve — and
    # `{**os.environ, ...}` extends the inherited environment rather than replacing it.
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, repo-local binary
        [str(VITE_NODE), str(TS_DRIVER)],
        cwd=REPO_ROOT,
        env={**os.environ, "JCS_CASE_FILE": str(case_file)},
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, (
        f"the TypeScript bridge exited {completed.returncode}\n"
        f"stdout: {completed.stdout.decode('utf-8', 'replace')[-2000:]}\n"
        f"stderr: {completed.stderr.decode('utf-8', 'replace')[-4000:]}"
    )
    results = [
        json.loads(line) for line in completed.stdout.decode("utf-8").split("\n") if line != ""
    ]
    assert len(results) == len(wire_cases), (
        f"the bridge answered {len(results)} of {len(wire_cases)} cases"
    )

    by_id = {entry["id"]: entry for entry in results}
    assert len(by_id) == len(results), "the bridge answered the same case id twice"

    mismatched = [
        case.cid
        for case in wire_cases
        if not _same_json_value(
            json.loads(by_id[case.cid]["echo"]), json.loads(json.dumps(case.value))
        )
    ]
    assert not mismatched, (
        f"{len(mismatched)} case(s) reached the TypeScript side as a DIFFERENT value than "
        f"Python sent — {mismatched[:5]}. This is a JSON transport fault, not a "
        f"canonicalization disagreement: nothing downstream of it can be believed. Compare the "
        f"case file the fixture wrote against the driver's `echo` field before touching any "
        f"canonicalizer."
    )

    return {
        entry["id"]: Outcome(entry["ok"], entry.get("out", ""), entry.get("error", ""))
        for entry in results
    }


# ---------------------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------------------


def test_both_canonicalisers_are_the_ones_in_this_worktree() -> None:
    """The gate must be grading THIS tree's code, not another worktree's or a stale install.

    Both modules are reached by ordinary import: `e2e/` is in the root `testpaths` and both
    `contracts` and `trust` are `.pkgroot` namespaces on the root `pythonpath`, so there is no
    `importlib` path surgery anywhere in this file — which is the second half of T-106's
    acceptance criterion 2 and the reason the gate runs in the ordinary suite.
    """
    for module in (contracts_signing, trust_canonical):
        assert module.__file__ is not None, module.__name__
        resolved = Path(module.__file__).resolve()
        assert resolved.is_relative_to(REPO_ROOT), (
            f"{module.__name__} resolved to {resolved}, which is outside {REPO_ROOT}: this run "
            f"is grading a different worktree's canonicaliser"
        )

    assert Path(contracts_signing.__file__ or "").resolve() == (
        REPO_ROOT / "packages" / "contracts" / "src" / "signing.py"
    )
    assert Path(trust_canonical.__file__ or "").resolve() == (
        REPO_ROOT / "apps" / "trust" / "src" / "ledger" / "canonical.py"
    )


def test_the_corpus_is_the_committed_one() -> None:
    """An emptied or shrunken corpus must fail loudly rather than make the gate vacuous."""
    entries = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert len(entries) == 27, (
        f"canonicalization_corpus.json holds {len(entries)} cases, not the committed 27; both "
        f"signer suites pin this file too, so a change here is a protocol change"
    )
    assert all({"input", "expected"} <= set(entry) for entry in entries)


@pytest.mark.parametrize("case", CURATED_CASES, ids=[case.cid for case in CURATED_CASES])
def test_the_python_implementations_agree(case: Case) -> None:
    """Either every implementation renders identical bytes, or every one refuses.

    The render-versus-refuse split is graded first and separately, because it is the failure
    that actually shipped: one side rendering `9007199254740993` (coerced to `…992`) while the
    other raised meant a bid could be signed and then be unrecordable, and neither side emitted
    bytes it considered wrong.
    """
    outcomes = {impl.name: _run(impl, case.value) for impl in PY_IMPLS}

    accepted = {name for name, outcome in outcomes.items() if outcome.ok}
    refused = {name for name, outcome in outcomes.items() if not outcome.ok}
    assert not (accepted and refused), (
        f"{case.cid}: render/refuse split — {sorted(accepted)} produced bytes while "
        f"{sorted(refused)} refused. Details: "
        + "; ".join(
            f"{name}={outcome.detail}" for name, outcome in outcomes.items() if not outcome.ok
        )
    )

    if case.refuse:
        assert not accepted, f"{case.cid}: expected every implementation to refuse this input"
        return

    assert accepted, (
        f"{case.cid}: every implementation refused an input the gate expects to be "
        f"canonicalisable. Details: "
        + "; ".join(f"{name}={outcome.detail}" for name, outcome in outcomes.items())
    )

    rendered = {outcome.text for outcome in outcomes.values()}
    assert len(rendered) == 1, f"{case.cid}: the implementations disagree —\n" + "\n".join(
        f"  {name}: {outcome.text!r}" for name, outcome in outcomes.items()
    )

    if case.expected is not None:
        produced = next(iter(rendered))
        assert produced == case.expected, (
            f"{case.cid}: both implementations agree on {produced!r}, but the externally "
            f"pinned answer is {case.expected!r}. They are wrong together, which is the one "
            f"kind of divergence agreement cannot catch."
        )


#: The curated cases the TypeScript twin can be asked about at all. Filtered here rather than
#: skipped inside the test: a skipped case reads exactly like a passing one, and the 19
#: exclusions are a deliberate, commented property of the inputs (JavaScript has no integer type
#: and no way to spell a `Decimal`, a `set` or a non-string key) rather than a runtime condition.
TS_CASES: tuple[Case, ...] = tuple(case for case in CURATED_CASES if case.wire)


@pytest.mark.parametrize("case", TS_CASES, ids=[case.cid for case in TS_CASES])
def test_the_typescript_twin_agrees(case: Case, ts_outcomes: dict[str, Outcome]) -> None:
    """The same comparison across the language boundary, for every wire-representable case."""
    ts = ts_outcomes[case.cid]
    py = _run(PY_IMPLS[0], case.value)

    assert ts.ok == py.ok, (
        f"{case.cid}: render/refuse split across languages — TypeScript "
        f"{'rendered' if ts.ok else 'refused'} and Python "
        f"{'rendered' if py.ok else 'refused'}. "
        f"ts={ts.detail or ts.text!r} py={py.detail or py.text!r}"
    )
    if not ts.ok:
        return
    assert ts.text == py.text, (
        f"{case.cid}: the TypeScript twin and the Python signer disagree —\n"
        f"  typescript: {ts.text!r}\n"
        f"  python:     {py.text!r}"
    )
    if case.expected is not None:
        assert ts.text == case.expected, f"{case.cid}: TypeScript differs from the pinned answer"


def test_the_random_differential_finds_no_divergence() -> None:
    """A seeded random walk over JSON trees, driven through both Python implementations.

    The curated cases cover what someone thought to write down. This covers what nobody did:
    ragged shortest-repr doubles, subnormals, astral keys next to control characters, empty
    keys, and objects nested five deep. The seed is fixed so a divergence is reproducible from
    the case id alone.
    """
    divergences: list[str] = []
    for case in RANDOM_CASE_LIST:
        outcomes = [_run(impl, case.value) for impl in PY_IMPLS]
        if len({outcome.ok for outcome in outcomes}) != 1:
            divergences.append(
                f"{case.cid}: render/refuse split "
                + ", ".join(
                    f"{impl.name}={'ok' if outcome.ok else outcome.detail}"
                    for impl, outcome in zip(PY_IMPLS, outcomes, strict=True)
                )
            )
        elif outcomes[0].ok and len({outcome.text for outcome in outcomes}) != 1:
            divergences.append(
                f"{case.cid}: "
                + " != ".join(
                    f"{impl.name}={outcome.text!r}"
                    for impl, outcome in zip(PY_IMPLS, outcomes, strict=True)
                )
            )
    assert not divergences, (
        f"{len(divergences)} of {len(RANDOM_CASE_LIST)} random inputs canonicalise "
        f"differently (seed {RANDOM_SEED}):\n" + "\n".join(divergences[:10])
    )


def test_the_random_differential_crosses_the_language_boundary(
    ts_outcomes: dict[str, Outcome],
) -> None:
    """The same random corpus, through the TypeScript twin."""
    divergences: list[str] = []
    for case in RANDOM_CASE_LIST:
        ts = ts_outcomes[case.cid]
        py = _run(PY_IMPLS[0], case.value)
        if ts.ok != py.ok:
            divergences.append(f"{case.cid}: ts_ok={ts.ok} py_ok={py.ok} {ts.detail}{py.detail}")
        elif ts.ok and ts.text != py.text:
            divergences.append(f"{case.cid}: ts={ts.text!r} != py={py.text!r}")
    assert not divergences, (
        f"{len(divergences)} of {len(RANDOM_CASE_LIST)} random inputs canonicalise differently "
        f"in TypeScript (seed {RANDOM_SEED}):\n" + "\n".join(divergences[:10])
    )


# ---------------------------------------------------------------------------------------
# the properties the corpus alone does not prove
# ---------------------------------------------------------------------------------------


def test_key_order_is_utf16_code_units_and_nothing_else() -> None:
    """RFC 8785 §3.2.3, stated as the thing that distinguishes it from the two near-misses.

    Agreement between two implementations proves nothing if both are wrong the same way, and
    `sort_keys=True` is exactly the wrong way that is one keystroke from correct. So this
    asserts the produced order IS the UTF-16 order and is NOT either of the orders a plausible
    shortcut would produce — which is only observable with non-BMP keys, because everything
    below U+D800 sorts identically under all three.
    """
    value = {key: index for index, key in enumerate(ORDERING_KEYS)}

    utf16_order = sorted(ORDERING_KEYS, key=lambda key: key.encode("utf-16-be"))
    code_point_order = sorted(ORDERING_KEYS)
    utf8_order = sorted(ORDERING_KEYS, key=lambda key: key.encode("utf-8"))

    assert utf16_order != code_point_order, (
        "the ordering fixture no longer separates UTF-16 order from code-point order, so this "
        "test proves nothing about ordering; add a non-BMP key back to ORDERING_KEYS"
    )
    assert utf16_order != utf8_order, "the fixture no longer separates UTF-16 from UTF-8 order"

    for impl in PY_IMPLS:
        rendered = impl.canonicalise(value)
        produced = [ORDERING_KEYS[position] for position in _key_positions(rendered, value)]
        assert produced == utf16_order, (
            f"{impl.name} ordered object members as "
            f"{[hex(ord(key[0])) for key in produced]}, not the RFC's UTF-16 code-unit order "
            f"{[hex(ord(key[0])) for key in utf16_order]}"
        )
        assert produced != code_point_order, f"{impl.name} is sorting by code point"


def _key_positions(rendered: str, value: dict[str, int]) -> list[int]:
    """Read the member order back out of canonical output, using each key's unique value."""
    payload = json.loads(rendered)
    assert payload == value
    # `json.loads` preserves document order in the resulting dict, which is the member order.
    return list(payload.values())


def test_nesting_agrees_inside_the_limit_and_refuses_outside_it() -> None:
    """Excluded difference 3, handled without pinning a stack-dependent threshold.

    Inside the limit the implementations must agree; far outside it they must BOTH refuse. The
    exact depth at which each starts refusing differs (they use a different number of frames
    per level) and is not a JCS property, so it is deliberately not asserted.
    """
    shallow: Any = "leaf"
    for _ in range(SAFE_NESTING_DEPTH):
        shallow = {"a": [shallow]}
    outcomes = [_run(impl, shallow) for impl in PY_IMPLS]
    assert all(outcome.ok for outcome in outcomes), (
        f"depth {SAFE_NESTING_DEPTH} is supposed to be inside every implementation's limit: "
        + "; ".join(
            f"{impl.name}={outcome.detail}"
            for impl, outcome in zip(PY_IMPLS, outcomes, strict=True)
        )
    )
    assert len({outcome.text for outcome in outcomes}) == 1

    deep: Any = "leaf"
    for _ in range(OVERFLOW_NESTING_DEPTH):
        deep = [deep]
    deep_outcomes = [_run(impl, deep) for impl in PY_IMPLS]
    assert not any(outcome.ok for outcome in deep_outcomes), (
        f"depth {OVERFLOW_NESTING_DEPTH} was canonicalised by "
        f"{[impl.name for impl, outcome in zip(PY_IMPLS, deep_outcomes, strict=True) if outcome.ok]}: "
        f"refusal parity at the extreme is what stands in for a pinned depth limit"
    )


def test_the_excluded_differences_are_still_the_only_ones() -> None:
    """Excluded differences 1 and 2, pinned as STILL TRUE so the exclusion cannot go stale.

    Neither can arrive over the wire: `json.loads` yields only `dict`, `list`, `str`, `int`,
    `float`, `bool` and `None`, so no parsed payload holds a `datetime` or a `range`. If a
    future edit converges these, this test goes red and the module docstring's exclusion list
    should shrink — which is the point of asserting it rather than leaving a comment.
    """
    import datetime as dt

    moment = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    assert trust_canonical.canonical_json(moment) == '"2026-01-01T00:00:00.000Z"', (
        "the ledger no longer normalises a datetime; D16 requires it to accept the type its "
        "own events carry, so this is a change to the ledger's contract"
    )
    assert not _run(PY_IMPLS[0], moment).ok, (
        "contracts.signing now accepts a datetime — excluded difference 1 is gone and the "
        "module docstring should say so"
    )

    assert trust_canonical.canonical_json(range(3)) == "[0,1,2]"
    assert not _run(PY_IMPLS[0], range(3)).ok, (
        "contracts.signing now accepts an arbitrary Sequence — excluded difference 2 is gone"
    )

    assert set(json.loads('{"a":[1,2.5,"s",true,null]}')) == {"a"}


def test_both_spellings_of_the_ledger_canonicaliser_agree() -> None:
    """`trust.ledger.canonical` and `apps.trust.src.ledger.canonical` must not drift.

    T-119 did what T-106 acceptance criterion 3 asked: `apps/trust/src/ledger/__init__.py` now
    binds the two spellings with the `_bind_submodules` block T-014 introduced for
    `packages/llm`, so both names resolve to ONE module object. Measured in this process:
    `trust.ledger.canonical is apps.trust.src.ledger.canonical` → `True`, one `sys.modules`
    entry, the same `canonical_json` function object.

    That makes the comparison below **trivially satisfied by construction** — a module cannot
    disagree with itself — and the test is KEPT anyway, deliberately, because it is the
    regression guard for exactly that binding. Remove the binding, or let a stale
    `__pycache__`, a half-applied edit, or a second physical file on one of the paths
    reintroduce a second copy, and the two spellings are two module objects again, free to
    canonicalise differently, with nothing else in the suite noticing. That is why the guard
    is cheap and permanent: the frozen acceptance suite reaches the ledger by the `apps.`
    spelling while every member package reaches it by the `trust.` spelling, so a divergence
    would split D16's single source of hashing truth in half. The `__file__` assertion is part
    of the same guard — it fails if the `apps.` spelling ever resolves to a different file.
    """
    other = importlib.import_module("apps.trust.src.ledger.canonical")
    assert Path(other.__file__ or "").resolve() == (
        REPO_ROOT / "apps" / "trust" / "src" / "ledger" / "canonical.py"
    )
    for case in CURATED_CASES:
        first = _run(PY_IMPLS[1], case.value)
        second = _run(Impl("apps.trust.src.ledger.canonical", other.canonical_json), case.value)
        assert (first.ok, first.text) == (second.ok, second.text), (
            f"{case.cid}: the two import spellings of the ledger canonicaliser disagree"
        )


def test_the_gate_covers_every_input_class_it_claims_to() -> None:
    """A conformance suite that quietly loses a class of input is worse than none.

    Each floor is the size of the class as built above, so deleting cases is a red test rather
    than a smaller green one.
    """
    counts: dict[str, int] = {}
    for case in ALL_CASES:
        counts[case.klass] = counts.get(case.klass, 0) + 1

    floors = {
        "corpus": 27,
        "rfc_sample": 2,
        "key_order": 6,
        "numbers": 36,
        "number_refusal": 10,
        "strings": 15,
        "surrogate_refusal": 6,
        "structural": 13,
        "structural_refusal": 9,
        "random": RANDOM_CASES,
    }
    assert counts == floors, f"input-class coverage changed: {counts} != {floors}"

    ids = [case.cid for case in ALL_CASES]
    assert len(set(ids)) == len(ids), "duplicate case id"
    assert all(case.cid.isascii() for case in ALL_CASES)

    wire = sum(1 for case in ALL_CASES if case.wire)
    assert wire == len(ALL_CASES) - 19, (
        "the set of cases excluded from the TypeScript comparison changed; every exclusion "
        "must be one of the 10 non-double numbers or the 9 non-JSON types, each commented at "
        "its construction site"
    )
