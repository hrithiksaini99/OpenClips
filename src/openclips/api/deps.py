"""FastAPI dependency factories for admin auth and request sessions."""

import secrets
from collections.abc import Callable, Generator

from fastapi import Header, HTTPException, status
from sqlalchemy.orm import Session

SessionFactory = Callable[[], Session]


def make_auth_dependency(admin_token: str) -> Callable[..., None]:
    """Build a fail-closed bearer-token dependency for mutating endpoints."""

    def require_admin(
        authorization: str | None = Header(default=None),
    ) -> None:
        if not admin_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Admin authentication is not configured on this deployment",
            )
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token, admin_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid admin credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_admin


def make_session_dependency(
    session_factory: SessionFactory,
) -> Callable[..., Generator[Session, None, None]]:
    """Yield a request-scoped session that commits on success."""

    def get_session() -> Generator[Session, None, None]:
        with session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    return get_session
