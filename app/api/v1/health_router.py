from fastapi import APIRouter

from app.controllers import health_controller
from app.schemas.health_schema import HealthResponse

router = APIRouter(
    prefix="/health",
    tags=["Health"]
)

@router.get("", response_model=HealthResponse, summary="Health check")
def health_check():
    """
    API endpoint to check if the service is running.
    Delegates immediately to the controller.
    """
    return health_controller.get_health_status()

@router.get("/ping", response_model=HealthResponse, summary="Ping")
def ping():
    """
    Simple ping endpoint.
    Delegates immediately to the controller.
    """
    return health_controller.get_ping_status()
