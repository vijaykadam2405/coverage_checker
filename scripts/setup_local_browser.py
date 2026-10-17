"""Install a pinned browser into CrawlScope's data folder, not a shared cache.

Run with the project .venv after installing requirements-browser.txt. Downloads
only official software assets. Does not start a scan, a service or the crawler.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
VERSION, RELEASE = '152.0.4', 'beta.27'


def executable_in(directory: Path, pkgman) -> Path:
    if pkgman.OS_NAME == 'mac':
        return directory / 'Camoufox.app' / 'Contents' / 'MacOS' / 'camoufox'
    return directory / pkgman.LAUNCH_FILE[pkgman.OS_NAME]


def install() -> Path:
    from camoufox import pkgman

    target = ROOT / 'data' / 'browser' / 'camoufox'
    if (target / 'version.json').exists():
        record = json.loads((target / 'version.json').read_text(encoding='utf-8'))
        if record == {'version': VERSION, 'release': RELEASE} and executable_in(target, pkgman).is_file():
            print('CrawlScope local browser already installed. Shared cache untouched.')
            return target
        raise RuntimeError('A different local browser exists; preserve it before changing versions.')
    if target.exists():
        raise RuntimeError('An incomplete local browser exists; inspect it before retrying installation.')
    arch = pkgman.CamoufoxFetcher.get_platform_arch()
    name = f'camoufox-{VERSION}-{RELEASE}-{pkgman.OS_NAME}.{arch}.zip'
    response = requests.get(f'https://api.github.com/repos/daijro/camoufox/releases/tags/v{VERSION}-{RELEASE}', timeout=30)
    response.raise_for_status()
    asset = next((item for item in response.json()['assets'] if item['name'] == name), None)
    if not asset or not asset['browser_download_url'].startswith('https://github.com/daijro/camoufox/releases/download/'):
        raise RuntimeError('Pinned official browser asset not available for this platform')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='crawlscope-browser-', dir=target.parent) as temporary:
        archive = Path(temporary) / 'browser.zip'
        digest = hashlib.sha256()
        print(f'Downloading official {name} into CrawlScope only...', flush=True)
        with requests.get(asset['browser_download_url'], stream=True, timeout=(30, 60)) as download:
            download.raise_for_status()
            with archive.open('wb') as output:
                for chunk in download.iter_content(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
        if archive.stat().st_size != asset['size']:
            raise RuntimeError('Incomplete browser download; existing installations unchanged')
        if asset.get('digest') and asset['digest'] != 'sha256:' + digest.hexdigest():
            raise RuntimeError('Browser asset checksum mismatch')
        stage = Path(temporary) / 'unpacked'
        stage.mkdir()
        with zipfile.ZipFile(archive) as browser:
            for item in browser.infolist():
                destination = (stage / item.filename).resolve()
                if not destination.is_relative_to(stage.resolve()):
                    raise RuntimeError('Unsafe browser archive path')
            browser.extractall(stage)
        (stage / 'version.json').write_text(json.dumps({'version': VERSION, 'release': RELEASE}), encoding='utf-8')
        executable = executable_in(stage, pkgman)
        if not executable.is_file():
            raise RuntimeError('Browser executable missing after extraction')
        if sys.platform != 'win32':
            for artifact in stage.rglob('*'):
                if artifact.is_file():
                    os.chmod(artifact, 0o755)
        shutil.move(str(stage), str(target))
    print('Local browser ready. No browser service or main-crawler connection created.')
    return target


if __name__ == '__main__':
    install()
