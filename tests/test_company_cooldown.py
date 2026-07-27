"""Company cooldown resolution: prefs override, env default, sane clamps."""
from db import company_cooldown_days, COMPANY_COOLDOWN_DAYS


def test_default_is_30():
    assert COMPANY_COOLDOWN_DAYS == 30
    assert company_cooldown_days(None) == 30
    assert company_cooldown_days({}) == 30


def test_prefs_override_and_clamps():
    assert company_cooldown_days({"company_cooldown_days": 60}) == 60
    assert company_cooldown_days({"company_cooldown_days": 0}) == 0      # disable
    assert company_cooldown_days({"company_cooldown_days": -5}) == 0     # clamp low
    assert company_cooldown_days({"company_cooldown_days": 9999}) == 365 # clamp high
    assert company_cooldown_days({"company_cooldown_days": "junk"}) == 30
