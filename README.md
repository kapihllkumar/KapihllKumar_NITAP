# 📄 Invoice Extraction API 

A fast and accurate **invoice data extraction API** built using:

- **Python + Flask**
- **Google Gemini **
- **OCR + Line Item Parsing**
- Supports **PDF**, **PNG**, **JPG**, **JPEG**, **GIF**, and **URL-based documents**
- Supports **Base64 documents**
- Auto-detects file type from MIME or Magic Bytes

This project extracts:

- Page-wise line items  
- Item name, amount, rate, quantity  
- Page type: *Bill Detail*, *Final Bill*, *Pharmacy*  

---

## 🚀 Features

✔ Upload **PDF / Image** via `multipart/form-data`  
✔ Send **URL** pointing to an invoice (PDF/PNG/JPG)  
✔ Supports **Base64 encoded documents**  
✔ Automatic file type detection  
✔ Uses **Gemini 2.0 Flash** for OCR  
✔ Clean structured JSON  
✔ Token usage tracking  

---

## 📦 Project Structure

```
├── app.py
├── .env
├── requirements.txt
└── README.md
```

---

## 🔧 Setup Instructions

### 1. Clone the Repository

```bash
git clone https://github.com/kapihllkumar/KapihllKumar_NITAP.git
cd KapihllKumar_NITAP
```

---

### 2. Create & Activate Virtual Environment

```bash
python -m venv venv
venv\Scripts\activate   # Windows
```

---

### 3. Install Dependencies

Create a `requirements.txt` if missing:

```
flask
requests
python-dotenv
google-genai
```

Install packages:

```bash
pip install -r requirements.txt
```

---

### 4. Configure Environment Variables

Create a `.env` file with:

```
GEMINI_API_KEY=YOUR_API_KEY_HERE
```

🚨 **Do NOT commit `.env` to GitHub.**  
Add `.env` to `.gitignore`:

```
.env
```

---

### 5. Run the Server

```bash
python app.py
```

Server will run on:

```
http://localhost:8000
```

---

## 📌 API Endpoint

### **POST /extract-bill-data**

Works in **3 modes**:

---

## ✅ 1. Upload File (PDF/PNG/JPG)

```powershell
$FilePath = "C:\path\invoice.png"
$boundary = [System.Guid]::NewGuid().ToString()
$LF = "`r`n"

$fileBytes = [System.IO.File]::ReadAllBytes($FilePath)
$fileContent = [System.Text.Encoding]::GetEncoding("ISO-8859-1").GetString($fileBytes)

$type = "image/png"

$bodyLines =
    "--$boundary$LF" +
    "Content-Disposition: form-data; name=`"file`"; filename=`"invoice.png`"$LF" +
    "Content-Type: $type$LF$LF" +
    $fileContent + $LF +
    "--$boundary--$LF"

$response = Invoke-WebRequest -Uri "http://localhost:8000/extract-bill-data" `
    -Method POST `
    -ContentType "multipart/form-data; boundary=$boundary" `
    -Body $bodyLines

$response.Content | ConvertFrom-Json | ConvertTo-Json -Depth 50
```

---

## ✅ 2. Send URL

```powershell
$body = @{
    document = "https://example.com/invoice.png"
}

$response = Invoke-WebRequest `
    -Uri "http://localhost:8000/extract-bill-data" `
    -Method POST `
    -ContentType "application/json" `
    -Body ($body | ConvertTo-Json)

$response.Content | ConvertFrom-Json | ConvertTo-Json -Depth 50
```

---

## ✅ 3. Base64 Document

```json
{
  "document": "<BASE64_STRING>"
}
```

---

## 📝 Response Format

```json
{
  "is_success": true,
  "token_usage": {
    "total_tokens": 1234,
    "input_tokens": 567,
    "output_tokens": 667
  },
  "data": {
    "pagewise_line_items": [
      {
        "page_no": "1",
        "page_type": "Bill Detail",
        "bill_items": [
          {
            "item_name": "XYZ Service",
            "item_amount": 2500.0,
            "item_rate": 500.0,
            "item_quantity": 5.0
          }
        ]
      }
    ],
    "total_item_count": 5
  }
}
```

---

## 🤝 Contributions

Pull requests are welcome!

---

## 📜 License

MIT License.
