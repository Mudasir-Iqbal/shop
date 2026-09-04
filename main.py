import os
from dotenv import load_dotenv
import json
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from fastapi import FastAPI, Request
import google.generativeai as genai
load_dotenv()
app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "parts_bot_verify_token_786")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
vision_model = genai.GenerativeModel("gemini-1.5-flash")

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def send_whatsapp_message(to: str, text: str):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    res = requests.post(url, headers=headers, json=payload)
    print(f"Meta Send Status: {res.status_code} | Body: {res.text}")
    
def download_whatsapp_media(media_id: str) -> bytes:
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    res = requests.get(url, headers=headers).json()
    media_url = res.get("url")
    return requests.get(media_url, headers=headers).content

@app.get("/")
def home():
    return {"status": "Parts Bot is Running Live!"}

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return int(params.get("hub.challenge"))
    return "Verification failed", 400

@app.post("/webhook")
async def handle_incoming(request: Request):
    data = await request.json()
    
    try:
        entry = data.get("entry", [])[0]["changes"][0]["value"]
        if "messages" not in entry:
            return {"status": "ignored"}
        
        msg = entry["messages"][0]
        sender = msg["from"]
        msg_type = msg.get("type")
        
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT state, temp_data FROM user_sessions WHERE phone_number = %s", (sender,))
        session = cursor.fetchone()
        current_state = session["state"] if session else "IDLE"

        # --- OPTION 1: IMAGE SCAN (Zero Start) ---
        if msg_type == "image":
            image_id = msg["image"]["id"]
            img_bytes = download_whatsapp_media(image_id)
            
            prompt = """
            Extract bike part information from the image label. 
            Return strictly a JSON object: {"brand": "...", "part_name": "...", "model": "..."}
            If unknown, leave fields as empty string.
            """
            
            response = vision_model.generate_content([
                {"mime_type": "image/jpeg", "data": img_bytes},
                prompt
            ])
            
            clean_res = response.text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean_res)
            brand = parsed.get("brand", "").lower()
            part_name = parsed.get("part_name", "")

            cursor.execute("""
                SELECT p.part_name, p.model, p.rate, c.name as company 
                FROM products p 
                JOIN companies c ON p.company_id = c.id 
                WHERE p.part_name ILIKE %s OR c.name ILIKE %s
            """, (f"%{part_name}%", f"%{brand}%"))
            results = cursor.fetchall()
            
            if results:
                reply = f"🔍 *Found {len(results)} Rate(s):*\n"
                for r in results:
                    reply += f"• *{r['part_name']}* ({r['company'].upper()} - {r['model']}): Rs. {r['rate']}\n"
            else:
                reply = f"❌ Detected: *{part_name}* ({brand.title()}), lekin DB me rate exist nahi karta."
                
            send_whatsapp_message(sender, reply)
            conn.close()
            return {"status": "success"}

        # --- OPTION 2: TEXT & MENU PROCESSING ---
        text = msg["text"]["body"].strip()
        text_lower = text.lower()

        if text_lower == "start":
            cursor.execute("""
                INSERT INTO user_sessions (phone_number, state) VALUES (%s, 'MAIN_MENU')
                ON CONFLICT (phone_number) DO UPDATE SET state = 'MAIN_MENU', temp_data = NULL
            """, (sender,))
            conn.commit()
            menu = (
                "📋 *MAIN MENU*\n\n"
                "1️⃣ Show RATE list\n"
                "2️⃣ Rate list update\n"
                "   2.1 Honda\n"
                "   2.2 Crown\n"
                "   2.3 TQR\n"
                "   2.4 ISH\n"
                "3️⃣ Setting\n"
                "   3.1 Add new company\n\n"
                "_Reply with option (e.g. 1, 2.1, 3.1)_"
            )
            send_whatsapp_message(sender, menu)

        elif current_state == "MAIN_MENU":
            if text == "1":
                cursor.execute("""
                    SELECT p.part_name, p.model, p.rate, c.name as company 
                    FROM products p JOIN companies c ON p.company_id = c.id
                    ORDER BY c.name, p.part_name
                """)
                rows = cursor.fetchall()
                if not rows:
                    send_whatsapp_message(sender, "Database me abhi koi rates available nahi hain.")
                else:
                    rates_msg = "📊 *Rate List:*\n\n"
                    for r in rows:
                        rates_msg += f"• {r['part_name']} ({r['company'].upper()} {r['model']}): Rs. {r['rate']}\n"
                    send_whatsapp_message(sender, rates_msg)
                cursor.execute("UPDATE user_sessions SET state = 'IDLE' WHERE phone_number = %s", (sender,))
                conn.commit()

            elif text in ["2.1", "2.2", "2.3", "2.4"]:
                mapping = {"2.1": "honda", "2.2": "crown", "2.3": "tqr", "2.4": "ish"}
                company = mapping[text]
                cursor.execute("UPDATE user_sessions SET state = 'AWAITING_RATE_UPDATE', temp_data = %s WHERE phone_number = %s", (company, sender))
                conn.commit()
                send_whatsapp_message(sender, f"Send details for *{company.upper()}* in format:\n`Part, Model, Rate`\n\nExample: `Piston, CD70, 1450`")

            elif text == "3.1":
                cursor.execute("UPDATE user_sessions SET state = 'AWAITING_NEW_COMPANY' WHERE phone_number = %s", (sender,))
                conn.commit()
                send_whatsapp_message(sender, "Nayi company ka naam type karein:")

            else:
                send_whatsapp_message(sender, "Invalid option. Dobara select karein ya `start` likhein.")

        elif current_state == "AWAITING_RATE_UPDATE":
            company_name = session["temp_data"]
            try:
                parts = [x.strip() for x in text.split(",")]
                p_name, p_model, p_rate = parts[0], parts[1], float(parts[2])
                
                cursor.execute("SELECT id FROM companies WHERE name ILIKE %s", (company_name,))
                comp = cursor.fetchone()
                
                cursor.execute("""
                    INSERT INTO products (company_id, part_name, model, rate) 
                    VALUES (%s, %s, %s, %s)
                """, (comp["id"], p_name, p_model, p_rate))
                conn.commit()
                send_whatsapp_message(sender, f"✅ Added: *{p_name}* ({p_model}) = Rs. {p_rate} under *{company_name.upper()}*")
            except Exception:
                send_whatsapp_message(sender, "❌ Format error! Is tarah bhejein:\n`Part Name, Model, Rate`")
            
            cursor.execute("UPDATE user_sessions SET state = 'IDLE' WHERE phone_number = %s", (sender,))
            conn.commit()

        elif current_state == "AWAITING_NEW_COMPANY":
            new_comp = text_lower.strip()
            cursor.execute("INSERT INTO companies (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (new_comp,))
            conn.commit()
            send_whatsapp_message(sender, f"✅ Company *{new_comp.upper()}* add ho chuki hai!")
            cursor.execute("UPDATE user_sessions SET state = 'IDLE' WHERE phone_number = %s", (sender,))
            conn.commit()

        else:
            cursor.execute("""
                SELECT p.part_name, p.model, p.rate, c.name as company 
                FROM products p JOIN companies c ON p.company_id = c.id 
                WHERE p.part_name ILIKE %s OR p.model ILIKE %s
            """, (f"%{text}%", f"%{text}%"))
            results = cursor.fetchall()
            if results:
                reply = f"🔍 *Search Results:*\n"
                for r in results:
                    reply += f"• {r['part_name']} ({r['company'].upper()} {r['model']}): Rs. {r['rate']}\n"
                send_whatsapp_message(sender, reply)
            else:
                send_whatsapp_message(sender, "Part nahi mila. Menu ke liye `start` bhejein ya picture upload karein.")

        conn.close()
    except Exception as e:
        print(f"Error: {e}")
        
    return {"status": "ok"}