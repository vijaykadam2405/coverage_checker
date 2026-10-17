"""Tests must never consume production Corpus quota or load desktop credentials."""
import httpx
import pytest


@pytest.fixture(autouse=True)
def isolated_corpus_credentials(monkeypatch, tmp_path):
    # Tests explicitly select their engine; a desktop .env must not silently
    # replace existing Chromium fixtures with an optional local Firefox runtime.
    monkeypatch.setenv('CRAWLSCOPE_BROWSER_ENGINE', 'chromium')
    monkeypatch.setenv('CRAWLSCOPE_CORPUS_API_KEY', 'test-only-key')
    monkeypatch.delenv('CRAWLSCOPE_CORPUS_CA_BUNDLE', raising=False)
    monkeypatch.setenv('CRAWLSCOPE_DATA_DIR', str(tmp_path / 'runtime'))
    # Explicit opt-in for controlled localhost fixtures, never the production app.
    monkeypatch.setenv('CRAWLSCOPE_ALLOW_PRIVATE_TARGETS', '1')
    original = httpx.AsyncClient.send

    async def guarded_send(self, request, *args, **kwargs):
        if request.url.host == '187.127.134.12':
            raise AssertionError('Tests must not call the production Corpus API')
        return await original(self, request, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, 'send', guarded_send)
