import re
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
    update_appointment_fields,
    find_active_orders,
    cancel_order_record,
    next_order_id,
    link_voice_call,
    get_linked_console_thread,
    unlink_voice_call,
    get_recent_typed_chat_messages,
    get_conversation,
)
from backend.adapters.factory import AdapterFactory
from backend.config import settings
from backend.tenant.thread_scope import logical_thread_id

logger = logging.getLogger(__name__)


def _tenant_id(config: Optional[RunnableConfig] = None) -> str:
    return (config or {}).get("configurable", {}).get("tenant_id") or settings.DEFAULT_TENANT_ID


async def _load_tenant_context(config: RunnableConfig):
    from backend.tenant.context import IntegrationConfigs, TenantContext, TenantSettings
    from backend.tenant.registry import get_tenant_by_id

    tid = _tenant_id(config)
    ctx = await get_tenant_by_id(tid)
    if ctx:
        return ctx
    return TenantContext(
        tenant_id=tid,
        org_name=tid,
        settings=TenantSettings(),
        integrations=IntegrationConfigs(),
    )

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
async def search_crm(company: str, config: RunnableConfig) -> str:
    """
    Search the CRM for an existing lead profile or company info.
    Returns details of the company if found, otherwise returns a message indicating no record exists.
    """
    tenant = await _load_tenant_context(config)
    from backend.integrations.service import normalize_integrations

    integrations = normalize_integrations(tenant.integrations_raw)
    crm_provider = (integrations.get("crm") or {}).get("provider", "internal").lower()
    if crm_provider not in ("none", "", "internal"):
        crm = AdapterFactory.crm(tenant)
        return await crm.search_company(company)

    from backend.database import get_db

    db = get_db()
    # V14: `company` is transcribed caller speech. Unescaped, "." matches any lead
    # and "(a+)+$" is a ReDoS against the Mongo server.
    safe_company = re.escape((company or "").strip()[:64])
    if not safe_company:
        return "I didn't catch the company name — could you say it again?"
    lead = await db.leads.find_one(
        {"tenant_id": tenant.tenant_id, "company": {"$regex": safe_company, "$options": "i"}}
    )
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
    thread_id = logical_thread_id(config)
    lead_data = {
        "company": company,
        "job_title": job_title,
        "intent_score": intent_score,
        "status": status,
        "fit": fit
    }
    await save_lead(_tenant_id(config), thread_id, lead_data)
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
    thread_id = logical_thread_id(config)
    tenant_id = _tenant_id(config)
    from backend.database import get_db
    db = get_db()
    booking = {
        "tenant_id": tenant_id,
        "thread_id": thread_id,
        "company": company,
        "meeting_time": meeting_time,
        "status": "Scheduled"
    }
    await db.meetings.insert_one(booking)
    await db.leads.update_one(
        {"tenant_id": tenant_id, "thread_id": thread_id},
        {"$set": {"status": "Demo Scheduled"}},
        upsert=True
    )
    return f"Demo scheduled successfully for {company} at {meeting_time}."

@tool
async def query_pos_database(
    product_query: Optional[str] = None,
    order_id: Optional[int] = None,
    customer_email: Optional[str] = None,
    customer_phone: Optional[str] = None,
    config: RunnableConfig = None,
) -> str:
    """
    Look up records in this company's connected database tables.

    The tables differ per company: they may hold products, services, treatments,
    staff, films, courses, properties, blog posts, FAQs or anything else. The
    available category names are listed in the CONNECTED DATA / TENANT DATA MODEL
    section of your instructions — pass one of those names, or the name of a
    specific item, as `product_query`.

    Use this whenever the caller asks what the company offers, has, or does, or
    for detail on a named item. For order status, provide order_id plus
    customer_email or customer_phone.
    """
    tenant = await _load_tenant_context(config or {})
    tenant_id = tenant.tenant_id

    q_low = (product_query or "").lower()
    is_pagination_query = any(w in q_low for w in ("more", "other", "additional", "next", "else", "further"))

    cache_key = f"q:{product_query or ''}_ord:{order_id or ''}_em:{customer_email or ''}_ph:{customer_phone or ''}"
    from backend.integrations.query_cache import get_query_cache, set_query_cache

    if not is_pagination_query:
        cached_res = await get_query_cache(tenant_id, cache_key)
        if cached_res is not None:
            logger.info("Serving query_pos_database from cache for tenant %s (%s)", tenant_id, cache_key)
            return cached_res

    try:
        # Defence in depth: building the adapter resolves stored secrets, which
        # can raise. Outside this try, any such failure escaped the tool and the
        # graph turned it into the generic "Sorry, I hit a small snag."
        pos = AdapterFactory.pos(tenant)

        if order_id is not None:
            res = await pos.get_order_status(
                int(order_id),
                customer_email=customer_email,
                customer_phone=customer_phone,
            )
        elif product_query is not None:
            res = await pos.list_products(product_query)
        else:
            res = await pos.list_products(None)

        if res and not str(res).startswith("Inventory query failed"):
            await set_query_cache(tenant_id, cache_key, res, ttl=900)

        return res
    except Exception as e:
        logger.error("query_pos_database failed for tenant %s: %s", tenant.tenant_id, e, exc_info=True)
        return f"Inventory query failed: {e}"

from datetime import datetime, timezone

def _normalize_appointment_date(raw_date: Optional[str]) -> str:
    if not raw_date or not raw_date.strip():
        return ""
    text = raw_date.strip()
    current_year = datetime.now(timezone.utc).year  # e.g., 2026

    # Forcefully replace any past year (e.g. 2020..2025) with current year (2026)
    match = re.search(r"\b(20\d\d)\b", text)
    if match:
        year_val = int(match.group(1))
        if year_val < current_year:
            text = re.sub(r"\b20[0-2][0-5]\b", str(current_year), text)
    else:
        # If no 4-digit year is present, append current year
        text = f"{text} {current_year}"

    return text


@tool
async def handoff_to_human(
    reason: str,
    caller_name: str,
    caller_phone: str,
    caller_email: str = "",
    config: RunnableConfig = None,
) -> str:
    """
    Logs the caller's details and notifies a human representative to follow up.
    REQUIRED: Collect caller_name, caller_phone, caller_email, AND the specific reason BEFORE calling this tool.
    Use ONLY when:
    1. The user explicitly asks to speak with or be contacted by a human.
    2. You genuinely don't know the answer and they want further help.
    Do NOT use this to reject or disqualify anyone.
    """
    import asyncio as _asyncio
    from backend.supervisors.email import send_supervisor_handoff_email

    thread_id = logical_thread_id(config)
    tenant_id = _tenant_id(config)

    norm_email = _normalize_email(caller_email)
    norm_phone = _normalize_phone(caller_phone)

    # Validate required contact fields before firing handoff
    missing = []
    if not caller_name or caller_name.strip().lower() in ("", "unknown", "caller", "user", "guest", "n/a", "none"):
        missing.append("full name")
    if not norm_phone or norm_phone.strip().lower() in ("", "unknown", "n/a", "none"):
        missing.append("phone number")
    if not norm_email or "@" not in norm_email:
        missing.append("email address")

    if missing:
        if len(missing) == 1:
            return f"I can connect you with a supervisor! I just need your {missing[0]} so our team can reach out. Could you please provide that?"
        items_str = ", ".join(missing[:-1]) + f" and {missing[-1]}"
        return f"I can connect you with a supervisor! I just need your {items_str} so our team can reach out. Could you please provide those?"

    # Validate that a specific reason was provided by caller
    generic_reasons = ("", "user requested human follow-up", "talk to human", "speak to supervisor", "human follow-up", "human", "supervisor", "unknown", "n/a", "none")
    if not reason or reason.strip().lower() in generic_reasons:
        return f"I'd be glad to connect you with a supervisor, {caller_name.strip()}! Could you please share the topic or reason for your request so I can route it to the right department?"

    await save_lead(tenant_id, thread_id, {
        "status": "Handoff Requested",
        "handoff_reason": reason or "User requested human follow-up",
        "name": caller_name.strip(),
        "phone": norm_phone,
        "email": norm_email,
    })

    caller_info = f"Name: {caller_name.strip()} | Phone: {norm_phone} | Email: {norm_email}"
    logger.info(f"Human follow-up for thread {thread_id}: {caller_info} — {reason}")

    # Fire WhatsApp alert and supervisor email concurrently
    await _asyncio.gather(
        send_whatsapp_alert(thread_id, reason or "Human follow-up", caller_info),
        send_supervisor_handoff_email(
            tenant_id=tenant_id,
            thread_id=thread_id,
            caller_name=caller_name.strip(),
            caller_phone=norm_phone,
            caller_email=norm_email,
            reason=reason or "User requested human follow-up",
        ),
        return_exceptions=True,  # don't let a notification failure crash the handoff
    )

    return f"Perfect, {caller_name.strip()}! I've passed your details to our team. A supervisor will reach out to you shortly at {norm_email}. Is there anything else I can help you with in the meantime?"



_WORD_DIGITS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "oh": "0",
}


def _normalize_email(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = raw.strip()
    # Handle spoken dictation transcriptions: "at" -> "@", "dot" / "the regional com" -> "."
    text = re.sub(r"\s+(at|@)\s+", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(at|@)\b", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(dot|\.)\s+", ".", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdot\b", ".", text, flags=re.IGNORECASE)
    # Common speech-to-text transcriptions for ".com"
    text = re.sub(r"\s+com\b", ".com", text, flags=re.IGNORECASE)
    text = text.replace(" ", "").lower()
    return text


def _normalize_phone(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = raw.strip().lower()
    for word, digit in _WORD_DIGITS.items():
        text = re.sub(rf"\b{word}\b", digit, text)
    digits = re.sub(r"[^\d+]", "", text)
    return digits if len(digits) >= 6 else raw.strip()


_PLACEHOLDER_TIMES = {
    "00:00", "00:00:00", "0:00", "12:00 am", "12:00am", "00:00am",
    "now", "right now", "today", "current time", "present",
    "tbd", "n/a", "none", "unknown", "pending", "unspecified",
    "default", "any time", "anytime", "asap"
}


def _is_placeholder_time(time_str: Optional[str]) -> bool:
    if not time_str or not time_str.strip():
        return True
    cleaned = time_str.strip().lower()
    return cleaned in _PLACEHOLDER_TIMES


_DATE_KEYWORDS = {
    "january", "jan", "february", "feb", "march", "mar", "april", "apr", "may",
    "june", "jun", "july", "jul", "august", "aug", "september", "sept", "sep",
    "october", "oct", "november", "nov", "december", "dec",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    "tomorrow", "today", "tonight", "next week", "this week", "weekend",
    "1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "10th",
    "11th", "12th", "13th", "14th", "15th", "16th", "17th", "18th", "19th", "20th",
    "21st", "22nd", "23rd", "24th", "25th", "26th", "27th", "28th", "29th", "30th", "31st",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
    "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
    "eighteenth", "nineteenth", "twentieth", "thirtieth"
}

_TIME_KEYWORDS = {
    "am", "pm", "a.m.", "p.m.", "o'clock", "oclock", "noon", "midnight",
    "morning", "afternoon", "evening", "night",
    "1pm", "2pm", "3pm", "4pm", "5pm", "6pm", "7pm", "8pm", "9pm", "10pm", "11pm", "12pm",
    "1am", "2am", "3am", "4am", "5am", "6am", "7am", "8am", "9am", "10am", "11am", "12am",
}


async def _has_user_provided_date_and_time(tenant_id: str, thread_id: str) -> tuple[bool, bool]:
    try:
        conv = await get_conversation(tenant_id, thread_id)
        if not conv or not conv.get("messages"):
            # No history to verify against — fail closed (assume NOT provided)
            # rather than waving the booking through unchecked.
            return False, False
        
        user_texts = [m.get("content", "") for m in conv.get("messages", []) if m.get("role") == "user"]
        full_text = " ".join(user_texts).lower()
        if not full_text.strip():
            return False, False
            
        has_date = any(re.search(rf"\b{kw}\b", full_text) for kw in _DATE_KEYWORDS) or bool(re.search(r"\b20\d\d\b", full_text)) or bool(re.search(r"\b\d{1,2}/\d{1,2}\b", full_text))
        has_time = any(re.search(rf"\b{kw}\b", full_text) for kw in _TIME_KEYWORDS) or bool(re.search(r"\b\d{1,2}:\d{2}\b", full_text))
        return has_date, has_time
    except Exception:
        # Same reasoning: an error checking history must not be treated as
        # confirmation that the caller spoke a date/time.
        return False, False


@tool
async def book_appointment(
    name: str,
    email: str,
    phone: str,
    date: str,
    time: str,
    notes: Optional[str] = "",
    config: RunnableConfig = None,
) -> str:
    """
    Books a meeting or consultation appointment.
    Collects the caller's name, email, phone number, preferred date, and preferred time as explicitly specified by the caller.
    Checks if the slot is available and confirms booking.
    Always collect ALL 5 fields explicitly from the caller before calling this tool.
    NEVER assume, guess, or default date or time to 'today', 'now', current time, or sample dates.
    CRITICAL: DO NOT invoke this tool if the caller has not explicitly stated BOTH preferred date AND preferred time in their messages.
    """
    thread_id = logical_thread_id(config)
    tenant_id = _tenant_id(config)
    
    norm_email = _normalize_email(email)
    norm_phone = _normalize_phone(phone)

    # Verify caller actually provided date and time in history
    user_has_date, user_has_time = await _has_user_provided_date_and_time(tenant_id, thread_id)

    # Validate required fields
    missing = []
    if not name or name.strip() == "":
        missing.append("name")
    if not norm_email or "@" not in norm_email:
        missing.append("email")
    if not norm_phone:
        missing.append("phone number")
    norm_date = _normalize_appointment_date(date)
    if not norm_date or not user_has_date:
        missing.append("preferred date")
    if not time or _is_placeholder_time(time) or not user_has_time:
        missing.append("preferred time")
    
    if missing:
        if len(missing) == 1:
            return f"I still need your {missing[0]} to complete your booking. Could you please provide that?"
        items_str = ", ".join(missing[:-1]) + f" and {missing[-1]}"
        return f"I still need your {items_str} to complete your booking. Could you please provide those?"
    
    # Check availability
    available = await check_slot_available(tenant_id, norm_date, time.strip())
    if not available:
        return f"Unfortunately, {norm_date} at {time.strip()} is already taken. Could you suggest another date or time that works for you?"
    
    # Confirm and create booking
    appt = await create_appointment(
        tenant_id=tenant_id,
        thread_id=thread_id,
        name=name.strip(),
        email=norm_email,
        phone=norm_phone,
        date_str=norm_date,
        time_str=time.strip(),
        notes=notes or ""
    )
    
    return f"You're all set, {name.strip()}! Your appointment is confirmed for {norm_date} at {time.strip()}. We'll send a confirmation to {norm_email} shortly."


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
    thread_id = logical_thread_id(config)
    tenant_id = _tenant_id(config)

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

    tenant_ctx = await _load_tenant_context(config)
    is_demo_tenant = tenant_id == settings.DEFAULT_TENANT_ID

    product = await AdapterFactory.pos(tenant_ctx).lookup_product(product_name.strip())
    # T05: the SQLite fallback is the shared demo catalog with no tenant column.
    # Only the demo tenant may fall back to it.
    if not product and is_demo_tenant:
        product = _lookup_product(product_name.strip())
    if not product:
        # T06: this used to recite Alpha's SaaS price list to every tenant's caller.
        return (
            f"I couldn't find anything matching '{product_name}' in our catalogue. "
            "Could you tell me the exact name, or describe what you're after?"
        )

    if product["stock_quantity"] <= 0:
        return f"Sorry, {product['name']} is currently out of stock. Would you like to hear about our other packages?"

    # T07: the SQLite orders table has no tenant_id column and a globally shared
    # INTEGER PRIMARY KEY sequence, so writing every tenant's customer email and
    # phone there both leaks PII and lets order ids collide across tenants.
    # MongoDB is the authoritative, tenant-scoped store; SQLite stays demo-only.
    if is_demo_tenant:
        order_id = _create_sqlite_order(
            customer_email=customer_email.strip(),
            customer_phone=customer_phone.strip(),
            product_name=product["name"],
            total_price=product["price"],
        )
    else:
        order_id = await next_order_id(tenant_id)

    await create_order(
        tenant_id=tenant_id,
        thread_id=thread_id,
        customer_name=customer_name.strip(),
        customer_email=customer_email.strip(),
        customer_phone=customer_phone.strip(),
        product_name=product["name"],
        total_price=product["price"],
        sqlite_order_id=order_id,
    )

    await save_lead(tenant_id, thread_id, {
        "company": customer_name.strip(),
        "status": "Order Placed",
        "intent_score": 10,
        "fit": True,
    })

    return (
        f"Perfect! I've taken your order for the {product['name']} at {product['price']}. "
        f"Your order number is {order_id}. "
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
    thread_id = logical_thread_id(config)
    tenant_id = _tenant_id(config)

    if (not email or "@" not in email) and (not phone or phone.strip() == ""):
        return "I can look that up for you — could you share the email or phone number you used when booking?"

    appts = await find_active_appointments(
        tenant_id,
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
    thread_id = logical_thread_id(config)
    tenant_id = _tenant_id(config)

    if (not email or "@" not in email) and (not phone or phone.strip() == ""):
        return "I can cancel that for you — what email or phone number did you use when you booked?"

    # Find active appointments by caller identity first
    appts = await find_active_appointments(
        tenant_id,
        email=email.strip() if email else None,
        phone=phone.strip() if phone else None,
        thread_id=thread_id,
    )

    if not appts:
        return "I couldn't find an active appointment under those details. Could you check your email or phone number?"

    if len(appts) > 1:
        # Filter down by date/time if provided
        if date and date.strip():
            matched = [a for a in appts if date.strip().lower() in a.get("date", "").lower()]
            if matched:
                appts = matched
        if len(appts) > 1 and time and not _is_placeholder_time(time):
            matched = [a for a in appts if time.strip().lower() in a.get("time", "").lower()]
            if matched:
                appts = matched

    if len(appts) > 1:
        summary = "; ".join(f"{a.get('date')} at {a.get('time')}" for a in appts)
        return (
            f"You have multiple upcoming appointments ({summary}). "
            "Which date and time would you like to cancel?"
        )

    target = appts[0]
    cancelled = await cancel_appointment_record(tenant_id, target["_id"])
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
    thread_id = logical_thread_id(config)
    tenant_id = _tenant_id(config)

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
        tenant_id,
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

    available = await check_slot_available(tenant_id, new_date, new_time)
    if not available:
        return f"{new_date} at {new_time} is already taken. Could you suggest another date or time?"

    updated = await reschedule_appointment_record(tenant_id, target["_id"], new_date, new_time)
    if not updated:
        return "I wasn't able to update that appointment. Would you like me to try again or connect you with a team member?"

    return (
        f"All set! I've moved your appointment to {new_date} at {new_time}. "
        "You'll receive an updated confirmation shortly. Anything else I can help with?"
    )


@tool
async def update_appointment_details(
    email: str,
    phone: str,
    new_email: Optional[str] = None,
    new_phone: Optional[str] = None,
    new_name: Optional[str] = None,
    new_date: Optional[str] = None,
    new_time: Optional[str] = None,
    current_date: Optional[str] = None,
    current_time: Optional[str] = None,
    config: RunnableConfig = None,
) -> str:
    """
    Update contact details (email, phone number, name) or date/time on an existing appointment.
    Use when the caller asks to update, edit, or change their email, phone number, name, date, or time on a booking.
    Requires existing email or phone for identity verification.
    """
    thread_id = logical_thread_id(config)
    tenant_id = _tenant_id(config)

    if (not email or "@" not in email) and (not phone or phone.strip() == ""):
        return "I can update your appointment details — what is the email or phone number used for the booking?"

    appts = await find_active_appointments(
        tenant_id,
        email=email.strip() if email else None,
        phone=phone.strip() if phone else None,
        thread_id=thread_id,
    )

    if not appts:
        return "I couldn't find an active appointment under those contact details. Could you verify the email or phone number used?"

    if len(appts) > 1:
        if current_date and current_date.strip():
            matched = [a for a in appts if current_date.strip().lower() in a.get("date", "").lower()]
            if matched:
                appts = matched
        if len(appts) > 1 and current_time and not _is_placeholder_time(current_time):
            matched = [a for a in appts if current_time.strip().lower() in a.get("time", "").lower()]
            if matched:
                appts = matched

    if len(appts) > 1:
        summary = "; ".join(f"{a.get('date')} at {a.get('time')}" for a in appts)
        return (
            f"You have multiple appointments ({summary}). "
            "Which one would you like to update — please specify the current date and time."
        )

    target = appts[0]
    updates = {}

    if new_email and "@" in new_email:
        updates["email"] = _normalize_email(new_email)
    if new_phone and new_phone.strip():
        updates["phone"] = _normalize_phone(new_phone)
    if new_name and new_name.strip():
        updates["name"] = new_name.strip()
    if new_date and new_date.strip():
        updates["date"] = new_date.strip()
    if new_time and not _is_placeholder_time(new_time):
        updates["time"] = new_time.strip()

    if not updates:
        return "Which detail would you like to update — your email, phone number, name, date, or time?"

    # Check slot if changing date/time
    check_date = updates.get("date", target.get("date"))
    check_time = updates.get("time", target.get("time"))
    if ("date" in updates or "time" in updates) and (check_date != target.get("date") or check_time != target.get("time")):
        available = await check_slot_available(tenant_id, check_date, check_time)
        if not available:
            return f"Unfortunately {check_date} at {check_time} is already booked. Could you pick a different time?"

    success = await update_appointment_fields(tenant_id, target["_id"], updates)
    if not success:
        return "I wasn't able to update your appointment details. Would you like me to try again?"

    updated_labels = []
    if "email" in updates:
        updated_labels.append(f"email to {updates['email']}")
    if "phone" in updates:
        updated_labels.append(f"phone number to {updates['phone']}")
    if "name" in updates:
        updated_labels.append(f"name to {updates['name']}")
    if "date" in updates or "time" in updates:
        updated_labels.append(f"appointment to {check_date} at {check_time}")

    summary_msg = ", ".join(updated_labels)
    return f"Done! I've updated your {summary_msg}. Is there anything else I can help with?"


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

    tenant_id = _tenant_id(config)

    if not oid:
        return "I can cancel that order — do you have your order number? It was shared when you placed the order."

    if (not email or "@" not in email) and (not phone or phone.strip() == ""):
        return "To cancel your order, I'll need the email or phone number you used when ordering."

    orders = await find_active_orders(
        tenant_id,
        order_id=oid,
        email=email.strip() if email else None,
        phone=phone.strip() if phone else None,
    )

    if not orders:
        from backend.database import get_db
        db = get_db()
        any_order = await db.orders.find_one({"tenant_id": tenant_id, "order_id": oid})
        if any_order and any_order.get("status") == "cancelled":
            return f"Order #{oid} is already cancelled. Is there anything else I can help with?"
        return (
            f"I couldn't find order #{oid} matching that email or phone. "
            "Could you double-check the order number and contact details?"
        )

    target = orders[0]
    if target.get("status") == "cancelled":
        return f"Order #{oid} is already cancelled. Can I help with anything else?"

    cancelled = await cancel_order_record(tenant_id, oid)
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
    thread_id = logical_thread_id(config)
    tenant_id = _tenant_id(config)
    typed = await get_recent_typed_chat_messages(tenant_id, thread_id, limit=8)

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

