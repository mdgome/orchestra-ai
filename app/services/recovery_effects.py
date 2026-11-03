"""Recovery effects: what a RECOVERY decision does beyond the generic continue / retry / abort.

HumanGateService only knows this neutral module. A recovery *kind* (identified by the stable `reason_code` stored in
the approval's request_payload and target, never by message text) registers an effect object here; the gate service
dispatches by code and stays free of any Project dependency.
"""
from dataclasses import dataclass


class EffectRetry(Exception):
    """The effect found its precondition (e.g. a fingerprint proof) stale. The whole decision transaction has been
    rolled back; the gate service re-prepares outside any transaction and tries again."""


@dataclass(frozen=True)
class EffectResult:
    resume: bool            # create the RESUME execution request (the effect already handled any terminal change)


class RecoveryEffects:
    """reason_code -> effect object with `async prepare(approval, decision, option, task_id)` and
    `async apply(session, repo, approval, task, decision, prepared) -> EffectResult`."""

    def __init__(self, handlers: dict | None = None):
        self._handlers = dict(handlers or {})

    def register(self, reason_code: str, effect) -> None:
        self._handlers[reason_code] = effect

    def get(self, reason_code: str | None):
        return self._handlers.get(reason_code) if reason_code else None
