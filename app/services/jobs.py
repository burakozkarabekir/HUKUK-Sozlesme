"""Background job runner for impact analyses (one per uvicorn worker).

Jobs live in `analysis_jobs`. Every worker polls the table and *claims* a queued
job with an atomic UPDATE, so a job runs exactly once even with several
workers. A running job writes a heartbeat; if a worker dies, another worker
re-queues the job after JOB_STALE_SECONDS (at most MAX_ATTEMPTS times).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.db import connect, utcnow_iso
from app.llm.client import LLMClient
from app.locks import new_owner_id
from app.services.impact import run_job

logger = logging.getLogger(__name__)

JOB_STALE_SECONDS = 300
HEARTBEAT_SECONDS = 20
MAX_ATTEMPTS = 2


class JobRunner:
    def __init__(self, settings: Settings, llm: LLMClient) -> None:
        self.settings = settings
        self.llm = llm
        self.worker_id = new_owner_id()
        self._loop_task: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._wakeup = asyncio.Event()

    # --- lifecycle ---
    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop(), name="impact-job-runner")

    def wake(self) -> None:
        """Called after a job is queued in this process: claim it without waiting for the poll."""
        self._wakeup.set()

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        for task in self._running.values():
            task.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._requeue_own()

    # --- polling ---
    async def _loop(self) -> None:
        while True:
            try:
                self._recover_stale()
                while len(self._running) < self.settings.IMPACT_MAX_PARALLEL_JOBS:
                    job_id = self._claim()
                    if job_id is None:
                        break
                    self._running[job_id] = asyncio.create_task(self._execute(job_id), name=f"impact-{job_id}")
            except Exception:
                logger.exception("job_runner_poll_failed")
            self._wakeup.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=self.settings.JOB_POLL_SECONDS)

    def _claim(self) -> str | None:
        conn = connect(self.settings.db_path)
        try:
            now = utcnow_iso()
            row = conn.execute(
                "UPDATE analysis_jobs SET status = 'running', worker_id = ?, started_at = ?, heartbeat_at = ?,"
                " attempts = attempts + 1"
                " WHERE id = (SELECT id FROM analysis_jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1)"
                " AND status = 'queued' RETURNING id",
                (self.worker_id, now, now),
            ).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()

    def _recover_stale(self) -> None:
        limit = (datetime.now(UTC) - timedelta(seconds=JOB_STALE_SECONDS)).isoformat(timespec="seconds")
        conn = connect(self.settings.db_path)
        try:
            stale = conn.execute(
                "SELECT id, attempts FROM analysis_jobs WHERE status = 'running' AND heartbeat_at < ?", (limit,)
            ).fetchall()
            for row in stale:
                if row["id"] in self._running:
                    continue
                if row["attempts"] >= MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE analysis_jobs SET status = 'failed', finished_at = ?, error_message = ? WHERE id = ?",
                        (utcnow_iso(), "Analiz tamamlanamadı (sunucu yeniden başlatıldı).", row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE analysis_jobs SET status = 'queued', worker_id = NULL WHERE id = ?", (row["id"],)
                    )
                logger.warning("impact_job_stale", extra={"job_id": row["id"], "attempts": row["attempts"]})
        finally:
            conn.close()

    def _requeue_own(self) -> None:
        """Graceful shutdown: give unfinished jobs back to the queue for another worker."""
        conn = connect(self.settings.db_path)
        try:
            conn.execute(
                "UPDATE analysis_jobs SET status = 'queued', worker_id = NULL, attempts = MAX(attempts - 1, 0)"
                " WHERE worker_id = ? AND status = 'running'",
                (self.worker_id,),
            )
        finally:
            conn.close()

    # --- execution ---
    async def _heartbeat(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            conn = connect(self.settings.db_path)
            try:
                conn.execute("UPDATE analysis_jobs SET heartbeat_at = ? WHERE id = ?", (utcnow_iso(), job_id))
            finally:
                conn.close()

    async def _execute(self, job_id: str) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(job_id))
        try:
            await run_job(job_id, self.llm, self.settings)
        finally:
            heartbeat.cancel()
            self._running.pop(job_id, None)
            self._wakeup.set()
