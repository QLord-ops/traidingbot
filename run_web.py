import os

import uvicorn

if __name__ == "__main__":
    # 127.0.0.1 по умолчанию; в Docker переопределяется WEB_HOST=0.0.0.0
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8000"))
    uvicorn.run("src.web_app:app", host=host, port=port, reload=False)
