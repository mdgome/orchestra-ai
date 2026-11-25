from fastapi import Depends, Request

from app.container import Container
from app.services.report_service import ReportService


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_reports(c: Container = Depends(get_container)) -> ReportService:
    """The artifact / report service; `Container.report_service()` answers REPORTS_NOT_CONFIGURED (503) when there is none."""
    return c.report_service()
