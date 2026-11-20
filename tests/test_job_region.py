"""US / non-US classification of free-text job locations (US-only feed)."""
import pytest

from locations import job_region, us_only_for


@pytest.mark.parametrize("loc", [
    "Hawthorne, CA", "Starbase, TX", "United States", "Remote - USA", "US-Remote",
    "San Francisco, CA | New York City, NY", "Mountain View, California (HQ)",
    "Irvine, California", "New York City", "Remote (U.S.)", "SF, CA", "NY",
    "Hybrid - San Francisco", "Atlanta; Austin; Boston; Chicago; Dallas",
    # multi-office postings that include a US office stay visible
    "Dallas, Texas; San Francisco, California; Vancouver, Canada",
    "REMOTE (US, Canada)", "Canada; United Kingdom; United States",
    "London, UK or Remote (US)", "Remote - North America",
    # US towns that share a foreign city's name
    "Dublin, CA", "Vancouver, WA", "Vienna, VA", "Melbourne, FL", "Paris, TX",
])
def test_us(loc):
    assert job_region(loc) == "us"


@pytest.mark.parametrize("loc", [
    "Bengaluru, India", "Toronto, Canada", "Dublin, Ireland", "Dublin", "London, UK",
    "Singapore", "Toronto, Ontario, Canada", "Canada - Remote (ON, AB, BC, or NS Only)",
    "Sydney, Australia", "Vancouver, BC", "Toronto; Vancouver", "Toronto, ON, CA",
    "Berlin, DE", "Warsaw, Masovian Voivodeship, Poland", "Reykjavík", "Remote (EMEA)",
    "Remote - India", "Remote, CAN", "Kuwait City, Kuwait", "Mexico City",
    "Remote, France; Remote, Germany; Remote, United Kingdom", "Ukraine",
])
def test_non_us(loc):
    assert job_region(loc) == "non_us"


@pytest.mark.parametrize("loc", ["Remote", "Hybrid", "", None, "Worldwide", "Remote (Global)", "In-Office"])
def test_unknown_is_never_hidden(loc):
    assert job_region(loc) == "unknown"


def test_new_mexico_is_us_not_mexico():
    assert job_region("Albuquerque, New Mexico") == "us"
    assert job_region("Santa Fe, NM") == "us"


def test_us_only_for_prefs():
    assert us_only_for({"country": "United States"})
    assert us_only_for({"country": "usa"})
    assert not us_only_for({"country": "Canada"})
    assert not us_only_for({})            # unknown country: never hide the feed
    assert not us_only_for(None)
    assert not us_only_for({"country": "United States", "us_only": False})  # explicit opt-out
    assert us_only_for({"country": "", "us_only": True})
