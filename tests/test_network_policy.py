import ssl

import httpcore2
import httpx2
import pytest

from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.network import (
    PublicHTTPSTransport,
    PublicNetworkBackend,
    canonical_host,
    validate_download_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://files.example.test/a",
        "https://user:pass@files.example.test/a",
        "https://files.example.test:444/a",
        "https://files.example.test/a#fragment",
        "https://files.example.test.evil.test/a",
        "https://127.0.0.1/a",
        "https://files.example.test\\@evil.test/a",
        "https://files.example.test/a\nb",
        "file:///etc/passwd",
        "https://[::1]/a",
    ],
)
def test_download_urls_are_exact_https_without_credentials(url):
    with pytest.raises(TalkVideoError, match="unsafe_download_url"):
        validate_download_url(url, ("files.example.test",))


@pytest.mark.parametrize(
    "host",
    [
        "*.example.test",
        "localhost",
        "x.local",
        "127.1",
        "0x7f.0.0.1",
        "2130706433",
        "coefont.cloud",
        "hiroyuki.coefont.cloud",
        "files.example.test.",
        "https://files.example.test",
    ],
)
def test_policy_does_not_accept_wildcards_local_addresses_or_maker(host):
    with pytest.raises(ValueError):
        canonical_host(host)


class RecordingStream(httpcore2.AsyncNetworkStream):
    def __init__(self):
        self.response = b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nwav"
        self.writes = []
        self.tls_hostname = None
        self.secure_context = False

    async def read(self, max_bytes, timeout=None):  # noqa: ASYNC109 - SDK stream contract.
        chunk, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return chunk

    async def write(self, buffer, timeout=None):  # noqa: ASYNC109 - SDK stream contract.
        self.writes.append(buffer)

    async def aclose(self):
        return None

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):  # noqa: ASYNC109
        self.tls_hostname = server_hostname
        self.secure_context = (
            ssl_context.check_hostname and ssl_context.verify_mode == ssl.CERT_REQUIRED
        )
        return self

    def get_extra_info(self, info):
        return None


class RecordingBackend(httpcore2.AsyncNetworkBackend):
    def __init__(self):
        self.hosts = []
        self.stream = RecordingStream()

    async def connect_tcp(self, host, port, **kwargs):
        self.hosts.append((host, port))
        return self.stream


async def test_dns_is_validated_then_pinned_but_tls_uses_original_name():
    resolutions = []

    async def resolve(host, port):
        resolutions.append((host, port))
        return ["93.184.216.34"]  # No connection is made: RecordingBackend is in-memory.

    backend = RecordingBackend()
    transport = PublicHTTPSTransport(
        ("files.example.test",),
        network_backend=PublicNetworkBackend(resolve, backend),
    )
    try:
        response = await transport.handle_async_request(
            httpx2.Request("GET", "https://files.example.test/audio")
        )
        assert await response.aread() == b"wav"
        await response.aclose()
        assert resolutions == [("files.example.test", 443)]
        assert backend.hosts == [("93.184.216.34", 443)]
        assert backend.stream.tls_hostname == "files.example.test"
        assert backend.stream.secure_context
    finally:
        await transport.aclose()


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["169.254.169.254"],
        ["::1"],
        ["fc00::1"],
        ["::ffff:127.0.0.1"],
        ["93.184.216.34", "192.168.1.1"],
        ["224.0.0.1"],
        [],
    ],
)
async def test_private_or_mixed_dns_answers_never_connect(addresses):
    async def resolve(host, port):
        return addresses

    backend = RecordingBackend()
    with pytest.raises(TalkVideoError, match="unsafe_download_url"):
        await PublicNetworkBackend(resolve, backend).connect_tcp("files.example.test", 443)
    assert backend.hosts == []


async def test_download_transport_rejects_auth_headers_before_network():
    backend = RecordingBackend()

    async def resolve(host, port):
        raise AssertionError("Credential-bearing download must not resolve or connect.")

    transport = PublicHTTPSTransport(
        ("files.example.test",), network_backend=PublicNetworkBackend(resolve, backend)
    )
    try:
        for header in ["Authorization", "Cookie", "X-Coefont-Date", "X-Coefont-Content"]:
            with pytest.raises(TalkVideoError, match="unsafe_download_url"):
                await transport.handle_async_request(
                    httpx2.Request(
                        "GET", "https://files.example.test/a", headers={header: "fixture"}
                    )
                )
    finally:
        await transport.aclose()
