"""Offline smoke tests for the generic filter/lane/score rules (no network, placeholder config)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jobs"))
import finder  # noqa: E402


def job(title, desc="", location="Remote, LATAM", days=1, salary=None):
    return finder.Job(source="test", title=title, company="Acme", url="https://example.com/1", location=location,
                      description=desc, published=datetime.now(timezone.utc) - timedelta(days=days),
                      salary_usd_year=salary)


def test_paid_media_title_gets_lane_a():
    j = job("Senior Media Buyer", "Meta ads and Google Ads at scale. Conversions API.")
    lane, pts = finder.assign_lane(j)
    assert (lane, pts) == ("A", 30)


def test_crm_and_paid_in_title_is_ac():
    lane, _ = finder.assign_lane(job("Paid Media & CRM Manager"))
    assert lane == "AC"


def test_hybrid_is_dropped_outside_configured_area():
    assert finder.hard_drop(job("Media Buyer", "This is a hybrid role", location="Berlin")).startswith("not fully remote")


def test_other_language_required_is_dropped():
    assert finder.hard_drop(job("Media Buyer", "Fluent German is required")).startswith("language")


def test_stale_posting_dropped():
    assert finder.hard_drop(job("Media Buyer", days=finder.MAX_AGE_DAYS + 3)).startswith("stale")


def test_us_residency_dropped():
    ok, why = finder.location_ok(job("Media Buyer", "Must reside in the US."))
    assert not ok and why.startswith("residency")


def test_salary_parser_annual_usd():
    assert finder.parse_salary_text("US$ 60k - 80k / year") == (60000.0, 80000.0)


def test_score_is_bounded_and_letter_rules_flag_results():
    j = job("Senior Paid Media Manager", "meta ads google ads crm lifecycle")
    j.lane, pts = finder.assign_lane(j)
    finder.score(j, pts)
    assert 0 <= j.score <= 100
    assert "numeric result" in finder.letter_ok("We grew ROAS by 40% in a month.")
