"""
PUT /profile/ must only queue the (paid, ~250 Haiku calls) re-score when a
preference the scorer actually reads changed. The dashboard persists
`dashboard_filters` through this endpoint on every chip click.
"""
import inspect
from api.routes.profile import score_inputs_changed, SCORE_RELEVANT_PREF_KEYS


BASE = {
    "skills": [{"name": "Python", "level": "professional"}],
    "job_title": "Software Engineer",
    "city": "Los Angeles",
    "work_preference": "remote",
    "imap_user": "me@gmail.com",
    "dashboard_filters": {"location": "LA", "work_type": ["remote"]},
}


def test_filter_only_change_does_not_rescore():
    merged = {**BASE, "dashboard_filters": {"location": "Seattle", "work_type": []}}
    assert score_inputs_changed(BASE, merged) is False


def test_imap_and_backend_flags_do_not_rescore():
    merged = {**BASE, "imap_user": "other@gmail.com", "imap_pass": "enc", "auto_apply": True,
              "last_scraped_at": "2026-01-01", "title_roles": ["devops"]}
    assert score_inputs_changed(BASE, merged) is False


def test_identical_payload_does_not_rescore():
    assert score_inputs_changed(BASE, dict(BASE)) is False


def test_unset_equivalents_do_not_rescore():
    """None / '' / [] / missing are all 'unset' — the dashboard round-trips
    GET -> PUT and can turn a missing key into null or [] without meaning it."""
    existing = {**BASE, "keywords": None, "languages": []}
    merged = {**BASE, "keywords": "", "languages": None}
    assert score_inputs_changed(existing, merged) is False
    existing.pop("keywords")
    assert score_inputs_changed(existing, merged) is False


def test_score_relevant_changes_do_rescore():
    for key, value in [
        ("skills", [{"name": "Go", "level": "beginner"}]),
        ("job_title", "DevOps Engineer"),
        ("city", "Seattle"),
        ("job_categories", ["devops"]),
        ("years_experience", "7"),
        ("keywords", "kubernetes"),
        ("open_to_lower_level", True),
    ]:
        assert key in SCORE_RELEVANT_PREF_KEYS
        assert score_inputs_changed(BASE, {**BASE, key: value}) is True, key


def test_every_pref_the_matcher_reads_is_in_the_gate():
    """If someone adds a prefs.get(...) to the matcher, the gate must learn it."""
    import re
    import matcher
    src = inspect.getsource(matcher.build_profile_summary) + inspect.getsource(matcher.score_jobs)
    read_keys = set(re.findall(r'prefs\.get\("([a-z_]+)"', src))
    missing = read_keys - set(SCORE_RELEVANT_PREF_KEYS)
    assert not missing, f"matcher reads prefs not covered by the rescore gate: {missing}"


def test_update_profile_only_rescores_when_gate_says_so():
    from api.routes import profile
    src = inspect.getsource(profile.update_profile)
    assert "needs_rescore = score_inputs_changed(existing_prefs, merged_prefs)" in src
    assert "if needs_rescore:" in src
    assert '"rescoring": needs_rescore' in src
