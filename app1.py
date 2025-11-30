import os
import io
import json
import base64
import re
import tempfile
import traceback
from typing import List, Dict, Any

import requests
from flask import Flask, request, jsonify, send_file

from google import genai
from dotenv import load_dotenv
import fitz   # PDF → image conversion

# ---------------- CONFIG ----------------
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.5-flash"
client = genai.Client(api_key=GEMINI_API_KEY)

app = Flask(__name__)


# ---------------- FRONTEND ROUTE ----------------

@app.route("/", methods=["GET"])
def home():
    return send_file("frontend.html")


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

    output_path = os.path.join(
        tempfile.gettempdir(),
        f"bill_download_{next(tempfile._get_candidate_names())}{suffix}"
    )

    with open(output_path, "wb") as f:
        f.write(response.content)

    return output_path


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

OUTPUT FORMAT (strict JSON):
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

-------------- ITEM EXTRACTION RULES --------------

• Preserve EXACT top-to-bottom item order. No sorting or reordering.
• Treat multi-line names as ONE item_name.
• Extract ALL items appearing anywhere on the page (multiple boxes/columns included).
• Never extract headers, totals, discounts, taxes, round-off, or summaries.
• Multi-column pages: read column 1 fully, then column 2, then column 3.
 Handwritten medicine names MUST be extracted even if unclear and is an item
background or low contrast should NOT cause item skipping.

-------------- TEXT NORMALIZATION --------------

• item_name may contain ONLY ASCII: A–Z, a–z, 0–9, space, -, /, ., +, ()  
• Remove all non-ASCII garbage: accents, bullets, curly quotes, invisible marks.  
  Example: “Pavtaví­z-DSR” → “Pavtaviz-DSR”

• Normalize any unicode-dot or NBSP-dot to a plain ".".

-------------- QUANTITY / RATE / AMOUNT RULES --------------

• Quantity must be taken only from the real QTY text.  
• OCR-decimals like 1.79, 1.46, 2.16 → use INTEGER part unless true decimal shown.  
• Formats like “1x2”, “2×10” → multiply (1×2=2, 2×10=20).

• If two numbers appear (excluding qty):
    - smaller → item_rate
    - larger → item_amount
• If only per-unit amount appears and qty > 1 → treat as item_rate.
• If a vertically aligned handwritten number exists → treat as amount.
• Split numbers like “266 94”, “266-94”, “266\n94” → 266.94

-------------- PAGE TYPE CLASSIFICATION --------------

Set page_type = "Pharmacy" if ANY item looks like a medicine:
• Brand names (Pantaviz, Amitias, Domitox, Divalgress-ER-500, etc.)  
• Formulations: ER, SR, DSR, XR, Forte, Plus, Injection, Suspension, Tablet, Capsule  
If NO medicine-like items → choose “Bill Detail” or “Final Bill”.  
When unsure → choose “Pharmacy”.


A “slip” = the boxed yellow area with Qty / Name of the Drug / Batch No / Exp / Rs.
• If the page contains two slips (top + bottom), you MUST extract items from both in order.
• Re-read every visible handwritten row until all items are extracted.
Convert messy handwriting into best-guess ASCII:
Examples:
    “Gurati 25” → “Gurati 25”
    “Igurati 25” → “Igurati 25”
    “Shelcal 500” → “Shelcal 500”
    “Pelastinar” → “Pelastinar”
    “Prilaystmon” → “Prilaystmon”
    “Gluticone Cox” → “Gluticone-Cox”


-------------- QUANTITY CORRECTION (CRITICAL) --------------
• Quantity MUST be realistic and integer unless explicitly printed decimal.
• Fix obvious OCR errors:
    - If OCR gives huge wrong numbers (e.g., 600, 300, 2000 for 3×, 6× slips), infer the TRUE integer quantity from context.
    - If the page shows “3X”, “3 x”, “3 tab”, “3pcs”, “3no”, “3 strips”, then quantity MUST be 3.
    - If amount is total and per-unit rate is missing, infer quantity from typical pharmacy patterns.
• If OCR returns decimals like 1.79, 8.25 → use integer part 
(1, 8).
• Quantity must match handwritten Qty column on that page.
if you fin 3X12 ddont output as 31 just 3 as output

-------------- DO NOT INCLUDE --------------

• totals, subtotals, net, balance, deposit, refund
• discounts, concessions, GST, CGST, SGST, round off
• section headers, titles, or column labels

Return ONLY valid JSON.




"""


# ---------------- PDF PAGE SPLITTING ----------------

def split_pdf_to_images(pdf_path: str):
    doc = fitz.open(pdf_path)
    imgs = []

    for page_number in range(len(doc)):
        page = doc.load_page(page_number)
        pix = page.get_pixmap(dpi=200)

        filename = f"bill_page_{page_number+1}_{next(tempfile._get_candidate_names())}.png"
        output_path = os.path.join(tempfile.gettempdir(), filename)

        pix.save(output_path)
        imgs.append(output_path)

    doc.close()
    return imgs


# ---------------- FILE SERVE ----------------

@app.route("/tmp-file/<filename>", methods=["GET"])
def get_tmp_file(filename):
    path = f"/tmp/{filename}"
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    return send_file(path)


# ---------------- MAIN API ----------------

@app.route("/extract-bill-data", methods=["POST"])
def extract_bill_data():
    try:
        # ----- FILE INPUT -----
        if "file" in request.files:
            path = save_uploaded_file(request.files["file"])
            print(f"[INPUT RECEIVED] Source file path - {path}")
        else:
            body = request.get_json(force=True)
            if "document" not in body:
                return jsonify({"is_success": False, "error": "Missing 'document'"}), 400

            doc = body["document"]

            if doc.startswith("http://") or doc.startswith("https://"):
                path = download_file(doc)
                print(f"[INPUT RECEIVED URL] Saved as - {path}")
            else:
                raw = base64.b64decode(doc)
                header = raw[:4]
                ext = get_extension_from_magic_bytes(header)

                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                tmp.write(raw)
                tmp.close()

                path = tmp.name

        # -------- SPLIT PDF INTO IMAGES --------
        page_images = split_pdf_to_images(path)

        combined_pages = []

        # ---- NEW TOKEN COUNTERS ----
        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens_used = 0

        # -------- PROCESS EACH PAGE --------
        for page_index, img_path in enumerate(page_images, start=1):

            file_ref = client.files.upload(file=img_path)

            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[EXTRACTION_PROMPT,
                          f"Extract items from page {page_index}",
                          file_ref]
            )

            raw = response.text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            raw = re.sub(r"^json", "", raw, flags=re.IGNORECASE).strip()

            try:
                parsed = json.loads(raw)
            except:
                start = raw.find("{")
                end = raw.rfind("}") + 1
                cleaned = raw[start:end]
                cleaned = cleaned.replace("\x00", "")
                cleaned = re.sub(r",\s*}", "}", cleaned)
                cleaned = re.sub(r",\s*]", "]", cleaned)
                parsed = json.loads(cleaned)

            page_data = parsed["pagewise_line_items"][0]
            page_data["page_no"] = str(page_index)

            combined_pages.append(page_data)

            # -------- TRACK TOKEN USAGE --------
            inp, outp, tot = get_token_usage(response)
            total_input_tokens += inp
            total_output_tokens += outp
            total_tokens_used += tot

        # -------- NORMALIZE --------
        total_item_count = 0
        output_pages = []

        for page in combined_pages:
            page_no = str(page.get("page_no", ""))
            page_type = page.get("page_type", "Bill Detail")
            bill_items = page.get("bill_items", [])

            normalized_items = []
            for it in bill_items:
                amount = safe_float(it.get("item_amount"))
                rate = safe_float(it.get("item_rate"))
                qty = safe_float(it.get("item_quantity"))

                if amount == 0:
                    continue

                normalized_items.append({
                    "item_name": (it.get("item_name") or "").strip().replace("\n", " "),
                    "item_amount": float(f"{amount:.2f}"),
                    "item_rate": float(f"{rate:.2f}"),
                    "item_quantity": float(f"{qty:.2f}")
                })

            total_item_count += len(normalized_items)

            output_pages.append({
                "page_no": page_no,
                "page_type": page_type,
                "bill_items": normalized_items
            })

        final_response = {
            "is_success": True,
            "token_usage": {
                "total_tokens": total_tokens_used,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens
            },
            "data": {
                "pagewise_line_items": output_pages,
                "total_item_count": total_item_count
            }
        }

        # ===== PRINT FINAL FULL RESPONSE =====
        print("\n================ FINAL COMBINED RESPONSE ================")
        print(json.dumps(final_response, indent=2))
        print("=========================================================\n")


        # -------- FINAL RESPONSE --------
        return jsonify({
            "is_success": True,
            "token_usage": {
                "total_tokens": total_tokens_used,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens
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



# -------- Render Entry Point --------
if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=PORT)
