from typing import Any

from pydantic import BaseModel


class ApiResponse(BaseModel):
    status: int
    message: str | None = None
    data: Any | None = None
