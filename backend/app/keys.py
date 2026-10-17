"""Where the model API keys live.

Keys are entered on the Connections page, sent once over HTTPS, and stored on the
server only. They are never returned to the browser or written to logs.

- "secretmanager": Google Cloud Secret Manager (production on Cloud Run).
- "env": environment variables plus in-memory changes (local development).
"""
from __future__ import annotations

import asyncio
import logging

from .config import Settings

log = logging.getLogger("sasi.keys")

SECRET_NAMES = {"claude": "ANTHROPIC_API_KEY", "chatgpt": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}

# Stored in place of a key when it is removed. Writing it needs the same permission as saving a key,
# so removing works whenever saving works.
REMOVED = "__removed__"


def hint(key: str) -> str:
    """A safe reminder of which key is stored: only the last 4 characters."""
    return f"…{key[-4:]}" if len(key) >= 12 else "set"


class EnvKeyStore:
    def __init__(self, settings: Settings):
        self._keys = {
            "claude": settings.anthropic_api_key.strip(),
            "chatgpt": settings.openai_api_key.strip(),
            "gemini": settings.gemini_api_key.strip(),
        }

    async def load(self) -> dict[str, str]:
        return {p: k for p, k in self._keys.items() if k}

    async def save(self, provider: str, key: str) -> None:
        self._keys[provider] = key

    async def delete(self, provider: str) -> None:
        self._keys[provider] = ""


class SecretManagerKeyStore:
    def __init__(self, project: str):
        from google.cloud import secretmanager

        self._client = secretmanager.SecretManagerServiceClient()
        self._project = project

    def _secret(self, provider: str) -> str:
        return f"projects/{self._project}/secrets/{SECRET_NAMES[provider]}"

    def _read(self, provider: str) -> str:
        from google.api_core import exceptions

        try:
            resp = self._client.access_secret_version(name=f"{self._secret(provider)}/versions/latest")
            value = resp.payload.data.decode("utf-8").strip()
            return "" if value == REMOVED else value
        except (exceptions.NotFound, exceptions.FailedPrecondition):
            return ""  # secret or version missing: provider not connected
        except exceptions.PermissionDenied:
            log.warning("no permission to read %s; run deploy/setup-gcp.sh", SECRET_NAMES[provider])
            return ""

    def _enabled_versions(self, provider: str) -> list[str]:
        versions = self._client.list_secret_versions(parent=self._secret(provider), filter="state:ENABLED")
        return [v.name for v in versions]

    def _cleanup(self, provider: str, keep: str) -> None:
        """Destroy older versions, so old keys are really gone (and the free tier's 6 active versions are not used up).

        The newest version is already in place when this runs, so a cleanup problem is only logged.
        """
        keep_id = keep.rsplit("/", 1)[-1]
        try:
            for name in self._enabled_versions(provider):
                if name.rsplit("/", 1)[-1] != keep_id:
                    self._client.destroy_secret_version(name=name)
        except Exception as e:  # noqa: BLE001
            log.warning("could not destroy older versions of %s: %s", SECRET_NAMES[provider], type(e).__name__)

    def _write(self, provider: str, key: str) -> None:
        new = self._client.add_secret_version(parent=self._secret(provider), payload={"data": key.encode("utf-8")})
        self._cleanup(provider, new.name)

    def _remove(self, provider: str) -> None:
        from google.api_core import exceptions

        try:
            new = self._client.add_secret_version(parent=self._secret(provider), payload={"data": REMOVED.encode("utf-8")})
        except exceptions.NotFound:
            return  # no secret, so no stored key
        self._cleanup(provider, new.name)

    async def load(self) -> dict[str, str]:
        values = await asyncio.gather(*(asyncio.to_thread(self._read, p) for p in SECRET_NAMES))
        return {p: v for p, v in zip(SECRET_NAMES, values) if v}

    async def save(self, provider: str, key: str) -> None:
        await asyncio.to_thread(self._write, provider, key)

    async def delete(self, provider: str) -> None:
        await asyncio.to_thread(self._remove, provider)


def build_key_store(settings: Settings):
    if settings.key_store == "secretmanager":
        return SecretManagerKeyStore(settings.gcp_project)
    return EnvKeyStore(settings)
