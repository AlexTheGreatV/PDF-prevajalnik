from fastapi import APIRouter

from app.models import OcrStatus
from app.services.ocr import ocr_status

router = APIRouter()


@router.get("/status", response_model=OcrStatus)
def status() -> OcrStatus:
    return ocr_status()
