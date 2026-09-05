"""
app.py — Entry point for the Banking Transaction Risk Investigation Assistant.
Starts FastAPI, serves the built frontend as static files, and mounts API routes.
"""

import os
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv

load_dotenv()

from src.api import router

app = FastAPI(
    title="Banking Transaction Risk Investigation Assistant",
    description="PS06 — NexusTiq24 Hackathon",
    version="1.0.0",
)

# Mount API routes
app.include_router(router, prefix="/api")

# Health endpoint (required: startup check within 90s)
@app.get("/health")
def health():
    return {"status": "ok", "track": "PS06"}

# Serve built frontend
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend", "dist")

if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def serve_frontend():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
else:
    @app.get("/")
    def serve_placeholder():
        return {"message": "Frontend not built yet. Run the app and check /health."}


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
