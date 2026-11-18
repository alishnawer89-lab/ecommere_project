import os
import json
import time
import urllib.request
import urllib.error

from db import get_db_connection

# API key .env file ya environment variable se aati hai (code mein key mat likhna)
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.8-flash')
# Agar main model busy (503) ho ya na mile to bot in models par khud chala jayega
FALLBACK_MODELS = ['gemini-3.7-flash', 'gemini-3.5-flash-lite']
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

DEFAULT_CATALOG = [
    ("Itachi Uchiha - Akatsuki Crimson Oversized T-Shirt", 1999.00, "shirt"),
    ("Streetwear Black Track Pants with Minimalist Print", 2999.00, "pants"),
]


def get_catalog_text():
    """Database se products nikal kar chatbot ko batane ke liye text banata hai."""
    items = []
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT name, price, category FROM products LIMIT 50")
            for p in cursor.fetchall():
                items.append((p['name'], p['price'], p.get('category') or 'shirt'))
            cursor.close()
        except Exception as e:
            print(f"Chatbot catalog error: {e}")
        finally:
            conn.close()

    if not items:
        items = DEFAULT_CATALOG

    return "\n".join(f"- {name} | ${price} USD | category: {cat}" for name, price, cat in items)


def build_system_prompt():
    return f"""You are the friendly shopping assistant of "Fit Verse", an online anime streetwear store (oversized shirts, pants and bottoms).

Rules:
- Answer ONLY using the store information given below. Never invent products, prices, discounts, delivery times or policies.
- If you do not know something (shipping, returns, offers etc.), say you are not sure and ask the customer to contact the store.
- Prices are in USD. Sizes available: S, M, L, XL. Payment is done through PayPal at checkout.
- To buy: click "CHOOSE OPTIONS" on a product, pick size and quantity, then open the Cart and press "Proceed to Payment". Customers must be logged in to add items to the cart.
- Reply in the same language the customer writes in (English, Urdu, Roman Urdu etc.). Keep answers short and helpful.
- Do not discuss anything unrelated to the store.

Current products:
{get_catalog_text()}
"""


def call_gemini(model, payload, api_key):
    """Ek model ko request bhejta hai aur jawab ka text wapis karta hai."""
    request = urllib.request.Request(
        GEMINI_URL.format(model=model),
        data=json.dumps(payload).encode('utf-8'),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode('utf-8'))
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(part.get("text", "") for part in parts).strip()


def ask_gemini(user_message, history=None):
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        print("GEMINI_API_KEY set nahi hai!")
        return "Sorry, chatbot abhi available nahi hai."

    contents = []
    for item in (history or []):
        contents.append({"role": item["role"], "parts": [{"text": item["text"]}]})
    # Gemini ko pehla message hamesha 'user' ka chahiye
    while contents and contents[0]["role"] != "user":
        contents.pop(0)
    contents.append({"role": "user", "parts": [{"text": user_message}]})

    payload = {
        "system_instruction": {"parts": [{"text": build_system_prompt()}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 1000},
    }

    models = [GEMINI_MODEL] + [m for m in FALLBACK_MODELS if m != GEMINI_MODEL]

    for model in models:
        for attempt in range(2):
            try:
                text = call_gemini(model, payload, api_key)
                return text or "Sorry, mujhe is ka jawab nahi mila. Dobara try karein."
            except urllib.error.HTTPError as e:
                print(f"Gemini HTTP error {e.code} (model: {model}): {e.read().decode('utf-8', errors='ignore')}")
                if e.code in (400, 401, 403):
                    # API key ka masla hai, doosre model try karne ka faida nahi
                    return "Sorry, abhi kuch masla aa gaya hai. Thori der baad dobara try karein."
                if e.code in (429, 500, 502, 503, 504) and attempt == 0:
                    time.sleep(1.5)  # busy hai, ek dafa dobara koshish
                    continue
                break  # doosre model par chalo
            except Exception as e:
                print(f"Gemini error (model: {model}): {e}")
                break

    return "Sorry, abhi kuch masla aa gaya hai. Thori der baad dobara try karein."