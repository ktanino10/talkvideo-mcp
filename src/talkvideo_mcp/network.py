from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from urllib.parse import urlsplit

import httpcore2
import httpx2

from talkvideo_mcp.errors import TalkVideoError

SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)
Resolver = Callable[[str, int], Awaitable[list[str]]]


def unsafe_network() -> TalkVideoError:
    return TalkVideoError(
        "unsafe_download_url",
        "The URL or resolved network address is outside the explicit HTTPS download policy.",
        "Use operator-confirmed exact public hosts; never broaden trust automatically.",
        needs_user_action=True,
    )


def canonical_host(value: str) -> str:
    if (
        not value
        or len(value) > 253
        or value != value.strip()
        or value.endswith(".")
        or any(char in value for char in "/\\:@%*?#[]")
    ):
        raise ValueError("An exact DNS hostname without URL syntax or wildcards is required.")
    host = value.lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
        raise ValueError("Only explicit ASCII DNS hostnames are supported.")
    if (
        "." not in host
        or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+)){0,3}", host)
        or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            for label in host.split(".")
        )
        or host.endswith((".localhost", ".local", ".internal", ".lan"))
        or host in {"coefont.cloud", "hiroyuki.coefont.cloud"}
    ):
        raise ValueError("Local, wildcard, Maker, or unqualified hosts are not allowed.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("IP literals are not download hostnames.")


def validate_download_url(value: str, allowed_hosts: tuple[str, ...]) -> str:
    if not value or len(value) > 4096 or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise unsafe_network()
    try:
        parsed = urlsplit(value)
        host = canonical_host(parsed.hostname or "")
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.fragment
            or "\\" in value
            or host not in allowed_hosts
        ):
            raise unsafe_network()
    except ValueError as exc:
        raise unsafe_network() from exc
    return value


def public_address(value: str) -> bool:
    if "%" in value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


async def resolve_public(host: str, port: int) -> list[str]:
    records = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return list(dict.fromkeys(str(record[4][0]) for record in records))


class PublicNetworkBackend(httpcore2.AsyncNetworkBackend):
    """Validate DNS and connect to that exact IP; TLS still uses the original origin hostname."""

    def __init__(
        self,
        resolver: Resolver = resolve_public,
        backend: httpcore2.AsyncNetworkBackend | None = None,
    ) -> None:
        self.resolver = resolver
        self.backend = backend if backend is not None else httpcore2.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - HTTPcore's required backend signature.
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        if port != 443:
            raise unsafe_network()
        async with asyncio.timeout(min(timeout if timeout is not None else 5, 5)):
            addresses = await self.resolver(host, port)
            if not addresses or not all(public_address(address) for address in addresses):
                raise unsafe_network()
            return await self.backend.connect_tcp(
                addresses[0],
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - HTTPcore's required backend signature.
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        raise unsafe_network()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class CoreResponseStream(httpx2.AsyncByteStream):
    def __init__(self, response: httpcore2.Response) -> None:
        self.response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for block in self.response.aiter_stream():
            yield block

    async def aclose(self) -> None:
        await self.response.aclose()


class PublicHTTPSTransport(httpx2.AsyncBaseTransport):
    """No proxies, implicit retries, alternate protocols, or second DNS lookup before connection."""

    def __init__(
        self,
        hosts: tuple[str, ...],
        *,
        authenticated_api: bool = False,
        network_backend: PublicNetworkBackend | None = None,
    ) -> None:
        self.hosts = hosts
        self.authenticated_api = authenticated_api
        self.pool = httpcore2.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=network_backend
            if network_backend is not None
            else PublicNetworkBackend(),
            retries=0,
            max_connections=1,
            max_keepalive_connections=0,
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        validate_download_url(str(request.url), self.hosts)
        if self.authenticated_api:
            if (
                request.method != "POST"
                or request.url.host != "api.coefont.cloud"
                or request.url.path != "/v2/text2speech"
                or request.url.query
            ):
                raise unsafe_network()
        elif request.method != "GET" or any(
            header in request.headers
            for header in (
                "authorization",
                "cookie",
                "proxy-authorization",
                "x-coefont-date",
                "x-coefont-content",
            )
        ):
            raise unsafe_network()
        if not isinstance(request.stream, httpx2.AsyncByteStream):
            raise TypeError("An asynchronous request stream is required.")
        response = await self.pool.handle_async_request(
            httpcore2.Request(
                request.method,
                httpcore2.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx2.Response(
            response.status,
            headers=response.headers,
            stream=CoreResponseStream(response),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.pool.aclose()
