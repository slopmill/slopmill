# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetching from the public internet on the author's behalf: images for the preview, pages
for research. Every hop is checked to resolve to a public address, so a design or a search
result can never point the server at this machine or the home network."""
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit


class FetchRefused(Exception):
    pass


def _check_public(host):
    """Refuse a host that resolves to anything but a public address. An uploaded design
    names the address the image proxy fetches from; without this it could point the
    server at this machine or the home network."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        raise FetchRefused(f"cannot resolve {host!r}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise FetchRefused(f"{host} is a private or local address; an uploaded design "
                               f"may only fetch images from the public internet")


MAX_ASSET_BYTES = 50_000_000


class Fetched:
    def __init__(self, status_code, content, content_type="", url=""):
        self.status_code, self.content = status_code, content
        self.content_type, self.url = content_type, url


def fetch_public(url, allow_private=False, hops=3, max_bytes=None, timeout=20,
                 user_agent="slopmill/0.2 (editor preview)"):
    """GET url, following at most `hops` redirects, each one checked the same way, and the
    address actually connected to checked again (a name that resolves to a public address
    for the check and a private one for the connection gets nothing). The body is read in
    pieces and abandoned past max_bytes (default 50MB)."""
    import httpx
    cap = max_bytes or MAX_ASSET_BYTES
    for _ in range(hops + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or (not allow_private and parts.scheme != "https"):
            raise FetchRefused("only https addresses are fetched")
        if not allow_private:
            _check_public(parts.hostname or "")
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=False,
                          headers={"User-Agent": user_agent}) as r:
            if not allow_private:
                _check_peer(r)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200:
                return Fetched(r.status_code, b"", ctype, url)
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > cap:
                    raise FetchRefused(f"the file is over {cap // 1_000_000}MB")
            return Fetched(200, bytes(buf), ctype, url)
    raise FetchRefused("too many redirects")


def _check_peer(response):
    """The address the connection really went to must be public too."""
    stream = response.extensions.get("network_stream")
    addr = stream.get_extra_info("server_addr") if stream is not None else None
    if not addr:
        return            # the transport does not say; the name check above still ran
    ip = ipaddress.ip_address(str(addr[0]).split("%")[0])
    if not ip.is_global:
        raise FetchRefused("the address connected to is private or local")
