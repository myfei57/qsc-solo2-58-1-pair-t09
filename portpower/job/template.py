"""Step definitions: the ordered plan a job kind walks through."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from portpower.job.record import JobRecord


class StepContext:
    """Read-only view over the current record handed to each step action.

    Later steps may consult the settled results of earlier ones (for example
    to rebuild a transient handle after a resume) without re-running them.
    """

    def __init__(self, record: JobRecord) -> None:
        self._record = record

    @property
    def record(self) -> JobRecord:
        return self._record

    def settled(self, name: str) -> dict[str, object] | None:
        step = self._record.settled(name)
        if step is None:
            return None
        return dict(step.result)


StepAction = Callable[[StepContext], dict[str, object] | None]


@dataclass(frozen=True)
class StepDefinition:
    name: str
    action: StepAction
