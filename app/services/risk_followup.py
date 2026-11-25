"""The follow-up gate of a finished or superseded risk request (design 7.5 items 6 (a), 10 and 11).

`open_followup_gate` is the ONE gate-creating path of the worker and of the `_resume_from_approval` handler: it runs inside the
caller's transaction (Task -> Project already locked) and creates
  * the existing EXECUTION_UNCERTAIN recovery gate for a defensive code (a corrupted link);
  * a new PROJECT_DIRTY gate at the CURRENT epoch for every other code (fields recomputed from the locked project row, never copied).
Both carry the four lineage keys, written once in the INSERT, so a repeat finds the successor (T9) and opens nothing. It never
updates an Approval row, never fails the Task and never cleans up steps, sessions or leases.

`RiskFollowUp` is the `follow_up(session, task, request, code)` callable of `ProcessRiskWorker`: it opens a gate only when the Task is
still `WAITING_APPROVAL` with no PENDING approval of ANY type and the request's approval has no lineage successor.
"""
from app.infra.workspace_fingerprint import is_current_version
from app.models.states import TaskState
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.project_repository import ProjectRepository
from app.services.approval_lineage import lineage_keys
from app.services.human_gate_service import HumanGateService
from app.services.process_risk_worker import DEFENSIVE_CODES
from app.validation import policies as P

SHOWN_CODE = {"LIVE_PROCESS_PRESENT": "OPERATOR_OVERRIDE_REFUSED_LIVE_PROCESS"}


async def open_followup_gate(gates: HumanGateService, session, task, origin, project, code: str, lineage_origin: str,
                             extra_detail: dict | None = None):
    """`origin` is the Approval the new gate follows. Returns the new Approval."""
    epoch = project.write_epoch if project is not None else None
    lineage = {**lineage_keys(origin, epoch, lineage_origin), "risk_request_code": SHOWN_CODE.get(code, code)}
    step_key = origin.checkpoint_step_key
    if code in DEFENSIVE_CODES or project is None or task.project_id != project.id:
        return await gates.open_recovery(session, task, None, reason="a risk request found an inconsistent link: " + code,
                                         checkpoint_step_key=step_key, target_extra=lineage,
                                         detail=extra_detail)
    return await gates.open_recovery(
        session, task, None, reason="the project workspace has unresolved changes from an earlier task",
        checkpoint_step_key=step_key, reason_code=P.RECOVERY_REASON_PROJECT_DIRTY, options=P.PROJECT_DIRTY_OPTIONS,
        target_extra={"project_id": str(project.id), "dirty_epoch": project.write_epoch,
                      "baseline_ref": (project.baseline_fingerprint or "")[-16:], **lineage},
        detail={"project": project.name, "baseline_version_ok": is_current_version(project.baseline_fingerprint),
                "previous_request": str(origin.id), "code": SHOWN_CODE.get(code, code), **(extra_detail or {})})


class RiskFollowUp:
    def __init__(self, gates: HumanGateService):
        self.gates = gates

    async def __call__(self, session, task, request, code: str) -> None:
        approvals = ApprovalRepository(session)
        if task.status != str(TaskState.WAITING_APPROVAL) or await approvals.pending_for_task(task.id):
            return
        if await approvals.successors_of(request.approval_id):
            return
        origin = await approvals.get(request.approval_id)
        if origin is None:
            return
        project = await ProjectRepository(session).get(request.project_id)
        await open_followup_gate(self.gates, session, task, origin, project, code, "WORKER")
