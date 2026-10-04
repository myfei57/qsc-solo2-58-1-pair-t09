"""Execution engine that drives jobs forward along their frozen step ledger."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from portpower.job.store import (
    Job,
    JobAlreadyExists,
    JobState,
    JobStore,
    StepState,
)

# An action performs the step's side effects and returns the checkpoint result.
# Actions must be idempotent: an action may run more than once if the process
# died after the effect but before the checkpoint was written.
StepAction = Callable[[Any, Job], dict[str, Any]]

# A projection rebuilds the read-side conclusion from checkpoint results only.
# It must not touch runtime collaborators or perform any side effect.
StepProjection = Callable[[Job], dict[str, Any]]


@dataclass(frozen=True)
class StepDef:
    key: str
    action: StepAction


@dataclass(frozen=True)
class JobDefinition:
    """The step tailoring for one job kind.

    The version is frozen onto every job at open time. Changing, adding or
    removing steps here only affects newly opened jobs; existing ledgers keep
    their own step rows, so settled step results never change shape.
    """

    kind: str
    version: str
    steps: tuple[StepDef, ...]
    project: StepProjection

    def keys(self) -> tuple[str, ...]:
        return tuple(step.key for step in self.steps)

    def action_for(self, key: str) -> StepAction | None:
        for step in self.steps:
            if step.key == key:
                return step.action
        return None


class JobAlreadyComplete(RuntimeError):
    """Raised when execution is requested for a completed job."""


class JobDefinitionMissing(LookupError):
    """Raised for a job kind the process has no definition for."""


class JobKindMismatch(RuntimeError):
    """Raised when an existing job is replayed through a different kind."""


class JobEngine:
    def __init__(self, jobs: JobStore) -> None:
        self._jobs = jobs
        self._definitions: dict[str, JobDefinition] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        # Optional observer fired after a step checkpoint lands. Used to
        # simulate a crash between the checkpoint and the next step.
        self.after_checkpoint: Callable[[str, str, dict[str, Any]], None] | None = (
            None
        )

    def _lock_for(self, job_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(job_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[job_id] = lock
            return lock

    @property
    def jobs(self) -> JobStore:
        return self._jobs

    def register(self, definition: JobDefinition) -> None:
        if not definition.steps:
            raise ValueError(f"definition for {definition.kind} has no steps")
        keys = [step.key for step in definition.steps]
        if len(set(keys)) != len(keys):
            raise ValueError(f"duplicate step keys in definition {definition.kind}")
        self._definitions[definition.kind] = definition

    def open(
        self,
        job_id: str,
        kind: str,
        request: dict[str, Any],
    ) -> Job:
        definition = self._require_definition(kind)
        return self._jobs.open(
            job_id,
            kind,
            definition.version,
            request,
            definition.keys(),
        )

    def open_or_get(
        self,
        job_id: str,
        kind: str,
        request: dict[str, Any],
    ) -> tuple[Job, bool]:
        """Return ``(job, opened)``; a repeated id re-attaches to its job."""

        try:
            return self.open(job_id, kind, request), True
        except JobAlreadyExists:
            job = self._jobs.require(job_id)
            if job.kind != kind:
                raise JobKindMismatch(
                    f"job {job_id} is {job.kind}, not {kind}"
                ) from None
            return job, False

    def run(self, runtime: Any, job_id: str) -> Job:
        """Advance the job to the next unfinished step checkpoint by checkpoint.

        Steps recorded as done are never re-executed; execution resumes at the
        first pending or failed row. The job is marked complete only once every
        ledger row is done. Concurrent submissions for the same job serialise.
        """

        with self._lock_for(job_id):
            return self._run_locked(runtime, job_id)

    def _run_locked(self, runtime: Any, job_id: str) -> Job:
        job = self._jobs.require(job_id)
        if job.state is JobState.COMPLETE:
            return job
        definition = self._require_definition(job.kind)

        for record in job.steps:
            if record.state is StepState.DONE:
                continue
            action = self._action_for(definition, record.key)
            try:
                result = action(runtime, job)
            except Exception as exc:
                self._jobs.mark_step_failed(job_id, record.key, str(exc))
                raise
            self._jobs.mark_step_done(job_id, record.key, result)
            if self.after_checkpoint is not None:
                self.after_checkpoint(job_id, record.key, result)
            job = self._jobs.require(job_id)
        return self._jobs.complete(job_id)

    def replay(self, job_id: str) -> dict[str, Any]:
        """Rebuild the read-side conclusion without executing any step.

        Only completed jobs can be replayed: a half-finished chain has no final
        conclusion yet. The projection reads checkpoint results exclusively, so
        replay never creates quotas, bindings, meter flags or audit entries.
        """

        job = self._jobs.require(job_id)
        if job.state is not JobState.COMPLETE:
            raise JobNotReplayable(
                f"job {job_id} is {job.state.value}, only complete jobs replay"
            )
        definition = self._require_definition(job.kind)
        return definition.project(job)

    def _require_definition(self, kind: str) -> JobDefinition:
        definition = self._definitions.get(kind)
        if definition is None:
            raise JobDefinitionMissing(f"no step definition for job kind {kind!r}")
        return definition

    @staticmethod
    def _action_for(
        definition: JobDefinition, step_key: str
    ) -> StepAction:
        # Steps are frozen at open time; a definition trimmed afterwards may no
        # longer list a row the ledger still holds. Such a row is settled and
        # can never need executing, so reaching here means it was unfinished in
        # an incompatible definition.
        action = definition.action_for(step_key)
        if action is None:
            raise JobDefinitionMissing(
                f"definition {definition.kind} no longer provides step {step_key!r}"
            )
        return action


class JobNotReplayable(RuntimeError):
    """Raised when replay is asked for a job that never finished."""
