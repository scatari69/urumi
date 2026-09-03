import hmac

from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from core.config import settings

COOKIE_NAME = "urumi_admin"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600
_SALT = "urumi-admin-session"
_PAYLOAD = "admin"

# The password is the only secret; changing it invalidates every existing session.
_serializer = URLSafeTimedSerializer(settings.ADMIN_PASSWORD, salt=_SALT)


def check_password(candidate: str) -> bool:
    return hmac.compare_digest(candidate, settings.ADMIN_PASSWORD)


def issue_token() -> str:
    return _serializer.dumps(_PAYLOAD)


def is_valid_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS) == _PAYLOAD
    except (BadSignature, SignatureExpired):
        return False


def is_authenticated(request: Request) -> bool:
    return is_valid_token(request.cookies.get(COOKIE_NAME))


class NotAuthenticated(Exception):
    pass


async def require_auth(request: Request) -> None:
    """Route dependency: raises NotAuthenticated, handled into a redirect/401."""
    if not is_authenticated(request):
        raise NotAuthenticated()


def set_session_cookie(response: RedirectResponse, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(COOKIE_NAME)
