"""Central API router — grader-only service.

Only two routers are mounted: the public health check (used by the Docker
healthcheck and the load balancer) and the AP FRQ grader. The grader endpoints
are intentionally PUBLIC (no JWT) in every environment — they MUST be restricted
at the edge (ALB / Nginx / WAF / security group); the PDF fetch is SSRF-guarded
in app/services/grader/url_guard.py as the in-app backstop.
"""
from fastapi import APIRouter

from app.api.v1 import grader_router, health_router

# Central API Router
api_router = APIRouter()

# ── Public ──────────────────────────────────────────────────────────
api_router.include_router(health_router.router)

# ── Grader (intentionally public — restricted at the edge) ──────────
api_router.include_router(grader_router.router)
