"""Apply the VETTED cross-domain moves. An explicit allowlist, not a bulk accept.

Each pair below was checked against what the target actually serves. The rejected ones are recorded
here too, because "we looked and said no" is information the next person needs — otherwise a later
recheck re-proposes them and someone accepts them by default.

REJECTED, deliberately:
    pgz.pl              -> elixscent.com                 a perfume shop (Polish Armaments Group!)
    sentekglobal.com    -> deloitte.com/services         not the company
    sener-aerospace.com -> monitor.geanetondemand.com    junk redirect
    prignitzer.de       -> nordkurier.de/regional/...    regional news, not the manufacturer
    eurotrophy.com      -> deki.dk                       medals and trophies, not defence
    raesystems.com      -> automation.honeywell.com/...  CORRECT (Honeywell bought RAE) but it
                                                         points into a site already carrying 2,190
                                                         unresolved URLs — not worth the blast
"""
import os
from urllib.parse import urlsplit
from mallory_orchestrator import scheduler as sch
from mallory_orchestrator.db import SessionLocal
from mallory_orchestrator.models import Source, ScheduledJob
from sqlalchemy import select

APPLY = os.environ.get("APPLY") == "1"

MOVES = {
    # rebrands and acquisitions, target verified to serve the company's real site
    "maxar.com":             "https://vantor.com/",
    "patria.fi":             "https://www.patriagroup.com/",
    "atlas-elektronik.com":  "https://www.tkmsgroup.com/atlas-elektronik",
    "bluefinrobotics.com":   "https://gdmissionsystems.com/underwater-vehicles/bluefin-robotics",
    "elbitsystems-us.com":   "https://www.elbitamerica.com/",
    "elettronicagroup.com":  "https://www.eltgroup.net/",
    "cronsystems.com":       "https://cronai.ai/",
    "faudi.com":             "https://www.faudi.de/",
    "irvingshipbuilding.com":"https://shipsforcanada.ca/",
    "kayser-threde.com":     "https://www.ohb.de/",
    "luerssen.de":           "https://www.lurssen.com/de/",
    "mwgroup.fi":            "https://mw.group/",
    "norma.cc":              "https://www.norma-ammunition.com/",
    "opkrt.ru":              "https://ruselectronics.ru/",
    "pae.com":               "https://www.amentum.com/",
    "survey-copter.com":     "https://www.airbus.com/en/products-services/defence/uas",
    "ultra.group":           "https://www.cobhamultra.com/",
    "umoe.no":               "https://www.umoe.com/",
    "vinnell.com":           "https://www.northropgrumman.com",
    "vt-group.com":          "https://vtgdefense.com/",
    "wescam.com":            "https://www.l3harris.com",
    "wfel.com":              "https://knds.com/en/subsidiaries/knds-defence-uk",
    "airrobot.de":           "https://airrobot.com",
    "bigbangboom.solutions": "https://www.bigbangboom.com/",
    "skylock1.com":          "https://skylocksys.com/",
}

db = SessionLocal()
now = sch.utcnow()
applied, missing, unchanged = [], [], []
for dom, url in sorted(MOVES.items()):
    s = db.get(Source, dom)
    if s is None:
        missing.append(dom); continue
    if (s.seed_urls or [""])[0].rstrip("/") == url.rstrip("/"):
        unchanged.append(dom); continue
    if APPLY:
        s.seed_urls = [url]
        for sj in db.scalars(select(ScheduledJob)).all():
            hosts = {(urlsplit(str(u)).hostname or "").lower().removeprefix("www.")
                     for u in ((sj.job or {}).get("seed_urls") or [])}
            if dom.removeprefix("www.") in hosts or sj.name.lower().removeprefix("www.") == dom:
                job = dict(sj.job or {}); job["seed_urls"] = [url]
                sj.job = job
                sj.next_run = now
    applied.append((dom, url))

if APPLY:
    db.commit()
print("%s %d move(s):" % ("APPLIED" if APPLY else "WOULD APPLY", len(applied)))
for d, u in applied:
    print("   %-24s -> %s" % (d, u))
if unchanged: print("already set:", len(unchanged))
if missing:   print("not in catalogue:", ", ".join(missing))
