"""Workflow-level check that the JVM presets' Tester path rules (`is_test_path`) are enforced on the real TEST step.

No JDK, Maven or Gradle is run: the policy is resolved against a fake toolchain path, and the violation check happens
before the Orchestrator would execute any test command."""
from pathlib import Path

import pytest

from app.services import test_policy_resolver as resolver
from tests.harness import Harness, structured


@pytest.fixture(autouse=True)
def fake_toolchain(monkeypatch):
    monkeypatch.setattr(resolver.shutil, "which", lambda name: f"/opt/{name}")


def writes_as_tester(*rel_paths):
    def tester(call):
        for rel in rel_paths:
            target = Path(call.cwd) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("class X {}\n", encoding="utf-8")
        return structured(summary="x", tests_added=list(rel_paths), human_gate_request=None)
    return tester


async def run_task(db, tmp_path, preset, *rel_paths):
    h = Harness(db, tmp_path, claude_handlers={"TESTER": writes_as_tester(*rel_paths)})
    task = await h.new_task(test_policy={"preset": preset}, limits={"max_workflow_iterations": 0})
    await h.start(task.id)
    return h, await h.task(task.id)


@pytest.mark.parametrize("preset", ["maven", "gradle"])
@pytest.mark.parametrize("rel", [
    "src/main/java/com/x/FooTest.java",        # production source, whatever the file is called
    "mod/src/main/java/FooIT.java",
    "src/main/resources/application.yml",
    "pom.xml", "build.gradle", "mvnw", "gradlew.bat",
])
async def test_a_tester_changing_production_or_build_files_fails_the_task(db, tmp_path, preset, rel):
    h, task = await run_task(db, tmp_path, preset, rel)
    assert task.status == "FAILED" and task.failure_reason == "TESTER_MODIFIED_NON_TEST_FILES"
    assert "CODE_REVIEWER" not in h.roles_called(h.codex)


@pytest.mark.parametrize("preset", ["maven", "gradle"])
async def test_a_tester_changing_only_src_test_files_passes_the_path_check(db, tmp_path, preset):
    h, task = await run_task(db, tmp_path, preset, "src/test/java/com/x/FooTest.java",
                             "mod/src/test/java/BarTest.java", "src/test/resources/application.yml")
    assert task.failure_reason != "TESTER_MODIFIED_NON_TEST_FILES"
    test_steps = [s for s in await h.steps_of(task.id) if s.step_key == "TEST"]
    assert test_steps and test_steps[0].status == "SUCCEEDED"        # the step got past the path check and ran the policy
    assert "test_result" in test_steps[0].output

