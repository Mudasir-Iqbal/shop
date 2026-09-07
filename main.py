"""
Pamir Auto Parts — WhatsApp Bot & Store API (main.py)
Integrates:
- WhatsApp Cloud API + Meta Webhooks
- Gemini 3.5 Flash (Vision & Function Calling)
- Supabase / PostgreSQL v6 Database Schema
- Frontend REST APIs for Web Interface
"""

import os
import json
import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any

import psycopg2
import requests
from psycopg2.extras import RealDictCursor, Json
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import google.generativeai as genai
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from file_processor import process_part_image, process_vendor_document

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "parts_bot_verify_token_786")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)

app = FastAPI(title="Pamir Auto Parts Engine v6")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UNDO_WINDOW_SECONDS = 300  # 5 minutes


# =====================================================================
# DB & DATA SANITIZATION HELPERS
# =====================================================================

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def to_float(v):
    return float(v) if isinstance(v, Decimal) else v

def clean_row(row: dict) -> dict:
    """JSON/Gemini safe row serialization."""
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif v is None:
            out[k] = ""
        else:
            out[k] = v
    return out

def sanitize_for_gemini(data: Any) -> Any:
    """Ensure data structures have no Decimal or complex non-serializable objects."""
    if isinstance(data, dict):
        return {k: sanitize_for_gemini(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [sanitize_for_gemini(item) for item in data]
    elif isinstance(data, Decimal):
        return float(data)
    elif isinstance(data, datetime):
        return data.isoformat()
    return data

@app.on_event("startup")
def ensure_bot_tables():
    """Bot conversation state aur session memory maintain karne ke liye."""
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_sessions (
                    phone_number VARCHAR(30) PRIMARY KEY,
                    history JSONB NOT NULL DEFAULT '[]'::jsonb,
                    last_action JSONB,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
    finally:
        conn.close()


# =====================================================================
# WHATSAPP MESSAGING & MEDIA
# =====================================================================

def send_whatsapp_message(to: str, text: str):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        return res.ok
    except Exception as e:
        print(f"Error sending WhatsApp message: {e}")
        return False

def upload_whatsapp_media(file_path: str, mime_type: str = "application/pdf") -> Optional[str]:
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f, mime_type)}
            data = {"messaging_product": "whatsapp"}
            res = requests.post(url, headers=headers, files=files, data=data, timeout=20)
        if res.ok:
            return res.json().get("id")
        return None
    except Exception as e:
        print(f"Error uploading WhatsApp media: {e}")
        return None

def send_whatsapp_document(to: str, file_path: str, filename: str, caption: str = ""):
    media_id = upload_whatsapp_media(file_path)
    if not media_id:
        send_whatsapp_message(to, "File bhejne mein masla hua, dobara koshish karein.")
        return False
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {"id": media_id, "filename": filename, "caption": caption},
    }
    res = requests.post(url, headers=headers, json=payload, timeout=10)
    return res.ok

def download_whatsapp_media(media_id: str) -> bytes:
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    res = requests.get(url, headers=headers, timeout=10).json()
    media_url = res.get("url")
    return requests.get(media_url, headers=headers, timeout=20).content


# =====================================================================
# SESSION STATE & UNDO
# =====================================================================

def load_session(phone: str) -> dict:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT history, last_action FROM bot_sessions WHERE phone_number=%s", (phone,))
            row = cur.fetchone()
            if row:
                return {"history": row["history"] or [], "last_action": row["last_action"]}
            return {"history": [], "last_action": None}
    finally:
        conn.close()

def save_session(phone: str, history: list, last_action: Optional[dict] = None):
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_sessions (phone_number, history, last_action, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (phone_number) DO UPDATE
                SET history = EXCLUDED.history, 
                    last_action = COALESCE(EXCLUDED.last_action, bot_sessions.last_action),
                    updated_at = NOW()
            """, (phone, Json(history), Json(last_action) if last_action else None))
    finally:
        conn.close()

def clear_last_action(phone: str):
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE bot_sessions SET last_action=NULL WHERE phone_number=%s", (phone,))
    finally:
        conn.close()


# =====================================================================
# FUZZY RESOLVERS (v6 Schema Driven)
# =====================================================================

def resolve_customers(cur, name_query: str) -> list:
    cur.execute("""
        SELECT id, name, phone_number, current_balance
        FROM customers 
        WHERE name ILIKE %(q)s OR phone_number ILIKE %(q)s
        ORDER BY similarity(name, %(raw)s) DESC LIMIT 6
    """, {"q": f"%{name_query}%", "raw": name_query})
    return [clean_row(r) for r in cur.fetchall()]

def resolve_parts(cur, part_query: str) -> list:
    cur.execute("""
        SELECT bm.id AS brand_mapping_id, mp.part_name, mp.bike_model, bm.brand_name,
               bm.specification, mp.box_rack_location,
               COALESCE(SUM(pb.quantity_remaining), 0) AS stock,
               (SELECT effective_unit_cost FROM purchase_batches
                WHERE brand_mapping_id = bm.id AND quantity_remaining > 0
                ORDER BY created_at ASC LIMIT 1) AS list_rate
        FROM brand_mappings bm
        JOIN master_parts mp ON mp.id = bm.master_part_id
        LEFT JOIN purchase_batches pb ON pb.brand_mapping_id = bm.id
        WHERE bm.is_active AND (
            mp.part_name ILIKE %(q)s OR mp.bike_model ILIKE %(q)s OR
            bm.brand_name ILIKE %(q)s OR bm.specification ILIKE %(q)s
        )
        GROUP BY bm.id, mp.part_name, mp.bike_model, bm.brand_name, bm.specification, mp.box_rack_location
        ORDER BY mp.part_name LIMIT 10
    """, {"q": f"%{part_query}%"})
    return [clean_row(r) for r in cur.fetchall()]


# =====================================================================
# GEMINI TOOL IMPLEMENTATIONS
# =====================================================================

def tool_search_part(args: dict, ctx: dict) -> dict:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            rows = resolve_parts(cur, args["query"])
        if not rows:
            return {"found": False, "message": "Yeh part record mein nahi mila."}
        return {"found": True, "items": rows}
    finally:
        conn.close()

def tool_create_sale(args: dict, ctx: dict) -> dict:
    invoice_type = args["invoice_type"].upper()
    customer_name = args.get("customer_name")
    raw_items = args["items"]

    conn = get_db()
    try:
        with conn.cursor() as cur:
            customer_id = None
            customer_row = None
            if invoice_type == "CREDIT":
                if not customer_name:
                    return {"error": "Udhaar sale ke liye customer ka naam batana lazmi hai."}
                matches = resolve_customers(cur, customer_name)
                if len(matches) == 0:
                    return {"error": f"Customer '{customer_name}' nahi mila. Pehle add karein."}
                if len(matches) > 1:
                    return {"needs_clarification": True, "type": "customer", "options": matches}
                customer_row = matches[0]
                customer_id = customer_row["id"]

            resolved_items = []
            for it in raw_items:
                parts = resolve_parts(cur, it["part_query"])
                if len(parts) == 0:
                    return {"error": f"Item '{it['part_query']}' record mein nahi mila."}
                if len(parts) > 1:
                    return {"needs_clarification": True, "type": "part", "query": it["part_query"], "options": parts}
                p = parts[0]
                if p["stock"] < it["quantity"]:
                    return {"error": f"{p['part_name']} ({p['brand_name']}) ka stock sirf {p['stock']} hai, matlooba {it['quantity']} dastiyab nahi."}
                resolved_items.append({
                    "brand_mapping_id": p["brand_mapping_id"],
                    "quantity": it["quantity"],
                    "sold_price": it["sold_price"],
                    "label": f"{p['part_name']} ({p['brand_name']})",
                })

        with conn:
            with conn.cursor() as cur:
                items_json = json.dumps([
                    {"brand_mapping_id": i["brand_mapping_id"], "quantity": i["quantity"], "sold_price": i["sold_price"]}
                    for i in resolved_items
                ])
                cur.execute(
                    "SELECT create_sale_invoice(%s, %s, %s::jsonb) AS invoice_id",
                    (customer_id, invoice_type, items_json),
                )
                invoice_id = cur.fetchone()["invoice_id"]
                cur.execute("SELECT total_amount FROM sales_invoices WHERE id=%s", (invoice_id,))
                total = to_float(cur.fetchone()["total_amount"])

                new_balance = None
                if invoice_type == "CREDIT":
                    cur.execute("""
                        INSERT INTO ledger_transactions (customer_id, invoice_id, entry_type, amount, description)
                        VALUES (%s, %s, 'DEBIT', %s, %s) RETURNING balance_after
                    """, (customer_id, invoice_id, total, f"Sale Invoice #{invoice_id}"))
                    new_balance = to_float(cur.fetchone()["balance_after"])

        if customer_row and customer_row.get("phone_number"):
            lines = "\n".join(f"• {i['quantity']}x {i['label']} @ Rs.{i['sold_price']} = Rs.{i['quantity']*i['sold_price']}" for i in resolved_items)
            receipt = f"🧾 *Naya Bill #{invoice_id}*\n{lines}\n\n*Total: Rs. {total}*"
            if invoice_type == "CREDIT":
                receipt += f"\nAapka baqi balance: Rs. {new_balance}"
            send_whatsapp_message(customer_row["phone_number"], receipt)

        return {
            "success": True, "invoice_id": invoice_id, "total_amount": total,
            "invoice_type": invoice_type, "customer": customer_row["name"] if customer_row else "Cash Sale",
            "customer_new_balance": new_balance, "items": resolved_items,
        }
    except psycopg2.Error as e:
        return {"error": e.diag.message_primary or str(e)}
    finally:
        conn.close()

def tool_get_khata(args: dict, ctx: dict) -> dict:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            matches = resolve_customers(cur, args["customer_name"])
            if len(matches) == 0:
                return {"error": "Customer nahi mila."}
            if len(matches) > 1:
                return {"needs_clarification": True, "type": "customer", "options": matches}
            c = matches[0]
            cur.execute("""
                SELECT si.id AS invoice_id, si.total_amount, si.amount_paid, si.created_at,
                       mp.part_name, bm.brand_name, si2.quantity_sold, si2.selling_price
                FROM sales_invoices si
                JOIN sale_items si2 ON si2.invoice_id = si.id
                JOIN purchase_batches pb ON pb.id = si2.batch_id
                JOIN brand_mappings bm ON bm.id = pb.brand_mapping_id
                JOIN master_parts mp ON mp.id = bm.master_part_id
                WHERE si.customer_id = %s AND si.invoice_type='CREDIT' AND NOT si.is_cancelled
                  AND si.amount_paid < si.total_amount
                ORDER BY si.created_at
            """, (c["id"],))
            unpaid_lines = [clean_row(r) for r in cur.fetchall()]
            return {"customer": c, "unpaid_items": unpaid_lines}
    finally:
        conn.close()

def tool_record_payment(args: dict, ctx: dict) -> dict:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            matches = resolve_customers(cur, args["customer_name"])
            if len(matches) == 0:
                return {"error": "Customer record mein nahi mila."}
            if len(matches) > 1:
                return {"needs_clarification": True, "type": "customer", "options": matches}
            c = matches[0]

        amount = float(args["amount"])
        method = args.get("method", "cash")

        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_balance FROM customers WHERE id=%s FOR UPDATE", (c["id"],))
                prev_balance = to_float(cur.fetchone()["current_balance"])

                cur.execute("""
                    SELECT id, amount_paid FROM sales_invoices
                    WHERE customer_id=%s AND invoice_type='CREDIT' AND NOT is_cancelled AND amount_paid < total_amount
                """, (c["id"],))
                before = {r["id"]: to_float(r["amount_paid"]) for r in cur.fetchall()}

                cur.execute("""
                    INSERT INTO ledger_transactions (customer_id, entry_type, amount, description)
                    VALUES (%s, 'CREDIT', %s, %s) RETURNING id, balance_after
                """, (c["id"], amount, f"{method} payment received"))
                ledger_row = cur.fetchone()
                new_balance = to_float(ledger_row["balance_after"])

                allocations = []
                if before:
                    cur.execute("SELECT id, amount_paid FROM sales_invoices WHERE id = ANY(%s)", (list(before.keys()),))
                    for r in cur.fetchall():
                        delta = to_float(r["amount_paid"]) - before[r["id"]]
                        if delta:
                            allocations.append({"invoice_id": r["id"], "delta": delta})

        sess = load_session(ctx["phone"])
        save_session(ctx["phone"], sess["history"], last_action={
            "type": "payment", "ledger_id": ledger_row["id"], "customer_id": c["id"],
            "customer_name": c["name"], "amount": amount, "prev_balance": prev_balance,
            "allocations": allocations, "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        if c.get("phone_number"):
            msg = f"Wasooli Update:\nRs. {amount:,.0f} receive ho gaye.\nBaqi Khata: Rs. {new_balance:,.0f}"
            send_whatsapp_message(c["phone_number"], msg)

        return {"success": True, "customer": c["name"], "amount": amount,
                "prev_balance": prev_balance, "new_balance": new_balance}
    finally:
        conn.close()

def tool_get_low_stock(args: dict, ctx: dict) -> dict:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT mp.part_name, mp.bike_model, mp.box_rack_location, mp.min_stock_level,
                       COALESCE(SUM(pb.quantity_remaining), 0) AS current_stock
                FROM master_parts mp
                JOIN brand_mappings bm ON bm.master_part_id = mp.id
                LEFT JOIN purchase_batches pb ON pb.brand_mapping_id = bm.id
                WHERE mp.is_active
                GROUP BY mp.id, mp.part_name, mp.bike_model, mp.box_rack_location, mp.min_stock_level
                HAVING COALESCE(SUM(pb.quantity_remaining), 0) < mp.min_stock_level
                ORDER BY current_stock ASC
            """)
            return {"items": [clean_row(r) for r in cur.fetchall()]}
    finally:
        conn.close()

def tool_get_dead_stock(args: dict, ctx: dict) -> dict:
    days = args.get("days", 90)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT mp.part_name, bm.brand_name, bm.last_sold_at,
                       COALESCE(SUM(pb.quantity_remaining), 0) AS stock,
                       COALESCE(SUM(pb.quantity_remaining * pb.effective_unit_cost), 0) AS blocked_investment
                FROM brand_mappings bm
                JOIN master_parts mp ON mp.id = bm.master_part_id
                JOIN purchase_batches pb ON pb.brand_mapping_id = bm.id
                WHERE pb.quantity_remaining > 0
                  AND (bm.last_sold_at IS NULL OR bm.last_sold_at < NOW() - (%s || ' days')::INTERVAL)
                GROUP BY mp.part_name, bm.brand_name, bm.last_sold_at
                ORDER BY bm.last_sold_at ASC NULLS FIRST
            """, (days,))
            rows = [clean_row(r) for r in cur.fetchall()]
            total_blocked = sum(r["blocked_investment"] for r in rows)
            return {"items": rows, "total_blocked_investment": total_blocked, "days": days}
    finally:
        conn.close()

def tool_cancel_invoice(args: dict, ctx: dict) -> dict:
    conn = get_db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT cancel_invoice(%s, %s)", (args["invoice_id"], args.get("reason", "Cancelled via Bot")))
        return {"success": True, "invoice_id": args["invoice_id"]}
    except psycopg2.Error as e:
        return {"error": e.diag.message_primary or str(e)}
    finally:
        conn.close()

def tool_return_item(args: dict, ctx: dict) -> dict:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT si2.id, mp.part_name, bm.brand_name, si2.quantity_sold
                FROM sale_items si2
                JOIN purchase_batches pb ON pb.id = si2.batch_id
                JOIN brand_mappings bm ON bm.id = pb.brand_mapping_id
                JOIN master_parts mp ON mp.id = bm.master_part_id
                WHERE si2.invoice_id = %s AND (mp.part_name ILIKE %s OR bm.brand_name ILIKE %s)
                LIMIT 1
            """, (args["invoice_id"], f"%{args['part_query']}%", f"%{args['part_query']}%"))
            row = cur.fetchone()
            if not row:
                return {"error": "Yeh item is invoice ke record mein nahi mila."}
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT return_sale_item(%s, %s)", (row["id"], args["quantity"]))
        return {"success": True, "returned": f"{args['quantity']}x {row['part_name']} ({row['brand_name']})"}
    except psycopg2.Error as e:
        return {"error": e.diag.message_primary or str(e)}
    finally:
        conn.close()

def tool_scheme_profit_report(args: dict, ctx: dict) -> dict:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT mp.part_name, bm.brand_name, pb.scheme_buy_qty, pb.scheme_free_qty,
                       pb.effective_unit_cost, pb.created_at,
                       (pb.scheme_free_qty * pb.effective_unit_cost) AS approx_extra_profit
                FROM purchase_batches pb
                JOIN brand_mappings bm ON bm.id = pb.brand_mapping_id
                JOIN master_parts mp ON mp.id = bm.master_part_id
                WHERE pb.scheme_free_qty > 0
                ORDER BY pb.created_at DESC LIMIT 30
            """)
            rows = [clean_row(r) for r in cur.fetchall()]
            total_free = sum(r["scheme_free_qty"] for r in rows)
            total_profit = sum(r["approx_extra_profit"] for r in rows)
            return {"batches": rows, "total_free_pieces": total_free, "total_extra_profit": total_profit}
    finally:
        conn.close()

def tool_correct_stock_or_price(args: dict, ctx: dict) -> dict:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            parts = resolve_parts(cur, args["part_query"])
            if len(parts) == 0:
                return {"error": "Item nahi mila."}
            if len(parts) > 1:
                return {"needs_clarification": True, "type": "part", "options": parts}
            p = parts[0]

        with conn:
            with conn.cursor() as cur:
                changes = []
                if args.get("new_rate") is not None:
                    cur.execute("UPDATE brand_mappings SET current_selling_price=%s WHERE id=%s",
                                (args["new_rate"], p["brand_mapping_id"]))
                    changes.append(f"Rate updated -> Rs. {args['new_rate']}")
                if args.get("stock_adjustment") is not None:
                    cur.execute("""
                        UPDATE purchase_batches SET quantity_remaining = quantity_remaining + %s
                        WHERE id = (SELECT id FROM purchase_batches WHERE brand_mapping_id=%s ORDER BY created_at DESC LIMIT 1)
                    """, (args["stock_adjustment"], p["brand_mapping_id"]))
                    changes.append(f"Stock adjusted -> {args['stock_adjustment']:+d}")
        return {"success": True, "item": f"{p['part_name']} ({p['brand_name']})", "changes": changes}
    except psycopg2.Error as e:
        return {"error": e.diag.message_primary or str(e)}
    finally:
        conn.close()

def tool_register_customer(args: dict, ctx: dict) -> dict:
    conn = get_db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO customers (name, phone_number, customer_type)
                    VALUES (%s, %s, %s) RETURNING id, name
                """, (args["name"], args.get("phone_number"), args.get("customer_type", "RETAIL").upper()))
                row = cur.fetchone()
        return {"success": True, "customer_id": row["id"], "name": row["name"]}
    except psycopg2.Error as e:
        return {"error": e.diag.message_primary or str(e)}
    finally:
        conn.close()

def tool_send_reminder(args: dict, ctx: dict) -> dict:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            matches = resolve_customers(cur, args["customer_name"])
            if len(matches) == 0:
                return {"error": "Customer record mein nahi mila."}
            if len(matches) > 1:
                return {"needs_clarification": True, "type": "customer", "options": matches}
            c = matches[0]
        if not c.get("phone_number"):
            return {"error": f"{c['name']} ka phone number record mein nahi hai."}
        if c["current_balance"] <= 0:
            return {"error": f"{c['name']} par koi baqi balance nahi hai."}
        send_whatsapp_message(c["phone_number"],
            f"Salam! Pamir Auto Parts se aapka khata reminder: Aapka kul baqi balance Rs. {c['current_balance']:,.0f} hai. Shukriya.")
        return {"success": True, "sent_to": c["name"], "balance": c["current_balance"]}
    finally:
        conn.close()

def tool_generate_report(args: dict, ctx: dict) -> dict:
    report_type = args["report_type"]
    conn = get_db()
    try:
        if report_type == "daily_sales":
            path, fname = build_daily_sales_pdf(conn)
        elif report_type == "customer_statement":
            with conn.cursor() as cur:
                matches = resolve_customers(cur, args.get("customer_name", ""))
            if len(matches) == 0:
                return {"error": "Customer record mein nahi mila."}
            if len(matches) > 1:
                return {"needs_clarification": True, "type": "customer", "options": matches}
            path, fname = build_customer_statement_pdf(conn, matches[0])
        else:
            return {"error": "Ghalat report type select ki hai."}

        send_whatsapp_document(ctx["phone"], path, fname, caption="📄 Matlooba PDF Report")
        return {"success": True, "filename": fname}
    finally:
        conn.close()


# =====================================================================
# PDF GENERATION LOGIC
# =====================================================================

def clean_pdf_str(text: str) -> str:
    """ASCII safe string converter for FPDF default core fonts."""
    return str(text).encode('ascii', 'replace').decode('ascii')

def build_daily_sales_pdf(conn) -> tuple:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT si.id, si.invoice_type, si.total_amount, si.created_at, c.name AS customer_name
            FROM sales_invoices si LEFT JOIN customers c ON c.id = si.customer_id
            WHERE si.created_at::date = CURRENT_DATE AND NOT si.is_cancelled
            ORDER BY si.created_at
        """)
        rows = cur.fetchall()
        cur.execute("""
            SELECT COALESCE(SUM(si2.quantity_sold * (si2.selling_price - si2.cost_price)), 0) AS profit
            FROM sale_items si2 JOIN sales_invoices si ON si.id = si2.invoice_id
            WHERE si.created_at::date = CURRENT_DATE AND NOT si.is_cancelled
        """)
        profit = to_float(cur.fetchone()["profit"])

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Pamir Auto Parts - Daily Sales Report", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, datetime.now().strftime("%d %b %Y"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    cash_total = sum(to_float(r["total_amount"]) for r in rows if r["invoice_type"] == "CASH")
    credit_total = sum(to_float(r["total_amount"]) for r in rows if r["invoice_type"] == "CREDIT")

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, clean_pdf_str(f"Cash Sales: Rs. {cash_total:,.0f} | Udhaar: Rs. {credit_total:,.0f} | Profit: Rs. {profit:,.0f}"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(25, 8, "Inv #", border=1)
    pdf.cell(65, 8, "Customer", border=1)
    pdf.cell(30, 8, "Type", border=1)
    pdf.cell(40, 8, "Amount (Rs.)", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    for r in rows:
        pdf.cell(25, 7, str(r["id"]), border=1)
        pdf.cell(65, 7, clean_pdf_str(r["customer_name"] or "Cash Sale"), border=1)
        pdf.cell(30, 7, str(r["invoice_type"]), border=1)
        pdf.cell(40, 7, clean_pdf_str(f"{to_float(r['total_amount']):,.0f}"), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    fname = f"Sales_Report_{datetime.now().strftime('%d_%b_%Y')}.pdf"
    path = f"/tmp/{fname}"
    pdf.output(path)
    return path, fname

def build_customer_statement_pdf(conn, customer: dict) -> tuple:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT si.id, si.total_amount, si.amount_paid, si.is_fully_paid, si.created_at,
                   mp.part_name, bm.brand_name, si2.quantity_sold, si2.selling_price
            FROM sales_invoices si
            JOIN sale_items si2 ON si2.invoice_id = si.id
            JOIN purchase_batches pb ON pb.id = si2.batch_id
            JOIN brand_mappings bm ON bm.id = pb.brand_mapping_id
            JOIN master_parts mp ON mp.id = bm.master_part_id
            WHERE si.customer_id = %s AND si.invoice_type='CREDIT' AND NOT si.is_cancelled
            ORDER BY si.created_at
        """, (customer["id"],))
        rows = cur.fetchall()

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Pamir Auto Parts - Customer Statement", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, clean_pdf_str(f"Customer: {customer['name']}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 7, clean_pdf_str(f"Current Balance: Rs. {customer['current_balance']:,.0f}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    grouped = {}
    for r in rows:
        grouped.setdefault(r["id"], {"paid": r["is_fully_paid"], "total": r["total_amount"], "items": []})
        grouped[r["id"]]["items"].append(r)

    pdf.set_font("Helvetica", "", 10)
    for inv_id, data in grouped.items():
        line = f"Invoice #{inv_id} - Total: Rs. {to_float(data['total']):,.0f}"
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, clean_pdf_str(line), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9)
        for item in data["items"]:
            item_str = f"   * {item['quantity_sold']}x {item['part_name']} ({item['brand_name']}) @ Rs.{to_float(item['selling_price']):,.0f}"
            pdf.cell(0, 6, clean_pdf_str(item_str), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1)

    fname = f"Statement_{clean_pdf_str(customer['name']).replace(' ', '_')}.pdf"
    path = f"/tmp/{fname}"
    pdf.output(path)
    return path, fname


# =====================================================================
# GEMINI ENGINE & TOOL MAPPING
# =====================================================================

TOOL_MAP = {
    "search_part": tool_search_part,
    "create_sale": tool_create_sale,
    "get_khata": tool_get_khata,
    "record_payment": tool_record_payment,
    "get_low_stock": tool_get_low_stock,
    "get_dead_stock": tool_get_dead_stock,
    "cancel_invoice": tool_cancel_invoice,
    "return_item": tool_return_item,
    "scheme_profit_report": tool_scheme_profit_report,
    "correct_stock_or_price": tool_correct_stock_or_price,
    "register_customer": tool_register_customer,
    "send_reminder": tool_send_reminder,
    "generate_report": tool_generate_report,
}

TOOLS = [{"function_declarations": [
    {
        "name": "search_part",
        "description": "Part ka rate, stock, rack/box location check karein. Multiple brands milne par comparison list aati hai.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Part ka naam, model, ya brand"},
        }, "required": ["query"]},
    },
    {
        "name": "create_sale",
        "description": "Cash ya Udhaar bill create karein.",
        "parameters": {"type": "object", "properties": {
            "invoice_type": {"type": "string", "enum": ["CASH", "CREDIT"]},
            "customer_name": {"type": "string", "description": "Sirf CREDIT ke liye required"},
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "part_query": {"type": "string"},
                "quantity": {"type": "integer"},
                "sold_price": {"type": "number"},
            }, "required": ["part_query", "quantity", "sold_price"]}},
        }, "required": ["invoice_type", "items"]},
    },
    {
        "name": "get_khata",
        "description": "Customer ka baqi udhaar aur unpaid bills check karein.",
        "parameters": {"type": "object", "properties": {
            "customer_name": {"type": "string"},
        }, "required": ["customer_name"]},
    },
    {
        "name": "record_payment",
        "description": "Customer se aane wali raqam/wasooli record karein.",
        "parameters": {"type": "object", "properties": {
            "customer_name": {"type": "string"},
            "amount": {"type": "number"},
            "method": {"type": "string"},
        }, "required": ["customer_name", "amount"]},
    },
    {
        "name": "get_low_stock",
        "description": "Wo items jin ka stock minimum level se kam ho gaya hai.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_dead_stock",
        "description": "Wo items jo kafi arsay se sale nahi huay aur investment block hai.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer"},
        }},
    },
    {
        "name": "cancel_invoice",
        "description": "Pura invoice cancel karein (stock aur khata automatically reverse hoga).",
        "parameters": {"type": "object", "properties": {
            "invoice_id": {"type": "integer"},
            "reason": {"type": "string"},
        }, "required": ["invoice_id"]},
    },
    {
        "name": "return_item",
        "description": "Kisi invoice ka koi ek item return karein.",
        "parameters": {"type": "object", "properties": {
            "invoice_id": {"type": "integer"},
            "part_query": {"type": "string"},
            "quantity": {"type": "integer"},
        }, "required": ["invoice_id", "part_query", "quantity"]},
    },
    {
        "name": "scheme_profit_report",
        "description": "Vendor schemes (maslan 10+1 free) se hone wala extra profit dekhein.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "correct_stock_or_price",
        "description": "Stock quantity ya selling price manually update karein.",
        "parameters": {"type": "object", "properties": {
            "part_query": {"type": "string"},
            "new_rate": {"type": "number"},
            "stock_adjustment": {"type": "integer"},
        }, "required": ["part_query"]},
    },
    {
        "name": "register_customer",
        "description": "Naya customer/mechanic register karein.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "phone_number": {"type": "string"},
            "customer_type": {"type": "string", "enum": ["MECHANIC", "WHOLESALE", "RETAIL"]},
        }, "required": ["name"]},
    },
    {
        "name": "send_reminder",
        "description": "Customer ko WhatsApp par payment reminder send karein.",
        "parameters": {"type": "object", "properties": {
            "customer_name": {"type": "string"},
        }, "required": ["customer_name"]},
    },
    {
        "name": "generate_report",
        "description": "Daily Sales ya Khata statement ka PDF generate karke send karein.",
        "parameters": {"type": "object", "properties": {
            "report_type": {"type": "string", "enum": ["daily_sales", "customer_statement"]},
            "customer_name": {"type": "string"},
        }, "required": ["report_type"]},
    },
]}]

SYSTEM_PROMPT = """Aap Pamir Auto Parts shop ke AI Assistant hain jo hamesha natural Roman Urdu me baat karte hain.
Rules:
1. Jawab concise, clear aur bulleted points me dein.
2. Prices hamesha Pakistani Rupees (Rs.) me mention karein.
3. Agar kisi function se 'needs_clarification' mile, to user ko numbered list (1, 2, 3) dikhayein taake wo asani se select kar sakay.
4. Low stock ya dead stock ki details sirf tab dein jab user specifically poochay."""

model = genai.GenerativeModel(
    "gemini-3.5-flash",
    tools=TOOLS,
    system_instruction=SYSTEM_PROMPT,
)

vision_model = genai.GenerativeModel("gemini-3.5-flash")

def run_gemini_turn(phone: str, user_text: str) -> str:
    sess = load_session(phone)
    history = sess["history"][-20:]

    chat = model.start_chat(history=history)
    response = chat.send_message(user_text)

    try:
        part = response.candidates[0].content.parts[0]
    except (IndexError, AttributeError):
        return "Message samajh nahi aa saka, dobara koshish karein."

    if hasattr(part, "function_call") and part.function_call and part.function_call.name:
        fn_name = part.function_call.name
        fn_args = dict(part.function_call.args)
        handler = TOOL_MAP.get(fn_name)
        
        raw_result = handler({**fn_args}, {"phone": phone}) if handler else {"error": "Tool mojood nahi hai."}
        safe_result = sanitize_for_gemini(raw_result)

        response2 = chat.send_message(
            genai.protos.Content(parts=[genai.protos.Part(
                function_response=genai.protos.FunctionResponse(name=fn_name, response={"result": safe_result})
            )])
        )
        final_text = response2.text
    else:
        final_text = response.text

    new_history = [{"role": m.role, "parts": [p.text for p in m.parts if hasattr(p, "text") and p.text]} for m in chat.history]
    save_session(phone, new_history)
    return final_text


# =====================================================================
# FAST UNDO HANDLER
# =====================================================================

def try_undo(phone: str) -> str:
    sess = load_session(phone)
    action = sess.get("last_action")
    if not action or action.get("type") != "payment":
        return "Undo karne ke liye koi recent payment entry mojood nahi hai."

    ts = datetime.fromisoformat(action["timestamp"])
    if (datetime.now(timezone.utc) - ts).total_seconds() > UNDO_WINDOW_SECONDS:
        return "5 minute ka undo time guzar chuka hai, ab entry reverse nahi ho sakti."

    conn = get_db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM ledger_transactions WHERE customer_id=%s ORDER BY created_at DESC LIMIT 1
                """, (action["customer_id"],))
                latest = cur.fetchone()
                if not latest or latest["id"] != action["ledger_id"]:
                    return "Is ke baad customer ki nayi transaction register ho chuki hai, undo safe nahi hai."

                cur.execute("DELETE FROM ledger_transactions WHERE id=%s", (action["ledger_id"],))
                cur.execute("UPDATE customers SET current_balance=%s WHERE id=%s",
                            (action["prev_balance"], action["customer_id"]))
                for a in action.get("allocations", []):
                    cur.execute("UPDATE sales_invoices SET amount_paid = amount_paid - %s WHERE id=%s",
                                (a["delta"], a["invoice_id"]))
        clear_last_action(phone)
        return f"↩️ Undo Mukammal: {action['customer_name']} ki Rs. {action['amount']:,.0f} wasooli cancel ho gayi. Balance dobara Rs. {action['prev_balance']:,.0f} ho gaya."
    finally:
        conn.close()


# =====================================================================
# VISION: OCR & VENDOR INVOICE IMPORT
# =====================================================================

def handle_price_list_document(file_bytes: bytes, mime_type: str, vendor_hint: str) -> str:
    prompt = f"""Extract vendor price list for auto parts (Vendor hint: {vendor_hint}).
Return strictly a valid JSON array of objects:
[{{"brand_item_code": "...", "part_name": "...", "bike_model": "...", "rate": 0.0, "quantity": 1, "scheme_buy": 0, "scheme_free": 0}}]
Do not output markdown code blocks or explanations."""
    response = vision_model.generate_content([{"mime_type": mime_type, "data": file_bytes}, prompt])
    clean = response.text.replace("```json", "").replace("```", "").strip()
    try:
        items = json.loads(clean)
    except Exception:
        return "List format parse nahi ho saka, file dobara bhejein."

    conn = get_db()
    updated, added = 0, 0
    try:
        with conn:
            with conn.cursor() as cur:
                for it in items:
                    part_name = it.get("part_name", "").strip()
                    bike_model = it.get("bike_model", "").strip() or "UNIVERSAL"
                    rate = float(it.get("rate") or 0)
                    qty = int(it.get("quantity") or 1)
                    if not part_name or rate <= 0:
                        continue

                    cur.execute("""
                        SELECT id FROM master_parts WHERE part_name ILIKE %s AND bike_model ILIKE %s LIMIT 1
                    """, (part_name, bike_model))
                    mp = cur.fetchone()
                    if not mp:
                        sku = f"SKU-{bike_model}-{part_name}".upper().replace(" ", "-")[:60]
                        cur.execute("""
                            INSERT INTO master_parts (universal_sku, part_name, bike_model)
                            VALUES (%s, %s, %s) ON CONFLICT (universal_sku) DO NOTHING RETURNING id
                        """, (sku, part_name, bike_model))
                        row = cur.fetchone()
                        mp_id = row["id"] if row else None
                        if not mp_id:
                            cur.execute("SELECT id FROM master_parts WHERE universal_sku=%s", (sku,))
                            mp_id = cur.fetchone()["id"]
                        added += 1
                    else:
                        mp_id = mp["id"]

                    code = it.get("brand_item_code") or f"{vendor_hint}-{part_name}"[:100]
                    cur.execute("""
                        SELECT id FROM brand_mappings WHERE brand_name=%s AND brand_item_code=%s
                    """, (vendor_hint, code))
                    bm = cur.fetchone()
                    if not bm:
                        cur.execute("""
                            INSERT INTO brand_mappings (master_part_id, brand_name, brand_item_code, current_selling_price)
                            VALUES (%s, %s, %s, %s) RETURNING id
                        """, (mp_id, vendor_hint, code, rate))
                        bm_id = cur.fetchone()["id"]
                    else:
                        bm_id = bm["id"]
                        updated += 1

                    scheme_buy = int(it.get("scheme_buy") or 0)
                    scheme_free = int(it.get("scheme_free") or 0)
                    total_cost = rate * qty
                    cur.execute("""
                        INSERT INTO purchase_batches
                            (brand_mapping_id, total_purchase_cost, quantity_bought, quantity_remaining,
                             scheme_buy_qty, scheme_free_qty, source_list_date)
                        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_DATE)
                    """, (bm_id, total_cost, qty, qty, scheme_buy, scheme_free))
        return f"✅ Price List Update Hogayi!\nVendor: {vendor_hint}\nScanned: {len(items)}\nNew Added: {added}\nUpdated: {updated}"
    finally:
        conn.close()

def handle_part_photo(img_bytes: bytes) -> str:
    prompt = """Identify the auto spare part and brand from the packaging/label.
Return strictly JSON: {"brand": "...", "part_name": "...", "model": "..."}"""
    response = vision_model.generate_content([{"mime_type": "image/jpeg", "data": img_bytes}, prompt])
    clean = response.text.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(clean)
    except Exception:
        return "Tasweer se part identify nahi ho saka."

    query = f"{parsed.get('part_name','')} {parsed.get('brand','')}".strip()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            rows = resolve_parts(cur, query)
        if not rows:
            return f"Part identify hua: {parsed.get('part_name','?')} ({parsed.get('brand','?')}), lekin stock me rate nahi mila."
        lines = "\n".join(f"• {r['part_name']} ({r['brand_name']} {r['bike_model']}): Rs. {r['list_rate']} | Stock: {r['stock']} pcs | Loc: {r['box_rack_location']}" for r in rows)
        return f"🔍 System Match Found:\n{lines}"
    finally:
        conn.close()


# =====================================================================
# BACKGROUND ASYNC WHATSAPP WORKER
# =====================================================================

def process_whatsapp_payload(data: dict):
    try:
        entry = data.get("entry", [])[0]["changes"][0]["value"]
        if "messages" not in entry:
            return

        msg = entry["messages"][0]
        sender = msg["from"]
        msg_type = msg.get("type")

        if msg_type == "image":
            img_bytes = download_whatsapp_media(msg["image"]["id"])
            caption = (msg["image"].get("caption") or "").lower()
            if any(w in caption for w in ["list", "rate", "price", "vendor"]):
                reply = handle_price_list_document(img_bytes, "image/jpeg", caption.split()[0] if caption else "Vendor")
            else:
                reply = handle_part_photo(img_bytes)
            send_whatsapp_message(sender, reply)
            return

        if msg_type == "document":
            doc = msg["document"]
            file_bytes = download_whatsapp_media(doc["id"])
            caption = doc.get("caption") or ""
            reply = handle_price_list_document(file_bytes, doc.get("mime_type", "application/pdf"), caption or "Vendor")
            send_whatsapp_message(sender, reply)
            return

        if msg_type != "text":
            return

        text = msg["text"]["body"].strip()

        if text.lower() in ("undo", "undo karo", "wapis karo"):
            send_whatsapp_message(sender, try_undo(sender))
            return

        reply = run_gemini_turn(sender, text)
        send_whatsapp_message(sender, reply)
    except Exception as e:
        print(f"Async WhatsApp processing error: {e}")


# =====================================================================
# WHATSAPP WEBHOOK ROUTES
# =====================================================================

@app.get("/")
def root():
    return {"status": "Pamir Auto Parts System Active", "version": "6.0.0"}

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return int(params.get("hub.challenge"))
    return "Verification failed", 400

@app.post("/webhook")
async def handle_incoming(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    background_tasks.add_task(process_whatsapp_payload, data)
    return {"status": "queued"}


# =====================================================================
# REST APIS FOR WEB FRONTEND
# =====================================================================

class CustomerCreateRequest(BaseModel):
    name: str
    phone_number: Optional[str] = None
    customer_type: Optional[str] = "RETAIL"

class SaleItemRequest(BaseModel):
    brand_mapping_id: int
    quantity: int
    sold_price: float

class SaleCreateRequest(BaseModel):
    invoice_type: str  # CASH or CREDIT
    customer_id: Optional[int] = None
    items: List[SaleItemRequest]

@app.get("/api/parts/search")
def api_search_parts(q: str = Query(..., min_length=1)):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            items = resolve_parts(cur, q)
        return {"count": len(items), "data": items}
    finally:
        conn.close()

@app.get("/api/customers/search")
def api_search_customers(q: str = Query(..., min_length=1)):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            customers = resolve_customers(cur, q)
        return {"count": len(customers), "data": customers}
    finally:
        conn.close()

@app.post("/api/customers")
def api_create_customer(req: CustomerCreateRequest):
    conn = get_db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO customers (name, phone_number, customer_type)
                    VALUES (%s, %s, %s) RETURNING id, name, phone_number, customer_type, current_balance
                """, (req.name, req.phone_number, req.customer_type.upper()))
                row = clean_row(cur.fetchone())
        return {"success": True, "data": row}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

@app.post("/api/sales")
def api_create_sale(req: SaleCreateRequest):
    conn = get_db()
    try:
        items_payload = json.dumps([i.dict() for i in req.items])
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT create_sale_invoice(%s, %s, %s::jsonb) AS invoice_id",
                    (req.customer_id, req.invoice_type.upper(), items_payload)
                )
                invoice_id = cur.fetchone()["invoice_id"]
                cur.execute("SELECT total_amount FROM sales_invoices WHERE id=%s", (invoice_id,))
                total = to_float(cur.fetchone()["total_amount"])

                if req.invoice_type.upper() == "CREDIT" and req.customer_id:
                    cur.execute("""
                        INSERT INTO ledger_transactions (customer_id, invoice_id, entry_type, amount, description)
                        VALUES (%s, %s, 'DEBIT', %s, %s)
                    """, (req.customer_id, invoice_id, total, f"Web Invoice #{invoice_id}"))

        return {"success": True, "invoice_id": invoice_id, "total_amount": total}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

@app.get("/api/sales/today")
def api_today_sales():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT si.id, si.invoice_type, si.total_amount, si.created_at, c.name AS customer_name
                FROM sales_invoices si LEFT JOIN customers c ON c.id = si.customer_id
                WHERE si.created_at::date = CURRENT_DATE AND NOT si.is_cancelled
                ORDER BY si.created_at DESC
            """)
            sales = [clean_row(r) for r in cur.fetchall()]
        return {"count": len(sales), "sales": sales}
    finally:
        conn.close()


def process_whatsapp_payload(data: dict):
    try:
        entry = data.get("entry", [])[0]["changes"][0]["value"]
        if "messages" not in entry:
            return

        msg = entry["messages"][0]
        sender = msg["from"]
        msg_type = msg.get("type")

        # 1. IMAGE PROCESSING (Photo / Packing / Label)
        if msg_type == "image":
            img_bytes = download_whatsapp_media(msg["image"]["id"])
            caption = (msg["image"].get("caption") or "").strip()
            
            conn = get_db()
            try:
                # Agar caption me list/rate/invoice likha ho to Vendor mode, warna Part OCR
                if any(w in caption.lower() for w in ["list", "rate", "invoice", "vendor", "bill"]):
                    vendor_hint = caption.split()[0] if caption else "Vendor"
                    reply = process_vendor_document(img_bytes, "image/jpeg", vendor_hint, conn)
                else:
                    reply = process_part_image(img_bytes, conn)
            finally:
                conn.close()

            send_whatsapp_message(sender, reply)
            return

        # 2. DOCUMENT PROCESSING (PDF / Invoices)
        if msg_type == "document":
            doc = msg["document"]
            file_bytes = download_whatsapp_media(doc["id"])
            caption = (doc.get("caption") or "").strip()
            mime_type = doc.get("mime_type", "application/pdf")
            vendor_hint = caption.split()[0] if caption else "Vendor"

            conn = get_db()
            try:
                reply = process_vendor_document(file_bytes, mime_type, vendor_hint, conn)
            finally:
                conn.close()

            send_whatsapp_message(sender, reply)
            return

        # 3. TEXT PROCESSING
        if msg_type != "text":
            return

        text = msg["text"]["body"].strip()

        if text.lower() in ("undo", "undo karo", "wapis karo"):
            send_whatsapp_message(sender, try_undo(sender))
            return

        reply = run_gemini_turn(sender, text)
        send_whatsapp_message(sender, reply)

    except Exception as e:
        print(f"Async WhatsApp processing error: {e}")
