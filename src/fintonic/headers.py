"""Per-request auth headers, mirroring the Android app's interceptor (`mc/c.java`).

The app sends `Authorization`, `finia_token` and `x-fintonic-user-code` on every request,
but `DEVICE-UUID` only once the device is verified. The device verification calls
themselves (`device/provisions`, `device/verification`) need it before that, as the web does.
"""

from __future__ import annotations

from fintonic.session import Session


def auth_headers(session: Session, *, force_device: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    if session.access_token:
        headers["authorization"] = f"Bearer {session.access_token}"
    if session.finia_token:
        headers["finia_token"] = session.finia_token
    if session.user_code:
        headers["x-fintonic-user-code"] = session.user_code
    if session.device_uuid and (session.device_verified or force_device):
        headers["device-uuid"] = session.device_uuid
    return headers
