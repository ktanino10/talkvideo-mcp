from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urljoin
from uuid import UUID

import httpcore2
import httpx2
from pydantic import Field, SecretStr, ValidationError, model_validator

from talkvideo_mcp.audio import (
    AmbiguousSubmission,
    SubmissionRejected,
    parse_source_wav,
    parse_wav,
    validate_settings,
)
from talkvideo_mcp.config import CoefontConfig, Credentials, SpeechOptions
from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.media import normalize_source_wav
from talkvideo_mcp.models import (
    MAX_FILE_BYTES,
    AudioNormalization,
    AudioSettings,
    Digest,
    Model,
    RawWavInfo,
    RevisionRef,
    WavInfo,
    digest_bytes,
)
from talkvideo_mcp.network import PublicHTTPSTransport, validate_download_url
from talkvideo_mcp.revisions import revision_path
from talkvideo_mcp.storage import LocalStore

API_URL = "https://api.coefont.cloud/v2/text2speech"
MAX_REQUEST_BYTES = 32_768
MAX_REQUESTS_PER_ROOT = 512
MAX_DOWNLOAD_ATTEMPTS = 3
MAX_REDIRECTS = 3
MAX_LOCAL_RETRY_WAIT = 2.0
POST_SECONDS = 30
DOWNLOAD_SECONDS = 30
URL_LIFETIME_SECONDS = 7 * 24 * 60 * 60
HTTP_FAILURES = (
    httpx2.TransportError,
    httpcore2.NetworkError,
    httpcore2.TimeoutException,
    httpcore2.ProtocolError,
    TimeoutError,
    OSError,
)
Phase = Literal[
    "submitting",
    "redirect",
    "source_received",
    "downloaded",
    "normalized_received",
    "ready",
    "rejected",
]


class Text2SpeechRequest(SpeechOptions):
    coefont: UUID
    text: str = Field(min_length=1, max_length=1000, repr=False)
    yomi: str | None = Field(default=None, min_length=1, max_length=4000, repr=False)
    accent: str | None = Field(default=None, min_length=1, max_length=4000, pattern=r"^[12]+$")
    format: Literal["wav"] = "wav"

    @model_validator(mode="after")
    def phonetic_pair(self) -> Text2SpeechRequest:
        if not self.text.strip() or any(
            ord(char) < 32 and char not in "\n\r\t" for char in self.text
        ):
            raise ValueError("The speech text is empty or contains control characters.")
        if (self.yomi is None) != (self.accent is None):
            raise ValueError("Supply yomi and accent together, for an authorized Japanese voice.")
        if self.yomi is not None and self.accent is not None and len(self.yomi) != len(self.accent):
            raise ValueError("Accent must have one 1/2 character per yomi character.")
        return self

    def request_bytes(self) -> bytes:
        data = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(data) > MAX_REQUEST_BYTES:
            raise TalkVideoError(
                "coefont_request_too_large",
                "The official API request exceeds the local request-byte limit.",
                "Use a shorter cue or explicit shorter phonetic annotation; nothing was submitted.",
            )
        return data


class PrivateTicket(Model):
    """Never exported to MCP: its short-lived URL can contain a signed download credential."""

    request_sha256: Digest
    provider_fingerprint: str
    phase: Phase
    issued_at: int
    execution_mode: Literal["mock", "live"]
    download_url: SecretStr | None = Field(default=None, repr=False)
    download_attempts: int = Field(default=0, ge=0, le=MAX_DOWNLOAD_ATTEMPTS)
    redirect_count: int = Field(default=0, ge=0, le=MAX_REDIRECTS)
    retry_not_before: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    error_code: str | None = None
    raw_sha256: Digest | None = None
    raw_info: RawWavInfo | None = None
    normalized_sha256: Digest | None = None
    normalized_info: WavInfo | None = None
    normalization_method: Literal["identity", "pcm_rewrap", "ffmpeg_pcm_s16le"] | None = None


def signed_headers(credentials: Credentials, timestamp: int, body: bytes) -> dict[str, str]:
    date = str(timestamp)
    signature = hmac.new(
        credentials.access_secret.get_secret_value().encode("utf-8"),
        date.encode("ascii") + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": credentials.access_key.get_secret_value(),
        "X-Coefont-Date": date,
        "X-Coefont-Content": signature,
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
    }


def rejected(code: str) -> SubmissionRejected:
    return SubmissionRejected(
        code,
        "The official API rejected this request; no automatic regeneration is allowed.",
        "The operator must resolve contract/voice access, request restrictions, or quota. "
        "Do not bypass limits, substitute voices, or reset the durable request journal.",
        needs_user_action=True,
    )


class RetrievalDeferred(TalkVideoError):
    def __init__(self, deadline: float) -> None:
        self.retry_not_before = deadline
        super().__init__(
            "coefont_retrieval_deferred",
            "The persisted download cooldown has not elapsed.",
            f"Resume this job after UNIX time {deadline:.3f}; never issue a replacement POST.",
            needs_user_action=True,
        )


def retry_deadline(value: str | None, now: float, fallback: float) -> float:
    if value is None:
        return now + fallback
    try:
        if len(value) > 128:
            raise ValueError("Oversized cooldown header.")
        stripped = value.strip()
        if stripped.isascii() and stripped.isdecimal():
            deadline = now + int(stripped)
        else:
            date = parsedate_to_datetime(stripped)
            if date.tzinfo is None:
                raise ValueError("A timezone is required.")
            deadline = max(now, date.timestamp())
        if not 0 <= deadline < float("inf"):
            raise ValueError("Invalid cooldown.")
        return deadline
    except (ValueError, TypeError, OverflowError):
        raise TalkVideoError(
            "coefont_retry_after_invalid",
            "The provider's Retry-After value cannot be interpreted safely.",
            "Stop retrieval and resolve the cooldown outside MCP; do not shorten it or regenerate.",
            needs_user_action=True,
        ) from None


class RetryRetrieval(Exception):
    """A safe GET failed; its persisted cooldown must be respected before another GET."""


class CoefontProvider:
    side_effects_possible = True

    def __init__(
        self,
        config: CoefontConfig,
        credentials: Credentials,
        *,
        api_transport: httpx2.AsyncBaseTransport | None = None,
        download_transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if (api_transport is None) != (download_transport is None):
            raise TalkVideoError(
                "coefont_mixed_transports",
                "Offline tests require both injected transports, without live HTTP fallback.",
                "Inject both in-memory transports, or use neither under explicit operator setup.",
            )
        if config.activation_missing():
            raise TalkVideoError(
                "coefont_unconfigured",
                "The official adapter requires explicit operator configuration and authorization.",
                "Confirm the eligible API contract, private-use voice permission, paid requests "
                "and exact download hosts outside MCP before activation.",
                needs_user_action=True,
            )
        self.config = config.model_copy(deep=True)
        self.credentials = credentials
        self.fixture_only = api_transport is not None or download_transport is not None
        self.fingerprint = config.fingerprint() + ("-mock" if self.fixture_only else "-live")
        self.live_verified = False
        self.voice_access_observed = False
        self.blocked_reason: str | None = None
        self._api_transport = api_transport
        self._download_transport = download_transport
        self._store: LocalStore | None = None
        self.clock = clock
        self.sleep = sleep

    def bind_store(self, store: LocalStore) -> None:
        if self._store is not None and self._store is not store:
            raise RuntimeError("An official provider instance belongs to exactly one engine.")
        self._store = store

    def _storage(self) -> LocalStore:
        if self._store is None:
            raise RuntimeError("The official provider requires a bound durable store.")
        return self._store

    def _write_storage(self) -> LocalStore:
        store = self._storage()
        if not store.writer_held:
            raise RuntimeError("The official provider requires the engine's single-writer lock.")
        return store

    def request_for(self, text: str) -> Text2SpeechRequest:
        if self.config.voice_id is None:
            raise RuntimeError("Configured voice identity is missing.")
        try:
            return Text2SpeechRequest(
                coefont=self.config.voice_id,
                text=text,
                **self.config.options.model_dump(),
            )
        except ValidationError:
            raise TalkVideoError(
                "coefont_invalid_text",
                "A cue is not valid for the documented official API text/parameter bounds.",
                "Prepare shorter complete cues within 1..1000 characters. Do not drop text.",
            ) from None

    def _key(self, request: Text2SpeechRequest) -> str:
        if request.coefont != self.config.voice_id:
            raise TalkVideoError(
                "coefont_voice_mismatch",
                "The request voice differs from the operator-authorized voice.",
                "Do not select another voice through a tool request.",
                needs_user_action=True,
            )
        return digest_bytes(self.fingerprint.encode() + b"\n" + request.request_bytes())

    @staticmethod
    def _ticket_path(key: str) -> str:
        return f".state/coefont/requests/{key}.json"

    @staticmethod
    def _audio_path(key: str, kind: Literal["raw", "normalized"]) -> str:
        return f".state/coefont/audio/{key}.{kind}.wav"

    def _load(self, request: Text2SpeechRequest) -> PrivateTicket | None:
        store = self._storage()
        path = self._ticket_path(self._key(request))
        if not store.exists(path):
            return None
        ticket = store.read_model(path, PrivateTicket)
        if (
            ticket.request_sha256 != digest_bytes(request.request_bytes())
            or ticket.provider_fingerprint != self.fingerprint
            or ticket.execution_mode != ("mock" if self.fixture_only else "live")
        ):
            raise TalkVideoError(
                "coefont_journal_integrity",
                "The durable official request does not match its text/settings fingerprint.",
                "Preserve the journal; do not resubmit or edit its state.",
                needs_user_action=True,
            )
        return ticket

    def _save(self, key: str, ticket: PrivateTicket, *, replace: bool = True) -> None:
        data = ticket.model_dump(mode="json")
        if ticket.download_url is not None:
            data["download_url"] = ticket.download_url.get_secret_value()
        # Intentional private persistence only. No key/secret is saved, and tools never return URLs.
        self._write_storage().write_bytes(
            self._ticket_path(key),
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode(),
            replace=replace,
        )

    def operation_status(self, text: str) -> Literal["new", "ambiguous", "retrieval", "rejected"]:
        ticket = self._load(self.request_for(text))
        if ticket is None:
            return "new"
        if ticket.phase == "submitting":
            return "ambiguous"
        if ticket.phase == "rejected":
            return "rejected"
        return "retrieval"

    def retry_not_before(self, text: str) -> float | None:
        ticket = self._load(self.request_for(text))
        if ticket is not None and ticket.retry_not_before is not None:
            if ticket.retry_not_before > self.clock():
                return ticket.retry_not_before
        return None

    async def _submit(self, request: Text2SpeechRequest, key: str, ticket: PrivateTicket) -> None:
        if self._api_transport is None:
            if self.fixture_only:
                raise RuntimeError("An offline transport cannot be replaced by live HTTP.")
            self._api_transport = PublicHTTPSTransport(
                ("api.coefont.cloud",), authenticated_api=True
            )
        body = request.request_bytes()
        response: httpx2.Response | None = None
        try:
            async with asyncio.timeout(POST_SECONDS):
                response = await self._api_transport.handle_async_request(
                    httpx2.Request(
                        "POST",
                        API_URL,
                        content=body,
                        headers=signed_headers(self.credentials, ticket.issued_at, body),
                        extensions={
                            "timeout": {"connect": 5, "read": POST_SECONDS, "write": 5, "pool": 5}
                        },
                    )
                )
                status = response.status_code
                codes = {
                    400: "coefont_request_rejected",
                    401: "coefont_auth_or_request_rejected",
                    403: "coefont_voice_forbidden",
                    404: "coefont_voice_missing",
                    429: "coefont_quota_exceeded",
                    500: "coefont_generation_failed",
                }
                if status in codes:
                    ticket.phase = "rejected"
                    ticket.error_code = codes[status]
                    self.blocked_reason = codes[status]
                    self._save(key, ticket)
                    raise rejected(codes[status])
                location = response.headers.get("location")
                if status != 302 or not location or len(location) > 4096:
                    raise AmbiguousSubmission()
                ticket.download_url = SecretStr(urljoin(API_URL, location))
                ticket.phase = "redirect"
                self._save(key, ticket)
                if not self.fixture_only:
                    self.voice_access_observed = True
        except HTTP_FAILURES:
            raise AmbiguousSubmission() from None
        finally:
            if response is not None:
                async with asyncio.timeout(5):
                    await response.aclose()

    def _cached_audio(
        self, key: str, kind: Literal["raw", "normalized"], expected: str
    ) -> bytes | None:
        store = self._storage()
        path = self._audio_path(key, kind)
        if not store.exists(path):
            return None
        data = store.read_bytes(path)
        if digest_bytes(data) != expected:
            raise TalkVideoError(
                "coefont_cached_audio_integrity",
                "Cached audio no longer matches its recorded SHA-256.",
                "Preserve the cache and do not regenerate or overwrite it.",
                needs_user_action=True,
            )
        return data

    def _cache_audio(self, key: str, kind: Literal["raw", "normalized"], data: bytes) -> None:
        if self._cached_audio(key, kind, digest_bytes(data)) is not None:
            return
        self._write_storage().write_bytes(self._audio_path(key, kind), data)

    async def _download_once(self, key: str, ticket: PrivateTicket) -> bytes:
        if ticket.download_url is None:
            raise TalkVideoError(
                "coefont_download_missing",
                "The completed generation has no recoverable download URL.",
                "Resolve this outside MCP; do not automatically regenerate the cue.",
                needs_user_action=True,
            )
        if self.clock() >= ticket.issued_at + URL_LIFETIME_SECONDS:
            raise TalkVideoError(
                "coefont_download_expired",
                "The documented seven-day download lifetime has elapsed.",
                "Preserve the journal; a new generation needs a separate operator decision.",
                needs_user_action=True,
            )
        if self._download_transport is None:
            if self.fixture_only:
                raise RuntimeError("An offline transport cannot be replaced by live HTTP.")
            self._download_transport = PublicHTTPSTransport(self.config.trusted_download_hosts)
        async with asyncio.timeout(DOWNLOAD_SECONDS):
            while True:
                url = validate_download_url(
                    ticket.download_url.get_secret_value(), self.config.trusted_download_hosts
                )
                response = await self._download_transport.handle_async_request(
                    httpx2.Request(
                        "GET",
                        url,
                        headers={
                            "Accept": "audio/wav, application/octet-stream",
                            "Accept-Encoding": "identity",
                        },
                        extensions={"timeout": {"connect": 5, "read": 10, "write": 5, "pool": 5}},
                    )
                )
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or ticket.redirect_count >= MAX_REDIRECTS:
                            raise TalkVideoError(
                                "coefont_redirect_limit",
                                "The bounded download redirect policy was exceeded.",
                                "Inspect trusted hosts; no new generation is attempted.",
                                needs_user_action=True,
                            )
                        destination = urljoin(url, location)
                        validate_download_url(destination, self.config.trusted_download_hosts)
                        ticket.download_url = SecretStr(destination)
                        ticket.redirect_count += 1
                        self._save(key, ticket)
                        continue
                    if response.status_code in {408, 429, 500, 502, 503, 504}:
                        try:
                            ticket.retry_not_before = retry_deadline(
                                response.headers.get("retry-after"),
                                self.clock(),
                                min(float(ticket.download_attempts), 2.0),
                            )
                        except TalkVideoError:
                            ticket.error_code = "coefont_retry_after_invalid"
                            self._save(key, ticket)
                            raise
                        self._save(key, ticket)
                        raise RetryRetrieval()
                    if response.status_code != 200:
                        raise TalkVideoError(
                            "coefont_download_rejected",
                            "The generated file could not be retrieved; its URL may have expired.",
                            "Do not issue another generation POST. Reconcile access outside MCP.",
                            needs_user_action=True,
                        )
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise TalkVideoError(
                            "coefont_download_encoding",
                            "Unexpected compressed transfer encoding.",
                            "Only bounded identity-encoded WAV downloads are accepted.",
                        )
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type not in {
                        "audio/wav",
                        "audio/x-wav",
                        "audio/wave",
                        "application/octet-stream",
                        "",
                    }:
                        raise TalkVideoError(
                            "coefont_download_type",
                            "The download is not a supported WAV response.",
                            "Keep the generation journal; HTML/text is never accepted as audio.",
                        )
                    length = response.headers.get("content-length")
                    if length is not None:
                        try:
                            declared = int(length)
                        except ValueError:
                            raise TalkVideoError(
                                "coefont_download_length",
                                "Invalid download length.",
                                "Inspect the response contract.",
                            ) from None
                        if not 0 < declared <= MAX_FILE_BYTES:
                            raise TalkVideoError(
                                "coefont_download_size",
                                "Download length is out of bounds.",
                                "Use a shorter authorized cue; do not regenerate automatically.",
                            )
                    else:
                        declared = None
                    body = bytearray()
                    async for block in response.aiter_bytes(chunk_size=65_536):
                        body.extend(block)
                        if len(body) > MAX_FILE_BYTES:
                            raise TalkVideoError(
                                "coefont_download_size",
                                "Download exceeds 64 MiB.",
                                "The partial body was discarded; no new generation is attempted.",
                            )
                    if declared is not None and len(body) != declared:
                        raise httpx2.ReadError("Incomplete generated-file retrieval.")
                    parse_source_wav(bytes(body))
                    return bytes(body)
                finally:
                    async with asyncio.timeout(5):
                        await response.aclose()

    async def _raw_audio(self, key: str, ticket: PrivateTicket) -> bytes:
        if ticket.error_code == "coefont_retry_after_invalid":
            raise TalkVideoError(
                "coefont_retry_after_invalid",
                "An uninterpretable provider cooldown is retained; retrieval is paused.",
                "Resolve the cooldown outside MCP; do not immediately retry or regenerate.",
                needs_user_action=True,
            )
        if ticket.raw_sha256 is not None:
            cached = self._cached_audio(key, "raw", ticket.raw_sha256)
            if cached is not None:
                source, _ = parse_source_wav(cached)
                if source != ticket.raw_info:
                    raise TalkVideoError(
                        "coefont_cached_audio_integrity",
                        "Source frame metadata differs.",
                        "Preserve the cache.",
                    )
                if ticket.download_url is not None or ticket.retry_not_before is not None:
                    ticket.phase = "downloaded"
                    ticket.download_url = None
                    ticket.retry_not_before = None
                    self._save(key, ticket)
                return cached
        wait_budget = MAX_LOCAL_RETRY_WAIT
        while ticket.download_attempts < MAX_DOWNLOAD_ATTEMPTS:
            if ticket.retry_not_before is not None:
                remaining = ticket.retry_not_before - self.clock()
                if remaining > 0:
                    if remaining > wait_budget:
                        raise RetrievalDeferred(ticket.retry_not_before)
                    await self.sleep(remaining)
                    wait_budget -= remaining
                    if self.clock() < ticket.retry_not_before:
                        raise RetrievalDeferred(ticket.retry_not_before)
                ticket.retry_not_before = None
                self._save(key, ticket)
            ticket.download_attempts += 1
            self._save(key, ticket)
            try:
                raw = await self._download_once(key, ticket)
                break
            except RetryRetrieval:
                if ticket.download_attempts >= MAX_DOWNLOAD_ATTEMPTS:
                    raise TalkVideoError(
                        "coefont_retrieval_exhausted",
                        "Download-only attempts are exhausted; the provider cooldown is retained.",
                        "Keep the cooldown and reconcile retrieval outside MCP; do not regenerate.",
                        needs_user_action=True,
                    ) from None
            except HTTP_FAILURES:
                if ticket.download_attempts >= MAX_DOWNLOAD_ATTEMPTS:
                    raise TalkVideoError(
                        "coefont_retrieval_exhausted",
                        "The cumulative download-only retry budget is exhausted.",
                        "No new POST is issued. Preserve the journal and reconcile retrieval.",
                        needs_user_action=True,
                    ) from None
                ticket.retry_not_before = self.clock() + min(float(ticket.download_attempts), 2.0)
                self._save(key, ticket)
        else:
            raise TalkVideoError(
                "coefont_retrieval_exhausted",
                "Download-only attempts are exhausted.",
                "Resolve retrieval outside MCP; do not reset the journal to regenerate.",
                needs_user_action=True,
            )
        source, _ = parse_source_wav(raw)
        if ticket.raw_sha256 is not None and ticket.raw_sha256 != digest_bytes(raw):
            raise TalkVideoError(
                "coefont_cached_audio_integrity",
                "Retrieved bytes differ from the recorded source.",
                "Preserve the journal; no existing source will be overwritten.",
                needs_user_action=True,
            )
        ticket.raw_sha256 = digest_bytes(raw)
        ticket.raw_info = source
        ticket.phase = "source_received"
        self._save(key, ticket)
        self._cache_audio(key, "raw", raw)
        ticket.phase = "downloaded"
        ticket.download_url = None
        ticket.retry_not_before = None
        self._save(key, ticket)
        return raw

    async def synthesize(self, text: str, settings: AudioSettings) -> bytes:
        return await self.synthesize_request(self.request_for(text), settings)

    async def synthesize_request(
        self, request: Text2SpeechRequest, settings: AudioSettings
    ) -> bytes:
        store = self._write_storage()
        key = self._key(request)
        ticket = self._load(request)
        if ticket is None:
            if any(store.exists(self._audio_path(key, kind)) for kind in ("raw", "normalized")):
                raise TalkVideoError(
                    "coefont_untracked_cache",
                    "Audio cache exists without its request journal.",
                    "Preserve the cache; do not issue a replacement generation POST.",
                    needs_user_action=True,
                )
            if (
                len(store.list_names(".state/coefont/requests", limit=MAX_REQUESTS_PER_ROOT))
                >= MAX_REQUESTS_PER_ROOT
            ):
                raise TalkVideoError(
                    "coefont_request_limit",
                    "The per-root official request journal limit was reached.",
                    "Archive deliberately; never change roots to bypass provider quotas.",
                    needs_user_action=True,
                )
            ticket = PrivateTicket(
                request_sha256=digest_bytes(request.request_bytes()),
                provider_fingerprint=self.fingerprint,
                phase="submitting",
                issued_at=int(self.clock()),
                execution_mode="mock" if self.fixture_only else "live",
            )
            self._save(key, ticket, replace=False)
            await self._submit(request, key, ticket)
        elif ticket.phase == "submitting":
            raise AmbiguousSubmission()
        elif ticket.phase == "rejected":
            raise rejected(ticket.error_code or "coefont_request_rejected")
        if ticket.normalized_sha256 is not None:
            normalized = self._cached_audio(key, "normalized", ticket.normalized_sha256)
            if normalized is not None:
                raw_cache = (
                    self._cached_audio(key, "raw", ticket.raw_sha256)
                    if ticket.raw_sha256 is not None
                    else None
                )
                if raw_cache is None or parse_source_wav(raw_cache)[0] != ticket.raw_info:
                    raise TalkVideoError(
                        "coefont_cached_audio_integrity",
                        "Normalized audio has no verified raw source.",
                        "Preserve the cache; do not return unverified provenance or regenerate.",
                        needs_user_action=True,
                    )
                info, _ = parse_wav(normalized)
                validate_settings(info, settings)
                if info != ticket.normalized_info:
                    raise TalkVideoError(
                        "coefont_cached_audio_integrity",
                        "Normalized frame metadata differs.",
                        "Preserve cached audio; do not regenerate.",
                        needs_user_action=True,
                    )
                return normalized
        raw = await self._raw_audio(key, ticket)
        normalized, source, info, method = await normalize_source_wav(
            raw, settings, allow_normalization=self.config.normalize_wav
        )
        if ticket.normalized_sha256 is not None and ticket.normalized_sha256 != digest_bytes(
            normalized
        ):
            raise TalkVideoError(
                "coefont_cached_audio_integrity",
                "Local normalization differs from its recorded hash.",
                "Preserve the cache and inspect local tool versions; do not overwrite it.",
                needs_user_action=True,
            )
        ticket.raw_info = source
        ticket.normalized_info = info
        ticket.normalized_sha256 = digest_bytes(normalized)
        ticket.normalization_method = method
        ticket.phase = "normalized_received"
        self._save(key, ticket)
        self._cache_audio(key, "normalized", normalized)
        ticket.phase = "ready"
        ticket.download_url = None
        self._save(key, ticket)
        if not self.fixture_only:
            self.live_verified = True
        return normalized

    def materialize_source(
        self, text: str, ref: RevisionRef, chunk_id: str, settings: AudioSettings
    ) -> AudioNormalization:
        request = self.request_for(text)
        ticket = self._load(request)
        if (
            ticket is None
            or ticket.raw_sha256 is None
            or ticket.raw_info is None
            or ticket.normalized_sha256 is None
            or ticket.normalized_info is None
            or ticket.normalization_method is None
        ):
            raise RuntimeError("Source provenance is not complete.")
        raw = self._cached_audio(self._key(request), "raw", ticket.raw_sha256)
        if raw is None:
            raise TalkVideoError(
                "coefont_cached_audio_integrity",
                "The recorded raw source is missing.",
                "Preserve state; do not regenerate missing source audio.",
                needs_user_action=True,
            )
        validate_settings(ticket.normalized_info, settings)
        store = self._write_storage()
        raw_name = f"raw/{chunk_id}.wav"
        target = revision_path(ref, raw_name)
        if store.exists(target):
            if digest_bytes(store.read_bytes(target)) != ticket.raw_sha256:
                raise TalkVideoError(
                    "artifact_integrity",
                    "The destination source differs from its hash.",
                    "Do not overwrite an existing raw source.",
                    needs_user_action=True,
                )
        else:
            store.write_bytes(target, raw)
        return AudioNormalization(
            raw_path=raw_name,
            raw_sha256=ticket.raw_sha256,
            normalized_sha256=ticket.normalized_sha256,
            raw=ticket.raw_info,
            normalized=ticket.normalized_info,
            method=ticket.normalization_method,
        )

    async def aclose(self) -> None:
        async with asyncio.timeout(5):
            if self._api_transport is not None:
                await self._api_transport.aclose()
            if self._download_transport is not None:
                await self._download_transport.aclose()
