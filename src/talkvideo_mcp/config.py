from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from pydantic import Field, SecretStr, field_validator

from talkvideo_mcp.errors import TalkVideoError
from talkvideo_mcp.models import Model
from talkvideo_mcp.network import canonical_host
from talkvideo_mcp.storage import LocalStore

ACCESS_KEY_ENV = "TALKVIDEO_COEFONT_ACCESS_KEY"
ACCESS_SECRET_ENV = "TALKVIDEO_COEFONT_ACCESS_SECRET"
API_DOCS = "https://docs.coefont.cloud/en/"
PLAN_URL = "https://coefont.cloud/selectPlan"


class SpeechOptions(Model):
    speed: float = Field(default=1.0, ge=0.1, le=10, allow_inf_nan=False, strict=True)
    pitch: float = Field(default=0.0, ge=-3000, le=3000, allow_inf_nan=False, strict=True)
    kuten: float = Field(default=0.5, ge=0, le=5, allow_inf_nan=False, strict=True)
    toten: float | None = Field(default=None, ge=0.2, le=2, allow_inf_nan=False, strict=True)
    volume: float = Field(default=1.0, ge=0.2, le=2, allow_inf_nan=False, strict=True)


class OperatorAuthorization(Model):
    """Operator-supplied references, not independently verified rights or a review receipt."""

    api_contract_reference: str = Field(min_length=1, max_length=512, repr=False)
    voice_permission_reference: str = Field(min_length=1, max_length=512, repr=False)
    paid_api_use_confirmed: bool = Field(default=False, strict=True)

    @field_validator("api_contract_reference", "voice_permission_reference")
    @classmethod
    def nonblank_reference(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("A nonblank operator reference is required.")
        return value


class CoefontConfig(Model):
    enabled: bool = Field(default=False, strict=True)
    voice_id: UUID | None = None
    authorization: OperatorAuthorization | None = None
    trusted_download_hosts: tuple[str, ...] = Field(default=(), max_length=4)
    normalize_wav: bool = Field(default=False, strict=True)
    options: SpeechOptions = Field(default_factory=SpeechOptions)

    @field_validator("voice_id")
    @classmethod
    def nonzero_voice(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("Supply the operator's actual authorized official API voice UUID.")
        return value

    @field_validator("trusted_download_hosts")
    @classmethod
    def exact_public_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        hosts = tuple(canonical_host(value) for value in values)
        if len(set(hosts)) != len(hosts):
            raise ValueError("Download hosts must be distinct exact hostnames.")
        return hosts

    def activation_missing(self) -> list[str]:
        missing = []
        if not self.enabled:
            missing.append("operator_enablement")
        if self.voice_id is None:
            missing.append("authorized_voice_id")
        if self.authorization is None:
            missing.append("official_api_contract_and_voice_permission")
        elif not self.authorization.paid_api_use_confirmed:
            missing.append("operator_confirmation_of_paid_api_use")
        if not self.trusted_download_hosts:
            missing.append("operator_confirmed_exact_download_hosts")
        return missing

    def fingerprint(self) -> str:
        # Transport policy may be tightened or a known download host explicitly added for GET
        # recovery. Voice/prosody/contract changes, unlike transport changes, create new audio.
        data = self.model_dump(mode="json", include={"voice_id", "authorization", "options"})
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        return "coefont-official-v2-" + hashlib.sha256(encoded).hexdigest()


class LocalConfig(Model):
    coefont: CoefontConfig = Field(default_factory=CoefontConfig)


class Credentials(Model):
    access_key: SecretStr = Field(repr=False)
    access_secret: SecretStr = Field(repr=False)

    @field_validator("access_key", "access_secret")
    @classmethod
    def valid_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not 1 <= len(raw) <= 512 or any(ord(char) < 33 or ord(char) > 126 for char in raw):
            raise ValueError("Invalid credential format.")
        return value

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> Credentials:
        access = environment.get(ACCESS_KEY_ENV)
        secret = environment.get(ACCESS_SECRET_ENV)
        if not access or not secret:
            raise TalkVideoError(
                "coefont_credentials_missing",
                "Official API credentials were not supplied by the operator.",
                "Set only the documented credential environment variables for the server process; "
                "never put keys in tool input, CLI arguments, configuration files, or logs.",
                needs_user_action=True,
            )
        return cls(access_key=SecretStr(access), access_secret=SecretStr(secret))


def load_config(path: Path) -> LocalConfig:
    absolute = path.absolute()
    store = LocalStore(absolute.parent)
    raw = store.read_bytes(absolute.name, limit=16_384)
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
        return LocalConfig.model_validate(parsed)
    except ValueError as exc:
        raise TalkVideoError(
            "invalid_local_config",
            "The explicit operator configuration is invalid; its contents are not logged.",
            "Use the documented private TOML structure. Credentials belong in the process "
            "environment, not in this file.",
            needs_user_action=True,
        ) from exc
