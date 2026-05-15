"""
Tests for the get_answer cache. This cache directly affects Claude token
spend on every application, so regressions here are expensive.
"""
import pytest
from applier.greenhouse import (
    _cache_get, _cache_set, _ANSWER_CACHE, _normalize_question, _profile_hash,
)


def setup_function(_fn):
    _ANSWER_CACHE.clear()


def test_cache_basic_round_trip():
    _cache_set("Are you a US citizen?", "select", "PROFILE_A", "No")
    assert _cache_get("Are you a US citizen?", "select", "PROFILE_A") == "No"


def test_cache_is_case_and_whitespace_insensitive():
    _cache_set("Are you a US citizen?", "select", "PROFILE_A", "No")
    assert _cache_get("  ARE YOU A US CITIZEN?  ", "select", "PROFILE_A") == "No"


def test_cache_is_partitioned_by_profile():
    _cache_set("Sponsorship?", "radio", "PROFILE_A", "No")
    # Different profile must NOT hit
    assert _cache_get("Sponsorship?", "radio", "PROFILE_B") is None


def test_cache_is_partitioned_by_field_type():
    _cache_set("Veteran status?", "select", "PROFILE_A", "Decline")
    # Same question but different field_type must NOT hit
    assert _cache_get("Veteran status?", "text", "PROFILE_A") is None


def test_empty_answer_is_not_cached():
    _cache_set("Question", "text", "PROFILE_A", "")
    assert _cache_get("Question", "text", "PROFILE_A") is None


def test_profile_hash_changes_when_profile_changes():
    h1 = _profile_hash("Profile content A")
    h2 = _profile_hash("Profile content B")
    assert h1 != h2
    assert h1 == _profile_hash("Profile content A")  # deterministic


def test_normalize_question_handles_whitespace():
    assert _normalize_question("  Hello\n World  ") == "hello world"
    assert _normalize_question("") == ""
    assert _normalize_question(None) == ""
