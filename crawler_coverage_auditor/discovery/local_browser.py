"""Local browser selection; never connect to the main crawler's browser service.

CamoFox's imported server uses Camoufox + Playwright Firefox. We use that same
engine directly, retaining CrawlScope's own routing, sessions and discovery.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def local_browser_dir() -> Path:
    return ROOT / 'data' / 'browser' / 'camoufox'


def browser_engine() -> str:
    engine = os.getenv('CRAWLSCOPE_BROWSER_ENGINE', 'camoufox').strip().lower()
    if engine not in {'chromium', 'camoufox'}:
        raise ValueError('CRAWLSCOPE_BROWSER_ENGINE must be chromium or camoufox')
    return engine


def camoufox_options() -> dict:
    from camoufox import pkgman
    from camoufox.addons import DefaultAddons

    # Camoufox 0.4.11 has no cache-directory environment option. This overrides
    # only this process's package manager constant, not files in site-packages
    # or the machine-wide browser cache. Never download during a live scan.
    pkgman.INSTALL_DIR = local_browser_dir()
    pkgman.camoufox_path(download_if_missing=False)
    return {
        'headless': True,
        'os': 'windows' if sys.platform == 'win32' else 'macos' if sys.platform == 'darwin' else 'linux',
        'humanize': True,
        'geoip': False,
        # Same no-runtime-addon-download choice as the imported server. No
        # Buster/CAPTCHA extensions, remote proxy, paid solver or REST endpoint.
        'exclude_addons': [DefaultAddons.UBO],
        'executable_path': pkgman.launch_path(),
        'firefox_user_prefs': {'pdfjs.disabled': True},
    }
