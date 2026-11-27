import uvicorn
from common.settings import get_settings

if __name__ == "__main__":
    settings = get_settings()

    uvicorn.run(
        app="app.app:app",
        host=settings.customer_service_host,
        port=settings.customer_service_port,
        reload=True,
    )
