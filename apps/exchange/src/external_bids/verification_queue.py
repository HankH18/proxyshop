"""Where an admitted external bid's verification work item goes (R8/R18).

The signed door refuses ``verification_queue_unavailable`` when nothing is bound — deliberately
and before the nonce is spent, so a misconfiguration cannot burn an honest submitter's one-shot
key. That means the keyring alone does not make the door usable: ``configure_external_bids``
takes both a ``keyring`` and a ``queue``, and until this module existed NEITHER had a production
caller, so a deployment that solved the secret-material problem would only have moved its
refusal one gate along.

WHERE IT LANDS AND WHY THAT DATASTORE. Redis, through
:func:`~proxyshop_support.redis_client.worker_redis` — this service's own datastore under D39,
already declared in ``apps/exchange/compose.yaml`` (``REDIS_URL``, ``depends_on: redis``, and a
``--redis`` readiness probe that refuses to start a worker it cannot isolate), and the same
place the auction state machine keeps auctions. So this adds no dependency, no configuration
key and nothing to any shipped artifact: a deployment that can run an auction can already queue
a work item.

**It now HAS a consumer, and the paragraph that stood here said it did not.**
:mod:`exchange.external_bids.draining` is it: an admitted submission's claims and its pitch
prose are decomposed, graded against the exchange's own catalogue snapshot and announced to
the ledger as ``claim_verified``, which is R8's "routed to claim extraction + verification
(R18)" performed rather than promised. It runs on the door's own request path, because that
is the only execution context this deployable has — no ``[project.scripts]``, no lifespan
hook, one ``uvicorn`` process — and :meth:`RedisVerificationQueue.pop` below is the ``LPOP``
that paragraph was waiting for. See :mod:`~.draining` for why a background worker and a new
served route are both closed, and for the four rules that stop a drain from becoming a
deletion.

**It refuses rather than truncates.** ``LTRIM`` to a ceiling would silently drop the oldest
admitted bids — work items for submissions whose nonce is already spent and which therefore
cannot be resubmitted identically. A full queue raises :class:`VerificationQueueFull` instead,
the door answers ``verification_queue_unavailable``, and the submission is refused with its
nonce intact. Fail closed, and never lose something already admitted.
"""

from __future__ import annotations

import json
import threading
from typing import Any

__all__ = [
    "MAX_QUEUED_WORK_ITEMS",
    "VERIFICATION_QUEUE_KEY",
    "RedisVerificationQueue",
    "VerificationQueueFull",
]

#: The Redis list admitted work items are appended to. ``WorkerRedis`` prefixes every key with
#: ``w{N}:`` (D39), so this is the name inside one worker's namespace, not the raw key.
VERIFICATION_QUEUE_KEY = "exchange:external-bid-verification"

#: How many work items may wait for a verifier before the door starts refusing.
#:
#: A bound on a channel that is open to an ADMITTED seller — it takes a registered signing key
#: to add to it — rather than to an anonymous caller, which is why it is generous. A work item
#: is one submission plus its envelope, a few kilobytes; ten thousand of them is a queue an
#: operator can still read, and a backlog that deep means no verifier is running, which is the
#: thing they need to be told about.
MAX_QUEUED_WORK_ITEMS = 10_000


class VerificationQueueFull(RuntimeError):
    """The backlog is at :data:`MAX_QUEUED_WORK_ITEMS`. The submission is refused, not dropped."""


class RedisVerificationQueue:
    """The ``queue`` seam of :func:`~exchange.external_bids.routes.configure_external_bids`.

    Duck-typed on purpose: the door resolves ``enqueue``/``put``/``submit``/… itself
    (``store_agent.external.door._resolve_enqueue``), so this class implements the one
    spelling and imports nothing from the store-agent package — the exchange does not depend on
    the seller's package and the image it ships does not contain it.

    The client is built on FIRST USE, never at construction. Binding this in the composition
    root runs on the request path, and ``worker_redis`` opens a socket and reads ``CONFIG GET
    databases`` to check its own isolation; doing that eagerly would turn a Redis that is
    briefly unreachable into a **503 on ``POST /auctions``** for a buyer whose auction needs no
    queue at all. Lazily, an unreachable Redis is exactly one thing: this door refusing
    ``verification_queue_unavailable``, with the nonce unspent.
    """

    def __init__(
        self,
        key: str = VERIFICATION_QUEUE_KEY,
        *,
        max_items: int = MAX_QUEUED_WORK_ITEMS,
        client: Any | None = None,
    ) -> None:
        self._key = key
        self._max_items = int(max_items)
        self._client = client
        self._lock = threading.Lock()

    def _redis(self) -> Any:
        client = self._client
        if client is not None:
            return client
        # Double-checked under the lock: two first submissions arriving together would
        # otherwise each build a client and each open a pool, and the loser's pool is leaked
        # for the life of the process.
        with self._lock:
            if self._client is None:
                from proxyshop_support.redis_client import worker_redis  # noqa: PLC0415

                self._client = worker_redis()
            return self._client

    def enqueue(self, item: Any) -> None:
        """Append one work item. Raises rather than returning, so the door can refuse.

        ``allow_nan=False`` for the reason the door's own body reader refuses non-finite
        numbers: a ``NaN`` written here is a queue entry no strict JSON client can read back,
        and the verifier that eventually drains this is such a client. The route already
        refuses those on the way in, so this is the second half of a rule rather than a new
        one — and it is the half that covers anything this exchange itself put in the item.
        """
        payload = json.dumps(item, allow_nan=False, sort_keys=True)
        client = self._redis()
        depth = int(client.llen(self._key))
        if depth >= self._max_items:
            raise VerificationQueueFull(
                f"{self._key} holds {depth} work items awaiting verification, at the ceiling of "
                f"{self._max_items}; this submission is refused rather than displacing one that "
                f"was already admitted"
            )
        # The depth read above is NOT atomic with this append, and the bound is therefore
        # approximate under concurrency: N simultaneous submissions can each see depth-1 and
        # each push. The excess is bounded by the number of concurrent admitted submissions,
        # which is a handful, and the alternative — a Lua script or a WATCH loop — buys
        # exactness on a ceiling that is a backlog alarm rather than a correctness boundary.
        client.rpush(self._key, payload)

    def pop(self) -> Any | None:
        """The oldest work item, removed, or ``None`` when the queue is empty.

        ``LPOP`` against the ``RPUSH`` :meth:`enqueue` writes, so the order is the order the
        exchange admitted them in — which matters because the ceiling refuses rather than
        truncates, so the head of this list is the submission that has been waiting longest
        and whose nonce has been spent for longest.

        Returns the item DECODED, because that is what it was enqueued as: the caller stored a
        JSON object and a caller reading back a string would have to re-implement half of
        :func:`json.loads`' failure handling to use it. A stored entry that will not parse is
        answered as ``None`` rather than raised — it is a row nothing in this tree could have
        written (:meth:`enqueue` refuses non-finite numbers on the way in) and dropping one
        such row is strictly better than a consumer that cannot get past it.
        """
        raw = self._redis().lpop(self._key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def depth(self) -> int:
        """How many work items are waiting. For an operator, and for this module's own gates."""
        return int(self._redis().llen(self._key))
