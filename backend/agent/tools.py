import sqlite3
import logging
from typing import Optional
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

from backend.database import save_lead, SQLITE_DB_PATH, check_slot_available, create_appointment
from backend.config import settings

logger = logging.getLogger(__name__)

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
            cursor.execute("SELECT name, price, stock_quantity, description FROM products WHERE name LIKE ?", (f"%{product_query}%",))
            rows = cursor.fetchall()
            if not rows:
                return f"No products matching query '{product_query}' were found in the database."
            
            results = [f"- {r[0]}: Price={r[1]}, In Stock={r[2]} ({r[3]})" for r in rows]
            return "Inventory Query Results:\n" + "\n".join(results)
            
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
    config: RunnableConfig
) -> str:
    """
    Logs the caller's request for human follow-up and reassures them a representative will reach out.
    Use ONLY when:
    1. The user explicitly asks to speak with a human.
    2. You genuinely don't know the answer and the user wants further help.
    Do NOT use this to reject or disqualify any lead.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")
    from backend.database import get_db
    db = get_db()
    await db.leads.update_one(
        {"thread_id": thread_id},
        {"$set": {"status": "Follow-up Requested", "handoff_reason": reason}},
        upsert=True
    )
    logger.info(f"Human follow-up requested for thread {thread_id}: {reason}")
    return "I've noted your details and a representative will reach out to you within a couple of minutes. Is there anything else I can help you with in the meantime?"


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

