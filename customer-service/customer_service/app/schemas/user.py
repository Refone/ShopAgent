from typing import Literal

from pydantic import BaseModel, Field


class CurrentUser(BaseModel):
    user_id: str = Field(
        ...,
        description="当前用户的 ID",
    )
    role: Literal["customer", "agent", "admin"] = Field(
        "customer",
        description="当前用户的角色",
    )
