"""Resources deliberately excluded from CrawlScope inventories."""
from urllib.parse import unquote, urlsplit


def excluded_resource_url(url: str | None) -> bool:
    """Ignore EOT fonts, including encoded names and versioned font URLs.

    Only the path is considered: an HTML search/query mentioning an EOT file
    is not itself a font. Other resource extensions remain unchanged.
    """
    if not url:
        return False
    try:
        return unquote(urlsplit(url.strip()).path).lower().endswith('.eot')
    except ValueError:
        return False
