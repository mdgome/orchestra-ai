import uuid

from fastapi import APIRouter, Depends, Query

from app.container import Container
from app.routers.deps import get_container, get_reports
from app.schemas.changes import ChangesOut, render_changes
from app.schemas.reports import (ArtifactListOut, ArtifactOut, ReportOut, render_artifact, render_artifact_list,
                                 render_report)
from app.services.report_service import ReportService

router = APIRouter(prefix="/tasks", tags=["reports"])


@router.get("/{task_id}/artifacts", response_model=ArtifactListOut)
async def list_artifacts(task_id: uuid.UUID, kind: str | None = Query(None), limit: int = Query(50, ge=1, le=100),
                         offset: int = Query(0, ge=0), reports: ReportService = Depends(get_reports)):
    """Stored step results (plan, design review, implementation, tests, code review), oldest first, paginated."""
    return render_artifact_list(await reports.artifact_list(task_id, kind, limit, offset))


@router.get("/{task_id}/artifacts/{artifact_id}", response_model=ArtifactOut)
async def read_artifact(task_id: uuid.UUID, artifact_id: uuid.UUID, reports: ReportService = Depends(get_reports)):
    return render_artifact(await reports.artifact_detail(task_id, artifact_id))


@router.get("/{task_id}/report", response_model=ReportOut)
async def read_report(task_id: uuid.UUID, reports: ReportService = Depends(get_reports)):
    """Deterministic completion report built only from stored facts (no LLM call)."""
    return render_report(await reports.report(task_id))


@router.get("/{task_id}/changes", response_model=ChangesOut)
async def read_changes(task_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                       path: str | None = Query(None, max_length=1024), verify_workspace: bool = Query(False),
                       c: Container = Depends(get_container)):
    """The workspace now compared with the baseline taken when the task took the write lock. A LIVE comparison, not a
    snapshot: see lock_consistency / workspace_consistency. `verify_workspace=true` measures twice."""
    return render_changes(await c.change_service().changes(task_id, limit=limit, offset=offset, path=path,
                                                           verify_workspace=verify_workspace))
