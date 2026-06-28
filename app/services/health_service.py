def check_health() -> dict:
    """
    Service layer for checking health.
    Can include DB pings, Redis checks, etc.
    """
    # Simply returning a dictionary representing the core business logic state
    return {"status": "ok", "message": "Service is healthy"}

def check_ping() -> dict:
    return {"status": "pong", "message": None}
