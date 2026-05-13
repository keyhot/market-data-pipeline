from typing import Any, Optional

from pydantic import BaseModel


class ApiResponse(BaseModel):
    status: int
    message: Optional[str] = None
    data: Optional[Any] = None
