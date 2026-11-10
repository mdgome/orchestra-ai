"""The one recursive sanitiser: masking, resource bounds, the byte budget and what it reports."""
import copy
import uuid

import pytest

from app.infra.response_sanitizer import Sanitizer, SanitizeReason as R, is_denied_key
from app.validation import policies as P

SID = "native-session-0001"
TASK_ID = uuid.UUID("12345678-1234-4234-8234-123456789abc")


def reasons(cut):
    return set(cut.reasons)


# --- masking ---------------------------------------------------------------------------------------------------------
def test_the_input_is_never_modified_and_no_marker_key_is_ever_added():
    original = {"a": {"native_session_id": SID, "list": [1, 2, {"token": "x"}]}, "text": f"see {SID}"}
    snapshot = copy.deepcopy(original)
    cut = Sanitizer((SID,)).json(original)
    assert original == snapshot
    assert cut.value == {"a": {"list": [1, 2, {}]}, "text": f"see {Sanitizer((SID,)).text(SID)[0]}"}
    assert not any(key.startswith("_") for key in cut.value)          # nothing like `_truncated` is injected


def test_exact_deny_keys_and_the_suffix_rule_remove_keys_but_normal_fields_survive():
    value = {"native_session_id": "x", "snapshot_before": {"f": "h"}, "snapshot_after": {}, "idempotency_key": "k",
             "api_token": "t", "client_secret": "s", "db_password": "p", "access_token": "t",
             "session_fingerprint": "sess:abcd1234", "token_count": 7, "max_tokens": 100, "name": "kept",
             "nested": {"refresh_token": "r", "keep": 1}}
    cut = Sanitizer().json(value)
    assert cut.value == {"session_fingerprint": "sess:abcd1234", "token_count": 7, "max_tokens": 100, "name": "kept",
                         "nested": {"keep": 1}}
    assert R.KEY_REMOVED.value in cut.reasons and "nested/refresh_token" in cut.omitted_paths
    assert not cut.truncated                                   # masking is not information loss
    assert is_denied_key("  API_TOKEN ") and not is_denied_key("token_count") and not is_denied_key("tokenizer")


def test_only_known_native_session_ids_are_replaced_and_ordinary_uuids_are_kept():
    other = "99999999-8888-4777-8666-555555555555"
    cut = Sanitizer((SID,)).json({"msg": f"{SID} ran step {TASK_ID} and {other}", "id": TASK_ID})
    assert SID not in str(cut.value) and "sess:" in cut.value["msg"]
    assert str(TASK_ID) in cut.value["msg"] and other in cut.value["msg"]       # not "any UUID-shaped string"
    assert cut.value["id"] == str(TASK_ID)


def test_credentials_in_nested_json_are_masked():
    cut = Sanitizer().json({"a": [{"url": "https://user:s3cretT0ken@host/x.git"}],
                            "b": {"log": "auth Bearer abcdefghijklmnopqrstuv failed", "gh": "ghp_abcdefghijklmnop1234"}})
    flat = str(cut.value)
    assert "s3cretT0ken" not in flat and "abcdefghijklmnopqrstuv" not in flat and "ghp_abcdefghijklmnop1234" not in flat
    assert R.SECRET_REDACTED.value in cut.reasons and not cut.truncated


def test_text_masks_and_truncates_with_its_own_reasons():
    text, why = Sanitizer((SID,)).text(f"{SID} sk-abcdefghijklmnopqrstuvwx " + "y" * 50, max_chars=40)
    assert SID not in text and "sk-abcdef" not in text and len(text) <= 40 + len("...[truncated]")
    assert R.STRING_TRUNCATED.value in why and R.SECRET_REDACTED.value in why
    assert Sanitizer().text(None) == ("", ())


# --- resource bounds, each with its own reason ----------------------------------------------------------------------------
def test_depth_limit(monkeypatch):
    monkeypatch.setattr(P, "SANITIZE_MAX_DEPTH", 3)
    cut = Sanitizer().json({"a": {"b": {"c": {"d": 1}}}})
    assert cut.value == {"a": {"b": {"c": None}}} and reasons(cut) == {R.DEPTH_LIMIT.value}
    assert cut.truncated and "a/b/c" in cut.omitted_paths


def test_keys_per_object_limit(monkeypatch):
    monkeypatch.setattr(P, "SANITIZE_MAX_KEYS", 3)
    cut = Sanitizer().json({f"k{i}": i for i in range(10)})
    assert list(cut.value) == ["k0", "k1", "k2"] and reasons(cut) == {R.KEYS_LIMIT.value}


def test_array_limit(monkeypatch):
    monkeypatch.setattr(P, "SANITIZE_MAX_ITEMS", 4)
    cut = Sanitizer().json({"xs": list(range(20))})
    assert cut.value == {"xs": [0, 1, 2, 3]} and reasons(cut) == {R.ARRAY_TRUNCATED.value}


def test_string_limit_keeps_a_visible_mark(monkeypatch):
    monkeypatch.setattr(P, "SANITIZE_MAX_STRING_CHARS", 10)
    cut = Sanitizer().json({"s": "z" * 100})
    assert cut.value["s"].startswith("z" * 10) and cut.value["s"].endswith("[truncated]")
    assert reasons(cut) == {R.STRING_TRUNCATED.value}


def test_node_limit(monkeypatch):
    monkeypatch.setattr(P, "SANITIZE_MAX_NODES", 5)
    cut = Sanitizer().json({"xs": list(range(50))})
    assert R.NODE_LIMIT.value in cut.reasons and cut.truncated


def test_cycles_do_not_recurse_forever():
    loop: dict = {"name": "x"}
    loop["self"] = loop
    chain: list = []
    chain.append(chain)
    cut = Sanitizer().json({"loop": loop, "chain": chain})
    assert cut.value["loop"] == {"name": "x", "self": None} and cut.value["chain"] == [None]
    assert reasons(cut) == {R.CYCLE_DETECTED.value}


def test_non_json_values_are_converted_not_crashed_on():
    class Weird:
        def __str__(self):
            return "weird"

    cut = Sanitizer().json({"w": Weird(), "f": float("nan"), "t": (1, 2), "b": True})
    assert cut.value == {"w": "weird", "f": None, "t": [1, 2], "b": True}


# --- the byte budget ----------------------------------------------------------------------------------------------------
def test_the_budget_cuts_the_biggest_string_first(monkeypatch):
    monkeypatch.setattr(P, "SANITIZE_MAX_STRING_CHARS", 100_000)
    cut = Sanitizer().json({"big": "b" * 5000, "small": "s" * 300, "n": 1}, max_bytes=1500)
    assert len(str(cut.value).encode()) <= 1500 + 100
    assert cut.value["small"] == "s" * 300 and cut.value["n"] == 1 and len(cut.value["big"]) < 5000
    assert reasons(cut) == {R.STRING_TRUNCATED.value} and "big" in cut.omitted_paths


def test_the_budget_then_cuts_array_tails():
    cut = Sanitizer().json({"xs": [{"v": i} for i in range(150)], "keep": "k"}, max_bytes=300)
    assert R.ARRAY_TRUNCATED.value in cut.reasons and cut.value["keep"] == "k" and 0 < len(cut.value["xs"]) < 150
    assert cut.value["xs"][0] == {"v": 0}


def test_the_budget_finally_removes_the_biggest_object_and_names_it():
    value = {"deep": {"a": {"b": 1, "c": 2, "d": 3}, "e": {"f": 4}}, "tiny": 1}
    cut = Sanitizer().json(value, max_bytes=40)
    assert R.SIZE_LIMIT.value in cut.reasons and "deep" in str(cut.omitted_paths) and cut.truncated


def test_a_hopeless_budget_empties_the_value_with_size_limit():
    cut = Sanitizer().json({"a": 1, "b": 2, "c": 3}, max_bytes=2)
    assert cut.value is None and R.SIZE_LIMIT.value in cut.reasons and cut.truncated


def test_each_cause_has_a_separate_reason_code(monkeypatch):
    monkeypatch.setattr(P, "SANITIZE_MAX_ITEMS", 2)
    monkeypatch.setattr(P, "SANITIZE_MAX_STRING_CHARS", 5)
    cut = Sanitizer().json({"api_token": "t", "xs": [1, 2, 3], "s": "123456789", "u": "https://a:b@h/x"})
    assert {R.KEY_REMOVED.value, R.ARRAY_TRUNCATED.value, R.STRING_TRUNCATED.value, R.SECRET_REDACTED.value} <= reasons(cut)
