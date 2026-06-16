from app.schemas.health_schema import HealthResponse
from app.services import health_service


def get_health_status() -> HealthResponse:
    """
    Controller layer handling the orchestrations for the /health endpoint
    """
    # Call service to get data
    data = health_service.check_health()
    # Format and return the appropriate Pydantic schema
    return HealthResponse(**data)


def get_ping_status() -> HealthResponse:
    data = health_service.check_ping()
    return HealthResponse(**data)
