from typing import Any

import structlog
from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from scribe.container import Container
from scribe.domain.errors import DependencyUnavailableError
from scribe.infrastructure.readiness import ReadinessChecker

router = APIRouter(prefix="/health", tags=["health"])
log = structlog.get_logger()


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
@inject
async def ready(
    checker: ReadinessChecker = Depends(Provide[Container.readiness]),
) -> dict[str, Any]:
    results = await checker.check()
    failed = sorted(name for name, error in results.items() if error)
    if failed:
        for name in failed:  # details go to the log, not to the client
            log.warning("readiness.failed", dependency=name, error=results[name])
        raise DependencyUnavailableError(f"Not ready: {', '.join(failed)}")
    return {"status": "ok", "checks": sorted(results)}
