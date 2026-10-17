"""Read-only visual QA of a running local dashboard; never starts audit jobs."""
import argparse
import json
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--output', type=Path, help='Screenshot directory; defaults to a temporary folder outside the project')
    args = parser.parse_args()
    if args.output is None:
        args.output = Path(tempfile.mkdtemp(prefix='crawlscope-ui-review-'))
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors, layouts = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.url)
        expect(page.get_by_role('heading', name='Overview', exact=True)).to_be_visible()
        for width in (1600, 1366, 820, 390):
            page.set_viewport_size({'width': width, 'height': 1000 if width > 800 else 844})
            for view in ('Overview', 'URL inventory', 'Analysis', 'Activity', 'Control room'):
                if width <= 800:
                    page.get_by_role('button', name='Open navigation').click()
                page.get_by_role('button', name=view, exact=True).click()
                expect(page.get_by_role('heading', name=view, exact=True)).to_be_visible()
                page.screenshot(path=str(args.output/f'{width}-{view.lower().replace(" ","-")}.png'), full_page=True, animations='disabled')
                layouts.append({'width': width, 'view': view, 'overflow': page.evaluate('document.documentElement.scrollWidth > innerWidth')})
        print(json.dumps({'errors': errors, 'layouts': layouts, 'screenshots': str(args.output.resolve())}))
        browser.close()
