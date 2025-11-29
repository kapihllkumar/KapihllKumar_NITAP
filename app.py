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
import fitz   # <-- added for PDF split

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

    # Generate a unique temp filename (NO open handle)
    output_path = os.path.join(
        tempfile.gettempdir(),
        f"bill_download_{next(tempfile._get_candidate_names())}{suffix}"
    )

    # Write content manually (no locked file)
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
Based on the content decide which is best suitable for page_type from allowed values.

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

IMPORTANT RULE ABOUT MULTI-LINE NAMES:
- Many item names in invoices span multiple physical lines (wrapping or sub-text).
- You MUST treat all consecutive text that belongs to the same item as ONE item_name.
- If a line below clearly belongs to the previous item (e.g., doctor qualifications, batch number, description), merge them into a single item_name field.
- DO NOT create a new bill item just because the text wrapped to next line.

IMPORTANT MAPPING RULE FOR RATE VS TOTAL AMOUNT:
Invoices may contain both a per-unit Amount and a Total Amount.

You MUST follow this mapping strictly:
- If a row contains both a per-unit Amount AND a Total (rate × quantity):
    - item_rate = the per-unit Amount exactly as printed
    - item_amount = the Total (rate × quantity) exactly as printed
- If only a per-unit Amount is shown and quantity > 1:
    - treat that value as item_rate, not item_amount
- If a Total is printed anywhere on the same line or vertically aligned:
    - assign it to item_amount
- NEVER assign the per-unit amount to item_amount.
- NEVER leave item_rate as 0 if a per-unit amount exists.
- If two numeric values appear for an item (excluding quantity):
    - the smaller number MUST be item_rate
    - the larger number MUST be item_amount

HANDWRITTEN / ROTATED INTERPRETATION RULE:
- If an item's amount is not printed on the exact line, look at vertically aligned handwritten numbers in the same column.
- If an amount is written slightly above/below the item, associate it with that item.
- Do NOT ignore items simply because the amount is not on the exact line.
- If a handwritten amount appears as two separated numbers (e.g., "266 94", "266\n94", "266-94"):
    - You MUST interpret it as a decimal → 266.94
- If the decimal point is faint / missing / unclear:
    - You MUST infer the decimal by reading the spatial alignment
- If two numbers are written slightly vertically aligned (top = whole, bottom = decimals):
    - Combine them into one decimal value
- NEVER round decimals. Always return exact printed decimal.

ITEM CONTINUATION RULE (CRITICAL):
Invoices may list items in multiple vertical sections, columns, or blocks.
You MUST extract EVERY item in the entire page, regardless of how many 
columns, boxes, or separated regions exist.

You MUST continue reading ALL visible rows until the end of the page.

Do NOT stop after the first column or section.
Do NOT stop after the first visible block of items.
Do NOT assume that the first box is the only list.

You MUST scan the ENTIRE PAGE from top-to-bottom and left-to-right
and extract ALL rows that contain:
- an item name
- a quantity
- a rate OR an amount OR handwritten value

If an item has *either* quantity OR rate OR amount,
then it MUST be included.

ITEM HEADER SKIP RULE:
NEVER treat the following as bill items:
- headings (“ITEM NAME”, “DESCRIPTION”, “RATE”, “AMOUNT”, “QTY”)
- section titles (“CONSULTATION”, “DRUGS”, “CONSUMABLES”, etc.)
- table borders or column names

MULTI-COLUMN RULE:
If items are arranged in two or more columns:
- Read column 1 top-to-bottom
- Then column 2 top-to-bottom
- Then column 3 top-to-bottom (if present)
Always preserve natural visual order.

ABSOLUTE MANDATE:
You MUST extract ALL items printed on the page, not just the first set of rows.

STRICT RULES:
- Preserve the original top-to-bottom item order.
- Extract every real item exactly once.
- DO NOT split items due to line breaks inside item_name.
- DO NOT add missing items.
- DO NOT group multiple different items.
- DO NOT infer extra items.
- Do NOT output subtotal/total.
- Return ONLY valid JSON.

DISCOUNT / FEE EXCLUSION RULE:
The following must NOT be treated as bill items under any circumstances:
- Any type of discount (e.g., "Discount", "GST Discount", "Bill Discount", "Concession")
- Any rounding adjustments (e.g., "Round Off", "R/o", "Rounding")
- Any tax summary lines (e.g., GST %, CGST, SGST)
- Any totals, subtotals, net amounts, balance, deposit, advance, refunds.

These lines must be completely ignored and MUST NOT appear inside bill_items
"""



# ---------------- PAGE SPLITTING (NEW) ----------------

def split_pdf_to_images(pdf_path: str):
    doc = fitz.open(pdf_path)
    imgs = []

    for page_number in range(len(doc)):
        page = doc.load_page(page_number)
        pix = page.get_pixmap(dpi=200)

        # Create guaranteed unique filename
        filename = f"bill_page_{page_number+1}_{next(tempfile._get_candidate_names())}.png"
        output_path = os.path.join(tempfile.gettempdir(), filename)

        # Save image (no locked file involved)
        pix.save(output_path)

        imgs.append(output_path)

    doc.close()
    return imgs




# ---------------- API PROCESSING ----------------
@app.route("/tmp-file/<filename>", methods=["GET"])
def get_tmp_file(filename):
    path = f"/tmp/{filename}"
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    return send_file(path)

@app.route("/extract-bill-data", methods=["POST"])
def extract_bill_data():
    try:
        if "file" in request.files:
            
            path = save_uploaded_file(request.files["file"])
            print(f"[INPUT RECEIVED] Source file path or URL used - {path}")
        else:
            body = request.get_json(force=True)
            if "document" not in body:
                return jsonify({"is_success": False, "error": "Missing 'document'"}), 400

            doc = body["document"]

            if doc.startswith("http://") or doc.startswith("https://"):
                path = download_file(doc)
                print(f"[INPUT RECEIVED] URL - {doc}\nSaved as - {path}")
            else:
                raw = base64.b64decode(doc)
                header = raw[:4]
                ext = get_extension_from_magic_bytes(header)

                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                tmp.write(raw)
                tmp.close()
                path = tmp.name

        # ---------------- PROCESS PAGE BY PAGE ----------------

        page_images = split_pdf_to_images(path)

        combined_pages = []

        for page_index, img_path in enumerate(page_images, start=1):

            file_ref = client.files.upload(file=img_path)

            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    EXTRACTION_PROMPT,
                    f"Extract items from page {page_index}",
                    file_ref
                ]
            )

            raw = response.text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            raw = re.sub(r"^json", "", raw, flags=re.IGNORECASE).strip()

            # Try normal parse
            try:
                parsed = json.loads(raw)
            except:
                # Cleanup
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

        # Replace global parsed
        pages = combined_pages

        # ---------------- NORMALIZATION (unchanged) ----------------

        total_item_count = 0
        output_pages = []

        for page in pages:
            page_no = str(page.get("page_no", ""))
            page_type = page.get("page_type", "Bill Detail")
            bill_items = page.get("bill_items", [])

            normalized_items = []
            for it in bill_items:

                amount = safe_float(it.get("item_amount"))
                rate   = safe_float(it.get("item_rate"))
                qty    = safe_float(it.get("item_quantity"))

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

        return jsonify({
            "is_success": True,
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
