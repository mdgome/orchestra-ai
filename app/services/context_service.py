"""Per-role minimal context. Nothing here carries message history or native session content."""
import os
from pathlib import Path

from app.infra.workspace_snapshot import SKIP_DIRS


def project_structure(root: Path, max_entries: int = 200) -> str:
    lines: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and d != "reports")
        rel = Path(dirpath).relative_to(root).as_posix()
        for name in sorted(filenames):
            lines.append(name if rel == "." else f"{rel}/{name}")
            if len(lines) >= max_entries:
                return "\n".join(lines) + "\n... (truncated)"
    return "\n".join(lines) or "(empty project)"


def planner_context(task, decisions: list[str], feedback: list, root: Path) -> dict:
    return {"requirements": task.requirements, "decisions": decisions, "feedback": feedback,
            "structure": project_structure(root)}


def design_review_context(task, plan: dict, decisions: list[str]) -> dict:
    return {"requirements": task.requirements, "plan": plan, "decisions": decisions}


def developer_context(plan: dict, decisions: list[str], feedback: list) -> dict:
    return {"plan": plan, "decisions": decisions, "feedback": feedback}


def tester_context(task, plan: dict, change_summary: str, test_command: list) -> dict:
    return {"requirements": task.requirements, "acceptance_criteria": plan["acceptance_criteria"],
            "change_summary": change_summary, "test_command": test_command}


def code_review_context(task, plan: dict, changed_files: list[str], test_result: dict, signals: dict) -> dict:
    return {"requirements": task.requirements, "acceptance_criteria": plan["acceptance_criteria"],
            "changed_files": changed_files, "test_result": test_result, "test_signals": signals}
