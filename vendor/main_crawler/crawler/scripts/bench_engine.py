"""Benchmark the async multi-tab engine — finds the real (W, T) sweet spot on THIS machine.

Sweeps browser count (W) x tabs-per-browser (T), runs a fixed render-everything workload for
each cell, and samples CPU% + peak RAM across the Chromium process tree (psutil). Prints a
table of pages/sec vs CPU vs RAM so you pick the cell with max throughput at CPU<~90% / RAM in
budget — turning the "~140 tabs" theory into a measured number.

Run inside the crawler container (playwright + chromium installed there):
  docker exec mallory-crawler-api-1 python scripts/bench_engine.py
Env overrides: BENCH_W=4,8,12  BENCH_T=8,12  BENCH_MAX_PAGES=30
"""
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import psutil

from crawler.models import Job
from crawler.async_engine import run_batch_async
from crawler.seed import load_seed

# Fixed workload: real, reliably-crawlable hosts (trade press = plain HTML + deep enough).
HOSTS = [
    ("https://www.armyrecognition.com/news/army-news", ["artillery", "howitzer", "155mm", "vehicle", "missile", "drone", "contract", "defence"]),
    ("https://www.defensenews.com/land/", ["artillery", "vehicle", "contract", "award", "missile"]),
    ("https://www.shephardmedia.com/news/landwarfareintl/", ["artillery", "armoured", "vehicle", "contract"]),
    ("https://www.navalnews.com/", ["naval", "ship", "submarine", "missile", "contract"]),
    ("https://www.overtdefense.com/", ["artillery", "rifle", "vehicle", "defence", "contract"]),
    ("https://www.thedefensepost.com/", ["defence", "military", "contract", "missile", "vehicle"]),
    ("https://www.militaryaerospace.com/", ["radar", "sensor", "avionics", "defence", "contract"]),
    ("https://www.defenceweb.co.za/", ["defence", "army", "vehicle", "contract", "Africa"]),
    ("https://www.edrmagazine.eu/", ["artillery", "vehicle", "defence", "missile", "contract"]),
    ("https://www.militaryafrica.com/", ["defence", "army", "vehicle", "contract"]),
    ("https://www.defenseworld.net/", ["defence", "contract", "missile", "vehicle", "artillery"]),
    ("https://idrw.org/category/indian-defense-news/", ["artillery", "K9", "ATAGS", "Tata", "DRDO", "contract"]),
]


def _jobs(max_pages, max_depth=2):
    return [
        Job(job_id=f"bench_{i}", job_type="news", seed_urls=[u], keywords=kw,
            target_entity=None, max_pages=max_pages, max_depth=max_depth,
            same_domain_only=True, render_js=True, freshness_days=None,
            capture=["html", "text"])
        for i, (u, kw) in enumerate(HOSTS)
    ]


class Sampler(threading.Thread):
    """Sample CPU% + total RSS across this process + all Chromium children, every 0.5s."""

    def __init__(self):
        super().__init__(daemon=True)
        self._stop_evt = threading.Event()   # not _stop — Thread already uses that name
        self.peak_cpu = 0.0
        self.peak_ram_gb = 0.0
        self._ncpu = psutil.cpu_count() or 1

    def run(self):
        psutil.cpu_percent(interval=None)  # prime
        while not self._stop_evt.is_set():
            time.sleep(0.5)
            # system-wide CPU% (0-100 across all cores) is the clearest "am I using the box"
            cpu = psutil.cpu_percent(interval=None)
            self.peak_cpu = max(self.peak_cpu, cpu)
            try:
                root = psutil.Process()
                procs = [root] + root.children(recursive=True)
                rss = sum(p.memory_info().rss for p in procs if p.is_running())
                self.peak_ram_gb = max(self.peak_ram_gb, rss / 1e9)
            except Exception:
                pass

    def stop(self):
        self._stop_evt.set()
        self.join(timeout=3)


def main():
    Ws = [int(x) for x in os.environ.get("BENCH_W", "4,8,12").split(",")]
    Ts = [int(x) for x in os.environ.get("BENCH_T", "8,12").split(",")]
    max_pages = int(os.environ.get("BENCH_MAX_PAGES", "30"))
    os.environ.setdefault("CRAWLER_ALLOW_NETWORK", "1")
    os.environ.setdefault("CRAWLER_PREFER_FIXTURES", "0")
    os.environ["CRAWLER_ENGINE_IDLE_S"] = "45"     # don't hang a cell forever

    seed = load_seed()
    jobs = _jobs(max_pages)
    total_cap = len(jobs) * max_pages

    print(f"host_cpu_count={psutil.cpu_count()}  ram_total={psutil.virtual_memory().total/1e9:.0f}GB")
    print(f"workload: {len(jobs)} hosts x max_pages={max_pages} (cap {total_cap} pages), render_js=ALL\n")
    print(f"{'W':>3} {'T':>3} {'tabs':>5} {'fetched':>8} {'kept':>5} {'pages/s':>8} {'cpu%':>6} {'ram_gb':>7} {'sec':>6}")
    print("-" * 62)

    rows = []
    for W in Ws:
        for T in Ts:
            os.environ["CRAWLER_BROWSERS"] = str(W)
            os.environ["CRAWLER_TABS_PER_BROWSER"] = str(T)
            # each fresh cell: wipe dedup so it actually crawls
            dbp = Path("data/crawl_history.sqlite")
            if dbp.exists():
                dbp.unlink()
            samp = Sampler()
            samp.start()
            t0 = time.perf_counter()
            results = run_batch_async(jobs, forward=False, seed=seed)
            el = time.perf_counter() - t0
            samp.stop()
            fetched = sum(r["summary"]["fetched"] for r in results)
            kept = sum(r["summary"]["kept"] for r in results)
            pps = fetched / el if el else 0
            rows.append((W, T, W * T, fetched, kept, pps, samp.peak_cpu, samp.peak_ram_gb, el))
            print(f"{W:>3} {T:>3} {W*T:>5} {fetched:>8} {kept:>5} {pps:>8.2f} {samp.peak_cpu:>6.0f} "
                  f"{samp.peak_ram_gb:>7.1f} {el:>6.0f}")
            time.sleep(5)  # cooldown so peaks don't cross-contaminate

    # recommend: max pages/s where cpu<90 and ram<55
    ok = [r for r in rows if r[6] < 90 and r[7] < 55]
    best = max(ok or rows, key=lambda r: r[5])
    print("-" * 62)
    print(f"RECOMMEND: W={best[0]} T={best[1]} ({best[2]} tabs) "
          f"-> {best[5]:.2f} pages/s at {best[6]:.0f}% CPU, {best[7]:.1f}GB RAM")
    print(f"  set: CRAWLER_BROWSERS={best[0]} CRAWLER_TABS_PER_BROWSER={best[1]}")


if __name__ == "__main__":
    main()
