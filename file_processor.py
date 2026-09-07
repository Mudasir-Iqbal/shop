import json
import re
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor
import google.generativeai as genai

vision_model = genai.GenerativeModel("gemini-1.5-flash")

def clean_sku(text: str) -> str:
    """Generate database-safe SKU adhering to VARCHAR(64)."""
    return re.sub(r'[^A-Z0-9-]', '', text.upper().replace(" ", "-"))[:60]

def process_part_image(img_bytes: bytes, conn) -> str:
    """
    Type 1: Single Part Packaging / Sticker Scanner
    Extracts brand, part, bike model and queries database directly.
    """
    prompt = """
    Analyze this spare part packaging/label image.
    Extract the following information strictly in JSON format:
    {
      "part_name": "Name of part (e.g. Spark Plug, Piston, Clutch Plate)",
      "brand": "Brand (e.g. Crown, Honda, ISH, NGK)",
      "bike_model": "Bike Model (e.g. CD70, CG125, GS150, 70cc)"
    }
    If any field is unreadable, set value to null. Output raw JSON only.
    """
    try:
        response = vision_model.generate_content([
            {"mime_type": "image/jpeg", "data": img_bytes},
            prompt
        ])
        raw_json = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw_json)
    except Exception as e:
        return "❌ Tasweer se part identify nahi ho saka. Barah-e-karam saaf photo bhejein."

    part_name = data.get("part_name")
    brand = data.get("brand")
    model = data.get("bike_model")

    if not part_name:
        return "⚠️ Part identify nahi ho saka. Label ki clear tasweer bhejein."

    # Fuzzy search using your v6 schema
    with conn.cursor() as cur:
        cur.execute("""
            SELECT bm.id, mp.part_name, mp.bike_model, bm.brand_name, 
                   bm.current_selling_price, mp.box_rack_location,
                   COALESCE(SUM(pb.quantity_remaining), 0) AS total_stock
            FROM master_parts mp
            JOIN brand_mappings bm ON bm.master_part_id = mp.id
            LEFT JOIN purchase_batches pb ON pb.brand_mapping_id = bm.id
            WHERE mp.is_active = TRUE
              AND (mp.part_name ILIKE %s OR mp.part_name % %s)
              AND (%s IS NULL OR bm.brand_name ILIKE %s)
            GROUP BY bm.id, mp.part_name, mp.bike_model, bm.brand_name, 
                     bm.current_selling_price, mp.box_rack_location
            LIMIT 5;
        """, (f"%{part_name}%", part_name, brand, f"%{brand}%" if brand else None))
        matches = cur.fetchall()

    if not matches:
        return f"🔍 Tasweer se pehchana gaya:\n• Part: *{part_name}*\n• Brand: *{brand or 'N/A'}*\n• Model: *{model or 'N/A'}*\n\n❌ Lekin database me matching stock/rate nahi mila."

    res = f"🔍 *Found Matches ({part_name})*:\n\n"
    for m in matches:
        res += (
            f"📦 *{m['part_name']}* ({m['brand_name']} - {m['bike_model']})\n"
            f"• Rate: *Rs. {float(m['current_selling_price']):,.0f}*\n"
            f"• Stock: *{m['total_stock']} pcs*\n"
            f"• Location: *{m['box_rack_location'] or 'Rack/Box unspecified'}*\n\n"
        )
    return res.strip()


def process_vendor_document(file_bytes: bytes, mime_type: str, vendor_name: str, conn) -> str:
    """
    Type 2: Bulk Vendor Price-List (PDF / Image Invoice)
    Parses items and commits into:
      1. master_parts (if new)
      2. brand_mappings (updates selling price)
      3. purchase_batches (records purchase & schemes)
    """
    prompt = f"""
    You are an ERP data extraction assistant for an Auto Parts business.
    Vendor Name: {vendor_name}
    Extract all items from this price list/invoice into a JSON array:
    [
      {{
        "brand_item_code": "code or serial",
        "part_name": "clean part name",
        "bike_model": "CD70, CG125 etc (default 'UNIVERSAL')",
        "purchase_cost": 0.0,
        "selling_price": 0.0,
        "quantity": 1,
        "scheme_buy": 0,
        "scheme_free": 0
      }}
    ]
    Note: Output valid strict JSON array only. No comments.
    """
    try:
        response = vision_model.generate_content([
            {"mime_type": mime_type, "data": file_bytes},
            prompt
        ])
        raw_json = response.text.replace("```json", "").replace("```", "").strip()
        items = json.loads(raw_json)
    except Exception as e:
        return f"❌ Document parsing error: File format ya data samajh nahi aaya."

    if not items or not isinstance(items, list):
        return "❌ Document se koi items extract nahi ho sakay."

    inserted_parts = 0
    updated_mappings = 0
    new_batches = 0

    with conn:
        with conn.cursor() as cur:
            for item in items:
                p_name = item.get("part_name", "").strip()
                b_model = item.get("bike_model", "UNIVERSAL").strip().upper()
                cost = float(item.get("purchase_cost") or 0.0)
                price = float(item.get("selling_price") or (cost * 1.15))  # Default 15% margin agar na ho
                qty = int(item.get("quantity") or 1)
                code = item.get("brand_item_code") or f"{vendor_name[:3]}-{p_name[:10]}"
                scheme_buy = int(item.get("scheme_buy") or 0)
                scheme_free = int(item.get("scheme_free") or 0)

                if not p_name or cost <= 0:
                    continue

                # 1. Master Part upsert
                cur.execute("""
                    SELECT id FROM master_parts 
                    WHERE part_name ILIKE %s AND bike_model ILIKE %s 
                    LIMIT 1;
                """, (p_name, b_model))
                mp_row = cur.fetchone()

                if mp_row:
                    master_part_id = mp_row["id"]
                else:
                    sku = clean_sku(f"{b_model}-{p_name}")
                    cur.execute("""
                        INSERT INTO master_parts (universal_sku, part_name, bike_model)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (universal_sku) DO NOTHING
                        RETURNING id;
                    """, (sku, p_name, b_model))
                    ret = cur.fetchone()
                    master_part_id = ret["id"] if ret else None
                    if not master_part_id:
                        cur.execute("SELECT id FROM master_parts WHERE universal_sku = %s", (sku,))
                        master_part_id = cur.fetchone()["id"]
                    inserted_parts += 1

                # 2. Brand Mapping upsert
                cur.execute("""
                    INSERT INTO brand_mappings (
                        master_part_id, brand_name, brand_item_code, 
                        current_selling_price, scheme_buy_qty, scheme_free_qty
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (brand_name, brand_item_code) 
                    DO UPDATE SET 
                        current_selling_price = EXCLUDED.current_selling_price,
                        scheme_buy_qty = EXCLUDED.scheme_buy_qty,
                        scheme_free_qty = EXCLUDED.scheme_free_qty,
                        updated_at = NOW()
                    RETURNING id;
                """, (master_part_id, vendor_name, code, price, scheme_buy, scheme_free))
                bm_id = cur.fetchone()["id"]
                updated_mappings += 1

                # 3. Insert Purchase Batch for FIFO tracking
                total_cost = cost * qty
                cur.execute("""
                    INSERT INTO purchase_batches (
                        brand_mapping_id, total_purchase_cost, quantity_bought, 
                        quantity_remaining, scheme_buy_qty, scheme_free_qty, source_list_date
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_DATE);
                """, (bm_id, total_cost, qty, qty, scheme_buy, scheme_free))
                new_batches += 1

    return (
        f"✅ *Vendor List Upload Successful!*\n"
        f"• Vendor: *{vendor_name}*\n"
        f"• Total Processed Items: *{len(items)}*\n"
        f"• New Master Parts Added: *{inserted_parts}*\n"
        f"• Rates/Mappings Updated: *{updated_mappings}*\n"
        f"• Stock Batches Logged: *{new_batches}*"
    )
