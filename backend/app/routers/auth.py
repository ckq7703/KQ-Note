import requests
from fastapi import APIRouter, Depends, HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import (
    AccessTokenResponse,
    AccountOut,
    GoogleLoginRequest,
    RefreshRequest,
    TokenPair,
    UserCredentials,
)
from ..security import (
    JWTError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_token_pair(user_id: int) -> TokenPair:
    return TokenPair(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
    )


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
def register(payload: UserCredentials, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    user = User(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    return _issue_token_pair(user.id)


@router.post("/login", response_model=TokenPair)
def login(payload: UserCredentials, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if user is None or user.password_hash is None or not verify_password(
        payload.password, user.password_hash
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    return _issue_token_pair(user.id)


def _exchange_code_for_id_token(payload: GoogleLoginRequest) -> str:
    """Loopback code -> Google ID token, using the server-held client secret."""
    if not settings.google_client_secret:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Server is missing GOOGLE_CLIENT_SECRET",
        )
    try:
        resp = requests.post(
            settings.google_token_uri,
            data={
                "code": payload.code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": payload.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": payload.code_verifier,
            },
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Google token endpoint unreachable: {e}")
    if resp.status_code != 200:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Google rejected the code: {resp.text}")
    id_tok = resp.json().get("id_token")
    if not id_tok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google response has no id_token")
    return id_tok


@router.post("/google", response_model=TokenPair)
def login_with_google(payload: GoogleLoginRequest, db: Session = Depends(get_db)):
    if payload.code:
        if not payload.code_verifier or not payload.redirect_uri:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "code_verifier and redirect_uri are required with code",
            )
        raw_id_token = _exchange_code_for_id_token(payload)
    elif payload.id_token:
        raw_id_token = payload.id_token
    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide either 'code' or 'id_token'"
        )

    try:
        idinfo = google_id_token.verify_oauth2_token(
            raw_id_token, google_requests.Request(), settings.google_client_id
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid Google ID token: {e}")

    if not idinfo.get("email_verified", False):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google email is not verified")

    email = idinfo["email"]
    google_sub = idinfo["sub"]
    avatar_url = idinfo.get("picture")

    user = db.query(User).filter(User.google_sub == google_sub).first()
    if user is None:
        # First Google login for this person — link to an existing password
        # account with the same email if there is one, otherwise create fresh.
        user = db.query(User).filter(User.email == email).first()
        if user is not None:
            user.google_sub = google_sub
        else:
            user = User(email=email, password_hash=None, google_sub=google_sub)
            db.add(user)
    user.avatar_url = avatar_url  # refresh in case their Google photo changed
    db.commit()
    db.refresh(user)

    return _issue_token_pair(user.id)


@router.get("/me", response_model=AccountOut)
def get_me(user: User = Depends(get_current_user)):
    return AccountOut(email=user.email, avatar_url=user.avatar_url)


@router.post("/refresh", response_model=AccessTokenResponse)
def refresh(payload: RefreshRequest):
    invalid = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")
    try:
        data = decode_token(payload.refresh_token)
        if data.get("type") != "refresh":
            raise invalid
        user_id = int(data["sub"])
    except (JWTError, KeyError, ValueError, TypeError):
        raise invalid

    return AccessTokenResponse(access_token=create_access_token(user_id))
