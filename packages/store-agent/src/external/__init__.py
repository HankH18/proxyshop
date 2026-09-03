"""The signed external door (D52, T-044) — where a Tier-2 seller's bid enters the system.

Five public names, and the split between them is the point of the module:

* `canonical_signing_bytes` and `payload_hash` are **re-exported from `contracts.signing`, not
  reimplemented here.** They are the same function objects, by identity. Two canonicalizers is
  two protocols: the moment a signer and a verifier derive their bytes from different code, a
  change to either one silently fails the door open, and nothing in either test suite would
  turn red. `contracts.signing` owns the single implementation and says so in its own module
  docstring; this package is the import path the frozen suite binds to, so the functions are
  *published* here and *defined* there.
* `sign_bid` is the seller-side half — HMAC-SHA256 over exactly those bytes, reproducible by
  anyone holding the contract and the key.
* `receive_bid` is the door itself.
* `NonceStore` is the replay memory it consumes nonces in.

**Admitted is not verified.** Everything this module does authenticates the *submitter*: that
the bid came from a registered signer, over a key that signer still holds, recently, once, and
before the auction closed. It says nothing whatever about whether the claims inside are true.
An accepted bid is enqueued marked `verified=False`, and R18's verification pipeline is what
decides the rest.
"""

from __future__ import annotations

# Re-exported by IDENTITY. Rebinding these names to wrappers — even wrappers that only forward
# — would break the one property that keeps the signer and the verifier on one protocol, which
# `packages/store-agent/tests/test_external_bids.py` asserts with `is`.
from contracts.signing import canonical_signing_bytes, payload_hash

from .door import receive_bid
from .nonces import NonceStore
from .signatures import sign_bid

#: Exactly the five names the frozen acceptance suite binds to. Everything else this package
#: grows — reason constants, the receipt dataclass, the submodules themselves — is private and
#: may be rearranged without a protocol change.
__all__ = [
    "NonceStore",
    "canonical_signing_bytes",
    "payload_hash",
    "receive_bid",
    "sign_bid",
]
