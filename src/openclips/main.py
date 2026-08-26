from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import sessionmaker

from openclips.api.deps import make_auth_dependency, make_session_dependency
from openclips.api.routes import build_router
from openclips.application.health import Probe, build_default_probes
from openclips.application.services import AppServices, build_services
from openclips.config import Settings

ProbeMap = dict[str, Probe]
SessionFactory = Any


class ReadinessBody(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, str]


def _readiness_body(probes: ProbeMap) -> tuple[ReadinessBody, int]:
    checks: dict[str, str] = {}
    for name, probe in sorted(probes.items()):
        try:
            probe()
        except Exception as error:
            checks[name] = f"unavailable: {error}"
        else:
            checks[name] = "ok"
    ready = all(result == "ok" for result in checks.values())
    return ReadinessBody(status="ready" if ready else "degraded", checks=checks), (
        200 if ready else 503
    )


def _default_session_factory(settings: Settings) -> SessionFactory:
    from openclips.infrastructure.db import make_engine

    engine = make_engine(settings.database_url)
    return sessionmaker(bind=engine)


def create_app(
    settings: Settings | None = None,
    probes: ProbeMap | None = None,
    session_factory: SessionFactory | None = None,
    services: AppServices | None = None,
) -> FastAPI:
    """Build the OpenClips API application."""
    resolved_settings = settings or Settings()
    resolved_probes = probes if probes is not None else build_default_probes(resolved_settings)
    resolved_sessions = session_factory or _default_session_factory(resolved_settings)
    resolved_services = services or build_services(resolved_settings)

    app = FastAPI(
        title="OpenClips",
        version="0.1.0",
        description="Self-hosted long-form video to short-form clips platform.",
    )
    app.state.settings = resolved_settings
    app.state.session_factory = resolved_sessions
    app.state.services = resolved_services

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        body, status_code = _readiness_body(resolved_probes)
        return JSONResponse(content=body.model_dump(), status_code=status_code)

    get_session = make_session_dependency(resolved_sessions)
    require_admin = make_auth_dependency(resolved_settings.admin_token)
    app.include_router(
        build_router(
            get_session=get_session,
            require_admin=require_admin,
            services=resolved_services,
        )
    )

    return app


def serve() -> None:
    """Run the API with uvicorn; used by the console script entry point."""
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "openclips.main:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
