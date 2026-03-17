"""
BR CRM — Twilio SMS Bot (GitHub Actions version)
=================================================
Runs once and exits. GitHub Actions calls it every 5 minutes.
Credentials are stored as GitHub Secrets — never hardcoded here.
"""

import os
import json
import requests
from datetime import datetime, timezone
from twilio.rest import Client

# ── Credentials from GitHub Secrets ─────────────────────────
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE       = os.environ["TWILIO_PHONE"]
AIRTABLE_API_KEY   = os.environ["AIRTABLE_API_KEY"]

# ── SMS Message ──────────────────────────────────────────────
SMS_MESSAGE = "Hi {name}, your quote has been sent. Feel free to reply here if you have any questions!"

# ── Config ───────────────────────────────────────────────────
AIRTABLE_BASE_ID = "appHxVgsx0rdhKHFX"
AIRTABLE_BASE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
LEADS_TABLE      = "Leads"
ACTIVITIES_TABLE = "Activities"

HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_API_KEY}",
    "Content-Type": "application/json"
}

PROCESSED_SIDS_FILE = "processed_sids.json"


# ── Processed SIDs ───────────────────────────────────────────

def load_processed_sids():
    if os.path.exists(PROCESSED_SIDS_FILE):
        with open(PROCESSED_SIDS_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_processed_sid(sid, processed_sids):
    processed_sids.add(sid)
    with open(PROCESSED_SIDS_FILE, "w") as f:
        json.dump(list(processed_sids), f)


# ── Airtable helpers ─────────────────────────────────────────

def get_unsent_leads():
    url = f"{AIRTABLE_BASE_URL}/{LEADS_TABLE}"
    params = {
        "filterByFormula": "AND({LA Booking Status} = 'Quote Sent', NOT({SMS Quote Sent} = TRUE()))",
        "fields[]": ["Customer Full Name", "Phone", "LA Booking Status", "SMS Quote Sent"]
    }
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    return resp.json().get("records", [])

def mark_sms_sent(lead_id):
    url = f"{AIRTABLE_BASE_URL}/{LEADS_TABLE}/{lead_id}"
    resp = requests.patch(url, headers=HEADERS, json={"fields": {"SMS Quote Sent": True}})
    resp.raise_for_status()

def log_activity(lead_record_id, direction, notes):
    url = f"{AIRTABLE_BASE_URL}/{ACTIVITIES_TABLE}"
    payload = {
        "fields": {
            "Lead":          [lead_record_id],
            "Activity Date": datetime.now(timezone.utc).isoformat(),
            "Activity Type": "SMS",
            "Direction":     direction,
            "Notes":         notes,
            "Completed":     True
        }
    }
    resp = requests.post(url, headers=HEADERS, json=payload)
    resp.raise_for_status()

def find_lead_by_phone(phone):
    url = f"{AIRTABLE_BASE_URL}/{LEADS_TABLE}"
    params = {
        "filterByFormula": f"{{Phone}} = '{phone}'",
        "fields[]": ["Customer Full Name", "Phone"]
    }
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    return records[0] if records else None


# ── Twilio helpers ───────────────────────────────────────────

def send_sms(phone, name, lead_record_id):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    body = SMS_MESSAGE.replace("{name}", name)
    try:
        msg = client.messages.create(body=body, from_=TWILIO_PHONE, to=phone)
        print(f"  ✅  Sent to {name} ({phone})  [SID: {msg.sid}]")
        log_activity(lead_record_id, "Outbound", f"Sent: {body}\n[Twilio SID: {msg.sid}]")
        mark_sms_sent(lead_record_id)
    except Exception as e:
        print(f"  ❌  Failed to send to {name} ({phone}): {e}")

def check_incoming_replies(processed_sids):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    try:
        messages = client.messages.list(to=TWILIO_PHONE, limit=50)
        for msg in messages:
            if msg.direction != "inbound":
                continue
            if msg.sid in processed_sids:
                continue
            lead = find_lead_by_phone(msg.from_)
            if lead:
                name = lead["fields"].get("Customer Full Name", "Unknown")
                log_activity(lead["id"], "Inbound",
                             f"Reply from {name}: {msg.body}\n[Twilio SID: {msg.sid}]")
                print(f"  📩  New reply from {name}: {msg.body}")
            else:
                print(f"  ⚠️   Reply from unknown number {msg.from_} (not in CRM)")
            save_processed_sid(msg.sid, processed_sids)
    except Exception as e:
        print(f"  ❌  Error checking replies: {e}")


# ── Main (runs once) ─────────────────────────────────────────

def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] BR CRM SMS Bot — running...")

    processed_sids = load_processed_sids()

    # 1. Send SMS to new Quote Sent leads
    print("\nChecking for new 'Quote Sent' leads...")
    leads = get_unsent_leads()
    if leads:
        print(f"  Found {len(leads)} lead(s) to SMS:")
        for lead in leads:
            fields = lead.get("fields", {})
            name  = fields.get("Customer Full Name", "there")
            phone = fields.get("Phone", "").strip()
            if phone:
                send_sms(phone, name, lead["id"])
            else:
                print(f"  ⚠️   No phone for {name}, skipping.")
    else:
        print("  No new leads to SMS.")

    # 2. Check for replies
    print("\nChecking for incoming replies...")
    check_incoming_replies(processed_sids)

    print("\nDone.")

if __name__ == "__main__":
    main()
