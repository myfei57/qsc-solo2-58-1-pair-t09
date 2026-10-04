"""Executes a step template against the ledger with breakpoint resume.

The runner walks the template for the job kind in order. A step that already
settled in the ledger is never executed again — its recorded result stands,
even if the template handed in has since been tailored. A step that failed
(or never ran) is executed and its outcome recorded, so an interrupted job
resumes from its breakpoint instead of starting over.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from portpower.job.ledger import JobLedger
from portpower.job.record import DONE, FAILED, JobRecord, StepRecord, timestamp
from portpower.job.template import StepContext, StepDefinition


@dataclass(frozen=True)
class RunOutcome:
    record: JobRecord
    executed: tuple[str, ...]
    skipped: tuple[str, ...]

    @property
    def resumed(self) -> bool:
        return bool(self.skipped)


class JobRunner:
    def __init__(self, ledger: JobLedger) -> None:
        self._ledger = ledger
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def run(
        self,
        record: JobRecord,
        template: list[StepDefinition],
    ) -> RunOutcome:
        """Walk ``template``, skipping steps already settled in the ledger."""

        with self._lock_for(record.job_id):
            record = self._ledger.require(record.job_id)
            executed: list[str] = []
            skipped: list[str] = []
            for definition in template:
                if record.settled(definition.name) is not None:
                    skipped.append(definition.name)
                    continue
                context = StepContext(record)
                try:
                    result = definition.action(context)
                except Exception as exc:
                    self._ledger.record_step(
                        record.job_id,
                        StepRecord(
                            name=definition.name,
                            status=FAILED,
                            error=str(exc),
                            at=timestamp(),
                        ),
                    )
                    self._ledger.mark(record.job_id, FAILED)
                    raise
                record = self._ledger.record_step(
                    record.job_id,
                    StepRecord(
                        name=definition.name,
                        status=DONE,
                        result=dict(result) if result else {},
                        at=timestamp(),
                    ),
                )
                executed.append(definition.name)
            record = self._ledger.mark(record.job_id, DONE)
            return RunOutcome(
                record=record,
                executed=tuple(executed),
                skipped=tuple(skipped),
            )

    def _lock_for(self, job_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(job_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[job_id] = lock
            return lock
