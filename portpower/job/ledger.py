"""Durable job ledger: one record per job, settled steps are immutable."""

from __future__ import annotations

from portpower.job.errors import (
    JobKindMismatch,
    JobNotFound,
    JobRequestMismatch,
    StepAlreadySettled,
)
from portpower.job.record import DONE, FAILED, RUNNING, JobRecord, StepRecord, timestamp
from portpower.store.kvstore import RecordStore

PREFIX = "job."


def job_key(job_id: str) -> str:
    return PREFIX + job_id


class JobLedger:
    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def ensure(
        self,
        kind: str,
        job_id: str,
        request: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> tuple[JobRecord, bool]:
        """Return the record for ``job_id``, creating it on first sight.

        A pre-existing record must carry the same kind and request payload;
        settled steps were produced by that payload and cannot be reinterpreted
        against a different one.
        """

        existing = self.load(job_id)
        if existing is not None:
            if existing.kind != kind:
                raise JobKindMismatch(
                    f"job {job_id} is a {existing.kind} job, not {kind}"
                )
            if existing.request != request:
                raise JobRequestMismatch(
                    f"job {job_id} was started with a different request"
                )
            return existing, False
        now = timestamp()
        record = JobRecord(
            job_id=job_id,
            kind=kind,
            status=RUNNING,
            request=dict(request),
            context=dict(context) if context else {},
            created_at=now,
            updated_at=now,
        )
        self._store.put_json(job_key(job_id), record.as_dict())
        return record, True

    def load(self, job_id: str) -> JobRecord | None:
        return JobRecord.from_dict(self._store.get_json(job_key(job_id)))

    def require(self, job_id: str) -> JobRecord:
        record = self.load(job_id)
        if record is None:
            raise JobNotFound(f"no job record for {job_id}")
        return record

    def record_step(self, job_id: str, step: StepRecord) -> JobRecord:
        """Append a step outcome, refusing to touch settled steps.

        A failed step may be retried: the new outcome replaces the old one and
        the attempt counter advances. A settled step is final — neither a
        re-run nor a tailored step definition may overwrite it.
        """

        def mutate(document: object) -> tuple[object, JobRecord]:
            record = self._parse(document, job_id)
            steps = list(record.steps)
            for index, existing in enumerate(steps):
                if existing.name != step.name:
                    continue
                if existing.settled:
                    raise StepAlreadySettled(
                        f"step {step.name} of job {job_id} already settled"
                    )
                steps[index] = StepRecord(
                    name=step.name,
                    status=step.status,
                    result=step.result,
                    error=step.error,
                    attempt=existing.attempt + 1,
                    at=step.at,
                )
                break
            else:
                steps.append(step)
            updated = self._replace(record, steps=tuple(steps))
            return updated.as_dict(), updated

        return self._store.update_json(job_key(job_id), mutate)

    def mark(self, job_id: str, status: str) -> JobRecord:
        def mutate(document: object) -> tuple[object, JobRecord]:
            record = self._parse(document, job_id)
            updated = self._replace(record, status=status)
            return updated.as_dict(), updated

        return self._store.update_json(job_key(job_id), mutate)

    def all(self) -> list[JobRecord]:
        from portpower.store.query import keys_with_prefix

        records: list[JobRecord] = []
        for key in keys_with_prefix(self._store, PREFIX):
            record = JobRecord.from_dict(self._store.get_json(key))
            if record is not None:
                records.append(record)
        records.sort(key=lambda record: (record.created_at, record.job_id))
        return records

    @staticmethod
    def _parse(document: object, job_id: str) -> JobRecord:
        record = JobRecord.from_dict(document)
        if record is None:
            raise JobNotFound(f"no job record for {job_id}")
        return record

    @staticmethod
    def _replace(record: JobRecord, **changes: object) -> JobRecord:
        from dataclasses import replace

        return replace(record, updated_at=timestamp(), **changes)
