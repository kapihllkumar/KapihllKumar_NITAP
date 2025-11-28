import requests
import json

url = "http://localhost:8000/extract-bill-data"

payload = {
    "document": "https://hackrx.blob.core.windows.net/assets/datathon-IIT/HackRx%20Bill%20Extraction%20API.postman_collection.json?sv=2025-07-05&spr=https&st=2025-11-28T07%3A21%3A28Z&se=2026-11-29T07%3A21%3A00Z&sr=b&sp=r&sig=GTu74m7MsMT1fXcSZ8v92ijcymmu55sRklMfkTPuobc%3D"
}

response = requests.post(url, json=payload)

print("STATUS:", response.status_code)
print("RESPONSE:\n", json.dumps(response.json(), indent=4))
