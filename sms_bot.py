"""
BR CRM — RingCentral SMS Bot (GitHub Actions version)
======================================================
Runs once and exits. GitHub Actions calls it every 5 minutes.

FEATURES:
1. Auto-sends SMS when LA Booking Status = "Quote Sent"
2. Logs incoming replies to Activities table in Airtable
3. Reply directly from Airtable:
   - Type your message in "SMS Reply" field on a Lead
   - Check the "Send Reply" checkbox
   - Within 5 minutes the SMS is sent and fields are cleared
"""

import os
import re
import json
import requests
from datetime import datetime, timezone

# ── RingCentral Credentials from GitHub Secrets ──────────────
RC_CLIENT_ID     = os.environ["RC_CLIENT_ID"]
RC_CLIENT_SECRET = os.environ["RC_CLIENT_SECRET"]
RC_JWT           = os.environ["RC_JWT"]
RC_PHONE         = os.environ["RC_PHONE"]

# ── Airtable Credentials ─────────────────────────────────────
AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]

# ── SMS Message for auto Quote Sent ─────────────────────────
SMS_MESSAGE = "Hi {name}, your quote has been sent. Feel free to reply here if you have any questions!"

# ── Config ───────────────────────────────────────────────────
AIRTABLE_BASE_ID  = "appHxVgsx0rdhKHFX"
AIRTABLE_BASE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
LEADS_TABLE       = "Leads"
ACTIVITIES_TABLE  = "Activities"
RC_SERVER         = "https://platform.ringcentral.com"

AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_API_KEY}",
    "Content-Type": "application/json"
}

PROCESSED_SIDS_FILE = "processed_sids.json"


# ── Phone number normalizer ──────────────────────────────────

def normalize_phone(phone):
    """Convert any phone format to E.164 (+1XXXXXXXXXX)."""
    digits = re.sub(r"\D", "", phone)  # strip everything except digits
    if len(digits) == 10:
        digits = "1" + digits          # add US country code
    if not digits.startswith("+"):
        digits = "+" + digits
    return digits


# ── RingCentral Auth ─────────────────────────────────────────

def get_rc_token():
    """Get RingCentral access token using JWT."""
    url = f"{RC_SERVER}/restapi/oauth/token"
    resp = requests.post(url, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": RC_JWT
    }, auth=(RC_CLIENT_ID, RC_CLIENT_SECRET))
    resp.raise_for_status()
    return resp.json()["access_token"]


# ── RingCentral SMS ──────────────────────────────────────────

def send_rc_sms(token, to_phone, body):
    """Send SMS via RingCentral."""
    url = f"{RC_SERVER}/restapi/v1.0/account/~/extension/~/sms"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "from": {"phoneNumber": RC_PHONE},
        "to":   [{"phoneNumber": to_phone}],
        "text": body
    }
    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()["id"]


def get_rc_incoming_messages(token):
    """Get recent inbound SMS messages from RingCentral."""
    url = f"{RC_SERVER}/restapi/v1.0/account/~/extension/~/message-store"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "messageType": "SMS",
        "direction":   "Inbound",
        "perPage":     50
    }
    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json().get("records", [])


# ── Processed SIDs ───────────────────────────────────────────

def load_processed_sids():
    if os.path.exists(PROCESSED_SIDS_FILE):
        with open(PROCESSED_SIDS_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_processed_sid(sid, processed_sids):
    processed_sids.add(str(sid))
    with open(PROCESSED_SIDS_FILE, "w") as f:
        json.dump(list(processed_sids), f)


# ── Airtable helpers ─────────────────────────────────────────

def get_unsent_leads():
    url = f"{AIRTABLE_BASE_URL}/{LEADS_TABLE}"
    params = {
        "filterByFormula": "AND({LA Booking Status} = 'Quote Sent', NOT({SMS Quote Sent} = TRUE()))",
        "fields[]": ["Customer Full Name", "Phone", "SMS Quote Sent"]
    }
    resp = requests.get(url, headers=AIRTABLE_HEADERS, params=params)
    resp.raise_for_status()
    return resp.json().get("records", [])


def get_leads_with_pending_reply():
    url = f"{AIRTABLE_BASE_URL}/{LEADS_TABLE}"
    params = {
        "filterByFormula": "AND({Send Reply} = TRUE(), {SMS Reply} != '')",
        "fields[]": ["Customer Full Name", "Phone", "SMS Reply"]
    }
    resp = requests.get(url, headers=AIRTABLE_HEADERS, params=params)
    resp.raise_for_status()
    return resp.json().get("records", [])


def mark_sms_sent(lead_id):
    url = f"{AIRTABLE_BASE_URL}/{LEADS_TABLE}/{lead_id}"
    resp = requests.patch(url, headers=AIRTABLE_HEADERS, json={"fields": {"SMS Quote Sent": True}})
    resp.raise_for_status()


def clear_reply_fields(lead_id):
    url = f"{AIRTABLE_BASE_URL}/{LEADS_TABLE}/{lead_id}"
    resp = requests.patch(url, headers=AIRTABLE_HEADERS,
                          json={"fields": {"SMS Reply": "", "Send Reply": False}})
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
    resp = requests.post(url, headers=AIRTABLE_HEADERS, json=payload)
    resp.raise_for_status()


def find_lead_by_phone(phone):
    # Normalize phone — try with and without formatting
    url = f"{AIRTABLE_BASE_URL}/{LEADS_TABLE}"
    params = {
        "filterByFormula": f"{{Phone}} = '{phone}'",
        "fields[]": ["Customer Full Name", "Phone"]
    }
    resp = requests.get(url, headers=AIRTABLE_HEADERS, params=params)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    return records[0] if records else None


# ── Main ─────────────────────────────────────────────────────

def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] BR CRM SMS Bot (RingCentral) — running...")

    # Get RingCentral access token
    try:
        token = get_rc_token()
        print("  ✅  RingCentral authenticated")
    except Exception as e:
        print(f"  ❌  RingCentral auth failed: {e}")
        return

    processed_sids = load_processed_sids()

    # 1. Auto-send SMS to new Quote Sent leads
    print("\n📤 Checking for new 'Quote Sent' leads...")
    try:
        leads = get_unsent_leads()
        if leads:
            print(f"  Found {len(leads)} lead(s) to SMS:")
            for lead in leads:
                fields = lead.get("fields", {})
                name  = fields.get("Customer Full Name", "there")
                phone = fields.get("Phone", "").strip()
                if phone:
                    body = SMS_MESSAGE.replace("{name}", name)
                    try:
                        msg_id = send_rc_sms(token, normalize_phone(phone), body)
                        log_activity(lead["id"], "Outbound", f"Sent: {body}\n[RC ID: {msg_id}]")
                        mark_sms_sent(lead["id"])
                        print(f"  ✅  Sent to {name} ({phone})")
                    except Exception as e:
                        print(f"  ❌  Failed to send to {name}: {e}")
                else:
                    print(f"  ⚠️   No phone for {name}, skipping.")
        else:
            print("  No new leads to SMS.")
    except Exception as e:
        print(f"  ❌  Error: {e}")

    # 2. Send manual replies typed in Airtable
    print("\n💬 Checking for manual replies to send...")
    try:
        reply_leads = get_leads_with_pending_reply()
        if reply_leads:
            print(f"  Found {len(reply_leads)} reply/replies to send:")
            for lead in reply_leads:
                fields = lead.get("fields", {})
                name   = fields.get("Customer Full Name", "Unknown")
                phone  = fields.get("Phone", "").strip()
                reply  = fields.get("SMS Reply", "").strip()
                if phone and reply:
                    try:
                        msg_id = send_rc_sms(token, normalize_phone(phone), reply)
                        log_activity(lead["id"], "Outbound", f"Reply sent: {reply}\n[RC ID: {msg_id}]")
                        clear_reply_fields(lead["id"])
                        print(f"  ✅  Reply sent to {name} ({phone}): {reply}")
                    except Exception as e:
                        print(f"  ❌  Failed to send reply to {name}: {e}")
                else:
                    print(f"  ⚠️   Missing phone or message for {name}, skipping.")
        else:
            print("  No pending replies.")
    except Exception as e:
        print(f"  ❌  Error: {e}")

    # 3. Check for incoming replies from customers
    print("\n📥 Checking for incoming replies...")
    try:
        messages = get_rc_incoming_messages(token)
        new_replies = 0
        for msg in messages:
            msg_id = str(msg.get("id", ""))
            if msg_id in processed_sids:
                continue
            from_number = msg.get("from", {}).get("phoneNumber", "")
            body = msg.get("subject", "")
            lead = find_lead_by_phone(from_number)
            if lead:
                name = lead["fields"].get("Customer Full Name", "Unknown")
                log_activity(lead["id"], "Inbound",
                             f"Reply from {name}: {body}\n[RC ID: {msg_id}]")
                print(f"  📩  New reply from {name}: {body}")
                new_replies += 1
            else:
                print(f"  ⚠️   Reply from unknown number {from_number} (not in CRM)")
            save_processed_sid(msg_id, processed_sids)
        if new_replies == 0:
            print("  No new replies.")
    except Exception as e:
        print(f"  ❌  Error checking replies: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
