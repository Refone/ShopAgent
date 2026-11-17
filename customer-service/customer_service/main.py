import uvicorn
from common.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        app="app.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
    )
