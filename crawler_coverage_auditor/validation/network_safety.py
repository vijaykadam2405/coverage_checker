"""Public-target egress checks, including redirects and browser subrequests.

DNS is checked before requests, not pinned by the transport. A hostile
DNS rebinding environment still needs an outbound firewall / hardened proxy.
"""
import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit


def _local_development():
    return os.getenv('CRAWLSCOPE_ALLOW_PRIVATE_TARGETS') == '1'


def validate_target(url, *, resolve=False):
    parts = urlsplit(url)
    if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
        raise ValueError('Enter an HTTP(S) URL without embedded credentials')
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    if _local_development():
        return
    name = parts.hostname.lower().rstrip('.')
    if name == 'localhost' or name.endswith(('.localhost', '.local', '.internal')):
        raise ValueError('Private/local website targets are blocked')
    try:
        addresses = [str(ipaddress.ip_address(name))]
    except ValueError:
        addresses = []
    if resolve and not addresses:
        try:
            addresses = [item[4][0] for item in socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)]
        except OSError:
            raise ValueError('Website DNS resolution failed; public target cannot be verified') from None
    if any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError('Private, loopback, link-local or metadata addresses are blocked')


async def check_public_request(url):
    try:
        async with asyncio.timeout(5):
            await asyncio.to_thread(validate_target, url, resolve=True)
    except TimeoutError:
        raise ValueError('Website DNS safety check timed out') from None
