"""Run the local React dashboard: python dashboard.py."""
import os

import uvicorn

from crawler_coverage_auditor.web.api import create_app

if __name__ == '__main__':
    uvicorn.run(create_app(), host='127.0.0.1', port=int(os.getenv('CRAWLSCOPE_PORT', '8000')))
