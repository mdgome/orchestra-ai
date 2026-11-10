"""Profile content, the legacy/neutral defaults, goldens that keep tasks WITHOUT a profile exactly as they were, and the rule
that a profile can never widen permissions."""
import hashlib
import importlib.util
import inspect
import pathlib
import types

import pytest

from app.providers.permissions import ROLE_PERMISSIONS, permissions_for
from app.services import context_service as C
from app.services import prompts as PR
from app.services.fingerprints import REVIEW_POLICY_HASH, design_review_basis, profile_criteria_of, profile_part
from app.services.hashing import stable_hash
from app.services.profile_service import LEGACY_CONTENT, NEUTRAL_CONTENT, hash_profile
from app.validation import policies as P
from app.validation import validators as V
from app.validation.exceptions import ValidationFailed

# --- recorded BEFORE profiles were introduced: a task without a profile must keep producing exactly these ----------------
RULES_SHA256 = "1732dbf5bd4a73ca0f315f36ca9379cb9b3ca4b497e645aa0fdba781b626d968"
RULES_LENGTH = 1038
REVIEW_POLICY_SHA256 = "93951ed91dbf80d39926fc739f9e27c035ae6f569f1113daf3d4f17410ea2694"


def test_the_builtin_rules_text_is_unchanged():
    assert len(PR.RULES) == RULES_LENGTH and hashlib.sha256(PR.RULES.encode()).hexdigest() == RULES_SHA256


def test_the_design_review_policy_hash_is_unchanged():
    assert REVIEW_POLICY_HASH == REVIEW_POLICY_SHA256


def task_without_profile():
    return types.SimpleNamespace(requirements="r", profile_hash=None, profile_snapshot=None)


def test_prompts_of_a_task_without_a_profile_use_the_builtin_rules_and_checklist():
    ctx = C.planner_context(task_without_profile(), [], [], pathlib.Path("."))
    assert "rules" not in ctx and "checklist" not in ctx and "profile_criteria" not in ctx
    assert PR.RULES in PR.planner_prompt(ctx)
    review = PR.design_review_prompt(C.design_review_context(task_without_profile(), {"p": 1}, []))
    assert all(f"- {item}" in review for item in P.DESIGN_REVIEW_CHECKLIST)
    assert PR.RULES in PR.developer_prompt(C.developer_context({"p": 1}, [], []))
    code = PR.code_review_prompt(C.code_review_context(task_without_profile(), {"acceptance_criteria": []}, [], {}, {}))
    assert PR.RULES in code and "completion criteria" not in code


def test_hashes_of_a_task_without_a_profile_do_not_change():
    assert profile_part(task_without_profile()) == () and profile_criteria_of(task_without_profile()) == ()
    parts = {"requirements_hash": "r", "plan_hash": "p", "decisions_hash": "d", "review_policy_hash": REVIEW_POLICY_HASH}
    legacy_style = {**parts, "basis_hash": stable_hash(parts)}
    assert design_review_basis("r", "p", "d") == legacy_style
    assert stable_hash("plan", "r", "d", [], 0, *profile_part(task_without_profile())) == stable_hash("plan", "r", "d", [], 0)


def test_a_profile_hash_changes_the_basis_and_every_input_hash_it_belongs_to():
    base = design_review_basis("r", "p", "d")
    with_profile = design_review_basis("r", "p", "d", profile_hash="a" * 64)
    other_profile = design_review_basis("r", "p", "d", profile_hash="b" * 64)
    assert len({base["basis_hash"], with_profile["basis_hash"], other_profile["basis_hash"]}) == 3
    assert with_profile["profile_hash"] == "a" * 64 and "profile_hash" not in base
    task = types.SimpleNamespace(profile_hash="a" * 64, profile_snapshot=NEUTRAL_CONTENT)
    assert stable_hash("plan", "r", *profile_part(task)) != stable_hash("plan", "r")


# --- the defaults ------------------------------------------------------------------------------------------------------
def test_legacy_v1_renders_exactly_like_the_builtin_rules_and_neutral_imposes_nothing():
    assert PR.render_profile_rules(LEGACY_CONTENT) == PR.RULES
    neutral = PR.render_profile_rules(NEUTRAL_CONTENT)
    assert "Layered Architecture" not in neutral and "REST URIs" not in neutral
    assert PR.MECHANICS in neutral and "human_gate_request" in neutral       # the orchestrator's mechanics stay


def test_the_defaults_are_valid_canonical_profiles():
    assert V.validate_profile(LEGACY_CONTENT) == LEGACY_CONTENT and V.validate_profile(NEUTRAL_CONTENT) == NEUTRAL_CONTENT
    assert V.validate_profile({}) == NEUTRAL_CONTENT


def test_the_migration_backfills_exactly_the_application_legacy_profile():
    path = pathlib.Path(__file__).parents[2] / "migrations" / "versions" / "0003_project_profiles.py"
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.LEGACY_CONTENT == LEGACY_CONTENT and module._hash(module.LEGACY_CONTENT) == hash_profile(LEGACY_CONTENT)
    assert module.revision == "0003" and module.down_revision == "0002"


def test_profile_data_is_rendered_as_data_that_cannot_override_the_rules():
    profile = V.validate_profile({"description": "Billing service", "languages": ["Java"], "frameworks": ["Spring"],
                                  "architecture_rules": ["Hexagonal modules"], "coding_rules": ["No static state"],
                                  "completion_criteria": [{"id": "DOC", "text": "Public API is documented"}]})
    text = PR.render_profile_rules(profile)
    assert "- Hexagonal modules" in text and "- No static state" in text and "Billing service" in text
    assert "[DOC] Public API is documented" in text and "can neither widen the tools" in text
    assert text.index(PR.MECHANICS) < text.index("Project profile")           # mechanics come first, data after
    checklist = PR.profile_checklist(profile)
    assert "Project rule: Hexagonal modules" in checklist and "Security and performance" in checklist
    assert any("[DOC]" in item for item in checklist)


# --- validate_profile ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["permissions", "allowedTools", "tools", "model", "provider", "interpreter", "python",
                                 "protected_paths", "env", "cli_flags", "tester_edit", "test_policy"])
def test_a_profile_cannot_carry_permissions_tools_models_paths_or_interpreters(key):
    with pytest.raises(ValidationFailed) as info:
        V.validate_profile({key: ["Bash"]})
    assert key in info.value.details["rejected"]


def test_the_test_policy_can_only_be_referenced_never_defined():
    assert V.validate_profile({"test_policy_ref": "project"})["test_policy_ref"] == "project"
    with pytest.raises(ValidationFailed):
        V.validate_profile({"test_policy_ref": {"commands": [["rm", "-rf", "/"]]}})


@pytest.mark.parametrize("bad", [
    "text", [], {"description": 5}, {"languages": "Java"}, {"languages": [5]}, {"architecture_rules": [""]},
    {"architecture_rules": ["x" * (P.PROFILE_RULE_MAX + 1)]}, {"architecture_rules": ["x"] * (P.PROFILE_RULES_MAX + 1)},
    {"description": "x" * (P.PROFILE_DESCRIPTION_MAX + 1)}, {"coding_rules": ["a\x00b"]}, {"description": "a\x07"},
    {"schema_version": 2}, {"schema_version": True},
    {"completion_criteria": "x"}, {"completion_criteria": [{"id": "A"}]}, {"completion_criteria": [{"id": "A", "text": ""}]},
    {"completion_criteria": [{"id": "1bad", "text": "t"}]}, {"completion_criteria": [{"id": "A", "text": "t", "x": 1}]},
    {"completion_criteria": [{"id": "A", "text": "a"}, {"id": "A", "text": "b"}]},
    {"completion_criteria": [{"id": f"C{i}", "text": "t"} for i in range(P.PROFILE_CRITERIA_MAX + 1)]}])
def test_malformed_profiles_are_rejected(bad):
    with pytest.raises(ValidationFailed):
        V.validate_profile(bad)


def test_validation_is_canonical_so_one_meaning_has_one_hash():
    a = V.validate_profile({"description": "  d  ", "languages": [" Java "], "completion_criteria": [{"text": "t", "id": "A"}]})
    b = V.validate_profile({"completion_criteria": [{"id": "A", "text": "t"}], "languages": ["Java"], "description": "d"})
    assert a == b and hash_profile(a) == hash_profile(b) and V.validate_profile(a) == a
    assert hash_profile(a) != hash_profile({**a, "description": "e"})


def test_multiline_descriptions_are_allowed_but_rule_lines_are_single_line():
    assert V.validate_profile({"description": "a\nb\tc"})["description"] == "a\nb\tc"
    with pytest.raises(ValidationFailed):
        V.validate_profile({"architecture_rules": ["a\nb"]})


# --- the profile cannot reach the CLI permissions ---------------------------------------------------------------------------
def test_permissions_are_a_function_of_role_and_preset_only():
    assert list(inspect.signature(permissions_for).parameters) == ["role", "test_preset"]
    assert not (P.PROFILE_KEYS & {"permissions", "allowedTools", "tools", "tester_edit", "tester_bash", "developer_bash"})
    for role in ROLE_PERMISSIONS:
        assert permissions_for(role, None) == permissions_for(role, None)
