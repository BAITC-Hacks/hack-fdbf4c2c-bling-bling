from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from app.api.meetings import router
from app.api.transcripts import router as transcripts_router
from app.api.diarization import router as diarization_router
from app.services.diarization import PyannoteDiarizationService
from app.services.transcription import FasterWhisperTranscriptionService
from app.api.action_items import router as action_items_router
from app.services.extraction import ActionItemExtractionService
from app.services.ollama import OllamaClient
from app.api.summaries import router as summaries_router
from app.services.summarization import MeetingSummaryService
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.database import Base, build_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configure_logging(settings.log_level)
        for directory in ("uploads", "processed", "exports"):
            (settings.data_dir / directory).mkdir(parents=True, exist_ok=True)
        settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        engine = build_engine(settings)
        try:
            Base.metadata.create_all(engine)
            application.state.session_factory = sessionmaker(engine, expire_on_commit=False)
            application.state.transcription_service = FasterWhisperTranscriptionService(settings)
            application.state.diarization_service = PyannoteDiarizationService(settings)
            application.state.extraction_service = ActionItemExtractionService(OllamaClient(settings), settings)
            application.state.summary_service = MeetingSummaryService(OllamaClient(settings), settings)
            logging.getLogger("app").info("API started; local STT adapter ready, model loads on demand")
            yield
        finally:
            engine.dispose()

    # Disable CDN-backed Swagger/ReDoc pages: no external frontend requests.
    application = FastAPI(
        title=settings.app_name, version="0.1.0", lifespan=lifespan,
        docs_url=None, redoc_url=None,
    )
    application.state.settings = settings
    application.include_router(router)
    application.include_router(transcripts_router)
    application.include_router(diarization_router)
    application.include_router(action_items_router)
    application.include_router(summaries_router)

    @application.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
