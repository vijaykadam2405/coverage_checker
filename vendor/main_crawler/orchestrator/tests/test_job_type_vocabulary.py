"""job_type is the CRAWLER's vocabulary, not the catalogue's.

The catalogue calls a site `manufacturer_ir`. The crawler validates job_type against a closed set
and 422s anything outside it. Passing the category straight through therefore produces a job that
dispatches, is rejected, and crawls nothing — with no error anywhere the operator looks.

That is exactly what had happened: all 508 enrolled re-crawl jobs carried job_type
"manufacturer_ir". Every one would have failed on its first tick. They were all still `pending`
with next_run six days out, so it had not surfaced yet — the schedule looked perfectly healthy.

These tests are cheap and exist because the failure is silent.
"""
from __future__ import annotations

import json
from pathlib import Path

from mallory_orchestrator import jobgen

# The crawler's own literal set, from the 422 it returns:
#   "Input should be 'news', 'tender', 'profile', 'spec' or 'patent_aux'"
CRAWLER_ACCEPTS = {"news", "tender", "profile", "spec", "patent_aux"}


def test_every_catalogue_category_maps_into_the_crawlers_vocabulary():
    cats = ["manufacturer_ir", "tender_portal", *jobgen.NEWS_CATEGORIES, None, "", "something_new"]
    for c in cats:
        assert jobgen.job_type_for(c) in CRAWLER_ACCEPTS, f"{c!r} -> {jobgen.job_type_for(c)!r}"


def test_a_manufacturer_becomes_spec_not_its_category():
    assert jobgen.job_type_for("manufacturer_ir") == "spec"
    assert jobgen.job_type_for("tender_portal") == "tender"
    assert jobgen.job_type_for("trade_press") == "news"


def test_an_unknown_category_still_produces_a_crawlable_job():
    """Falling back to the category would 422; falling back to a valid type still crawls."""
    assert jobgen.job_type_for("category_invented_next_year") in CRAWLER_ACCEPTS


def _job_types_written_by(fn_src: str) -> list[str]:
    return [ln for ln in fn_src.splitlines() if '"job_type"' in ln]


def test_no_caller_writes_a_raw_category_into_job_type():
    """The three places that build a job dict must all go through the mapping."""
    api_src = Path(jobgen.__file__).with_name("api.py").read_text(encoding="utf-8")
    bad = [ln.strip() for ln in _job_types_written_by(api_src)
           if "job_type_for" not in ln and "category" in ln]
    assert not bad, f"a raw category is being written into job_type: {bad}"


def test_the_enrolled_recrawl_job_would_be_accepted():
    """Rebuild what the enrol path writes and check the field the crawler validates."""
    api_src = Path(jobgen.__file__).with_name("api.py").read_text(encoding="utf-8")
    assert '"job_type": jobgen.job_type_for(r.get("category"))' in api_src, \
        "the schedule-enrolment job no longer maps category -> job_type"


def test_the_idle_instance_dispatch_would_be_accepted():
    api_src = Path(jobgen.__file__).with_name("api.py").read_text(encoding="utf-8")
    assert '"job_type": jobgen.job_type_for(s.category)' in api_src, \
        "the idle-instance dispatch no longer maps category -> job_type"


def test_generate_uses_the_shared_mapping():
    src = Path(jobgen.__file__).read_text(encoding="utf-8")
    assert 'jt = job_type_for(s.category)' in src, "generate() has its own inline copy of the rule"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} JOB-TYPE TESTS PASSED")
    print(json.dumps({c: jobgen.job_type_for(c)
                      for c in ("manufacturer_ir", "tender_portal", "trade_press")}))
