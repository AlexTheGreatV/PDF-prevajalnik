from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.ocr import router as ocr_router
from app.api.pdf import router as pdf_router
from app.api.translation import router as translation_router
from app.api.glossary import router as glossary_router
from app.api.translation_memory import router as translation_memory_router
from app.api.terminology_import import router as terminology_import_router
from app.core.config import settings

app = FastAPI(title="JUB/KOOP PDF Pipeline", version="0.1.0")
PDF_EXPORT_PATHS = {"/api/pdf/export-translated", "/api/pdf/export-translated/plan",
                    "/api/pdf/export-translated/reconstruction/preview", "/api/pdf/export-translated/reconstruction/download"}


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-PDF-Export-Mode", "X-PDF-Replacements-Applied",
                    "X-PDF-Typography-Profile", "X-PDF-Typography-Revision", "X-PDF-Source-Formatted",
                    "X-PDF-Reconstruction-Token", "X-PDF-Reconstruction-Counts"],
)


@app.middleware("http")
async def enforce_upload_size(request: Request, call_next):
    is_pdf_export = request.url.path in PDF_EXPORT_PATHS
    limit = settings.max_pdf_export_request_bytes if is_pdf_export else settings.max_upload_bytes
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Content-Length ni veljaven."})
        if declared_length < 0:
            return JSONResponse(status_code=400, content={"detail": "Content-Length ni veljaven."})
        if declared_length > limit:
            return JSONResponse(status_code=413, content={"detail": "Zahtevek je prevelik."})

    if is_pdf_export:
        messages = []
        received = 0
        while True:
            message = await request._receive()
            messages.append(message)
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    return JSONResponse(status_code=413, content={"detail": "Zahtevek je prevelik."})
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        message_index = 0

        async def replay_receive():
            nonlocal message_index
            if message_index < len(messages):
                message = messages[message_index]
                message_index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        request._receive = replay_receive

    return await call_next(request)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(pdf_router, prefix="/api/pdf", tags=["pdf"])
app.include_router(ocr_router, prefix="/api/ocr", tags=["ocr"])
app.include_router(translation_router, prefix="/api/translation/providers", tags=["translation"])
app.include_router(glossary_router, prefix="/api/glossary", tags=["glossary"])
app.include_router(translation_memory_router, prefix="/api/translation-memory", tags=["translation-memory"])
app.include_router(terminology_import_router, prefix="/api/glossary/imports/terminology", tags=["terminology-import"])
