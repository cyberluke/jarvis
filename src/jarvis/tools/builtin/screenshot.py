"""Screenshot tool implementation for OCR capture."""

from typing import Dict, Any, Optional
import os
import tempfile
import subprocess
import shutil
from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

class ScreenshotTool(Tool):
    """Tool for capturing screenshots and performing OCR."""

    @property
    def name(self) -> str:
        return "screenshot"

    @property
    def description(self) -> str:
        return "Capture a selected screen region and OCR the text. Use only if the OCR will materially help."

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": []
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        """Execute the screenshot tool."""
        context.user_print("📸 Capturing a screenshot for OCR…")
        debug_log("screenshot: capturing OCR...", "screenshot")
        
        ocr_text: str = ""
        image_captured = False
        
        try:
            if os.name == 'nt':
                # Windows implementation using Pillow
                from PIL import ImageGrab, Image
                import pytesseract
                
                img = ImageGrab.grab()
                if img:
                    image_captured = True
                    tess = shutil.which("tesseract")
                    if tess:
                        text = pytesseract.image_to_string(img)
                        if text and text.strip():
                            ocr_text = text.strip()
                        else:
                            ocr_text = "Capture succeeded but no text was extracted via OCR."
                    else:
                        ocr_text = "Capture succeeded but Tesseract was not found."
                else:
                    pass
            else:
                # macOS/Linux implementation using screencapture
                sc = shutil.which("screencapture")
                if sc:
                    tmpdir = tempfile.mkdtemp(prefix="jarvis_ocr_")
                    png_path = os.path.join(tmpdir, "shot.png")
                    try:
                        cmd = [sc, "-i", png_path]
                        ret = subprocess.run(cmd, capture_output=True)
                        if ret and ret.returncode == 0 and os.path.exists(png_path):
                            image_captured = True
                            tess = shutil.which("tesseract")
                            if tess:
                                import pytesseract
                                from PIL import Image
                                with Image.open(png_path) as im:
                                    text = pytesseract.image_to_string(im)
                                    if text and text.strip():
                                        ocr_text = text.strip()
                                    else:
                                        ocr_text = "Capture succeeded but no text was extracted via OCR."
                            else:
                                ocr_text = "Capture succeeded but Tesseract was not found."
                    finally:
                        if os.path.exists(png_path):
                            os.remove(png_path)
                        if os.path.exists(tmpdir):
                            os.rmdir(tmpdir)
                else:
                    return ToolExecutionResult(
                        success=False,
                        reply_text=None,
                        error_message="No compatible screenshot tool found (screencapture)."
                    )

            if not image_captured:
                debug_log("screenshot: capture failed", "screenshot")
                return ToolExecutionResult(
                    success=False,
                    reply_text=None,
                    error_message="Capture failed or was cancelled."
                )

            debug_log(f"screenshot: ocr_chars={len(ocr_text)}", "screenshot")
            context.user_print("✅ Screenshot processed.")
            return ToolExecutionResult(success=True, reply_text=ocr_text)

        except Exception as e:
            debug_log(f"screenshot: unexpected error: {e}", "screenshot")
            return ToolExecutionResult(success=False, reply_text=None, error_message=str(e))
