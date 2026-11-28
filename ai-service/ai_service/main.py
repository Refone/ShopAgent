import uvicorn

from ai_service.common.settings import get_settings

if __name__ == "__main__":
    settings = get_settings()

    uvicorn.run(
        app="ai_service.app.app:app",
        host=settings.ai_service_host,
        port=settings.ai_service_port,
        reload=True,
    )
