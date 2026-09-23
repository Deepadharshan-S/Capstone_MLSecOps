from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from app.core.rate_limiter import RateLimiter

from app.db.session import get_db
from app.schemas.auth import Token, UserCreate, UserResponse, MessageResponse
from app.services.dependencies import get_auth_service
from app.services.auth import AuthService

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(times=5, seconds=60))],
)
def register(
    user_in: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    auth_service: AuthService = Depends(get_auth_service),
):
    """
    Registers a new user after enforcing the password complexity policy.
    """
    ip_addr = request.client.host if request.client else None
    return auth_service.register_user(
        db=db,
        username=user_in.username,
        email=user_in.email,
        password=user_in.password,
        ip_address=ip_addr,
        user_in=user_in,
    )


@router.post(
    "/login",
    response_model=Token,
    dependencies=[Depends(RateLimiter(times=5, seconds=60))],
)
def login(
    response: Response,
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Session = Depends(get_db),
    auth_service: AuthService = Depends(get_auth_service),
):
    """
    Authenticates user, handles brute-force lockout, and issues access/refresh tokens.
    HTTP cookie management is handled exclusively at the router layer.
    """
    ip_addr = request.client.host if request.client else None
    token_dict = auth_service.authenticate_user(
        db=db,
        username=form_data.username,
        password=form_data.password,
        ip_address=ip_addr,
    )

    # Set secure HttpOnly cookie for the refresh token
    response.set_cookie(
        key="refresh_token",
        value=token_dict["refresh_token"],
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/api/auth",
    )

    return {"access_token": token_dict["access_token"], "token_type": token_dict["token_type"]}


@router.post(
    "/refresh",
    response_model=Token,
    dependencies=[Depends(RateLimiter(times=20, seconds=60))],
)
def refresh(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    auth_service: AuthService = Depends(get_auth_service),
):
    """
    Refreshes access and refresh tokens using Refresh Token Rotation (RTR).
    Includes automatic reuse detection to mitigate token theft.
    """
    ip_addr = request.client.host if request.client else None
    refresh_token = request.cookies.get("refresh_token")

    token_dict = auth_service.rotate_refresh_token(
        db=db,
        refresh_token=refresh_token,
        ip_address=ip_addr,
    )

    # Re-issue rotated secure HttpOnly cookie
    response.set_cookie(
        key="refresh_token",
        value=token_dict["refresh_token"],
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/api/auth",
    )

    return {"access_token": token_dict["access_token"], "token_type": token_dict["token_type"]}


@router.post(
    "/logout",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(RateLimiter(times=10, seconds=60))],
)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    auth_service: AuthService = Depends(get_auth_service),
):
    """
    Logs out the user by revoking the refresh token, blacklisting the access token, and clearing the cookie.
    """
    ip_addr = request.client.host if request.client else None

    # Extract access token from Authorization header
    access_token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        access_token = auth_header.split(" ")[1]

    # Extract refresh token from cookie
    refresh_token = request.cookies.get("refresh_token")

    result = auth_service.logout_user(
        db=db,
        access_token=access_token,
        refresh_token=refresh_token,
        ip_address=ip_addr,
    )

    # Clear HttpOnly cookie on client
    response.delete_cookie(key="refresh_token", path="/api/auth")

    return result
