# app.py — WhatsApp Cloud API + SQLite + Google Sheets (Apps Script)

import os
import re
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests

load_dotenv()

# ======== WHATSAPP / META CLOUD API ========
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
GRAPH_URL_WA = f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
TZ = ZoneInfo("America/New_York")  # zona horaria para cálculos

# ======== GOOGLE SHEETS (Apps Script) ========
GOOGLE_APPS_SCRIPT_URL = os.getenv("GOOGLE_APPS_SCRIPT_URL", "").strip()
GOOGLE_APPS_SCRIPT_KEY = os.getenv("GOOGLE_APPS_SCRIPT_KEY", "").strip()

# ======== FLASK ========
app = Flask(__name__)

# ======== DB (SQLite) ========
DB_PATH = "expenses.db"

def db_connect():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = db_connect()
    c = conn.cursor()
    # gastos
    c.execute("""
      CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user TEXT NOT NULL,
        amount REAL NOT NULL,
        category_id INTEGER NOT NULL,
        category_name TEXT NOT NULL,
        ts_utc TEXT NOT NULL,
        ts_epoch INTEGER
      )
    """)
    # sesiones (persistencia de estado)
    c.execute("""
      CREATE TABLE IF NOT EXISTS sessions (
        user TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        amount REAL
      )
    """)
    conn.commit()
    conn.close()

def ensure_ts_epoch_column():
    conn = db_connect()
    c = conn.cursor()
    c.execute("PRAGMA table_info(expenses)")
    cols = [row[1] for row in c.fetchall()]
    if "ts_epoch" not in cols:
        try:
            c.execute("ALTER TABLE expenses ADD COLUMN ts_epoch INTEGER")
            conn.commit()
        except Exception as e:
            print("ALTER TABLE expenses add ts_epoch failed:", e)
    conn.close()

def backfill_ts_epoch_from_ts_utc():
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT id, ts_utc FROM expenses WHERE ts_epoch IS NULL OR ts_epoch = ''")
    rows = c.fetchall()
    updated = 0
    for rid, ts in rows:
        try:
            dt = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
            epoch = int(dt.timestamp())
            c.execute("UPDATE expenses SET ts_epoch = ? WHERE id = ?", (epoch, rid))
            updated += 1
        except Exception as e:
            print("Backfill parse error for id", rid, ts, e)
    conn.commit()
    conn.close()
    if updated:
        print(f"Backfilled ts_epoch rows: {updated}")

init_db()
ensure_ts_epoch_column()
backfill_ts_epoch_from_ts_utc()

# ======== CATEGORÍAS / TRIGGERS ========
CATEGORIES = {
    "1": "Renta",
    "2": "Credit card bill",
    "3": "Medical bill",
    "4": "Utility bill",
    "5": "Car payment",
    "6": "Restaurante",
    "7": "Groceries & housekeeping",
    "8": "Traveling",
}
TRIGGERS = {"ingresar gasto", "ingresar un gasto", "gasto", "nuevo gasto"}

# ======== SESIONES (persistidas) ========
def get_session(user: str):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT state, amount FROM sessions WHERE user = ?", (user,))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO sessions (user, state, amount) VALUES (?, ?, ?)", (user, "idle", None))
        conn.commit()
        conn.close()
        return {"state": "idle", "amount": None}
    conn.close()
    return {"state": row[0], "amount": row[1]}

def set_session(user: str, state: str, amount):
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO sessions(user, state, amount) VALUES (?, ?, ?)
        ON CONFLICT(user) DO UPDATE SET state=excluded.state, amount=excluded.amount
    """, (user, state, amount))
    conn.commit()
    conn.close()

def reset_session(user: str):
    set_session(user, "idle", None)

# ======== MENSAJERÍA WHATSAPP ========
def send_whatsapp_text(to, text):
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
    r = requests.post(GRAPH_URL_WA, headers=headers, data=json.dumps(payload), timeout=15)
    if r.status_code >= 300:
        print("Error sending message:", r.status_code, r.text)

def send_whatsapp_category_list(to):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": "Elige la *categoría* del gasto:"},
            "footer": {"text": "Toca una opción 👇"},
            "action": {
                "button": "Ver categorías",
                "sections": [
                    {
                        "title": "Categorías",
                        "rows": [
                            {"id": "1", "title": "1. Renta"},
                            {"id": "2", "title": "2. Credit card bill"},
                            {"id": "3", "title": "3. Medical bill"},
                            {"id": "4", "title": "4. Utility bill"},
                            {"id": "5", "title": "5. Car payment"},
                            {"id": "6", "title": "6. Restaurante"},
                            {"id": "7", "title": "7. Groceries & housekeeping"},
                            {"id": "8", "title": "8. Traveling"},
                        ]
                    }
                ]
            }
        }
    }
    r = requests.post(GRAPH_URL_WA, headers=headers, data=json.dumps(payload), timeout=15)
    if r.status_code >= 300:
        print("Error sending category list:", r.status_code, r.text)
        return False
    return True

def ask_for_amount():
    return ("Ok, vamos a ingresar un gasto. 💸\n"
            "Dime el **valor del gasto en USD** (ej: 25.50).")

# ======== PARSEO ENTRANTE (WhatsApp) ========
def parse_sender_and_message(entry):
    """
    Devuelve (user, text, reply_id) donde:
      - text: texto si el mensaje es 'text'
      - reply_id: id de la opción si es interacción (list/button)
    """
    try:
        changes = entry.get("changes", [])
        for ch in changes:
            value = ch.get("value", {})
            messages = value.get("messages")
            if not messages:
                continue
            msg = messages[0]
            from_id = msg.get("from")
            msg_type = msg.get("type", "text")

            text = ""
            reply_id = None

            if msg_type == "text":
                text = (msg.get("text", {}).get("body", "") or "").strip()
            elif msg_type == "interactive":
                interactive = msg.get("interactive", {})
                if "list_reply" in interactive:
                    lr = interactive.get("list_reply", {}) or {}
                    reply_id = (lr.get("id") or "").strip()
                    text = (lr.get("title") or "").strip()
                elif "button_reply" in interactive:
                    br = interactive.get("button_reply", {}) or {}
                    reply_id = (br.get("id") or "").strip()
                    text = (br.get("title") or "").strip()

            return from_id, text, reply_id
    except Exception as e:
        print("parse_sender_and_message error:", e)
    return None, None, None

# ======== GASTOS (SQLite) ========
def save_expense(user, amount, category_id, category_name):
    conn = db_connect()
    c = conn.cursor()
    now = datetime.now(timezone.utc)
    ts_utc = now.isoformat()
    ts_epoch = int(now.timestamp())
    c.execute(
        "INSERT INTO expenses (user, amount, category_id, category_name, ts_utc, ts_epoch) VALUES (?, ?, ?, ?, ?, ?)",
        (user, amount, int(category_id), category_name, ts_utc, ts_epoch)
    )
    conn.commit()
    conn.close()

# ======== RANGOS DE TIEMPO ========
def month_bounds_now_ny():
    now_ny = datetime.now(TZ)
    start_ny = now_ny.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_ny.month == 12:
        next_month_ny = start_ny.replace(year=start_ny.year + 1, month=1)
    else:
        next_month_ny = start_ny.replace(month=start_ny.month + 1)
    start_utc = start_ny.astimezone(timezone.utc).isoformat()
    end_utc = next_month_ny.astimezone(timezone.utc).isoformat()
    label = f"Mes actual ({start_ny.strftime('%Y-%m')})"
    return start_utc, end_utc, label

def last_n_days_bounds_ny(n_days: int):
    now_ny = datetime.now(TZ).replace(microsecond=0)
    start_ny = now_ny - timedelta(days=n_days)
    start_ny = start_ny.replace(second=0)
    start_utc = start_ny.astimezone(timezone.utc).isoformat()
    end_utc = now_ny.astimezone(timezone.utc).isoformat()
    label = f"Últimos {n_days} días"
    return start_utc, end_utc, label

def month_bounds_epoch_ny():
    now_ny = datetime.now(TZ)
    start_ny = now_ny.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_ny.month == 12:
        next_month_ny = start_ny.replace(year=start_ny.year + 1, month=1)
    else:
        next_month_ny = start_ny.replace(month=start_ny.month + 1)
    start_epoch = int(start_ny.timestamp())
    end_epoch = int(next_month_ny.timestamp())
    label = f"Mes actual ({start_ny.strftime('%Y-%m')})"
    return start_epoch, end_epoch, label

def last_n_days_bounds_epoch_ny(n_days: int):
    now_ny = datetime.now(TZ).replace(microsecond=0)
    start_ny = now_ny - timedelta(days=n_days)
    start_epoch = int(start_ny.timestamp())
    end_epoch = int(now_ny.timestamp())
    label = f"Últimos {n_days} días"
    return start_epoch, end_epoch, label

# ======== CONSULTAS DE TOTALES ========
def get_total_for_category_in_range(user, category_id, start_epoch, end_epoch):
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        SELECT COALESCE(SUM(amount), 0.0)
        FROM expenses
        WHERE user = ?
          AND category_id = ?
          AND ts_epoch >= ?
          AND ts_epoch < ?
    """, (user, int(category_id), int(start_epoch), int(end_epoch)))
    total = c.fetchone()[0] or 0.0
    conn.close()
    return float(total)

def get_totals_all_categories_in_range(user, start_epoch, end_epoch):
    conn = db_connect()
    c = conn.cursor()
    totals = {}
    for cat_id in CATEGORIES.keys():
        c.execute("""
            SELECT COALESCE(SUM(amount), 0.0)
            FROM expenses
            WHERE user = ?
              AND category_id = ?
              AND ts_epoch >= ?
              AND ts_epoch < ?
        """, (user, int(cat_id), int(start_epoch), int(end_epoch)))
        totals[cat_id] = float(c.fetchone()[0] or 0.0)
    conn.close()
    return totals

def format_totals_table(totals_dict):
    lines = ["Categoría            Total (USD)"]
    lines.append("------------------- ----------")
    grand_total = 0.0
    for cat_id in sorted(CATEGORIES.keys(), key=lambda x: int(x)):
        name = CATEGORIES[cat_id]
        total = totals_dict.get(cat_id, 0.0)
        grand_total += total
        lines.append(f"{cat_id}. {name[:18]:18} ${total:10.2f}")
    lines.append("------------------- ----------")
    lines.append(f"TOTAL GENERAL        ${grand_total:10.2f}")
    return "\n".join(lines), grand_total

def get_month_total_for_category(user, category_id):
    start_e, end_e, _ = month_bounds_epoch_ny()
    return get_total_for_category_in_range(user, category_id, start_e, end_e)

# ======== UTILS ========
def normalize_amount(text):
    cleaned = text.replace(",", ".")
    m = re.search(r"[-+]?\d*\.?\d+", cleaned)
    if not m:
        return None
    try:
        return round(float(m.group()), 2)
    except ValueError:
        return None

# ======== GOOGLE SHEETS: enviar fila vía Apps Script ========
def append_expense_to_google_sheet(user, amount, category_id, category_name):
    """
    POST a tu Apps Script Web App (que escribe una fila en tu Google Sheet).
    """
    if not GOOGLE_APPS_SCRIPT_URL:
        print("GOOGLE_APPS_SCRIPT_URL not set; skipping Sheets append.")
        return False
    try:
        payload = {
            "user": user,
            "amount_usd": float(amount),
            "category_id": int(category_id),
            "category_name": category_name,
            "timestamp_iso": datetime.now(timezone.utc).isoformat()
        }
        headers = {"Content-Type": "application/json"}
        # (Opcional) shared key via header; or append ?key=... in URL
        if GOOGLE_APPS_SCRIPT_KEY:
            headers["X-AppsScript-Key"] = GOOGLE_APPS_SCRIPT_KEY
        r = requests.post(GOOGLE_APPS_SCRIPT_URL, headers=headers, json=payload, timeout=15)
        if r.status_code >= 300:
            print("Google Sheets append error:", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        print("Google Sheets append exception:", e)
        return False

# ======== WEBHOOK VERIFY (GET) ========
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403

# ======== WEBHOOK MENSAJES (POST) ========
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True, force=True) or {}
    entries = data.get("entry", [])
    for entry in entries:
        user, text, reply_id = parse_sender_and_message(entry)
        if not user:
            continue

        sess = get_session(user)
        state = sess["state"]
        lowered = (text or "").strip().lower()

        # ---------- RESET ----------
        if lowered in {"reset", "reiniciar", "cancel", "cancelar"}:
            reset_session(user)
            send_whatsapp_text(
                user,
                "🔄 Sesión reiniciada.\n"
                "Puedes empezar de nuevo escribiendo: *ingresar gasto*.\n\n"
                "Comandos útiles:\n"
                "- *resumen mes*\n"
                "- *resumen 7* | *resumen 15* | *resumen 30*\n"
                "- *resumen <cat>* o *resumen <cat> 7|15|30|mes*"
            )
            continue

        # ---------- ESTADO (diagnóstico) ----------
        if lowered == "estado":
            sess_dbg = get_session(user)
            send_whatsapp_text(
                user,
                f"🧭 Estado actual: {sess_dbg['state']}\n"
                f"Monto en memoria: {sess_dbg['amount'] if sess_dbg['amount'] is not None else '—'}"
            )
            continue

        # ---------- RESUMEN ----------
        if lowered.startswith("resumen"):
            parts = lowered.split()
            category = None
            days = None
            use_month = False

            # remove the command word
            parts = parts[1:]

            if len(parts) == 0:
                use_month = True

            elif len(parts) == 1:
                # Could be category (1–8), timeframe (7/15/30), or "mes"
                p1 = parts[0]
                if p1 in {"mes"}:
                    use_month = True
                elif p1 in {"7", "15", "30"}:
                    days = int(p1)
                elif p1 in CATEGORIES:
                    category = p1
                    use_month = True
                else:
                    use_month = True

            elif len(parts) >= 2:
                p1, p2 = parts[0], parts[1]
                if p1 in CATEGORIES:
                    category = p1
                    if p2 in {"mes"}:
                        use_month = True
                    elif p2 in {"7", "15", "30"}:
                        days = int(p2)
                    else:
                        use_month = True
                elif p1 in {"7", "15", "30"}:
                    days = int(p1)
                elif p1 in {"mes"}:
                    use_month = True
                else:
                    use_month = True

            # Select time window (epoch)
            if use_month:
                start_e, end_e, label = month_bounds_epoch_ny()
            elif days:
                start_e, end_e, label = last_n_days_bounds_epoch_ny(days)
            else:
                start_e, end_e, label = month_bounds_epoch_ny()

            # Build response
            if category:
                total = get_total_for_category_in_range(user, category, start_e, end_e)
                cat_name = CATEGORIES[category]
                msg = (
                    f"📊 Resumen de *{cat_name}* ({label}):\n"
                    f"Total: ${total:.2f}\n\n"
                    "Puedes usar:\n"
                    "- resumen 7 | 15 | 30\n"
                    "- resumen mes\n"
                    "- resumen <cat>\n"
                    "- resumen <cat> 7 | 15 | 30 | mes"
                )
                send_whatsapp_text(user, msg)
            else:
                totals = get_totals_all_categories_in_range(user, start_e, end_e)
                table, grand_total = format_totals_table(totals)
                msg = (
                    f"📊 Resumen ({label}):\n\n"
                    f"{table}\n\n"
                    f"Total general: ${grand_total:.2f}\n\n"
                    "Usa:\n"
                    "- resumen 7 | 15 | 30\n"
                    "- resumen mes\n"
                    "- resumen <cat>\n"
                    "- resumen <cat> 7 | 15 | 30 | mes"
                )
                send_whatsapp_text(user, msg)
            continue

        # ---------- INICIAR FLUJO DE GASTO ----------
        if lowered in TRIGGERS:
            set_session(user, "awaiting_amount", None)
            send_whatsapp_text(user, ask_for_amount())
            continue

        # ---------- ESPERANDO MONTO ----------
        if state == "awaiting_amount":
            amount = normalize_amount(text or "")
            if amount is None or amount <= 0:
                send_whatsapp_text(user, "El valor no parece válido. Intenta de nuevo (ej: 12.75).")
                continue
            set_session(user, "awaiting_category", amount)
            send_whatsapp_text(user, f"Perfecto. Monto registrado: ${amount:.2f}.")
            ok = send_whatsapp_category_list(user)
            if not ok:
                # Fallback: plain text menu
                send_whatsapp_text(
                    user,
                    "No pude enviar la lista interactiva.\n"
                    "Escribe un número del 1 al 8:\n"
                    "1. Renta\n2. Credit card bill\n3. Medical bill\n4. Utility bill\n"
                    "5. Car payment\n6. Restaurante\n7. Groceries & housekeeping\n8. Traveling"
                )
            continue

        # ---------- ESPERANDO CATEGORÍA ----------
        if state == "awaiting_category":
            chosen = None
            if reply_id and reply_id in CATEGORIES:
                chosen = reply_id
            elif lowered in CATEGORIES:
                chosen = lowered

            if chosen:
                # leer amount guardado
                sess2 = get_session(user)
                amount = float(sess2["amount"] or 0.0)
                category_id = chosen
                category_name = CATEGORIES[category_id]

                # Guarda en SQLite
                save_expense(user, amount, category_id, category_name)

                # Enviar a Google Sheets (Apps Script)
                ok_sheet = append_expense_to_google_sheet(
                    user=user,
                    amount=amount,
                    category_id=category_id,
                    category_name=category_name
                )
                if not ok_sheet:
                    print("Aviso: no se pudo escribir en Google Sheets (Apps Script).")

                # Total del mes en esa categoría
                month_start_e, month_end_e, _ = month_bounds_epoch_ny()
                month_total = get_total_for_category_in_range(user, category_id, month_start_e, month_end_e)

                msg = (
                    "✅ Gasto guardado:\n"
                    f"- Monto: ${amount:.2f}\n"
                    f"- Categoría: {category_id}. {category_name}\n\n"
                    f"📊 Total del mes en *{category_name}*: ${month_total:.2f}\n\n"
                    "Comandos útiles:\n"
                    "- *resumen mes*\n"
                    "- *resumen 7* | *resumen 15* | *resumen 30*\n"
                    "- *resumen <cat>* o *resumen <cat> 7|15|30|mes*\n"
                    "Escribe *ingresar gasto* para capturar otro."
                )
                send_whatsapp_text(user, msg)
                reset_session(user)
                continue
            else:
                send_whatsapp_text(
                    user,
                    "Respuesta inválida. Elige una opción de la lista o escribe un número del 1 al 8."
                )
                send_whatsapp_category_list(user)
                continue

        # ---------- IDLE / AYUDA ----------
        send_whatsapp_text(
            user,
            "Hola 👋\n"
            "Para registrar un gasto, escribe: *ingresar gasto*.\n\n"
            "Para ver totales, escribe: *resumen*, *resumen mes*, *resumen 30*, "
            "*resumen 7*, *resumen 15*, o *resumen <cat>* (1–8).\n"
            "Comandos: *reset*, *estado*"
        )

    return jsonify(status="ok"), 200

# ======== RUN ========
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
