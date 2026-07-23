from fastapi import FastAPI

from app.core.config import settings

app = FastAPI(title=settings.APP_NAME)


@app.get("/")
def root():
    return {
        "message": "Welcome to MLSecOps"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy"
    }