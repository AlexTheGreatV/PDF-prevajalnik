import { notFound } from "next/navigation";

import { OcrSmoke } from "../../components/ocr-smoke";

export default function OcrSmokePage() {
  if (process.env.NODE_ENV === "production" && process.env.NEXT_PUBLIC_ENABLE_OCR_SMOKE !== "true") {
    notFound();
  }
  return <OcrSmoke />;
}
