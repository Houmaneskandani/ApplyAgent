"""
The LIVE_APPLY_ATS allowlist gates the AUTONOMOUS apply path. A bug here could
let the bot submit to an unvetted ATS — or, worse, default to allowing
everything. These tests pin the safe-by-default behavior.
"""
import importlib
import os

import scheduler


def _allowlist_with_env(value):
    """Evaluate _live_apply_allowlist() under a controlled LIVE_APPLY_ATS."""
    prev = os.environ.get("LIVE_APPLY_ATS")
    try:
        if value is None:
            os.environ.pop("LIVE_APPLY_ATS", None)
        else:
            os.environ["LIVE_APPLY_ATS"] = value
        # function reads os.environ at call time, no reimport needed
        return scheduler._live_apply_allowlist()
    finally:
        if prev is None:
            os.environ.pop("LIVE_APPLY_ATS", None)
        else:
            os.environ["LIVE_APPLY_ATS"] = prev


def test_default_is_greenhouse_only():
    # Unset -> only the proven path. This is the whole point: a fresh deploy
    # never silently auto-applies to anything but greenhouse.
    assert _allowlist_with_env(None) == {"greenhouse"}


def test_empty_string_falls_back_to_greenhouse():
    assert _allowlist_with_env("") == {"greenhouse"}
    assert _allowlist_with_env("   ") == {"greenhouse"}


def test_comma_list_is_parsed_and_normalized():
    assert _allowlist_with_env("greenhouse, Lever , ASHBY") == {
        "greenhouse", "lever", "ashby",
    }


def test_wildcard_allows_all():
    assert _allowlist_with_env("*") == {"*"}
