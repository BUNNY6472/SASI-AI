"""Fetch explicit user URLs without giving the model network or credential access.

DNS is validated and the connection is pinned to the validated public IP. TLS
still verifies the original hostname. Redirects repeat validation. We never
follow links found in pages, send cookies, inherit proxies, or execute markup.
This is URL retrieval, not general web search or a claim of source correctness.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
import time
import zlib
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

MAX_BYTES = 512 * 1024
MAX_EXCERPT = 24_000
MAX_REDIRECTS = 3
MAX_SOURCES = 5
URL_PATTERN = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
ALLOWED_TYPES = {"text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/json"}


class EvidenceError(ValueError):
    """A source cannot be read within the evidence tool's policy or limits."""


def canonical_url(url: str) -> str:
    """Canonical identity used for fetched-source/citation matching."""
    p = urlsplit(url)
    if p.username is not None or p.password is not None:
        raise EvidenceError("URLs containing credentials are not allowed")
    host = (p.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    if not host:
        raise EvidenceError("URL has no hostname")
    port = p.port
    authority = f"[{host}]" if ":" in host else host
    if port and port != (443 if p.scheme.lower() == "https" else 80):
        authority += f":{port}"
    return urlunsplit((p.scheme.lower(), authority, p.path or "/", p.query, ""))


def urls_in_text(text: str, limit: int = 100) -> list[str]:
    urls: list[str] = []
    for match in URL_PATTERN.finditer(text[:500_000]):
        url = match.group().rstrip(".,;!?)\u005d}")
        if url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def _public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # Exclude transition/translation ranges as well as mapped private IPv4.
        return ip in ipaddress.ip_network("2000::/3") and not ip.sixtofour and not ip.teredo
    return True


async def _resolve(host: str) -> list[str]:
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        rows = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(row[4][0] for row in rows))


async def _target(url: str) -> tuple[str, str, str]:
    if len(url) > 4096 or any(ord(c) < 32 or c == "\\" for c in url):
        raise EvidenceError("Invalid or oversized URL")
    canonical = canonical_url(url)
    p = urlsplit(canonical)
    if p.scheme != "https" or p.port not in (None, 443):
        raise EvidenceError("Only public HTTPS URLs on port 443 are supported")
    host = p.hostname or ""
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise EvidenceError("Local and internal hosts are blocked")
    addresses = await _resolve(host)
    if not addresses or len(addresses) > 32 or not all(_public_ip(ip) for ip in addresses):
        raise EvidenceError("Source must resolve exclusively to public internet addresses")
    ip = addresses[0]
    authority = f"[{ip}]" if ":" in ip else ip
    pinned = urlunsplit(("https", authority, p.path or "/", p.query, ""))
    return canonical, host, pinned


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self.hidden = 0
        self.in_title = False
        self.size = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.hidden or self.size >= MAX_EXCERPT:
            return
        value = " ".join(data.split())[:MAX_EXCERPT - self.size]
        if value:
            self.parts.append(value)
            self.size += len(value) + 1
            if self.in_title:
                self.title.append(value[:300])


def _extract(raw: bytes, content_type: str) -> tuple[str, str]:
    text = raw.decode("utf-8", errors="replace")
    if content_type in {"text/html", "application/xhtml+xml"}:
        parser = _Text()
        parser.feed(text)
        return " ".join(parser.title)[:300], "\n".join(parser.parts)[:MAX_EXCERPT]
    return "", text[:MAX_EXCERPT]


async def _fetch(url: str) -> dict:
    original = url
    async with httpx.AsyncClient(timeout=httpx.Timeout(6.0), trust_env=False, follow_redirects=False,
                                limits=httpx.Limits(max_connections=1)) as client:
        for hop in range(MAX_REDIRECTS + 1):
            canonical, host, pinned = await _target(url)
            headers = {"Host": f"[{host}]" if ":" in host else host,
                       "User-Agent": "SASI-Evidence/1.0", "Accept-Encoding": "identity", "Cookie": "",
                       "Accept": "text/html,text/plain,text/markdown,application/json"}
            async with client.stream("GET", pinned, headers=headers, extensions={"sni_hostname": host}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if hop == MAX_REDIRECTS or not response.headers.get("location"):
                        raise EvidenceError("Redirect limit reached or missing redirect destination")
                    url = urljoin(canonical, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise EvidenceError(f"Source returned HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type not in ALLOWED_TYPES:
                    raise EvidenceError("Unsupported source format; provide a text or HTML source")
                encoding = response.headers.get("content-encoding", "identity").lower()
                if encoding not in ("", "identity", "gzip", "deflate"):
                    raise EvidenceError("Unsupported source compression")
                decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in ("gzip", "deflate") else None
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_BYTES:
                    raise EvidenceError("Source exceeds the 512 KiB retrieval limit")
                data, raw_size = bytearray(), 0
                async for chunk in response.aiter_raw():
                    raw_size += len(chunk)
                    if raw_size > MAX_BYTES:
                        raise EvidenceError("Source exceeds the 512 KiB retrieval limit")
                    try:
                        # max_length bounds expansion before allocating decompressed output.
                        decoded = decoder.decompress(chunk, MAX_BYTES - len(data) + 1) if decoder else chunk
                    except zlib.error as exc:
                        raise EvidenceError("Source has invalid compressed content") from exc
                    if len(data) + len(decoded) > MAX_BYTES:
                        raise EvidenceError("Expanded source exceeds the 512 KiB retrieval limit")
                    data.extend(decoded)
                if decoder and (not decoder.eof or decoder.unused_data):
                    raise EvidenceError("Incomplete or concatenated compressed content is unsupported")
            title, excerpt = _extract(bytes(data), content_type)
            if not excerpt.strip():
                raise EvidenceError("Source contains no readable text")
            return {"id": "web-" + hashlib.sha256(canonical.encode()).hexdigest()[:12],
                    "url": canonical, "requested_url": canonical_url(original), "title": title or canonical,
                    "excerpt": excerpt, "retrieved_at": time.time(), "source_kind": "web",
                    "content_hash": hashlib.sha256(bytes(data)).hexdigest(),
                    "truncated": len(excerpt) >= MAX_EXCERPT}
    raise EvidenceError("Source unavailable")


async def fetch_url(url: str, *, timeout_s: float = 10) -> dict:
    """Read one source. Raises EvidenceError/timeout/network error on failure."""
    return await asyncio.wait_for(_fetch(url), timeout=max(0.01, min(timeout_s, 15)))


async def collect_evidence(request: str, materials: str = "", files=None, *, deadline_s: float = 12,
                           max_sources: int = MAX_SOURCES) -> dict:
    """Return {items, issues, searched}; no paid APIs or generated URL fetching.

    Files can be StoredFile objects or mappings. Text extracts retain a clear
    attachment label; supplied materials do not become independent web evidence.
    Errors never masquerade as fetched evidence. Entire network stage is bounded.
    """
    items, issues = [], []
    urls = urls_in_text((request or "")[:250_000] + "\n" + (materials or "")[:250_000], limit=MAX_SOURCES + 1)
    count = max(0, min(max_sources, MAX_SOURCES))
    if len(urls) > count:
        issues.append({"url": "", "status": "skipped", "summary": "Additional URLs exceeded the source limit"})

    async def one(url):
        try:
            return await fetch_url(url, timeout_s=deadline_s)
        except Exception as exc:  # source errors are reported without aborting the run
            summary = str(exc) if isinstance(exc, EvidenceError) else f"Source could not be retrieved ({type(exc).__name__})"
            issues.append({"url": url, "status": "unavailable", "summary": summary[:300]})
            return None

    fetched = await asyncio.gather(*(one(url) for url in urls[:count]))
    items.extend(item for item in fetched if item)
    for i, f in enumerate((files or [])[:10]):
        get = f.get if isinstance(f, dict) else lambda k, default=None: getattr(f, k, default)
        text = get("text", "") or ""
        if not text or get("kind", "text") != "text":
            continue
        excerpt = text[:16_000]
        fid = str(get("id", i))[:100]
        items.append({"id": f"attachment-{fid}", "url": f"attachment:{fid}", "title": str(get("name", "Attachment"))[:300],
                      "excerpt": excerpt, "retrieved_at": time.time(), "source_kind": "attachment",
                      "content_hash": hashlib.sha256(excerpt.encode()).hexdigest(), "truncated": len(text) > len(excerpt)})
    if materials.strip():
        excerpt = materials[:16_000]
        items.append({"id": "user-materials", "url": "", "title": "User-supplied background (unverified)",
                      "excerpt": excerpt, "retrieved_at": time.time(), "source_kind": "user_material",
                      "content_hash": hashlib.sha256(excerpt.encode()).hexdigest(), "truncated": len(materials) > len(excerpt)})
    return {"items": items, "issues": issues, "searched": False}
