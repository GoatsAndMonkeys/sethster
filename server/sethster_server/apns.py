"""APNs alerts for sign-ups and decisions (docs/plans/accounts.md, APNs).

Token-based auth: a provider JWT (ES256, the .p8 key) cached and refreshed every 50 minutes, one shared HTTP/2
client. Off when there is no key; the in-app badge still works. Sending runs as a background task after the
response, so push never blocks or fails a request.

Env: SETHSTER_APNS_KEY (default /secrets/apns.p8), APNS_KEY_ID, APNS_TEAM_ID (default MMZ8VT9YVZ),
SETHSTER_APNS_TOPIC (default com.goatsandmonkeys.sethster).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("sethster.apns")

HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
JWT_MAX_AGE = 50 * 60
RETRY_DELAYS = (1.0, 4.0)  # two retries after the first try, for 5xx and timeouts
GONE_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}
AUTH_REASONS = {"InvalidProviderToken", "ExpiredProviderToken", "MissingProviderToken", "BadCertificateEnvironment"}


class ApnsSender:
    def __init__(self, key_path: Path | None, key_id: str | None, team_id: str | None, topic: str,
                 drop_device: Callable[[str], None], client_factory: Callable | None = None):
        self.key_path, self.key_id, self.team_id, self.topic = key_path, key_id, team_id, topic
        self.drop_device = drop_device
        self._client_factory = client_factory
        self._client = None
        self._jwt: tuple[float, str] | None = None
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.sent = 0
        self._key: str | None = None
        if key_path and key_id and team_id:
            try:
                self._key = key_path.read_text()
            except OSError as e:
                log.warning("APNs key %s unreadable (%s); push off", key_path, e)
        if not self.enabled:
            log.info("APNs push off (no key / APNS_KEY_ID / APNS_TEAM_ID)")

    @classmethod
    def from_env(cls, drop_device: Callable[[str], None]) -> "ApnsSender":
        return cls(Path(os.environ.get("SETHSTER_APNS_KEY", "/secrets/apns.p8")), os.environ.get("APNS_KEY_ID"),
                   os.environ.get("APNS_TEAM_ID", "MMZ8VT9YVZ"),
                   os.environ.get("SETHSTER_APNS_TOPIC", "com.goatsandmonkeys.sethster"), drop_device)

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def status(self) -> dict:
        return {"enabled": self.enabled, "sent": self.sent, "last_error": self.last_error,
                "last_error_at": self.last_error_at}

    def _provider_token(self) -> str:
        if self._jwt and time.time() - self._jwt[0] < JWT_MAX_AGE:
            return self._jwt[1]
        import jwt  # PyJWT[crypto]; imported here so the server runs without it when push is off
        t = time.time()
        tok = jwt.encode({"iss": self.team_id, "iat": int(t)}, self._key, algorithm="ES256",
                         headers={"kid": self.key_id})
        self._jwt = (t, tok)
        return tok

    def _http(self):
        if self._client is None:
            if self._client_factory:
                self._client = self._client_factory()
            else:
                import httpx
                self._client = httpx.AsyncClient(http2=True, timeout=10.0)
        return self._client

    def _fail(self, msg: str) -> None:
        log.error("APNs: %s", msg)
        self.last_error, self.last_error_at = msg, time.time()

    async def send(self, apns_token: str, env: str, alert: str, badge: int | None = None) -> bool:
        """One alert to one device; True when APNs accepted it. Never raises."""
        if not self.enabled:
            return False
        payload = {"aps": {"alert": alert, "sound": "default", **({"badge": badge} if badge is not None else {})}}
        url = f"{HOSTS.get(env, HOSTS['production'])}/3/device/{apns_token}"
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                headers = {"authorization": f"bearer {self._provider_token()}", "apns-topic": self.topic,
                           "apns-push-type": "alert", "apns-priority": "10"}
                r = await self._http().post(url, json=payload, headers=headers)
            except Exception as e:  # noqa: BLE001 - timeouts, connection errors, a bad key
                status, reason = None, f"{type(e).__name__}: {e}"
            else:
                status = r.status_code
                if status == 200:
                    self.sent += 1
                    return True
                try:
                    reason = (r.json() or {}).get("reason") or ""
                except ValueError:
                    reason = ""
            if status == 410 or reason in GONE_REASONS:
                log.info("APNs: device %s… gone (%s %s); dropped", apns_token[:8], status, reason)
                try:  # SQLite off the event loop: a busy accounts.db must not stall every request
                    await asyncio.to_thread(self.drop_device, apns_token)
                except Exception:  # noqa: BLE001
                    log.exception("APNs: dropping device %s… failed", apns_token[:8])
                return False
            if status == 403 or reason in AUTH_REASONS:
                self._jwt = None
                self._fail(f"credentials refused: {status} {reason}")
                return False
            if status is not None and status < 500 and status != 429:
                self._fail(f"rejected: {status} {reason}")
                return False
            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(RETRY_DELAYS[attempt])
        self._fail(f"gave up after retries: {status} {reason}")
        return False

    async def send_many(self, devices, alert: str, badge: int | None = None) -> None:
        for d in devices:
            await self.send(d["apns_token"], d["env"], alert, badge)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
