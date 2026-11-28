from fastapi import FastAPI

from ai_service.app.routers import health, run

app = FastAPI(title="AI Service", version="0.1.0")


app.include_router(health.router)
app.include_router(run.router)
