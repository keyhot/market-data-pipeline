from pydantic import BaseModel
from typing import Optional, Any


class ApiResponse(BaseModel):
    status: int
    message: Optional[str] = None
    data: Optional[Any] = None
