"""Control-room dashboard: live zone status, camera health and annotated snapshots."""

import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .monitor import Monitor

DASHBOARD_HTML = (Path(__file__).parent / "static" / "dashboard.html").read_text()
security = HTTPBasic(auto_error=False)


def create_app(monitor: Monitor, password: str | None = None) -> FastAPI:
    password = password if password is not None else os.environ.get("CROWDWATCH_DASHBOARD_PASSWORD", "")
    app = FastAPI(title="CrowdWatch")

    def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        # Snapshots show real people: never expose the dashboard without a password beyond localhost.
        if not password:
            return
        if creds is None or not secrets.compare_digest(creds.password.encode(), password.encode()):
            raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Basic"})

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def dashboard() -> str:
        return DASHBOARD_HTML

    @app.get("/api/status", dependencies=[Depends(auth)])
    def status() -> dict:
        return monitor.status()

    @app.get("/api/cameras/{camera_id}/snapshot.jpg", dependencies=[Depends(auth)])
    def snapshot(camera_id: str) -> Response:
        jpg = monitor.snapshots.get(camera_id)
        if not jpg:
            raise HTTPException(404, "No snapshot yet")
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app
