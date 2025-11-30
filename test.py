import os
from google import genai
from dotenv import load_dotenv

# Load GEMINI_API_KEY from .env
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")

if not API_KEY:
    raise ValueError("GEMINI_API_KEY not found in environment variables.")

# Create client
client = genai.Client(api_key=API_KEY)

def list_all_models():
    print("🔍 Fetching available Gemini models...\n")
    try:
        models = client.models.list()   # <-- Important
        print("✅ Models available for your API key:\n")
        for m in models:
            print(f"• {m.name}   (type: {m.model_type})")
    except Exception as e:
        print("\n❌ Error while fetching models:")
        print(e)

if __name__ == "__main__":
    list_all_models()
