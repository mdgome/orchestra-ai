import uuid

from fastapi import APIRouter, Depends, Query, status

from app.container import Container
from app.routers.deps import get_container
from app.schemas.api import ProjectCreate, ProjectOut, ProjectPatch, TaskCreate, TaskOut

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, c: Container = Depends(get_container)):
    """Registers an existing local workspace, or clones a repository into the managed clone root."""
    return await c.projects.create_project(name=body.name, workspace_path=body.workspace_path,
                                           repository_url=body.repository_url, test_policy=body.test_policy)


@router.get("", response_model=list[ProjectOut])
async def list_projects(status_filter: str | None = Query(None, alias="status"), limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0), c: Container = Depends(get_container)):
    return await c.projects.list_projects(status_filter, limit, offset)


@router.get("/{project_id}", response_model=ProjectOut)
async def read_project(project_id: uuid.UUID, c: Container = Depends(get_container)):
    return await c.projects.get_project(project_id)


@router.patch("/{project_id}", response_model=ProjectOut)
async def patch_project(project_id: uuid.UUID, body: ProjectPatch, c: Container = Depends(get_container)):
    # only the fields the client actually sent: an omitted field is kept, an explicit null is meaningful
    return await c.projects.update_project(project_id, body.model_dump(exclude_unset=True))


@router.post("/{project_id}/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_project_task(project_id: uuid.UUID, body: TaskCreate, c: Container = Depends(get_container)):
    """Records a PENDING task in the project's shared workspace. Execution is /tasks/{id}/execution-requests."""
    return await c.tasks.create_task(requirements=body.requirements, conversation_id=body.conversation_id,
                                     limits=body.limits, test_policy=body.test_policy, project_id=project_id)


@router.get("/{project_id}/tasks", response_model=list[TaskOut])
async def list_project_tasks(project_id: uuid.UUID, status_filter: str | None = Query(None, alias="status"),
                             limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                             c: Container = Depends(get_container)):
    return await c.tasks.list_tasks(status_filter, limit, offset, project_id=project_id)
