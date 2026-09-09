import time

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.services.translation import (
    LocalOpusTranslationProvider,
    LocalTranslationUnavailableError,
    TranslationInput,
)
from app.translation_models import LocalTranslationStatus, TranslationBatchRequest, TranslationBatchResponse

router = APIRouter()
_provider = LocalOpusTranslationProvider(
    model_path=settings.local_translation_model_path,
    device=settings.local_translation_device,
    compute_type=settings.local_translation_compute_type,
)


def get_local_translation_provider() -> LocalOpusTranslationProvider:
    return _provider


@router.get("/local/status", response_model=LocalTranslationStatus)
def local_status(
    provider: LocalOpusTranslationProvider = Depends(get_local_translation_provider),
) -> LocalTranslationStatus:
    return provider.status()


@router.post("/local/translate", response_model=TranslationBatchResponse)
async def local_translate(
    payload: TranslationBatchRequest,
    request: Request,
    provider: LocalOpusTranslationProvider = Depends(get_local_translation_provider),
) -> TranslationBatchResponse:
    if await request.is_disconnected():
        raise HTTPException(status_code=499, detail={"code": "cancelled", "message": "Zahteva je bila preklicana."})
    started = time.perf_counter()
    try:
        results = await run_in_threadpool(
            provider.translate_batch,
            [TranslationInput(segment_id=segment.segment_id, text=segment.text) for segment in payload.segments],
        )
    except LocalTranslationUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "provider_unavailable", "message": str(exc)},
        ) from exc
    return TranslationBatchResponse(
        provider_id=provider.status().provider_id,
        model_id=provider.status().model_id,
        processing_time_ms=max(0, round((time.perf_counter() - started) * 1000)),
        results=results,
    )
