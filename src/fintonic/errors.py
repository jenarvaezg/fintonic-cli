"""Exception hierarchy and mapping from Fintonic error responses.

Observed error envelope: `{"data": null, "error": {"errorCode": "401", "error": "Invalid Token"}}`.
"""

from __future__ import annotations

import json

import httpx

# Error strings the web app special-cases (found in its bundle).
_INVALID_TOKEN = "Invalid Token"
_BLOCKED_USER = "Blocked user"
_DEVICE_NOT_VERIFIED = "Verified deviceUuid is mandatory"
_SMS_LIMIT = "PROVISION_CALLS_EXCEEDED"


class FintonicError(Exception):
    """Base class for every error raised by this library."""


class ApiError(FintonicError):
    def __init__(self, status: int, message: str, path: str | None = None) -> None:
        self.status = status
        self.message = message
        self.path = path
        super().__init__(f"HTTP {status} en {path or '?'}: {message}")


class InputError(FintonicError, ValueError):
    """Invalid user input (unknown id, malformed split...)."""


class NetworkBlockedError(FintonicError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(
            f"Cloudflare ha bloqueado la petición (HTTP {status}). "
            "Suele ser por la IP (VPN, datacenter, CI): prueba desde otra red o más tarde."
        )


class AuthError(FintonicError):
    """Login failed or the user is blocked."""


class InvalidCredentialsError(AuthError):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__(f"Email o PIN incorrectos{f' ({detail})' if detail else ''}.")


class BlockedUserError(AuthError):
    def __init__(self) -> None:
        super().__init__("Usuario bloqueado por Fintonic. Desbloquéalo desde la app móvil.")


class NotLoggedInError(AuthError):
    def __init__(self) -> None:
        super().__init__("No hay sesión. Ejecuta `fintonic login`.")


class SessionExpiredError(AuthError):
    def __init__(self) -> None:
        super().__init__("La sesión ha caducado. Ejecuta `fintonic login` (o usa `--remember` para renovarla sola).")


class DeviceVerificationRequired(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "Este dispositivo no está verificado. Completa el SMS con `fintonic verify CODIGO` "
            "o vuelve a ejecutar `fintonic login`."
        )


class SmsLimitExceeded(AuthError):
    def __init__(self) -> None:
        super().__init__("Has superado el límite de SMS de verificación. Espera un rato antes de reintentar.")


def extract_error_message(response: httpx.Response) -> str:
    """Best-effort human message from an error response, without dumping whole bodies."""
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return response.text[:200] or response.reason_phrase

    if not isinstance(body, dict):
        return str(body)[:200]

    data = body.get("data")
    if isinstance(data, dict) and data.get("error"):
        return str(data.get("error_description") or data["error"])

    error = body.get("error")
    if isinstance(error, dict):
        for key in ("error", "message", "description", "errorCode", "code"):
            if error.get(key):
                return str(error[key])
        return str(error)[:200]
    if error:
        return str(error)
    return str(body.get("message") or response.reason_phrase)


def _is_cloudflare_block(response: httpx.Response) -> bool:
    if response.status_code not in (403, 429, 503):
        return False
    return "cf-mitigated" in response.headers or "text/html" in response.headers.get("content-type", "")


def raise_for_response(response: httpx.Response) -> None:
    if response.is_success:
        return
    if _is_cloudflare_block(response):
        raise NetworkBlockedError(response.status_code)

    message = extract_error_message(response)
    if _DEVICE_NOT_VERIFIED in message:
        raise DeviceVerificationRequired()
    if _SMS_LIMIT in message:
        raise SmsLimitExceeded()
    if _BLOCKED_USER in message:
        raise BlockedUserError()
    if response.status_code == 401 or _INVALID_TOKEN in message:
        raise SessionExpiredError()
    raise ApiError(response.status_code, message, response.request.url.path)
