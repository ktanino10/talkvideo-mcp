import asyncio
import hashlib
import hmac
import json
import secrets
from contextlib import asynccontextmanager
from email.utils import formatdate
from uuid import uuid4

import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from talkvideo_mcp.audio import AmbiguousSubmission, SubmissionRejected, encode_wav
from talkvideo_mcp.cli import configured_engine
from talkvideo_mcp.coefont import (
    API_URL,
    CoefontProvider,
    RetrievalDeferred,
    Text2SpeechRequest,
)
from talkvideo_mcp.config import CoefontConfig, Credentials, OperatorAuthorization
from talkvideo_mcp.engine import Engine
from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.media import media_tools
from talkvideo_mcp.models import (
    AudioSettings,
    CueInput,
    SaveRevisionInput,
    ScriptInput,
)
from talkvideo_mcp.revisions import record_review, review_subject
from talkvideo_mcp.storage import LocalStore
from talkvideo_mcp.text import prepare_script


def operator_config(*, normalize=False):
    return CoefontConfig(
        enabled=True,
        voice_id=uuid4(),
        authorization=OperatorAuthorization(
            api_contract_reference="synthetic-contract-not-a-real-authorization",
            voice_permission_reference="synthetic-private-use-fixture",
            paid_api_use_confirmed=True,
        ),
        trusted_download_hosts=("files.example.test", "cdn.example.test"),
        normalize_wav=normalize,
    )


def dummy_credentials():
    # Runtime-only random test values, never real keys or documentation sample credentials.
    return Credentials(
        access_key=SecretStr(secrets.token_hex(16)),
        access_secret=SecretStr(secrets.token_hex(24)),
    )


def wav_bytes(rate=16000, channels=1, frames=400):
    return encode_wav(bytes(frames * channels * 2), rate=rate, channels=channels, width=2)


class Clock:
    def __init__(self):
        self.value = 1_700_000_000.0
        self.sleeps = []

    def __call__(self):
        return self.value

    async def sleep(self, duration):
        self.sleeps.append(duration)
        self.value += duration


@asynccontextmanager
async def bound_provider(root, api, download, *, config=None, clock=None, credentials=None):
    config = config or operator_config()
    clock = clock or Clock()
    provider = CoefontProvider(
        config,
        credentials or dummy_credentials(),
        api_transport=httpx2.MockTransport(api),
        download_transport=httpx2.MockTransport(download),
        clock=clock,
        sleep=clock.sleep,
    )
    store = LocalStore(root)
    store.acquire_writer()
    provider.bind_store(store)
    try:
        yield provider, store
    finally:
        await provider.aclose()
        store.close()


async def test_exact_json_hmac_fixed_origin_and_no_download_credentials(tmp_path, caplog):
    config = operator_config()
    credentials = dummy_credentials()
    clock = Clock()
    token = secrets.token_hex(20)
    requests = []

    async def api(request):
        requests.append(request)
        assert str(request.url) == API_URL and request.method == "POST"
        body = request.content
        assert b"\xe6\x97\xa5" in body
        assert json.loads(body)["coefont"] == str(config.voice_id)
        date = str(int(clock()))
        expected = hmac.new(
            credentials.access_secret.get_secret_value().encode(),
            date.encode() + body,
            hashlib.sha256,
        ).hexdigest()
        assert request.headers["X-Coefont-Date"] == date
        assert request.headers["X-Coefont-Content"] == expected
        assert request.headers["Authorization"] == credentials.access_key.get_secret_value()
        return httpx2.Response(
            302,
            headers={
                "Location": f"https://files.example.test/audio?token={token}",
                "Set-Cookie": "must_not_forward=yes",
            },
        )

    async def download(request):
        requests.append(request)
        assert request.method == "GET"
        assert not any(
            name in request.headers
            for name in ("authorization", "cookie", "x-coefont-date", "x-coefont-content")
        )
        if request.url.host == "files.example.test":
            return httpx2.Response(
                302,
                headers={
                    "Location": f"https://cdn.example.test/audio?token={token}",
                    "Set-Cookie": "also_not_forwarded=yes",
                },
            )
        return httpx2.Response(200, content=wav_bytes(), headers={"Content-Type": "audio/wav"})

    async with bound_provider(
        tmp_path / "output", api, download, config=config, clock=clock, credentials=credentials
    ) as (provider, store):
        text = "日本語の「説明」。"
        result = await provider.synthesize(text, AudioSettings())
        assert result == wav_bytes()
        assert await provider.synthesize(text, AudioSettings()) == result
        assert [request.method for request in requests] == ["POST", "GET", "GET"]
        assert provider.fixture_only and not provider.live_verified
        journals = store.list_names(".state/coefont/requests", limit=512)
        serialized = store.read_bytes(f".state/coefont/requests/{journals[0]}")
        assert token.encode() not in serialized  # Signed URL removed after a durable raw copy.
        assert credentials.access_key.get_secret_value().encode() not in serialized
        assert credentials.access_secret.get_secret_value().encode() not in serialized
    assert token not in caplog.text


@pytest.mark.parametrize("length", [999, 1000, 1001])
def test_documented_text_length_bounds(length):
    if length <= 1000:
        assert len(Text2SpeechRequest(coefont=uuid4(), text="a" * length).text) == length
    else:
        with pytest.raises(ValidationError):
            Text2SpeechRequest(coefont=uuid4(), text="a" * length)


def test_phonetic_pair_and_numeric_bounds_are_typed():
    good = Text2SpeechRequest(coefont=uuid4(), text="漢字", yomi="かんじ", accent="122")
    assert good.yomi != good.text and len(good.yomi) == len(good.accent)
    for extra in [
        {"yomi": "a"},
        {"accent": "1"},
        {"yomi": "ab", "accent": "1"},
        {"yomi": "ab", "accent": "13"},
        {"format": "mp3"},
        {"text": " "},
        {"speed": 0.09},
        {"speed": 10.01},
        {"pitch": -3001},
        {"pitch": 3001},
        {"kuten": -0.1},
        {"kuten": 5.1},
        {"toten": 0.1},
        {"toten": 2.1},
        {"volume": 0.1},
        {"volume": 2.1},
        {"volume": float("nan")},
    ]:
        with pytest.raises(ValidationError):
            Text2SpeechRequest.model_validate({"coefont": uuid4(), "text": "test", **extra})


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "coefont_request_rejected"),
        (401, "coefont_auth_or_request_rejected"),
        (403, "coefont_voice_forbidden"),
        (404, "coefont_voice_missing"),
        (429, "coefont_quota_exceeded"),
        (500, "coefont_generation_failed"),
    ],
)
async def test_api_rejections_are_sticky_and_not_regenerated(tmp_path, status, code):
    calls = []

    def api(request):
        calls.append("POST")
        return httpx2.Response(status, content=b"PRIVATE error body must not be echoed")

    def download(request):
        raise AssertionError("No GET after a rejected generation.")

    async with bound_provider(tmp_path / "out", api, download) as (provider, _):
        for _ in range(2):
            with pytest.raises(SubmissionRejected, match=code) as error:
                await provider.synthesize("test", AudioSettings())
            assert "PRIVATE" not in error.value.problem.model_dump_json()
        assert calls == ["POST"]


@pytest.mark.parametrize(
    "status,headers", [(200, {}), (307, {"Location": "https://files.example.test/x"}), (302, {})]
)
async def test_only_documented_302_is_accepted_without_implicit_follow(tmp_path, status, headers):
    calls = []

    def api(request):
        calls.append("POST")
        return httpx2.Response(status, headers=headers)

    def download(request):
        raise AssertionError("Unexpected redirect must not be followed.")

    async with bound_provider(tmp_path / "out", api, download) as (provider, _):
        for _ in range(2):
            with pytest.raises(AmbiguousSubmission):
                await provider.synthesize("test", AudioSettings())
        assert calls == ["POST"]


async def test_ambiguous_post_survives_restart_without_a_second_post(tmp_path):
    config = operator_config()
    calls = []

    def api(request):
        calls.append("POST")
        raise httpx2.ReadTimeout("fixture uncertainty")

    def download(request):
        raise AssertionError("No file location was confirmed.")

    for _ in range(2):
        async with bound_provider(tmp_path / "out", api, download, config=config) as (provider, _):
            with pytest.raises(AmbiguousSubmission):
                await provider.synthesize("test", AudioSettings())
    assert calls == ["POST"]


@pytest.mark.parametrize(
    "url",
    [
        "https://unverified.example.test/audio",
        "https://user:password@files.example.test/audio",
        "http://files.example.test/audio",
        "https://127.0.0.1/audio",
    ],
)
async def test_unverified_or_unsafe_redirect_is_not_downloaded(tmp_path, url):
    calls = []

    def api(request):
        calls.append("POST")
        return httpx2.Response(302, headers={"Location": url})

    def download(request):
        calls.append("GET")
        raise AssertionError("Unsafe redirect reached transport.")

    async with bound_provider(tmp_path / "out", api, download) as (provider, _):
        with pytest.raises(TalkVideoError, match="unsafe_download_url"):
            await provider.synthesize("test", AudioSettings())
    assert calls == ["POST"]


@pytest.mark.parametrize("date_header", [False, True])
async def test_retry_after_persists_across_cancel_restart_and_resume(tmp_path, date_header):
    config = operator_config()
    clock = Clock()
    deadline = clock() + 30
    attempts = []

    def api(request):
        attempts.append("POST")
        return httpx2.Response(302, headers={"Location": "https://files.example.test/audio"})

    def download(request):
        attempts.append("GET")
        if attempts.count("GET") == 1:
            wait = formatdate(deadline, usegmt=True) if date_header else "30"
            return httpx2.Response(429, headers={"Retry-After": wait})
        return httpx2.Response(200, content=wav_bytes())

    async with bound_provider(tmp_path / "out", api, download, config=config, clock=clock) as (
        provider,
        _,
    ):
        with pytest.raises(RetrievalDeferred) as error:
            await provider.synthesize("test", AudioSettings())
        assert error.value.retry_not_before == deadline
        assert clock.sleeps == []
    async with bound_provider(tmp_path / "out", api, download, config=config, clock=clock) as (
        provider,
        _,
    ):
        with pytest.raises(RetrievalDeferred):
            await provider.synthesize("test", AudioSettings())
        assert attempts == ["POST", "GET"]
        clock.value = deadline
        assert await provider.synthesize("test", AudioSettings()) == wav_bytes()
    assert attempts == ["POST", "GET", "GET"]


async def test_short_retry_after_is_waited_not_clipped(tmp_path):
    clock = Clock()
    downloads = []

    def api(request):
        return httpx2.Response(302, headers={"Location": "https://files.example.test/audio"})

    def download(request):
        downloads.append(clock())
        if len(downloads) == 1:
            return httpx2.Response(503, headers={"Retry-After": "1"})
        return httpx2.Response(200, content=wav_bytes())

    async with bound_provider(tmp_path / "out", api, download, clock=clock) as (provider, _):
        await provider.synthesize("test", AudioSettings())
    assert clock.sleeps == [1.0]
    assert downloads[1] - downloads[0] == 1.0


async def test_invalid_retry_after_never_becomes_an_immediate_retry(tmp_path):
    gets = []

    def api(request):
        return httpx2.Response(302, headers={"Location": "https://files.example.test/audio"})

    def download(request):
        gets.append("GET")
        return httpx2.Response(503, headers={"Retry-After": "not a time"})

    async with bound_provider(tmp_path / "out", api, download) as (provider, _):
        for _ in range(2):
            with pytest.raises(TalkVideoError, match="coefont_retry_after_invalid"):
                await provider.synthesize("test", AudioSettings())
    assert gets == ["GET"]


async def test_get_attempts_are_cumulative_and_expiry_never_reposts(tmp_path):
    config = operator_config()
    clock = Clock()
    calls = []

    def api(request):
        calls.append("POST")
        return httpx2.Response(302, headers={"Location": "https://files.example.test/audio"})

    def download(request):
        calls.append("GET")
        return httpx2.Response(503, headers={"Retry-After": "0"})

    for _ in range(2):
        async with bound_provider(tmp_path / "out", api, download, config=config, clock=clock) as (
            provider,
            _,
        ):
            with pytest.raises(TalkVideoError, match="coefont_retrieval_exhausted"):
                await provider.synthesize("test", AudioSettings())
    assert calls == ["POST", "GET", "GET", "GET"]
    calls.clear()

    def deferred(request):
        calls.append("GET")
        return httpx2.Response(503, headers={"Retry-After": "30"})

    async with bound_provider(tmp_path / "expired", api, deferred, config=config, clock=clock) as (
        provider,
        _,
    ):
        with pytest.raises(RetrievalDeferred):
            await provider.synthesize("test", AudioSettings())
        clock.value += 7 * 24 * 60 * 60 + 1
        with pytest.raises(TalkVideoError, match="coefont_download_expired"):
            await provider.synthesize("test", AudioSettings())
        assert calls == ["POST", "GET"]


async def test_cached_audio_hashes_are_verified_instead_of_regenerated(tmp_path):
    calls = []

    def api(request):
        calls.append("POST")
        return httpx2.Response(302, headers={"Location": "https://files.example.test/audio"})

    def download(request):
        calls.append("GET")
        return httpx2.Response(200, content=wav_bytes())

    async with bound_provider(tmp_path / "out", api, download) as (provider, store):
        await provider.synthesize("test", AudioSettings())
        raw = next((store.root / ".state/coefont/audio").glob("*.raw.wav"))
        raw.write_bytes(b"not the verified source")
        with pytest.raises(TalkVideoError, match="coefont_cached_audio_integrity"):
            await provider.synthesize("test", AudioSettings())
        assert calls == ["POST", "GET"]


@pytest.mark.parametrize("half", ["api", "download"])
def test_half_injected_offline_mode_is_rejected_without_live_fallback(monkeypatch, half):
    import talkvideo_mcp.coefont as module

    def forbidden(*args, **kwargs):
        raise AssertionError("A live transport must never be constructed by an offline test.")

    monkeypatch.setattr(module, "PublicHTTPSTransport", forbidden)
    mock = httpx2.MockTransport(lambda request: httpx2.Response(200))
    with pytest.raises(TalkVideoError, match="coefont_mixed_transports"):
        CoefontProvider(
            operator_config(),
            dummy_credentials(),
            api_transport=mock if half == "api" else None,
            download_transport=mock if half == "download" else None,
        )


async def test_download_limits_html_partial_and_redirect_loop(tmp_path):
    def api(request):
        return httpx2.Response(302, headers={"Location": "https://files.example.test/audio"})

    responses = [
        (
            httpx2.Response(
                200, content=b"<html>not WAV</html>", headers={"Content-Type": "text/html"}
            ),
            "coefont_download_type",
        ),
        (
            httpx2.Response(200, content=b"", headers={"Content-Length": "67108865"}),
            "coefont_download_size",
        ),
        (httpx2.Response(200, content=wav_bytes()[:-3]), "invalid_wav"),
        (
            httpx2.Response(302, headers={"Location": "https://files.example.test/audio"}),
            "coefont_redirect_limit",
        ),
    ]
    for index, (response, code) in enumerate(responses):
        async with bound_provider(
            tmp_path / str(index), api, lambda request, value=response: value
        ) as (
            provider,
            _,
        ):
            with pytest.raises(TalkVideoError, match=code):
                await provider.synthesize("test", AudioSettings())


@pytest.mark.skipif(media_tools() is None, reason="Local conversion tools not installed")
async def test_normalization_resume_uses_cached_raw_without_post_or_get(tmp_path):
    config = operator_config(normalize=False)
    calls = []
    source = wav_bytes(rate=44100, channels=2, frames=4410)

    def api(request):
        calls.append("POST")
        return httpx2.Response(302, headers={"Location": "https://files.example.test/audio"})

    def download(request):
        calls.append("GET")
        return httpx2.Response(200, content=source)

    async with bound_provider(tmp_path / "out", api, download, config=config) as (provider, _):
        with pytest.raises(TalkVideoError, match="audio_normalization_required"):
            await provider.synthesize("test", AudioSettings())
    config.normalize_wav = True
    async with bound_provider(tmp_path / "out", api, download, config=config) as (provider, store):
        normalized = await provider.synthesize("test", AudioSettings())
        assert normalized != source
        assert calls == ["POST", "GET"]
        ticket_name = store.list_names(".state/coefont/requests", limit=512)[0]
        ticket = json.loads(store.read_bytes(f".state/coefont/requests/{ticket_name}"))
        assert ticket["raw_info"]["frames"] == 4410
        assert ticket["normalized_info"]["frames"] == 1600
        assert ticket["normalization_method"] == "ffmpeg_pcm_s16le"
        assert ticket["raw_sha256"] == hashlib.sha256(source).hexdigest()
        assert ticket["normalized_sha256"] == hashlib.sha256(normalized).hexdigest()


async def finish(engine, job_id):
    async with asyncio.timeout(10):
        while True:
            job = engine.get_job(job_id)
            if job.status not in {"queued", "running"}:
                return job
            await asyncio.sleep(0.01)


async def test_engine_deferred_job_resumes_get_only_and_keeps_mock_provenance(tmp_path):
    config = operator_config()
    clock = Clock()
    calls = []

    def api(request):
        calls.append("POST")
        return httpx2.Response(302, headers={"Location": "https://files.example.test/audio"})

    def download(request):
        calls.append("GET")
        if calls.count("GET") == 1:
            return httpx2.Response(429, headers={"Retry-After": "30"})
        return httpx2.Response(200, content=wav_bytes())

    def make_engine():
        return Engine(
            tmp_path / "output",
            official_provider=CoefontProvider(
                config,
                dummy_credentials(),
                api_transport=httpx2.MockTransport(api),
                download_transport=httpx2.MockTransport(download),
                clock=clock,
                sleep=clock.sleep,
            ),
        )

    engine = make_engine()
    script = ScriptInput(cues=[CueInput(display_text="mocked official API fixture")])
    revision = engine.save_revision(
        SaveRevisionInput(
            video_name="official-fixture",
            script=script,
            expected_plan_digest=prepare_script(script).plan_digest,
        )
    )
    try:
        cap = engine.capabilities().production_audio
        assert cap.implemented and cap.configured and cap.available
        assert cap.execution_mode == "mock" and not cap.live_verified
        waiting = engine.start_job(revision.ref, "audio", "preview")
        assert waiting.status == "awaiting_review" and not calls
        record_review(
            engine.store, revision, "script", review_subject(engine.store, revision, "script")
        )
        engine.resume_job(waiting.job_id)
        assert (await finish(engine, waiting.job_id)).status == "deferred"
        assert engine.resume_job(waiting.job_id).status == "deferred"
        assert calls == ["POST", "GET"]
    finally:
        await engine.close()
    restarted = make_engine()
    try:
        assert restarted.resume_job(waiting.job_id).status == "deferred"
        clock.value += 30
        restarted.resume_job(waiting.job_id)
        done = await finish(restarted, waiting.job_id)
        assert done.status == "succeeded" and done.diagnostic_only
        assert done.attempts == {"c0001-p0001": 1}
        assert calls == ["POST", "GET", "GET"]
        inspection = await restarted.inspect_output(revision.ref, 0, 20)
        chunk = next(a for a in inspection.artifacts if a.path.startswith("chunks/"))
        assert chunk.normalization.raw_sha256 == chunk.normalization.normalized_sha256
        assert chunk.normalization.raw_path.startswith("raw/")
        assert chunk.diagnostic_only and not inspection.perceptual_quality_assessed
        with pytest.raises(TalkVideoError, match="production_unavailable"):
            restarted.start_job(revision.ref, "video", "preview")
    finally:
        await restarted.close()


def test_default_configuration_never_reads_credentials_or_enables_api(tmp_path):
    class ForbiddenEnvironment(dict):
        def get(self, *args):
            raise AssertionError("Unconfigured startup must not look for real credentials.")

    engine = configured_engine(tmp_path / "output", False, environment=ForbiddenEnvironment())
    cap = engine.capabilities()
    assert cap.production_audio.implemented
    assert not cap.production_audio.available
    assert not cap.production_audio.configured
    assert not cap.production_audio.live_verified
    assert not cap.external_text_transmission_enabled
    assert not engine.store.root.exists()
    config = tmp_path / "disabled.toml"
    config.write_text("[coefont]\nenabled=false\n")
    configured_engine(
        tmp_path / "other-output", False, config_path=config, environment=ForbiddenEnvironment()
    )


async def test_official_preview_is_a_bounded_single_cue_not_long_generation(tmp_path):
    calls = []

    def api(request):
        calls.append("POST")
        return httpx2.Response(302, headers={"Location": "https://files.example.test/audio"})

    def download(request):
        calls.append("GET")
        return httpx2.Response(200, content=wav_bytes(frames=31 * 16000))

    engine = Engine(
        tmp_path / "output",
        official_provider=CoefontProvider(
            operator_config(),
            dummy_credentials(),
            api_transport=httpx2.MockTransport(api),
            download_transport=httpx2.MockTransport(download),
        ),
    )
    try:
        long_script = ScriptInput(cues=[CueInput(display_text="a" * 81)])
        long_revision = engine.save_revision(
            SaveRevisionInput(
                video_name="preview-text-bound",
                script=long_script,
                expected_plan_digest=prepare_script(long_script).plan_digest,
            )
        )
        with pytest.raises(TalkVideoError, match="preview_text_limit"):
            engine.start_job(long_revision.ref, "audio", "preview")
        assert not calls
        short_script = ScriptInput(
            cues=[
                CueInput(display_text="short first cue"),
                CueInput(display_text="not generated yet"),
            ]
        )
        short_revision = engine.save_revision(
            SaveRevisionInput(
                video_name="preview-time-bound",
                script=short_script,
                expected_plan_digest=prepare_script(short_script).plan_digest,
            )
        )
        record_review(
            engine.store,
            short_revision,
            "script",
            review_subject(engine.store, short_revision, "script"),
        )
        job = engine.start_job(short_revision.ref, "audio", "preview")
        done = await finish(engine, job.job_id)
        assert done.total_chunks == 1
        assert done.status == "failed" and done.problem_code == "preview_duration_limit"
        assert calls == ["POST", "GET"]
        assert not engine.store.exists(
            f"{short_revision.ref.video_name}/{short_revision.ref.revision_id}/preview.wav"
        )
    finally:
        await engine.close()
