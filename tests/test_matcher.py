"""Matcher correctness — title pre-filter + score parsing edge cases."""
from matcher import is_engineering_job


def test_engineering_job_positive():
    assert is_engineering_job("Senior Software Engineer")
    assert is_engineering_job("Backend Developer")
    assert is_engineering_job("Machine Learning Engineer")
    assert is_engineering_job("Data Platform Engineer")
    assert is_engineering_job("Site Reliability Engineer (SRE)")


def test_engineering_job_negative():
    assert not is_engineering_job("Sales Representative")
    assert not is_engineering_job("Recruiter")
    assert not is_engineering_job("Accountant")
    assert not is_engineering_job("Customer Success Manager")
    assert not is_engineering_job("Office Manager")
    assert not is_engineering_job("Product Manager")


def test_engineering_job_empty():
    assert not is_engineering_job("")
