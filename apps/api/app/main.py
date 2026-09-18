from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.ocr import router as ocr_router
from app.api.pdf import router as pdf_router
from app.api.translation import router as translation_router
from app.api.glossary import router as glossary_router
from app.core.config import settings

app = FastAPI(title="JUB/KOOP PDF Pipeline", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


@app.middleware("http")
async def enforce_upload_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.max_upload_bytes:
        return JSONResponse(status_code=413, content={"detail": "PDF je prevelik."})
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(pdf_router, prefix="/api/pdf", tags=["pdf"])
app.include_router(ocr_router, prefix="/api/ocr", tags=["ocr"])
app.include_router(translation_router, prefix="/api/translation/providers", tags=["translation"])
app.include_router(glossary_router, prefix="/api/glossary", tags=["glossary"])
