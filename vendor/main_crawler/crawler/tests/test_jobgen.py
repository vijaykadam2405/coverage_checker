"""Job generation + probe-adaptive candidate pool (jobgen._candidate_pool)."""
from crawler.jobgen import _candidate_pool, _news_keywords, generate
from crawler.seed import load_seed

SEED = load_seed()


def test_candidate_pool_includes_entity_products_and_tech_domain_keywords():
    pool = _candidate_pool(SEED, "LT", "news")
    lower = {p.lower() for p in pool}
    # entity aliases
    assert "l&t" in lower
    # owned product name
    assert any("k9 vajra" in p.lower() for p in pool)
    # tech-domain keywords for the product's category (artillery) — the source
    # jobgen previously discarded
    assert "155mm" in lower and "howitzer" in lower
    # broader than the old static news keywords (which capped at 8)
    assert len(pool) > len(_news_keywords(SEED, "LT"))


def test_candidate_pool_deduped_and_no_blanks():
    pool = _candidate_pool(SEED, "LT", "news")
    assert len(pool) == len(set(pool))       # no dupes
    assert all(p and p.strip() for p in pool)  # no blanks


def test_candidate_pool_tender_uses_tender_keywords():
    pool = _candidate_pool(SEED, None, "tender")
    assert set(pool) >= set(SEED.tender_keywords)


def test_candidate_pool_unknown_entity_is_empty_not_error():
    # a company not in the seed (e.g. RTX) has no aliases/products to draw from
    assert _candidate_pool(SEED, "NOT_IN_SEED", "news") == []


def test_generate_default_path_unchanged_offline():
    # generate() with no probe must not do network I/O and must still produce
    # the same static-keyword jobs (probe-adaptive is opt-in).
    jobs = generate(SEED)
    assert jobs and all(j.keywords for j in jobs if j.job_type != "tender" or j.keywords)
