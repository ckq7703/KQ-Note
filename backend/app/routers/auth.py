from fastapi import APIRouter, Depends, HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user
from ..models import Note, User
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
    db.flush()  # assign user.id before creating the note row
    db.add(Note(user_id=user.id, content="", version=0))
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


@router.post("/google", response_model=TokenPair)
def login_with_google(payload: GoogleLoginRequest, db: Session = Depends(get_db)):
    try:
        idinfo = google_id_token.verify_oauth2_token(
            payload.id_token, google_requests.Request(), settings.google_client_id
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
            db.flush()
            db.add(Note(user_id=user.id, content="", version=0))
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
