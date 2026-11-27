from functools import partial
from typing import Annotated

import jwt
from fastapi import Header, HTTPException, status
from pydantic import BaseModel, Field

from common.settings import get_settings
from schemas.enums import UserRole


class CurrentUser(BaseModel):
    user_id: str = Field(title="用户 ID")
    role: UserRole = Field(default=UserRole.USER, title="用户角色")


def authorized_user(
    roles: tuple[str, ...],
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证失败, 无用户信息",
        )

    access_token = authorization[len("Bearer ") :]

    current_user = decode_access_token(access_token)

    if current_user.role not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该用户无权访问该资源",
        )

    return current_user


def decode_access_token(access_token: str) -> CurrentUser:
    settings = get_settings()

    payload = jwt.decode(
        access_token,
        key=settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )

    return CurrentUser.model_validate(payload)


def require_roles(*roles: str):
    return partial(authorized_user, roles)
