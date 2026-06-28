"""Response schema for the health/liveness endpoints."""
from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = Field(description="Liveness status, e.g. 'ok' or 'pong'.")
    message: str | None = Field(default=None, description="Optional human-readable detail.")
