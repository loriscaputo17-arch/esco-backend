from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.routers import health, compose, push, worker

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[esco-backend] starting in {settings.ENV} mode")
    yield
    print("[esco-backend] shutting down")


app = FastAPI(
    title="ESCO Backend",
    description="The backend for ESCO — composes journeys, generates feed, sends notifications.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "service": "esco-backend",
        "status": "alive",
        "docs": "/docs",
    }


# Routers
app.include_router(health.router)
app.include_router(compose.router)
app.include_router(push.router)
app.include_router(worker.router)
