"""Per-role minimal context. Nothing here carries message history or native session content."""
import os
from pathlib import Path

from app.infra.workspace_snapshot import SKIP_DIRS
from app.services import prompts as PR


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


def profile_context(profile: dict | None) -> dict:
    """What a task's profile snapshot changes in the prompts. None (no profile) changes nothing: the built-in rules apply."""
    if not profile:
        return {}
    return {"rules": PR.render_profile_rules(profile), "checklist": PR.profile_checklist(profile),
            "profile_criteria": list(profile["completion_criteria"])}


def planner_context(task, decisions: list[str], feedback: list, root: Path) -> dict:
    return {"requirements": task.requirements, "decisions": decisions, "feedback": feedback,
            "structure": project_structure(root), **profile_context(task.profile_snapshot)}


def design_review_context(task, plan: dict, decisions: list[str]) -> dict:
    return {"requirements": task.requirements, "plan": plan, "decisions": decisions,
            **profile_context(task.profile_snapshot)}


def developer_context(plan: dict, decisions: list[str], feedback: list, profile: dict | None = None) -> dict:
    return {"plan": plan, "decisions": decisions, "feedback": feedback, **profile_context(profile)}


def tester_context(task, plan: dict, change_summary: str, test_command: list) -> dict:
    return {"requirements": task.requirements, "acceptance_criteria": plan["acceptance_criteria"],
            "change_summary": change_summary, "test_command": test_command}


def code_review_context(task, plan: dict, changed_files: list[str], test_result: dict, signals: dict) -> dict:
    return {"requirements": task.requirements, "acceptance_criteria": plan["acceptance_criteria"],
            "changed_files": changed_files, "test_result": test_result, "test_signals": signals,
            **profile_context(task.profile_snapshot)}
