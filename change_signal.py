"""
change_signal.py

The "doorbell" for the Content Hub change feed: a Postgres LISTEN/NOTIFY bridge
that tells consumers *when* to pull ``GET /changes`` instead of them polling.

Why LISTEN/NOTIFY (see CONTENT_HUB_CONTRACT_PLAN.md): prod runs one container
with ``uvicorn --workers 3`` — three processes — and the rows are written by
EXTERNAL autoingest workers, not this app. Shared Postgres is the only channel
visible to every uvicorn worker AND the external writers. A DB trigger
(migration ``006``) emits ``pg_notify('filing_changes', …)`` on every 13F insert
or revision, so the writer needs zero code change and every worker is signalled.

Each worker runs one daemon thread holding a dedicated autocommit LISTEN
connection; it bridges notifications onto the asyncio loop via
``call_soon_threadsafe``. A per-worker in-process ``SignalBroker`` fans each
notification out to that worker's connected SSE clients.
"""

import asyncio
import select
import threading
from typing import Optional, Set

import psycopg2
import psycopg2.extensions

from config import logger
from database import _get_db_connection_params

CHANNEL = "filing_changes"

# select() timeout so the listen loop periodically re-checks the stop flag.
_POLL_TIMEOUT_SECONDS = 5
# Bounded per-subscriber queue; the signal is a nudge, so on backpressure we drop
# rather than block — the consumer re-pulls from its own cursor regardless.
_SUBSCRIBER_QUEUE_SIZE = 100


class SignalBroker:
    """Per-worker fan-out of change notifications to connected SSE clients."""

    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue] = set()
        self._latest: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def latest(self) -> Optional[str]:
        """Last notification payload seen by this worker, for ?replay_latest."""
        return self._latest

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish_threadsafe(self, payload: str) -> None:
        """Called from the LISTEN thread; hops onto the event loop to fan out."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._publish, payload)

    def _publish(self, payload: str) -> None:
        self._latest = payload
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop-on-backpressure: a missed nudge is harmless, the consumer
                # still pulls everything after its cursor.
                pass


class ListenThread(threading.Thread):
    """Daemon thread holding an autocommit ``LISTEN filing_changes`` connection."""

    def __init__(self, broker: SignalBroker) -> None:
        super().__init__(name="filing-changes-listen", daemon=True)
        self._broker = broker
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            conn = None
            try:
                conn = psycopg2.connect(**_get_db_connection_params())
                conn.set_isolation_level(
                    psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
                )
                with conn.cursor() as cur:
                    cur.execute(f"LISTEN {CHANNEL};")
                logger.info("change_signal: LISTEN %s established", CHANNEL)

                while not self._stop.is_set():
                    if select.select([conn], [], [], _POLL_TIMEOUT_SECONDS) == (
                        [],
                        [],
                        [],
                    ):
                        continue  # timeout — re-check the stop flag
                    conn.poll()
                    while conn.notifies:
                        notify = conn.notifies.pop(0)
                        self._broker.publish_threadsafe(notify.payload)
            except Exception as exc:  # noqa: BLE001 - keep the listener alive
                logger.warning(
                    "change_signal: LISTEN loop error (%s); reconnecting", exc
                )
                self._stop.wait(2)  # brief backoff before reconnect
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass


def start_signal(broker: SignalBroker) -> ListenThread:
    """Bind the running loop and start the LISTEN thread. Call from lifespan."""
    broker.bind_loop(asyncio.get_running_loop())
    thread = ListenThread(broker)
    thread.start()
    return thread
