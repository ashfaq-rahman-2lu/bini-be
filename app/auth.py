"""One configured account and signed HttpOnly cookie sessions."""
import hashlib
import hmac
import json
import secrets
from collections import deque
from threading import Lock
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadData, URLSafeTimedSerializer
from pydantic import BaseModel, Field, SecretStr

from .config import settings

SESSION_SECONDS = 8 * 60 * 60
router = APIRouter(prefix="/api/auth", tags=["authentication"])
COOKIE_NAME = "bini_session"
_failed_logins: deque[float] = deque(maxlen=10)
_login_lock = Lock()


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=1024)


def serializer():
    password = settings.auth_password.get_secret_value()
    secret = settings.auth_secret.get_secret_value()
    if not settings.auth_username or not password or len(secret) < 32:
        raise HTTPException(503, "Configure AUTH_USERNAME, AUTH_PASSWORD and AUTH_SECRET on the backend.")
    # Changing either credential or the signing secret invalidates existing sessions.
    key = hmac.digest(secret.encode(), json.dumps([settings.auth_username, password]).encode(), "sha256")
    return URLSafeTimedSerializer(key, salt="bini-session-v1", signer_kwargs={"digest_method": hashlib.sha256})


def verify_request(request: Request):
    # A non-simple header forces cross-origin browsers to pass CORS preflight.
    # Origin checking also protects against unwanted same-site sibling origins.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("X-Bini-Request") != "1":
            raise HTTPException(403, "Missing request protection header.")
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            raise HTTPException(403, "Request origin is not allowed.")


def require_auth(request: Request):
    signer = serializer()
    try:
        session = signer.loads(request.cookies.get(COOKIE_NAME, ""), max_age=SESSION_SECONDS)
    except BadData:
        raise HTTPException(401, "Sign in to continue.") from None
    if not isinstance(session, dict) or session.get("username") != settings.auth_username:
        raise HTTPException(401, "Sign in to continue.")
    verify_request(request)
    return settings.auth_username


@router.post("/login")
def login(body: Login, response: Response, _: Annotated[None, Depends(verify_request)]):
    signer = serializer()
    with _login_lock:
        now = monotonic()
        while _failed_logins and _failed_logins[0] <= now - 60:
            _failed_logins.popleft()
        if len(_failed_logins) == 10:
            raise HTTPException(429, "Too many sign-in attempts. Try again in one minute.", headers={"Retry-After": "60"})
        correct_user = secrets.compare_digest(body.username.encode(), settings.auth_username.encode())
        correct_password = secrets.compare_digest(body.password.get_secret_value().encode(), settings.auth_password.get_secret_value().encode())
        if not (correct_user and correct_password):
            _failed_logins.append(now)
            raise HTTPException(401, "Incorrect username or password.")
        _failed_logins.clear()
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(COOKIE_NAME,
        signer.dumps({"username": settings.auth_username, "nonce": secrets.token_urlsafe(16)}),
        max_age=SESSION_SECONDS, httponly=True, secure=settings.auth_cookie_secure, samesite="lax", path="/api")
    return {"username": settings.auth_username}


@router.post("/logout", status_code=204)
def logout(response: Response, _: Annotated[None, Depends(verify_request)]):
    response.delete_cookie(COOKIE_NAME, path="/api", httponly=True,
                           secure=settings.auth_cookie_secure, samesite="lax")
    response.headers["Cache-Control"] = "no-store"


@router.get("/session")
def session(response: Response, username: Annotated[str, Depends(require_auth)]):
    response.headers["Cache-Control"] = "no-store"
    return {"username": username}
