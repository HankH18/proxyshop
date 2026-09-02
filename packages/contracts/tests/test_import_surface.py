"""Two import paths, one set of objects.

`contracts` (the flat-src namespace, D42) and `packages.contracts` (the repo-root dotted path the
frozen suite uses) must resolve to the SAME classes. If they did not, `isinstance` would start
returning False across a module boundary for objects that are obviously the same thing, and the
failure would surface far from here as an inexplicable validation error in some other service.
"""

from __future__ import annotations

import contracts as flat
import packages.contracts as dotted


def test_both_import_paths_yield_the_same_module() -> None:
    assert dotted.__all__ == flat.__all__


def test_both_import_paths_yield_the_same_class_objects() -> None:
    for name in flat.PINNED_PROTOCOL_OBJECTS:
        assert getattr(dotted, name) is getattr(flat, name), (
            f"{name} is a different class depending on how it was imported"
        )


def test_a_model_built_through_one_path_is_an_instance_of_the_other_path_s_class() -> None:
    from packages.contracts.tests._fixtures_protocol import make_bid

    bid = dotted.Bid.model_validate(make_bid())
    assert isinstance(bid, flat.Bid)


def test_every_exported_name_actually_resolves() -> None:
    missing = [name for name in flat.__all__ if not hasattr(flat, name)]
    assert missing == [], f"__all__ names nothing resolves to: {missing}"


def test_the_dotted_path_forwards_names_outside_all() -> None:
    """Submodules and helpers stay reachable, so a consumer is not forced to know which path it
    came in through."""
    assert dotted.validate_ledger_payload is flat.validate_ledger_payload
    assert dotted.buyer_label is flat.buyer_label


def test_the_package_never_names_the_superseded_directory() -> None:
    """D1: the schema package is `packages/contracts`, and the older name must appear nowhere.

    The forbidden string is assembled rather than written out, so this test does not match itself
    and then report its own source as the violation.
    """
    import pathlib

    forbidden = "packages/" + "protocol"
    root = pathlib.Path(flat.__file__).resolve().parent.parent
    offenders = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or "node_modules" in path.parts:
            continue
        if path.suffix not in {".py", ".ts", ".json", ".md"}:
            continue
        if forbidden in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"the superseded path `{forbidden}` appears in: {offenders}"
