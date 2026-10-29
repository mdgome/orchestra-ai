"""Structured agent outputs: JSON schemas handed to the CLIs and the Orchestrator-side validation.

The Orchestrator, not the agent, validates and stores every artifact. Schemas are strict
(all properties required, additionalProperties false) so both CLIs accept them as output schemas.
"""
from pathlib import PurePosixPath

from app.services.hashing import stable_hash
from app.validation.exceptions import ValidationFailed


class ArtifactInvalid(ValidationFailed):
    code = "ARTIFACT_INVALID"


def _str(desc=""):
    return {"type": "string", "description": desc}


def _arr(items, desc=""):
    return {"type": "array", "items": items, "description": desc}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_OPTION = _obj({"id": _str("short id, e.g. A"), "title": _str(), "pros": _str(), "cons": _str()})
GATE_REQUEST = _obj({
    "problem": _str(), "options": _arr(_OPTION), "complexity": _str("implementation complexity comparison"),
    "maintainability": _str("maintainability comparison"), "recommendation": _str(), "impact": _str("change impact scope"),
})
NULLABLE_GATE = {"anyOf": [{"type": "null"}, GATE_REQUEST]}

PLAN_SCHEMA = _obj({
    "requirements": _str("restated requirements"),
    "design_direction": _str(),
    "changed_files": _arr(_str(), "relative paths expected to change"),
    "implementation_steps": _arr(_str()),
    "test_plan": _arr(_str()),
    "acceptance_criteria": _arr(_str()),
    "tradeoffs": _arr(_str()),
    "human_gate_request": NULLABLE_GATE,
})
PLAN_FIELDS = tuple(k for k in PLAN_SCHEMA["properties"] if k != "human_gate_request")

_ISSUE = _obj({"severity": {"type": "string", "enum": ["CRITICAL", "MAJOR", "MINOR"]}, "description": _str()})
DESIGN_REVIEW_SCHEMA = _obj({
    "verdict": {"type": "string", "enum": ["PASS", "NEEDS_REVISION", "NEEDS_HUMAN_REVIEW"]},
    "issues": _arr(_ISSUE), "summary": _str(), "human_gate_request": NULLABLE_GATE,
})
CODE_REVIEW_SCHEMA = _obj({
    "verdict": {"type": "string", "enum": ["PASS", "FAIL", "NEEDS_HUMAN_REVIEW"]},
    "issues": _arr(_ISSUE), "summary": _str(),
    "plan_defect": {"type": "boolean", "description": "true when the PLAN itself is flawed"},
    "test_policy_concern": {"type": "boolean", "description": "true when required verification was weakened"},
    "human_gate_request": NULLABLE_GATE,
})
DEV_SCHEMA = _obj({"summary": _str(), "files_changed": _arr(_str()), "human_gate_request": NULLABLE_GATE})
TEST_AGENT_SCHEMA = _obj({"summary": _str(), "tests_added": _arr(_str()), "human_gate_request": NULLABLE_GATE})


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ArtifactInvalid(message)


def synthesize_gate_request(review: dict) -> dict:
    """The reviewer asked for a human but did not describe the decision: build the request from its own findings
    (never invent options silently: the two options are the only possible answers to 'proceed anyway?')."""
    issues = "; ".join(str(i.get("description", "")) for i in review.get("issues", []) if isinstance(i, dict))
    return {"problem": (review.get("summary") or "reviewer requested human review") + (f" Issues: {issues}" if issues else ""),
            "options": [{"id": "accept", "title": "Accept as is", "pros": "no more work", "cons": "reviewer concern stays"},
                        {"id": "revise", "title": "Require changes", "pros": "addresses the concern", "cons": "more work"}],
            "complexity": "not provided by the reviewer", "maintainability": "not provided by the reviewer",
            "recommendation": "revise", "impact": "not provided by the reviewer"}


def validate_gate_request(data) -> dict | None:
    if data is None:
        return None
    _require(isinstance(data, dict), "human_gate_request must be an object or null")
    _require(isinstance(data.get("problem"), str) and data["problem"].strip(), "gate request needs a problem")
    options = data.get("options")
    _require(isinstance(options, list) and len(options) >= 2, "gate request needs at least two options")
    for o in options:
        _require(isinstance(o, dict) and isinstance(o.get("id"), str) and o["id"].strip(), "option needs an id")
    ids = [o["id"] for o in options]
    _require(len(set(ids)) == len(ids), "option ids must be unique")
    return data


def _safe_relative_path(path: str) -> bool:
    p = PurePosixPath(path.replace("\\", "/"))
    return bool(path.strip()) and not p.is_absolute() and ".." not in p.parts and ":" not in path.split("/")[0]


def validate_plan_artifact(data) -> dict:
    """Returns the normalised artifact (without the gate request) or raises ArtifactInvalid."""
    _require(isinstance(data, dict), "plan must be a JSON object")
    for key in PLAN_FIELDS:
        _require(key in data, f"plan is missing required field '{key}'")
    for key in ("requirements", "design_direction"):
        _require(isinstance(data[key], str) and data[key].strip(), f"'{key}' must be a non-empty string")
    for key in ("changed_files", "implementation_steps", "test_plan", "acceptance_criteria", "tradeoffs"):
        _require(isinstance(data[key], list) and all(isinstance(x, str) and x.strip() for x in data[key]),
                 f"'{key}' must be a list of non-empty strings")
    for key in ("implementation_steps", "test_plan", "acceptance_criteria"):
        _require(len(data[key]) > 0, f"'{key}' must not be empty")
    for path in data["changed_files"]:
        _require(_safe_relative_path(path), f"changed file path is not a safe relative path: {path!r}")
    return {k: data[k] for k in PLAN_FIELDS}


def plan_hash(artifact: dict) -> str:
    return stable_hash(artifact)   # == TaskStep.output_hash of the PLAN step


def validate_design_review(data) -> dict:
    _require(isinstance(data, dict), "design review must be a JSON object")
    _require(data.get("verdict") in ("PASS", "NEEDS_REVISION", "NEEDS_HUMAN_REVIEW"), "invalid design review verdict")
    _require(isinstance(data.get("issues"), list), "issues must be a list")
    validate_gate_request(data.get("human_gate_request"))
    if data["verdict"] == "NEEDS_HUMAN_REVIEW" and data.get("human_gate_request") is None:
        data = {**data, "human_gate_request": synthesize_gate_request(data)}
    return data


def validate_code_review(data) -> dict:
    _require(isinstance(data, dict), "code review must be a JSON object")
    _require(data.get("verdict") in ("PASS", "FAIL", "NEEDS_HUMAN_REVIEW"), "invalid code review verdict")
    _require(isinstance(data.get("issues"), list), "issues must be a list")
    validate_gate_request(data.get("human_gate_request"))
    if data["verdict"] == "NEEDS_HUMAN_REVIEW" and data.get("human_gate_request") is None:
        data = {**data, "human_gate_request": synthesize_gate_request(data)}
    return data


def has_critical(review: dict) -> bool:
    return any(isinstance(i, dict) and i.get("severity") == "CRITICAL" for i in review.get("issues", []))
