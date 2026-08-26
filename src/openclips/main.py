from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from openclips.application.health import Probe, build_default_probes
from openclips.config import Settings

ProbeMap = dict[str, Probe]


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


def create_app(
    settings: Settings | None = None,
    probes: ProbeMap | None = None,
) -> FastAPI:
    """Build the OpenClips API application."""
    resolved_settings = settings or Settings()
    resolved_probes = probes if probes is not None else build_default_probes(resolved_settings)

    app = FastAPI(
        title="OpenClips",
        version="0.1.0",
        description="Self-hosted long-form video to short-form clips platform.",
    )
    app.state.settings = resolved_settings

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        body, status_code = _readiness_body(resolved_probes)
        return JSONResponse(content=body.model_dump(), status_code=status_code)

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
