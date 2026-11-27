import pytest

from app.models.states import (
    STEP_TRANSITIONS, TASK_TRANSITIONS, StepState, TaskMode, TaskState, check_step_transition, check_task_transition,
)
from app.validation import validators as V
from app.validation.exceptions import FailClosed, InvalidStateTransition, ValidationFailed


def test_every_task_state_has_a_transition_entry():
    assert set(TASK_TRANSITIONS) == set(TaskState)
    assert set(STEP_TRANSITIONS) == set(StepState)


@pytest.mark.parametrize("state", [TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED])
def test_terminal_task_states_have_no_exits(state):
    for target in TaskState:
        with pytest.raises(InvalidStateTransition):
            check_task_transition(state, target)


def test_design_review_must_precede_development():
    check_task_transition(TaskState.PLANNING, TaskState.DESIGN_REVIEWING)
    with pytest.raises(InvalidStateTransition):
        check_task_transition(TaskState.PLANNING, TaskState.DEVELOPING)
    check_task_transition(TaskState.DESIGN_REVIEWING, TaskState.DEVELOPING)


def test_step_waiting_approval_is_not_a_failure_state():
    check_step_transition(StepState.RUNNING, StepState.WAITING_APPROVAL)
    with pytest.raises(InvalidStateTransition):
        check_step_transition(StepState.SUCCEEDED, StepState.RUNNING)


def test_requirements_validation():
    assert V.validate_requirements("  build an API  ") == "build an API"
    with pytest.raises(ValidationFailed):
        V.validate_requirements("x")


def test_limits_validation_rejects_unknown_and_out_of_range():
    assert V.validate_limits({"max_replans": 1}).max_replans == 1
    with pytest.raises(ValidationFailed):
        V.validate_limits({"max_workflow_iterations": 9})
    with pytest.raises(ValidationFailed):
        V.validate_limits({"nope": 1})


def test_decision_requires_idempotency_key():
    V.validate_decision("APPROVED", "key-1", None)
    with pytest.raises(ValidationFailed):
        V.validate_decision("APPROVED", None, None)
    with pytest.raises(ValidationFailed):
        V.validate_decision("MAYBE", "k", None)


def test_test_policy_requires_arg_lists_not_shell_strings():
    with pytest.raises(ValidationFailed):
        V.validate_test_policy({"commands": ["pytest -q"]})
    assert V.validate_test_policy({"commands": [["pytest", "-q"]]})["min_tests_executed"] == 1


def test_session_binding_rules():
    import uuid
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    V.validate_session_binding(role="PLANNER", provider="CLAUDE", task_conversation_id=c1, session_conversation_id=c1)
    with pytest.raises(ValidationFailed):
        V.validate_session_binding(role="PLANNER", provider="CODEX", task_conversation_id=c1)
    with pytest.raises(FailClosed):  # task belongs to another conversation
        V.validate_session_binding(role="DEVELOPER", provider="CLAUDE", task_conversation_id=c1, session_conversation_id=c2)
    with pytest.raises(FailClosed):  # provider change must not reuse a native id
        V.validate_session_binding(role="DEVELOPER", provider="CLAUDE", task_conversation_id=c1, existing_provider="CODEX")


def _allowed(mode):
    pairs = set()
    for current in TaskState:
        for target in TaskState:
            try:
                check_task_transition(current, target, mode)
            except InvalidStateTransition:
                continue
            pairs.add((current, target))
    return pairs


def test_a_workflow_task_has_exactly_the_edges_of_the_transition_table():
    assert _allowed(TaskMode.WORKFLOW) == {(c, t) for c, ts in TASK_TRANSITIONS.items() for t in ts}
    assert _allowed("WORKFLOW") == _allowed(TaskMode.WORKFLOW)                           # a plain string works too
    with pytest.raises(InvalidStateTransition):
        check_task_transition(TaskState.PENDING, TaskState.TESTING)                      # the default mode is WORKFLOW
    with pytest.raises(InvalidStateTransition):
        check_task_transition(TaskState.TESTING, TaskState.COMPLETED)


def test_a_test_only_task_has_two_more_edges_and_nothing_else():
    extra = _allowed(TaskMode.TEST_ONLY) - _allowed(TaskMode.WORKFLOW)
    assert extra == {(TaskState.PENDING, TaskState.TESTING), (TaskState.TESTING, TaskState.COMPLETED)}
    assert _allowed(TaskMode.WORKFLOW) <= _allowed(TaskMode.TEST_ONLY)
    for terminal in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):       # terminal states still have no exit
        assert not [t for c, t in _allowed(TaskMode.TEST_ONLY) if c is terminal]


def test_an_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        check_task_transition(TaskState.PENDING, TaskState.TESTING, "SOMETHING_ELSE")
