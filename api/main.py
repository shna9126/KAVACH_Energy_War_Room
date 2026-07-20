from __future__ import annotations

from pathlib import Path
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from api.routes import backtest, digital_twin, kg, pipeline, signals, whatif
from api.schemas import HealthResponse
from api.websocket import router as websocket_router


load_dotenv()


app = FastAPI(title="KAVACH API", version="0.1.0")

_BOOTSTRAP_LOCK = threading.Lock()
_BOOTSTRAP_DONE = False

app.include_router(signals.router)
app.include_router(pipeline.router)
app.include_router(whatif.router)
app.include_router(kg.router)
app.include_router(backtest.router)
app.include_router(digital_twin.router)
app.include_router(websocket_router, prefix="/ws")


@app.on_event("startup")
def _warm_pipeline_once() -> None:
    # Pre-run ingestion/extraction/pipeline so first dashboard load has live data.
    global _BOOTSTRAP_DONE
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAP_DONE:
            return
        try:
            pipeline.bootstrap_refresh_on_startup()
        finally:
            # Mark done even on failures to avoid repeated startup blocking.
            _BOOTSTRAP_DONE = True

frontend_dir = Path("frontend")
if frontend_dir.exists():
    app.mount("/frontend", StaticFiles(directory=str(frontend_dir)), name="frontend")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/war-room", status_code=307)


@app.get("/war-room")
def war_room() -> FileResponse:
    global _BOOTSTRAP_DONE
    # Ensure one warm run completes before first page render in this process.
    if not _BOOTSTRAP_DONE:
        with _BOOTSTRAP_LOCK:
            if not _BOOTSTRAP_DONE:
                try:
                    pipeline.bootstrap_refresh_on_startup()
                finally:
                    _BOOTSTRAP_DONE = True

    index_path = Path("frontend/index.html")
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="War Room frontend not found")
    return FileResponse(index_path)
