"""Login: Auth0 device-authorization grant, or a pasted token as the fallback.

The backend accepts the same Auth0 JWT the dashboard uses, so the agent needs
no special auth endpoints — only a native Auth0 client that allows the device
grant (auth0_client_id / auth0_domain / auth0_audience in config).
"""
from __future__ import annotations

import time
from typing import Callable

import httpx

from .config import Credentials, Settings


class LoginError(RuntimeError):
    pass


def device_login(settings: Settings, say: Callable[[str], None] = print, timeout_s: int = 600) -> str:
    if not (settings.auth0_domain and settings.auth0_client_id and settings.auth0_audience):
        raise LoginError("auth0_domain, auth0_client_id and auth0_audience must be set in config.json "
                         "(or use `uapply-agent login --token <jwt>`)")
    base = f"https://{settings.auth0_domain}"
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{base}/oauth/device/code", data={
            "client_id": settings.auth0_client_id,
            "scope": "openid profile email offline_access",
            "audience": settings.auth0_audience,
        })
        if r.status_code != 200:
            raise LoginError(f"device code request failed: {r.status_code} {r.text[:200]}")
        d = r.json()
        say(f"Open {d.get('verification_uri_complete') or d['verification_uri']} and confirm code {d['user_code']}")
        interval = int(d.get("interval", 5))
        deadline = time.monotonic() + min(timeout_s, int(d.get("expires_in", timeout_s)))
        while time.monotonic() < deadline:
            time.sleep(interval)
            t = c.post(f"{base}/oauth/token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": d["device_code"],
                "client_id": settings.auth0_client_id,
            })
            body = t.json()
            if t.status_code == 200:
                where = Credentials.set_token(body["access_token"], body.get("refresh_token"))
                say(f"Logged in; token stored in {where}")
                return body["access_token"]
            err = body.get("error")
            if err == "slow_down":
                interval += 5
            elif err in ("authorization_pending",):
                continue
            else:
                raise LoginError(f"login failed: {err}: {body.get('error_description', '')}")
    raise LoginError("login timed out")


def token_login(token: str, say: Callable[[str], None] = print) -> str:
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token.count(".") != 2:
        raise LoginError("that does not look like a JWT")
    where = Credentials.set_token(token)
    say(f"Token stored in {where}")
    return token
