import asyncio
import gzip

import httpx
import pytest

from app.tools import evidence


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def mock_network(monkeypatch, handler, addresses=None):
    calls = []
    original = httpx.AsyncClient

    async def resolve(host):
        calls.append(host)
        return addresses(host) if addresses else ["93.184.216.34"]

    monkeypatch.setattr(evidence, "_resolve", resolve)
    monkeypatch.setattr(evidence.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    return calls


@pytest.mark.parametrize("url", ["http://example.org/", "https://localhost/", "https://x.internal/",
                                "https://user:secret@example.org/", "https://example.org:8443/", "file:///etc/passwd"])
def test_disallowed_urls_never_connect(monkeypatch, url):
    def handler(request):
        pytest.fail("Disallowed target reached network")
    mock_network(monkeypatch, handler)
    with pytest.raises(evidence.EvidenceError):
        asyncio.run(evidence.fetch_url(url))


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "192.168.0.1", "100.64.0.1", "::1",
                                    "::ffff:127.0.0.1", "64:ff9b::a00:1", "224.0.0.1"])
def test_private_or_special_dns_answer_is_blocked(monkeypatch, address):
    mock_network(monkeypatch, lambda request: pytest.fail("SSRF request sent"), lambda host: [address])
    with pytest.raises(evidence.EvidenceError, match="public"):
        asyncio.run(evidence.fetch_url("https://public-looking.example/"))


def test_mixed_public_private_dns_answer_fails_closed(monkeypatch):
    mock_network(monkeypatch, lambda request: pytest.fail("SSRF request sent"), lambda host: ["93.184.216.34", "10.0.0.1"])
    with pytest.raises(evidence.EvidenceError):
        asyncio.run(evidence.fetch_url("https://example.org/"))


def test_fetch_pins_public_address_preserving_host_and_tls_name(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, stream=Chunks(
            b"<html><title>Evidence title</title><script>bad()</script><style>hidden</style><body>Useful &amp; verified text</body></html>"))
    calls = mock_network(monkeypatch, handler)
    result = asyncio.run(evidence.fetch_url("https://example.org/report#part"))
    assert calls == ["example.org"]
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "example.org"
    assert seen[0].headers["cookie"] == ""
    assert seen[0].extensions["sni_hostname"] == "example.org"
    assert result["url"] == "https://example.org/report"
    assert result["title"] == "Evidence title"
    assert "bad()" not in result["excerpt"] and "Useful & verified text" in result["excerpt"]
    assert len(result["content_hash"]) == 64


def test_redirect_to_metadata_blocked_before_second_request(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://metadata.google.internal/computeMetadata/v1/"})
    mock_network(monkeypatch, handler)
    with pytest.raises(evidence.EvidenceError, match="internal"):
        asyncio.run(evidence.fetch_url("https://example.org/"))
    assert len(seen) == 1


def test_redirect_loop_stops(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "/loop"})
    mock_network(monkeypatch, handler)
    with pytest.raises(evidence.EvidenceError, match="Redirect limit"):
        asyncio.run(evidence.fetch_url("https://example.org/"))
    assert len(seen) == evidence.MAX_REDIRECTS + 1


@pytest.mark.parametrize("headers,chunks", [
    ({"content-type": "text/html", "content-length": str(evidence.MAX_BYTES + 1)}, [b"x"]),
    ({"content-type": "text/plain"}, [b"x" * evidence.MAX_BYTES, b"x"]),
    ({"content-type": "text/html", "content-encoding": "gzip"}, [b"compressed"]),
    ({"content-type": "application/octet-stream"}, [b"binary"]),
])
def test_response_limits_are_enforced(monkeypatch, headers, chunks):
    mock_network(monkeypatch, lambda request: httpx.Response(200, headers=headers, stream=Chunks(*chunks)))
    with pytest.raises(evidence.EvidenceError):
        asyncio.run(evidence.fetch_url("https://example.org/"))


def test_gzip_is_bounded_after_decompression(monkeypatch):
    payload = gzip.compress(b"x" * (evidence.MAX_BYTES + 1))
    mock_network(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/plain", "content-encoding": "gzip"},
                                                          stream=Chunks(payload)))
    with pytest.raises(evidence.EvidenceError, match="Expanded"):
        asyncio.run(evidence.fetch_url("https://example.org/"))


def test_regular_gzip_source_is_read(monkeypatch):
    payload = gzip.compress(b"Useful source text")
    mock_network(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/plain", "content-encoding": "gzip"},
                                                          stream=Chunks(payload)))
    assert asyncio.run(evidence.fetch_url("https://example.org/"))["excerpt"] == "Useful source text"


def test_total_deadline_covers_resolution(monkeypatch):
    async def slow(host):
        await asyncio.sleep(1)
        return ["93.184.216.34"]
    monkeypatch.setattr(evidence, "_resolve", slow)
    with pytest.raises(TimeoutError):
        asyncio.run(evidence.fetch_url("https://example.org/", timeout_s=0.01))


def test_collection_preserves_partial_success_and_honest_search_status(monkeypatch):
    async def fetch(url, **kwargs):
        if "bad" in url:
            raise evidence.EvidenceError("Blocked source")
        return {"id": "web-good", "url": url, "excerpt": "Source text", "source_kind": "web"}
    monkeypatch.setattr(evidence, "fetch_url", fetch)
    result = asyncio.run(evidence.collect_evidence("Read https://good.example/ and https://bad.example/", "User background",
                                                  [{"id": "f1", "name": "data.csv", "text": "a,b\n1,2", "kind": "text"}]))
    assert len(result["items"]) == 3 and len(result["issues"]) == 1
    assert result["searched"] is False
    assert result["items"][1]["source_kind"] == "attachment"
    assert result["items"][2]["source_kind"] == "user_material"
