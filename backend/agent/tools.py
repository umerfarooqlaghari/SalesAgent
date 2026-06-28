import sqlite3
import logging
from typing import Optional
import httpx
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

from backend.database import (
    save_lead,
    SQLITE_DB_PATH,
    check_slot_available,
    create_appointment,
    _lookup_product,
    _create_sqlite_order,
    create_order,
    find_active_appointments,
    cancel_appointment_record,
    reschedule_appointment_record,
    find_active_orders,
    cancel_order_record,
    link_voice_call,
    get_linked_console_thread,
    unlink_voice_call,
    get_recent_typed_chat_messages,
)
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


@tool
async def place_order(
    product_name: str,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
    config: RunnableConfig
) -> str:
    """
    Place a customer order for a product, package, or service.
    Use when the caller says they want to buy, purchase, or take a package/product/service.
    Collect customer_name, customer_email, and customer_phone before calling if not already known.
    product_name should match what they agreed to (e.g. 'SaaS Professional', 'Starter package').
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")

    missing = []
    if not product_name or product_name.strip() == "":
        missing.append("which product or package they want")
    if not customer_name or customer_name.strip() == "":
        missing.append("their full name")
    if not customer_email or "@" not in customer_email:
        missing.append("their email address")
    if not customer_phone or customer_phone.strip() == "":
        missing.append("their phone number")

    if missing:
        return (
            f"I'd love to take your order! I just need a couple more details: {', '.join(missing)}. "
            "Could you share those with me?"
        )

    product = _lookup_product(product_name.strip())
    if not product:
        return (
            f"I couldn't find a product matching '{product_name}'. "
            "We offer SaaS Starter ($49/mo), SaaS Professional ($199/mo), and SaaS Enterprise ($999/mo). "
            "Which one would you like to order?"
        )

    if product["stock_quantity"] <= 0:
        return f"Sorry, {product['name']} is currently out of stock. Would you like to hear about our other packages?"

    sqlite_order_id = _create_sqlite_order(
        customer_email=customer_email.strip(),
        customer_phone=customer_phone.strip(),
        product_name=product["name"],
        total_price=product["price"],
    )

    await create_order(
        thread_id=thread_id,
        customer_name=customer_name.strip(),
        customer_email=customer_email.strip(),
        customer_phone=customer_phone.strip(),
        product_name=product["name"],
        total_price=product["price"],
        sqlite_order_id=sqlite_order_id,
    )

    await save_lead(thread_id, {
        "company": customer_name.strip(),
        "status": "Order Placed",
        "intent_score": 10,
        "fit": True,
    })

    return (
        f"Perfect! I've taken your order for the {product['name']} at {product['price']}. "
        f"Your order number is {sqlite_order_id}. "
        "A sales agent will contact you shortly to finalize the details and next steps. "
        "Is there anything else I can help you with today?"
    )


@tool
async def lookup_appointments(
    email: str,
    phone: str,
    config: RunnableConfig,
) -> str:
    """
    Look up a caller's upcoming appointments.
    Use when they ask about their booking, meeting time, or before cancelling/rescheduling.
    Requires email or phone to verify identity.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")

    if (not email or "@" not in email) and (not phone or phone.strip() == ""):
        return "I can look that up for you — could you share the email or phone number you used when booking?"

    appts = await find_active_appointments(
        email=email.strip() if email else None,
        phone=phone.strip() if phone else None,
        thread_id=thread_id,
    )

    if not appts:
        return "I don't see any upcoming appointments under that email or phone. Would you like to book a new one?"

    lines = [
        f"- {a.get('name', 'Guest')}: {a.get('date')} at {a.get('time')} (status: {a.get('status', 'confirmed')})"
        for a in appts
    ]
    return "Here are your upcoming appointments:\n" + "\n".join(lines)


@tool
async def cancel_appointment(
    email: str,
    phone: str,
    date: str,
    time: str,
    config: RunnableConfig,
) -> str:
    """
    Cancel an existing appointment/meeting.
    Use when the caller wants to cancel their booking.
    Collect email or phone to verify identity. If they have multiple bookings, also ask for date and time.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")

    if (not email or "@" not in email) and (not phone or phone.strip() == ""):
        return "I can cancel that for you — what email or phone number did you use when you booked?"

    appts = await find_active_appointments(
        email=email.strip() if email else None,
        phone=phone.strip() if phone else None,
        thread_id=thread_id,
        date_str=date.strip() if date else None,
        time_str=time.strip() if time else None,
    )

    if not appts:
        return "I couldn't find an active appointment matching those details. Would you like me to look up your bookings first?"

    if len(appts) > 1 and (not date or not time):
        summary = "; ".join(f"{a.get('date')} at {a.get('time')}" for a in appts)
        return (
            f"You have multiple upcoming appointments ({summary}). "
            "Which date and time would you like to cancel?"
        )

    target = appts[0]
    cancelled = await cancel_appointment_record(target["_id"])
    if not cancelled:
        return "That appointment may already be cancelled. Can I help with anything else?"

    return (
        f"Done — your appointment on {target.get('date')} at {target.get('time')} has been cancelled. "
        "Would you like to reschedule for another time, or is there anything else I can help with?"
    )


@tool
async def reschedule_appointment(
    email: str,
    phone: str,
    new_date: str,
    new_time: str,
    current_date: str,
    current_time: str,
    config: RunnableConfig,
) -> str:
    """
    Reschedule an existing appointment to a new date and time.
    Use when the caller wants to change or move their meeting.
    Collect email or phone for verification. If multiple bookings exist, ask which one (current_date/current_time).
    Then collect the new preferred date and time before calling this tool.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")

    missing = []
    if (not email or "@" not in email) and (not phone or phone.strip() == ""):
        missing.append("email or phone used for the booking")
    if not new_date or new_date.strip() == "":
        missing.append("new preferred date")
    if not new_time or new_time.strip() == "":
        missing.append("new preferred time")

    if missing:
        return f"Happy to reschedule — I just need your {', '.join(missing)}."

    appts = await find_active_appointments(
        email=email.strip() if email else None,
        phone=phone.strip() if phone else None,
        thread_id=thread_id,
        date_str=current_date.strip() if current_date else None,
        time_str=current_time.strip() if current_time else None,
    )

    if not appts:
        return "I couldn't find an active appointment to reschedule. Would you like to book a new one instead?"

    if len(appts) > 1 and (not current_date or not current_time):
        summary = "; ".join(f"{a.get('date')} at {a.get('time')}" for a in appts)
        return (
            f"You have multiple appointments ({summary}). "
            "Which one would you like to move — please tell me the current date and time."
        )

    target = appts[0]
    new_date = new_date.strip()
    new_time = new_time.strip()

    if target.get("date") == new_date and target.get("time") == new_time:
        return f"Your appointment is already scheduled for {new_date} at {new_time}. Anything else I can help with?"

    available = await check_slot_available(new_date, new_time)
    if not available:
        return f"{new_date} at {new_time} is already taken. Could you suggest another date or time?"

    updated = await reschedule_appointment_record(target["_id"], new_date, new_time)
    if not updated:
        return "I wasn't able to update that appointment. Would you like me to try again or connect you with a team member?"

    return (
        f"All set! I've moved your appointment to {new_date} at {new_time}. "
        "You'll receive an updated confirmation shortly. Anything else I can help with?"
    )


@tool
async def cancel_order(
    order_id: int,
    email: str,
    phone: str,
    config: RunnableConfig,
) -> str:
    """
    Cancel a customer order.
    Use when the caller wants to cancel a purchase they placed.
    Requires order_id plus email or phone to verify ownership.
    """
    try:
        oid = int(order_id) if order_id is not None else 0
    except (TypeError, ValueError):
        oid = 0

    if not oid:
        return "I can cancel that order — do you have your order number? It was shared when you placed the order."

    if (not email or "@" not in email) and (not phone or phone.strip() == ""):
        return "To cancel your order, I'll need the email or phone number you used when ordering."

    orders = await find_active_orders(
        order_id=oid,
        email=email.strip() if email else None,
        phone=phone.strip() if phone else None,
    )

    if not orders:
        from backend.database import get_db
        db = get_db()
        any_order = await db.orders.find_one({"order_id": oid})
        if any_order and any_order.get("status") == "cancelled":
            return f"Order #{oid} is already cancelled. Is there anything else I can help with?"
        return (
            f"I couldn't find order #{oid} matching that email or phone. "
            "Could you double-check the order number and contact details?"
        )

    target = orders[0]
    if target.get("status") == "cancelled":
        return f"Order #{oid} is already cancelled. Can I help with anything else?"

    cancelled = await cancel_order_record(oid)
    if not cancelled:
        return "I wasn't able to cancel that order right now. Would you like me to connect you with a team member?"

    product = target.get("product_name", "your order")
    return (
        f"Your order #{oid} for {product} has been cancelled. "
        "A team member won't charge you for this order. Is there anything else I can help with today?"
    )


@tool
async def get_typed_chat_details(config: RunnableConfig) -> str:
    """
    Read contact details the caller typed in the chat box (name, email, phone, etc.).
    Use AFTER asking the caller to type information in the chat for accuracy — especially email and phone.
    Prefer typed chat values over spoken dictation when both exist.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")
    typed = await get_recent_typed_chat_messages(thread_id, limit=8)

    if not typed:
        return (
            "No typed messages found in the chat yet. "
            "Ask the caller to type their detail in the chat box, or accept dictation and read it back to confirm."
        )

    lines = "\n".join(f"- {msg}" for msg in typed)
    return (
        "Recent typed chat messages (prefer these for email/phone/name — more accurate than speech):\n"
        f"{lines}"
    )

