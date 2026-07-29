from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.token import Token
from app.schemas.user import UserCreate, UserResponse
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post(
    "/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED
)
def register(user_in: UserCreate, request: Request, db: Session = Depends(get_db)):
    """
    Registers a new user after enforcing the password complexity policy.
    """
    ip_addr = request.client.host if request.client else None
    return auth_service.register_user(db, user_in, ip_addr)


@router.post("/login", response_model=Token)
def login(
    response: Response,
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Session = Depends(get_db),
):
    """
    Authenticates user, handles brute-force lockout, and issues access/refresh tokens.
    """
    ip_addr = request.client.host if request.client else None
    return auth_service.authenticate_user(db, response, form_data, ip_addr)


@router.post("/refresh", response_model=Token)
def refresh(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    Refreshes access and refresh tokens using Refresh Token Rotation (RTR).
    Includes automatic reuse detection to mitigate token theft.
    """
    ip_addr = request.client.host if request.client else None
    return auth_service.rotate_refresh_token(db, request, response, ip_addr)


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    Logs out the user by revoking the refresh token, blacklisting the access token, and clearing the cookie.
    """
    ip_addr = request.client.host if request.client else None
    return auth_service.logout_user(db, request, response, ip_addr)
