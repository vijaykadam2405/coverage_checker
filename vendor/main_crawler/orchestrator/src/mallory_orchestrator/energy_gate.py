"""The "energy" keyword gate — diesel gensets, for the KOEL (Kirloskar Oil Engines) client.

KOEL is a client; we crawl KOEL + competitor + genset-news URLs and want to keep only pages actually
about diesel gensets — KOEL itself, its competitors' moves, and the genset market — not the rest of a
general-news or corporate site.

Applied as a job's ``require_keywords`` (crawler/gate.py), which REPLACES the global defence corpus
for that job: a page is kept iff it names one of these. So this gate is independent of the defence
watchlist — a KOEL crawl needs no defence terms, and a defence crawl keeps no genset pages.

A diesel genset is a diesel engine coupled to an electric generator for backup or prime power; the
India market runs ~5 kVA portable units to 2,000+ kVA industrial sets (Cummins India, Mahindra
Powerol, KOEL/Kirloskar). The keywords below cover the client, its named competitors, the other
makers active here, and the generic genset vocabulary that catches an article regardless of brand.

One flat list — crawler.keywords.from_list() builds the trie; FlashText matches whole words, so
"kVA" hits "500 kVA" but "genset" never matches inside another word.
"""
from __future__ import annotations

ENERGY_GATE_LABEL = "Energy — diesel genset / KOEL"

ENERGY_KEYWORDS: list[str] = [
    # ── KOEL (the client) + the Kirloskar group ──
    "KOEL", "KOEL Green", "Kirloskar Oil Engines", "Kirloskar", "Kirloskar Green",
    "Kirloskar Pneumatic",
    # ── competitors the client named ──
    "Cummins India", "Cummins", "Mahindra Powerol", "Powerol",
    # ── other diesel-genset makers active in the India market ──
    "Ashok Leyland", "Greaves Cotton", "Greaves", "Sudhir Power", "Jakson",
    "Sterling Generators", "Powerica", "Kohler", "Perkins", "Volvo Penta",
    "FG Wilson", "Baudouin", "HIMOINSA", "Deutz", "Yanmar",
    # ── diesel genset: product + generic terms (the real gate) ──
    "diesel genset", "diesel gensets", "diesel generator", "diesel generators",
    "genset", "gensets", "gen-set", "gen set", "generating set", "generating sets",
    "diesel generating set", "DG set", "DG sets",
    "generator", "generators", "power generator",
    "backup power", "power backup", "standby power", "prime power", "standby generator",
    "silent genset", "silent generator", "soundproof genset",
    "portable generator", "portable genset",
    "kVA", "kVA genset", "diesel engine", "power generation", "captive power",
    "CPCB IV", "CPCB IV+", "CPCB 4+", "gas genset", "generator rental", "power rental",
]


def _demo() -> None:
    # Data integrity: unique (case-insensitively), non-blank, and the must-haves are present.
    lowered = [k.lower() for k in ENERGY_KEYWORDS]
    assert len(lowered) == len(set(lowered)), \
        f"duplicate keyword: {[k for k in set(lowered) if lowered.count(k) > 1]}"
    assert all(k.strip() for k in ENERGY_KEYWORDS), "blank keyword in the list"
    for must in ("KOEL", "Kirloskar Oil Engines", "Cummins India", "Mahindra Powerol",
                 "diesel genset", "genset", "DG set", "kVA", "diesel engine"):
        assert must in ENERGY_KEYWORDS, f"missing must-have keyword {must!r}"
    print(f"ok — {len(ENERGY_KEYWORDS)} energy-gate keywords, all unique")


if __name__ == "__main__":
    _demo()
