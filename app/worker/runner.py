import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from app import queue
from app.models import JobKind
from app.queue import ClaimedJob
from app.worker.handlers import WorkerContext, handle_generate_draft, handle_send_reply

log = logging.getLogger(__name__)

Handler = Callable[[WorkerContext, ClaimedJob], Awaitable[None]]


class Worker:
    """Runs one consumer loop per job kind, each with its own concurrency limit."""

    def __init__(self, ctx: WorkerContext):
        self.ctx = ctx
        self._stop = asyncio.Event()
        settings = ctx.settings
        self._consumers: list[tuple[JobKind, Handler, int]] = [
            (JobKind.GENERATE_DRAFT, handle_generate_draft, settings.generation_concurrency),
            (JobKind.SEND_REPLY, handle_send_reply, settings.send_concurrency),
        ]

    def stop(self) -> None:
        self._stop.set()

    async def run(self, shutdown_grace_seconds: float = 30.0) -> None:
        log.info(
            "worker started (stub_mode=%s, model=%s)",
            self.ctx.llm.stub_mode,
            self.ctx.settings.llm_model,
        )
        await asyncio.gather(
            *(
                self._consume(kind, handler, concurrency, shutdown_grace_seconds)
                for kind, handler, concurrency in self._consumers
            )
        )
        log.info("worker stopped")

    async def _consume(
        self, kind: JobKind, handler: Handler, concurrency: int, grace: float
    ) -> None:
        slots = asyncio.Semaphore(concurrency)
        in_flight: set[asyncio.Task] = set()
        poll = self.ctx.settings.worker_poll_interval_seconds
        lease = self.ctx.settings.job_lease_seconds

        while not self._stop.is_set():
            await slots.acquire()
            try:
                job = await queue.claim(self.ctx.sessionmaker, kind, lease)
            except Exception:
                log.exception("failed to claim %s job", kind)
                job = None
            if job is None:
                slots.release()
                await self._sleep(poll)
                continue
            task = asyncio.create_task(self._run_job(handler, job), name=f"{kind}-{job.id}")
            in_flight.add(task)
            task.add_done_callback(lambda t: (in_flight.discard(t), slots.release()))

        if in_flight:
            log.info("waiting up to %.0fs for %d %s job(s)", grace, len(in_flight), kind)
            # Unfinished jobs keep their lease and are picked up again after it expires.
            await asyncio.wait(in_flight, timeout=grace)

    async def _run_job(self, handler: Handler, job: ClaimedJob) -> None:
        try:
            await handler(self.ctx, job)
        except Exception:
            # Unexpected bug or DB outage: leave the job running; the lease expiry
            # makes it claimable again, and attempts bound the number of tries.
            log.exception("job %s (%s) crashed", job.id, job.kind)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
