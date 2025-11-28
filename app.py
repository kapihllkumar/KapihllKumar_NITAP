import os
import io
import json
import base64
import tempfile
import traceback
from typing import List, Dict, Any

import requests
from flask import Flask, request, jsonify

from google import genai

# ---------------- CONFIG ----------------
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.0-flash"
client = genai.Client(api_key=GEMINI_API_KEY)

app = Flask(__name__)


# ---------------- FILE TYPE HELPERS ----------------

def get_extension_from_content_type(content_type: str) -> str:
    """Return correct file extension based on MIME type."""
    content_type = (content_type or "").lower()

    if "pdf" in content_type:
        return ".pdf"
    if "png" in content_type:
        return ".png"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    if "gif" in content_type:
        return ".gif"

    return ".bin"  # fallback


def get_extension_from_magic_bytes(header: bytes) -> str:
    """Detect file type using magic bytes from Base64 or uploads without extension."""
    if header.startswith(b"%PDF"):
        return ".pdf"
    if header.startswith(b"\x89PNG"):
        return ".png"
    if header[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if header.startswith(b"GIF"):
        return ".gif"
    return ".bin"


# ---------------- UTILITIES ----------------

def download_file(url: str) -> str:
    """Download file from URL and save with correct extension."""
    response = requests.get(url, timeout=60)
    response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    suffix = get_extension_from_content_type(content_type)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(response.content)
    tmp.close()
    return tmp.name


def save_uploaded_file(file_storage) -> str:
    """Save uploaded file (PDF or Image) with correct extension."""
    suffix = os.path.splitext(file_storage.filename)[1].lower()

    # If file had no extension, detect from MIME type
    if suffix == "":
        suffix = get_extension_from_content_type(file_storage.content_type)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    file_storage.save(tmp.name)
    return tmp.name


def safe_float(v) -> float:
    try:
        if v is None or v == "":
            return 0.0
        s = str(v).replace(",", "").replace("₹", "").replace("$", "")
        s = s.replace("(", "-").replace(")", "").strip()
        return float(s)
    except:
        return 0.0


# ---------------- GEMINI PROMPT ----------------

EXTRACTION_PROMPT = """
You are an expert in invoice understanding. You MUST detect page type yourself.

Possible page_type values ONLY:
- "Bill Detail"
- "Final Bill"
- "Pharmacy"

Your output MUST strictly follow this EXACT JSON schema:

{
  "pagewise_line_items": [
    {
      "page_no": "string",
      "page_type": "Bill Detail | Final Bill | Pharmacy",
      "bill_items": [
        {
          "item_name": "string",
          "item_amount": float,
          "item_rate": float,
          "item_quantity": float
        }
      ]
    }
  ]
}

Rules:
- Detect page_type based on content.
- Extract every line item exactly once.
- Do NOT add any subtotal or final total.
- Return valid JSON ONLY. No comments, no text outside JSON.
"""


# ---------------- API PROCESSING ----------------

@app.route("/extract-bill-data", methods=["POST"])
def extract_bill_data():
    try:
        # -------- Step 1: Load file or URL --------
        if "file" in request.files:
            path = save_uploaded_file(request.files["file"])

        else:
            body = request.get_json(force=True)
            if "document" not in body:
                return jsonify({"is_success": False, "error": "Missing 'document'"}), 400

            doc = body["document"]

            # Case 1: URL
            if doc.startswith("http://") or doc.startswith("https://"):
                path = download_file(doc)

            # Case 2: base64 PDF or image
            else:
                raw = base64.b64decode(doc)

                header = raw[:4]  # First 4 bytes
                ext = get_extension_from_magic_bytes(header)

                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                tmp.write(raw)
                tmp.close()
                path = tmp.name

        # -------- Step 2: Upload file to Gemini --------
        file_ref = client.files.upload(file=path)

        # -------- Step 3: Call Gemini LLM --------
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[EXTRACTION_PROMPT, "Here is the file:", file_ref]
        )

        raw = response.text.strip()

        # -------- Step 4: Parse JSON safely --------
        try:
            parsed = json.loads(raw)
        except:
            import re
            m = re.search(r"(\{[\s\S]*\})", raw)
            if m:
                parsed = json.loads(m.group(1))
            else:
                return jsonify({
                    "is_success": False,
                    "error": "Gemini did not return valid JSON",
                    "raw": raw
                }), 500

        pages = parsed.get("pagewise_line_items", [])

        # -------- Step 5: Post-processing --------
        total_item_count = 0
        output_pages = []

        for page in pages:
            page_no = str(page.get("page_no", ""))
            page_type = page.get("page_type", "Bill Detail")
            bill_items = page.get("bill_items", [])

            normalized_items = []
            for it in bill_items:
                normalized_items.append({
                    "item_name": (it.get("item_name") or "").strip(),
                    "item_amount": safe_float(it.get("item_amount")),
                    "item_rate": safe_float(it.get("item_rate")),
                    "item_quantity": safe_float(it.get("item_quantity")),
                })

            total_item_count += len(normalized_items)

            output_pages.append({
                "page_no": page_no,
                "page_type": page_type,
                "bill_items": normalized_items
            })

        # -------- Step 6: Token usage --------
        try:
            usage = response.metrics
            input_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
            total_tokens = input_tokens + output_tokens
        except:
            input_tokens = output_tokens = total_tokens = 0

        # -------- Step 7: Final Output --------
        result = {
            "is_success": True,
            "token_usage": {
                "total_tokens": total_tokens,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens
            },
            "data": {
                "pagewise_line_items": output_pages,
                "total_item_count": total_item_count
            }
        }

        return jsonify(result), 200

    except Exception as e:
        return jsonify({
            "is_success": False,
            "error": str(e),
            "trace": traceback.format_exc()
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
