import sqlite3
import logging
from typing import Optional
import httpx
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

from backend.database import save_lead, SQLITE_DB_PATH, check_slot_available, create_appointment
from backend.config import settings

logger = logging.getLogger(__name__)

async def send_whatsapp_alert(thread_id: str, reason: str, caller_info: str = ""):
    """
    Sends a WhatsApp notification via Twilio REST API when a lead requests human follow-up.
    Uses Account SID + Auth Token basic auth (not API key pair).
    """
    if not settings.ENABLE_WHATSAPP_ALERTS:
        logger.info("WhatsApp alerts disabled — set ENABLE_WHATSAPP_ALERTS=True to enable.")
        return

    account_sid = settings.TWILIO_ACCOUNT_SID
    # Use Auth Token if available, otherwise fall back to API Key Secret
    auth_token = settings.TWILIO_AUTH_TOKEN or settings.TWILIO_API_KEY_SECRET

    if not account_sid or not auth_token:
        logger.warning("Twilio credentials missing (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN). Cannot send WhatsApp alert.")
        return

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

    from_wa = settings.TWILIO_WHATSAPP_FROM or "whatsapp:+14155238886"
    to_wa = settings.TWILIO_WHATSAPP_TO
    if not to_wa:
        logger.warning("TWILIO_WHATSAPP_TO not set. Cannot send WhatsApp alert.")
        return

    # Ensure whatsapp: prefix
    if not from_wa.startswith("whatsapp:"):
        from_wa = f"whatsapp:{from_wa}"
    if not to_wa.startswith("whatsapp:"):
        to_wa = f"whatsapp:{to_wa}"

    body = (
        f"🔔 *Alpha — Lead Follow-Up Request*\n\n"
        f"Thread: `{thread_id}`\n"
        f"Reason: {reason}\n"
        f"{('Caller Info: ' + caller_info) if caller_info else ''}\n\n"
        f"👉 Open the console to review: https://salesagent-b6po.onrender.com"
    )

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.post(
                url,
                data={"From": from_wa, "To": to_wa, "Body": body},
                auth=(account_sid, auth_token)
            )
            if response.status_code == 201:
                logger.info(f"✅ WhatsApp alert sent for thread {thread_id}")
            else:
                logger.error(f"❌ Twilio returned {response.status_code}: {response.text}")
        except Exception as e:
            logger.error(f"❌ WhatsApp alert failed: {e}", exc_info=True)



@tool
async def search_crm(company: str) -> str:
    """
    Search the CRM for an existing lead profile or company info.
    Returns details of the company if found, otherwise returns a message indicating no record exists.
    """
    from backend.database import get_db
    db = get_db()
    lead = await db.leads.find_one({"company": {"$regex": company, "$options": "i"}})
    if lead:
        return f"Found CRM Record: Company={lead.get('company')}, Status={lead.get('status')}, Fit={lead.get('fit')}"
    return f"No existing CRM record found for company: {company}"

@tool
async def update_lead_status(
    company: str,
    job_title: str,
    intent_score: int,
    status: str,
    fit: bool,
    config: RunnableConfig
) -> str:
    """
    Update the lead status and firmographics in the CRM.
    Used to qualify or disqualify leads based on B2B fit.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")
    lead_data = {
        "company": company,
        "job_title": job_title,
        "intent_score": intent_score,
        "status": status,
        "fit": fit
    }
    await save_lead(thread_id, lead_data)
    return f"Lead status updated in CRM: Company={company}, Status={status}, Fit={fit}"

@tool
async def schedule_demo(
    meeting_time: str,
    company: str,
    config: RunnableConfig
) -> str:
    """
    Schedules a demo or discovery call with the lead.
    Pass in the requested meeting_time and the company name.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")
    from backend.database import get_db
    db = get_db()
    booking = {
        "thread_id": thread_id,
        "company": company,
        "meeting_time": meeting_time,
        "status": "Scheduled"
    }
    await db.meetings.insert_one(booking)
    await db.leads.update_one(
        {"thread_id": thread_id},
        {"$set": {"status": "Demo Scheduled"}},
        upsert=True
    )
    return f"Demo scheduled successfully for {company} at {meeting_time}."

@tool
async def query_pos_database(
    product_query: Optional[str] = None,
    order_id: Optional[int] = None,
    customer_email: Optional[str] = None,
    customer_phone: Optional[str] = None
) -> str:
    """
    Query the local read-only POS/Inventory database.
    Use this to check product pricing/stock or check order status for a customer.
    
    To check order status, you MUST provide the order_id along with either customer_email or customer_phone to authenticate ownership and secure private data.
    """
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    
    try:
        if order_id is not None:
            if not customer_email and not customer_phone:
                return "Error: You must provide the customer's email or phone number to verify ownership and query order details."
            
            cursor.execute("SELECT id, customer_email, customer_phone, status, total_price, items FROM orders WHERE id = ?", (order_id,))
            row = cursor.fetchone()
            if not row:
                return f"No order found with ID: {order_id}"
                
            db_id, db_email, db_phone, db_status, db_total, db_items = row
            
            email_match = customer_email and customer_email.lower().strip() == db_email.lower().strip()
            phone_match = customer_phone and customer_phone.strip() == (db_phone or "").strip()
            
            if not email_match and not phone_match:
                return "Security Error: Customer verification failed. The provided email or phone does not match this order."
                
            return f"Order #{db_id} Details: Status={db_status}, Items={db_items}, Total={db_total}."
            
        elif product_query is not None:
            # Generic terms like "products", "services", "all", "list" → return full catalog
            generic_terms = {"product", "products", "service", "services", "all", "list", "everything", "what do you have", ""}
            is_generic = product_query.strip().lower() in generic_terms

            if is_generic:
                cursor.execute("SELECT name, price, stock_quantity, description FROM products")
                rows = cursor.fetchall()
            else:
                cursor.execute(
                    "SELECT name, price, stock_quantity, description FROM products WHERE LOWER(name) LIKE LOWER(?)",
                    (f"%{product_query}%",)
                )
                rows = cursor.fetchall()
                if not rows:
                    # Fallback: return all products so agent can answer anyway
                    cursor.execute("SELECT name, price, stock_quantity, description FROM products")
                    rows = cursor.fetchall()

            if not rows:
                return "No products found in the database."

            results = [f"- {r[0]}: Price={r[1]}, In Stock={r[2]} ({r[3]})" for r in rows]
            return "Product Catalog:\n" + "\n".join(results)
            
        else:
            cursor.execute("SELECT name, price, stock_quantity, description FROM products")
            rows = cursor.fetchall()
            results = [f"- {r[0]}: Price={r[1]}, In Stock={r[2]} ({r[3]})" for r in rows]
            return "Available SaaS Packages:\n" + "\n".join(results)
            
    except Exception as e:
        return f"Database query failed: {str(e)}"
    finally:
        conn.close()

@tool
async def handoff_to_human(
    reason: str,
    caller_name: str,
    caller_phone: str,
    config: RunnableConfig
) -> str:
    """
    Logs the caller's details and notifies a human representative to follow up.
    REQUIRED: Collect caller_name and caller_phone BEFORE calling this tool.
    Use ONLY when:
    1. The user explicitly asks to speak with or be contacted by a human.
    2. You genuinely don't know the answer and they want further help.
    Do NOT use this to reject or disqualify anyone.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")
    from backend.database import get_db
    db = get_db()

    # Save caller details + mark as follow-up requested
    await db.leads.update_one(
        {"thread_id": thread_id},
        {"$set": {
            "status": "Follow-up Requested",
            "handoff_reason": reason,
            "name": caller_name,
            "phone": caller_phone
        }},
        upsert=True
    )

    caller_info = f"Name: {caller_name} | Phone: {caller_phone}"
    logger.info(f"Human follow-up for thread {thread_id}: {caller_info} — {reason}")

    # Fire WhatsApp notification to the operator
    await send_whatsapp_alert(thread_id, reason, caller_info)

    return "Perfect, I've passed your details to our team. A representative will reach out to you within a few minutes. Is there anything else I can help you with?"


@tool
async def book_appointment(
    name: str,
    email: str,
    phone: str,
    date: str,
    time: str,
    notes: str,
    config: RunnableConfig
) -> str:
    """
    Books a meeting or consultation appointment.
    Collects the caller's name, email, phone number, preferred date (e.g. 'June 30 2026'),
    and preferred time (e.g. '2:00 PM'). Checks if the slot is available and confirms booking.
    Always collect ALL fields before calling this tool.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")
    
    # Validate required fields
    missing = []
    if not name or name.strip() == "":
        missing.append("name")
    if not email or "@" not in email:
        missing.append("email")
    if not phone or phone.strip() == "":
        missing.append("phone number")
    if not date or date.strip() == "":
        missing.append("preferred date")
    if not time or time.strip() == "":
        missing.append("preferred time")
    
    if missing:
        return f"I still need the following details to complete your booking: {', '.join(missing)}. Could you please provide those?"
    
    # Check availability
    available = await check_slot_available(date.strip(), time.strip())
    if not available:
        return f"Unfortunately, {date} at {time} is already taken. Could you suggest another date or time that works for you?"
    
    # Confirm and create booking
    appt = await create_appointment(
        thread_id=thread_id,
        name=name.strip(),
        email=email.strip(),
        phone=phone.strip(),
        date_str=date.strip(),
        time_str=time.strip(),
        notes=notes or ""
    )
    
    return f"You're all set, {name}! Your appointment is confirmed for {date} at {time}. We'll send a confirmation to {email} shortly."

