import os
import io
import json
import base64
import tempfile
import traceback
from typing import List, Dict, Any

import requests
from flask import Flask, request, jsonify, send_file

from google import genai
from dotenv import load_dotenv

# ---------------- CONFIG ----------------
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.0-flash"
client = genai.Client(api_key=GEMINI_API_KEY)

app = Flask(__name__)


# ---------------- FRONTEND ROUTE ----------------

@app.route("/", methods=["GET"])
def home():
    return send_file("frontend.html")   # serve frontend


# ---------------- FILE TYPE HELPERS ----------------

def get_extension_from_content_type(content_type: str) -> str:
    content_type = (content_type or "").lower()

    if "pdf" in content_type:
        return ".pdf"
    if "png" in content_type:
        return ".png"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    if "gif" in content_type:
        return ".gif"

    return ".bin"


def get_extension_from_magic_bytes(header: bytes) -> str:
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
    response = requests.get(url, timeout=60)
    response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    suffix = get_extension_from_content_type(content_type)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(response.content)
    tmp.close()
    return tmp.name


def save_uploaded_file(file_storage) -> str:
    suffix = os.path.splitext(file_storage.filename)[1].lower()

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


def get_token_usage(response):
    usage = response.usage_metadata

    if usage is None:
        return 0, 0, 0

    input_tokens = getattr(usage, "prompt_token_count", 0)
    output_tokens = getattr(usage, "candidates_token_count", 0)
    total_tokens = getattr(usage, "total_token_count",
                           input_tokens + output_tokens)

    return input_tokens, output_tokens, total_tokens



# ---------------- GEMINI PROMPT ----------------

EXTRACTION_PROMPT = """
You are an expert in invoice understanding.

CRITICAL RULE ABOUT ORDER:
- You MUST preserve the EXACT order of all line items exactly as they appear in the document.
- Do NOT sort, regroup, reorder, merge or rearrange anything.
- The first visible item in the document must be the first item in bill_items.
- The last visible item must be the last in bill_items.
- Maintain pure top-to-bottom visual order exactly as shown in the bill.

Allowed page_type values ONLY:
- "Bill Detail"
- "Final Bill"
- "Pharmacy"
based on the content decide which is best suitable for page_type from allowed values

Your output MUST strictly follow this EXACT JSON schema:

{
  "pagewise_line_items": [
    {
      "page_no": "string(only number)",
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

STRICT RULES:
- Extract every line item exactly once.
- DO NOT add missing items.
- DO NOT group items.
- DO NOT move items.
- DO NOT correct spelling.
- DO NOT infer anything; extract exactly as is.
- Do NOT add subtotal or final total.
- Return ONLY valid JSON. No comments, no text outside JSON.
"""



# ---------------- API PROCESSING ----------------

@app.route("/extract-bill-data", methods=["POST"])
def extract_bill_data():
    try:
        if "file" in request.files:
            path = save_uploaded_file(request.files["file"])
        else:
            body = request.get_json(force=True)
            if "document" not in body:
                return jsonify({"is_success": False, "error": "Missing 'document'"}), 400

            doc = body["document"]

            if doc.startswith("http://") or doc.startswith("https://"):
                path = download_file(doc)
            else:
                raw = base64.b64decode(doc)
                header = raw[:4]
                ext = get_extension_from_magic_bytes(header)

                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                tmp.write(raw)
                tmp.close()
                path = tmp.name

        file_ref = client.files.upload(file=path)

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[EXTRACTION_PROMPT, "Here is the file:", file_ref]
        )

        raw = response.text.strip()

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

        input_tokens, output_tokens, total_tokens = get_token_usage(response)

        return jsonify({
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
        }), 200

    except Exception as e:
        return jsonify({
            "is_success": False,
            "error": str(e),
            "trace": traceback.format_exc()
        }), 500


# -------- Render-compatible entry point --------
if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=PORT)
