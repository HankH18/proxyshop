"""The sealed :class:`Envelope` value object and the written approval that activates one.

DESIGN §Interfaces pins the envelope's eight fields; ``contracts.Envelope`` pins their
types. What lives here is the part neither of those can express:

* an envelope **version** is a value, not a row you update — every field is immutable and
  every nested container is frozen (:mod:`merchant_svc.envelope.frozen`), so R9's "old
  versions never change" holds because there is nothing to change;
* ``activation`` is *lifecycle state*, not a term the merchant agreed to. It is therefore
  carried here but deliberately excluded from the approved-terms projection the digest is
  taken over (see :mod:`merchant_svc.envelope.digest`);
* an activated version carries the :class:`ApprovalArtifact` that authorized it. R6 asks for
  a *recorded* written approval, and a record that lives somewhere else is a record that can
  go missing while the envelope stays live — so it travels with the version it approves.

``approval`` is the one field the DESIGN Envelope does not have. :meth:`Envelope.to_contract`
projects it away, so what crosses a wire is exactly the pinned eight-field document.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

import contracts
from .frozen import FrozenDict, freeze, thaw
from pydantic import ValidationError

#: The seven fields a merchant actually agrees to when they approve an envelope.
TERM_FIELDS: Final[tuple[str, ...]] = (
    "store_id",
    "version",
    "floors",
    "max_discount_pct",
    "budget_cap",
    "pursue_clusters",
    "standing_commitments",
)

#: Every field of the DESIGN Envelope: the approved terms plus the lifecycle state.
ENVELOPE_FIELDS: Final[tuple[str, ...]] = TERM_FIELDS + ("activation",)

#: What :func:`~merchant_svc.envelope.versions.edit_envelope` will change. ``version`` is
#: derived and ``activation`` is a lifecycle transition; ``store_id`` names *whose* envelope
#: this is, and editing it would silently retarget an approval at another merchant's store.
EDITABLE_FIELDS: Final[tuple[str, ...]] = (
    "floors",
    "max_discount_pct",
    "budget_cap",
    "pursue_clusters",
    "standing_commitments",
)

SHADOW: Final[str] = contracts.EnvelopeActivation.shadow.value
ACTIVE: Final[str] = contracts.EnvelopeActivation.active.value
KILLED: Final[str] = contracts.EnvelopeActivation.killed.value

#: The version a freshly interviewed envelope gets. Versions start at 1 and only ever climb.
FIRST_VERSION: Final[int] = 1

#: ``from_obj(..., approval=KEEP)`` — carry whatever approval the input already recorded.
#: A distinct sentinel object, because ``None`` means "drop the approval" and both are asked
#: for by real callers.
KEEP: Final[Any] = object()


class EnvelopeError(Exception):
    """Base for every refusal this package makes."""


class EnvelopeInvalid(EnvelopeError, ValueError):
    """The data is not a DESIGN ``Envelope``."""


class EnvelopeEditRefused(EnvelopeError, ValueError):
    """The requested edit is not one an envelope edit may make."""


class ApprovalRejected(EnvelopeError, ValueError):
    """The written approval artifact is missing, incomplete, or bound to something else."""


def _utc_timestamp(raw: str) -> str:
    """``raw`` as an ISO-8601 UTC instant, or refuse.

    A naive timestamp is refused rather than assumed to be UTC: "approved at 09:00" is a
    different fact in Auckland and in Lisbon, and an approval record whose instant depends on
    where the reader is standing is not a record.
    """
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ApprovalRejected(f"the approval's timestamp is not ISO-8601: {raw!r}") from exc
    if moment.tzinfo is None:
        raise ApprovalRejected(
            f"the approval's timestamp carries no timezone: {raw!r}; an approval instant "
            "that depends on the reader's location is not a recorded instant"
        )
    return moment.astimezone(UTC).isoformat()


def _required_text(raw: Mapping[str, Any], key: str, complaint: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApprovalRejected(f"{complaint} ({key}={value!r})")
    return value.strip()


def _optional_text(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ApprovalRejected(f"the approval's {key} is present but empty")
    return value.strip()


@dataclass(frozen=True)
class ApprovalArtifact:
    """A merchant's recorded written approval of one envelope version.

    ``envelope_hash`` is what makes it *bound*: it is
    :func:`~merchant_svc.envelope.digest.approval_digest` of the terms the merchant read. An
    approval with no hash is a signature on a blank page, and one whose hash is another
    envelope's is a signature lifted off a different page — both are refused.
    """

    approver: str
    approved_at: str
    envelope_hash: str
    note: str | None = None
    document_ref: str | None = None

    #: The keys an approval artifact may carry. Unknown keys are refused rather than
    #: ignored: a field this code does not understand is a term nobody checked.
    FIELDS: ClassVar[tuple[str, ...]] = (
        "approver",
        "approved_at",
        "envelope_hash",
        "note",
        "document_ref",
    )

    def to_dict(self) -> dict[str, Any]:
        """The artifact as plain JSON-able data."""
        record: dict[str, Any] = {
            "approver": self.approver,
            "approved_at": self.approved_at,
            "envelope_hash": self.envelope_hash,
        }
        if self.note is not None:
            record["note"] = self.note
        if self.document_ref is not None:
            record["document_ref"] = self.document_ref
        return record

    @classmethod
    def parse(cls, raw: Any) -> ApprovalArtifact:
        """Read an approval artifact, or refuse and say which part is missing.

        Args:
            raw: the artifact as submitted — a mapping, or another
                :class:`ApprovalArtifact`.

        Returns:
            The parsed artifact, with ``approved_at`` normalized to UTC.

        Raises:
            ApprovalRejected: ``raw`` is absent, is not a mapping, names no approver, has no
                timestamp, has a timestamp with no timezone, carries no ``envelope_hash``, or
                carries a key this artifact has no meaning for.
        """
        if isinstance(raw, ApprovalArtifact):
            return raw
        if raw is None:
            raise ApprovalRejected(
                "no written approval artifact was recorded; an envelope is never activated "
                "on anybody's say-so alone (R6)"
            )
        if not isinstance(raw, Mapping):
            raise ApprovalRejected(
                f"a written approval artifact is a document, not a {type(raw).__name__}"
            )

        unknown = sorted(set(map(str, raw.keys())) - set(cls.FIELDS))
        if unknown:
            raise ApprovalRejected(
                f"the approval artifact carries fields nothing here checks: {unknown}"
            )

        approver = _required_text(raw, "approver", "the approval names no approver")
        stamp = _required_text(raw, "approved_at", "the approval is not dated")
        envelope_hash = _required_text(
            raw, "envelope_hash", "the approval is not bound to an envelope"
        )
        return cls(
            approver=approver,
            approved_at=_utc_timestamp(stamp),
            envelope_hash=envelope_hash,
            note=_optional_text(raw, "note"),
            document_ref=_optional_text(raw, "document_ref"),
        )


@dataclass(frozen=True)
class Envelope:
    """One immutable version of a store's sealed envelope.

    Build one with :meth:`from_obj`, which validates against ``contracts.Envelope``; the
    generated ``__init__`` is the low-level constructor the versioning functions use once the
    data is already known good.
    """

    store_id: str
    version: int
    floors: tuple[FrozenDict, ...]
    max_discount_pct: float
    budget_cap: float
    pursue_clusters: tuple[str, ...]
    standing_commitments: tuple[FrozenDict, ...]
    activation: str
    approval: ApprovalArtifact | None = None

    def to_dict(self) -> dict[str, Any]:
        """The envelope as plain JSON-able data, sharing nothing with this object.

        Includes ``approval`` — the recorded artifact is part of what this version *is*.
        Use :meth:`to_contract` for the pinned eight-field document that crosses a wire.
        """
        return {
            "store_id": self.store_id,
            "version": self.version,
            "floors": [thaw(floor) for floor in self.floors],
            "max_discount_pct": self.max_discount_pct,
            "budget_cap": self.budget_cap,
            "pursue_clusters": list(self.pursue_clusters),
            "standing_commitments": [thaw(claim) for claim in self.standing_commitments],
            "activation": self.activation,
            "approval": self.approval.to_dict() if self.approval is not None else None,
        }

    def to_contract(self) -> contracts.Envelope:
        """This version as the pinned DESIGN ``Envelope`` — the approval projected away."""
        document = self.to_dict()
        return contracts.Envelope.model_validate({key: document[key] for key in ENVELOPE_FIELDS})

    def with_activation(self, activation: str, approval: ApprovalArtifact | None) -> Envelope:
        """A new version-identical envelope in ``activation``. Never mutates ``self``."""
        return replace(self, activation=activation, approval=approval)

    @property
    def is_live(self) -> bool:
        """``True`` only when this version is *declared* active.

        Spelled as an equality against the one live value, never as "not killed": a version
        whose activation is missing, empty, misspelled or some future fourth state is not
        live, and a store whose envelope forgot a key must not bid.
        """
        return self.activation == ACTIVE

    @classmethod
    def from_obj(cls, raw: Any, *, approval: Any = KEEP) -> Envelope:
        """Coerce ``raw`` into a validated :class:`Envelope`.

        Args:
            raw: an :class:`Envelope`, anything exposing ``to_dict()`` (which is how an
                envelope built under the ``apps.merchant.svc.src`` import name is accepted by
                code that imported ``merchant_svc``), a ``contracts.Envelope``, or a mapping
                of the DESIGN fields.
            approval: :data:`KEEP` to carry ``raw``'s recorded approval through, ``None`` to
                drop it, or an artifact to attach.

        Returns:
            A validated envelope.

        Raises:
            EnvelopeInvalid: ``raw`` is not shaped like a DESIGN Envelope.
            ApprovalRejected: an approval came along that is not a usable artifact.
        """
        document = as_document(raw)

        if approval is KEEP:
            carried = document.get("approval")
            resolved = ApprovalArtifact.parse(carried) if carried is not None else None
        elif approval is None:
            resolved = None
        else:
            resolved = ApprovalArtifact.parse(approval)

        pinned = {key: document.get(key) for key in ENVELOPE_FIELDS}
        try:
            validated = contracts.Envelope.model_validate(pinned)
        except ValidationError as exc:
            raise EnvelopeInvalid(f"not a DESIGN Envelope: {exc}") from exc

        commitments = pinned["standing_commitments"]
        return cls(
            store_id=validated.store_id,
            version=validated.version,
            # Floors are re-projected from the validated model because ``EnvelopeFloor`` is a
            # closed two-field object: a submitted floor that omitted ``product_ref`` would
            # otherwise be stored without the key and read back as a *different* document.
            floors=tuple(
                FrozenDict({"product_ref": floor.product_ref, "min_price": float(floor.min_price)})
                for floor in validated.floors
            ),
            max_discount_pct=float(validated.max_discount_pct),
            budget_cap=float(validated.budget_cap),
            pursue_clusters=tuple(str(cluster) for cluster in validated.pursue_clusters),
            # Commitments keep the *submitted* shape rather than the validated dump: a Claim
            # has four optional fields, and materializing them as explicit nulls would change
            # a document that merely passed validation.
            standing_commitments=tuple(freeze(claim) for claim in (commitments or [])),
            activation=str(validated.activation.value),
            approval=resolved,
        )


def as_document(raw: Any) -> dict[str, Any]:
    """``raw`` as a plain mapping of envelope fields, whatever kind of envelope it is."""
    if isinstance(raw, Envelope):
        return raw.to_dict()
    if isinstance(raw, contracts.Envelope):
        return raw.model_dump(mode="json")
    to_dict = getattr(raw, "to_dict", None)
    if callable(to_dict):
        document = to_dict()
        if isinstance(document, Mapping):
            return {str(key): value for key, value in document.items()}
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    raise EnvelopeInvalid(f"an envelope is a document, not a {type(raw).__name__}")
