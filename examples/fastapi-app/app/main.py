"""Main FastAPI application."""

from fastapi import FastAPI

app = FastAPI(title="pycontainer-build Demo", version="1.0.0")


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": "Hello from pycontainer-build!",
        "framework": "FastAPI",
        "builder": "pycontainer-build"
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/info")
async def info():
    """Application info endpoint."""
    return {
        "name": "fastapi-demo",
        "version": "1.0.0",
        "description": "Demo app showing pycontainer-build integrations",
        "integrations": [
            "Poetry plugin",
            "Hatch plugin",
            "GitHub Actions",
            "Azure Developer CLI",
            "VS Code Extension"
        ]
    }
