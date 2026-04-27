#!/usr/bin/env python3
"""
PetScript → HubSpot Nightly Sync
- Creates custom properties on first run
- Matches clinics via email → phone → name+address
- Updates Company records with revenue data
- Pushes notes and tasks as HubSpot engagements
"""

import os
import re
import json
import time
import requests
from datetime import datetime, timezone
from difflib import SequenceMatcher

# ── Config ────────────────────────────────────────────────────────────────────
HS_TOKEN      = os.environ["HS_TOKEN"]
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]

HS_BASE       = "https://api.hubapi.com"
HS_HEADERS    = {
    "Authorization": f"Bearer {HS_TOKEN}",
    "Content-Type":  "application/json",
}
SB_HEADERS    = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

# ── Custom properties to ensure exist ────────────────────────────────────────
CUSTOM_PROPS = [
    {"name": "ps_ytd_revenue",    "label": "PS YTD Revenue",     "type": "number",   "fieldType": "number"},
    {"name": "ps_2025_revenue",   "label": "PS 2025 Revenue",    "type": "number",   "fieldType": "number"},
    {"name": "ps_2024_revenue",   "label": "PS 2024 Revenue",    "type": "number",   "fieldType": "number"},
    {"name": "ps_3yr_total",      "label": "PS 3-Year Total",    "type": "number",   "fieldType": "number"},
    {"name": "ps_last_order_date","label": "PS Last Order Date", "type": "date",     "fieldType": "date"},
    {"name": "ps_top_drugs",      "label": "PS Top Drugs",       "type": "string",   "fieldType": "textarea"},
    {"name": "ps_assigned_rep",   "label": "PS Assigned Rep",    "type": "string",   "fieldType": "text"},
    {"name": "ps_invoice_count",  "label": "PS Invoice Count",   "type": "number",   "fieldType": "number"},
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def normalize_phone(p):
    if not p:
        return ""
    return re.sub(r"\D", "", str(p))

def normalize_email(e):
    if not e:
        return ""
    return str(e).strip().lower()

def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def hs_get(path, params=None):
    r = requests.get(f"{HS_BASE}{path}", headers=HS_HEADERS, params=params)
    r.raise_for_status()
    return r.json()

def hs_post(path, body):
    r = requests.post(f"{HS_BASE}{path}", headers=HS_HEADERS, json=body)
    r.raise_for_status()
    return r.json()

def hs_patch(path, body):
    r = requests.patch(f"{HS_BASE}{path}", headers=HS_HEADERS, json=body)
    r.raise_for_status()
    return r.json()

def sb_get(table, params=None):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, params=params)
    r.raise_for_status()
    return r.json()

# ── Step 1: Ensure custom properties exist ───────────────────────────────────
def ensure_properties():
    print("Ensuring custom HubSpot properties exist...")
    existing = hs_get("/crm/v3/properties/companies")
    existing_names = {p["name"] for p in existing.get("results", [])}

    for prop in CUSTOM_PROPS:
        if prop["name"] in existing_names:
            print(f"  ✓ {prop['name']} exists")
            continue
        body = {
            "name":        prop["name"],
            "label":       prop["label"],
            "type":        prop["type"],
            "fieldType":   prop["fieldType"],
            "groupName":   "companyinformation",
            "description": f"Synced from PetScript PSTOOL",
        }
        hs_post("/crm/v3/properties/companies", body)
        print(f"  + Created {prop['name']}")
        time.sleep(0.3)

# ── Step 2: Load all HubSpot companies into memory ───────────────────────────
def load_hs_companies():
    print("Loading HubSpot companies...")
    companies = []
    after = None
    while True:
        params = {
            "limit": 100,
            "properties": "name,phone,domain,address,city,state,zip,ps_assigned_rep",
        }
        if after:
            params["after"] = after
        data = hs_get("/crm/v3/objects/companies", params=params)
        companies.extend(data.get("results", []))
        paging = data.get("paging", {})
        after = paging.get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.2)

    # Also load associated emails via search (contacts linked to companies)
    print(f"  Loaded {len(companies)} HubSpot companies")
    return companies

def load_hs_contacts_by_company():
    """Build map of company_id -> list of contact emails"""
    print("Loading HubSpot contacts for email matching...")
    contacts_map = {}
    after = None
    while True:
        params = {
            "limit": 100,
            "properties": "email,phone,associatedcompanyid",
            "associations": "companies",
        }
        if after:
            params["after"] = after
        data = hs_get("/crm/v3/objects/contacts", params=params)
        for c in data.get("results", []):
            props = c.get("properties", {})
            email = normalize_email(props.get("email", ""))
            phone = normalize_phone(props.get("phone", ""))
            assoc = c.get("associations", {}).get("companies", {}).get("results", [])
            for a in assoc:
                cid = str(a["id"])
                if cid not in contacts_map:
                    contacts_map[cid] = {"emails": [], "phones": []}
                if email:
                    contacts_map[cid]["emails"].append(email)
                if phone:
                    contacts_map[cid]["phones"].append(phone)
        paging = data.get("paging", {})
        after = paging.get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.2)
    print(f"  Loaded contacts for {len(contacts_map)} companies")
    return contacts_map

# ── Step 3: Parse data.js ─────────────────────────────────────────────────────
def parse_data_js():
    print("Parsing data.js...")
    with open("data.js", "r", encoding="utf-8") as f:
        content = f.read()

    # Extract the clinics array
    match = re.search(r"const\s+clinics\s*=\s*(\[.*?\]);", content, re.DOTALL)
    if not match:
        # Try window.PSTOOL_DATA
        match = re.search(r"window\.PSTOOL_DATA\s*=\s*(\{.*?\});", content, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            return data.get("clinics", [])
        print("  WARNING: Could not parse clinics from data.js")
        return []

    clinics = json.loads(match.group(1))
    print(f"  Parsed {len(clinics)} clinics from data.js")
    return clinics

# ── Step 4: Match clinic to HubSpot company ──────────────────────────────────
def match_clinic(clinic, hs_companies, contacts_map):
    clinic_email = normalize_email(clinic.get("email", ""))
    clinic_phone = normalize_phone(clinic.get("phone", ""))
    clinic_name  = (clinic.get("name") or clinic.get("customer_name") or "").strip()
    clinic_addr  = (clinic.get("address") or clinic.get("billing_address") or "").strip().lower()

    # 1. Email match via contacts
    if clinic_email:
        for comp in hs_companies:
            cid = str(comp["id"])
            contact_data = contacts_map.get(cid, {})
            if clinic_email in contact_data.get("emails", []):
                return comp, "email"
        # Also check company domain
        for comp in hs_companies:
            domain = normalize_email(comp["properties"].get("domain", ""))
            if domain and clinic_email.endswith("@" + domain):
                return comp, "domain"

    # 2. Phone match
    if clinic_phone:
        for comp in hs_companies:
            hs_phone = normalize_phone(comp["properties"].get("phone", ""))
            cid = str(comp["id"])
            contact_phones = contacts_map.get(cid, {}).get("phones", [])
            if hs_phone == clinic_phone or clinic_phone in contact_phones:
                return comp, "phone"

    # 3. Name + address fuzzy
    if clinic_name:
        best_score = 0
        best_comp  = None
        for comp in hs_companies:
            hs_name = (comp["properties"].get("name") or "").strip()
            hs_addr = (comp["properties"].get("address") or "").strip().lower()
            name_sim = similarity(clinic_name, hs_name)
            addr_sim = similarity(clinic_addr, hs_addr) if clinic_addr and hs_addr else 0
            score = (name_sim * 0.6) + (addr_sim * 0.4)
            if score > best_score:
                best_score = score
                best_comp  = comp
        if best_score >= 0.75:
            return best_comp, f"fuzzy({best_score:.2f})"

    return None, None

# ── Step 5: Build update payload ─────────────────────────────────────────────
def build_update_payload(clinic):
    """Map clinic fields to HubSpot custom properties"""
    props = {}

    # Revenue fields — adapt to your actual data.js structure
    ytd = clinic.get("ytd") or clinic.get("revenue_ytd") or clinic.get("2026") or 0
    rev_2025 = clinic.get("2025") or clinic.get("revenue_2025") or 0
    rev_2024 = clinic.get("2024") or clinic.get("revenue_2024") or 0
    total_3yr = clinic.get("total_3yr") or clinic.get("three_year_total") or (ytd + rev_2025 + rev_2024)

    if ytd:       props["ps_ytd_revenue"]    = round(float(ytd), 2)
    if rev_2025:  props["ps_2025_revenue"]   = round(float(rev_2025), 2)
    if rev_2024:  props["ps_2024_revenue"]   = round(float(rev_2024), 2)
    if total_3yr: props["ps_3yr_total"]      = round(float(total_3yr), 2)

    last_order = clinic.get("last_order") or clinic.get("last_invoice_date")
    if last_order:
        try:
            dt = datetime.strptime(str(last_order)[:10], "%Y-%m-%d")
            props["ps_last_order_date"] = int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
        except:
            pass

    rep = clinic.get("rep") or clinic.get("assigned_rep") or clinic.get("sales_rep")
    if rep:
        props["ps_assigned_rep"] = str(rep)

    inv_count = clinic.get("invoice_count") or clinic.get("num_invoices")
    if inv_count:
        props["ps_invoice_count"] = int(inv_count)

    top_drugs = clinic.get("top_drugs") or clinic.get("drugs")
    if top_drugs:
        if isinstance(top_drugs, list):
            props["ps_top_drugs"] = ", ".join(str(d) for d in top_drugs[:5])
        else:
            props["ps_top_drugs"] = str(top_drugs)[:500]

    return props

# ── Step 6: Push notes ────────────────────────────────────────────────────────
def push_notes(company_id, clinic_key, existing_note_bodies):
    """Fetch notes from Supabase and push new ones to HubSpot"""
    try:
        notes = sb_get("notes", params={
            "select": "*",
            "or": f"(customer_name.eq.{clinic_key},clinic_id.eq.{clinic_key})",
        })
    except:
        return 0

    pushed = 0
    for note in notes:
        body = note.get("content") or note.get("text") or note.get("note") or ""
        if not body or body in existing_note_bodies:
            continue
        created = note.get("created_at", "")
        timestamp = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1000) if created else int(time.time() * 1000)

        try:
            engagement = hs_post("/engagements/v1/engagements", {
                "engagement": {
                    "active":    True,
                    "type":      "NOTE",
                    "timestamp": timestamp,
                },
                "associations": {
                    "companyIds": [int(company_id)],
                },
                "metadata": {
                    "body": f"[PetScript] {body}",
                },
            })
            pushed += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"    Note push failed: {e}")

    return pushed

def get_existing_note_bodies(company_id):
    """Get existing note bodies on a HubSpot company to avoid dupes"""
    bodies = set()
    try:
        data = hs_get(f"/engagements/v1/engagements/associated/COMPANY/{company_id}/paged",
                      params={"limit": 100})
        for eng in data.get("results", []):
            if eng.get("engagement", {}).get("type") == "NOTE":
                body = eng.get("metadata", {}).get("body", "")
                bodies.add(body)
    except:
        pass
    return bodies

# ── Step 7: Push tasks ────────────────────────────────────────────────────────
def push_tasks(company_id, clinic_key, existing_task_titles):
    try:
        tasks = sb_get("tasks", params={
            "select": "*",
            "or": f"(customer_name.eq.{clinic_key},clinic_id.eq.{clinic_key})",
        })
    except:
        return 0

    pushed = 0
    for task in tasks:
        title = task.get("title") or task.get("task") or "Task from PetScript"
        if title in existing_task_titles:
            continue
        due = task.get("due_date")
        due_ts = None
        if due:
            try:
                due_ts = int(datetime.strptime(str(due)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
            except:
                pass

        try:
            hs_post("/engagements/v1/engagements", {
                "engagement": {
                    "active":    True,
                    "type":      "TASK",
                    "timestamp": due_ts or int(time.time() * 1000),
                },
                "associations": {
                    "companyIds": [int(company_id)],
                },
                "metadata": {
                    "subject": f"[PetScript] {title}",
                    "status":  "NOT_STARTED",
                    "body":    task.get("description") or "",
                },
            })
            pushed += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"    Task push failed: {e}")

    return pushed

def get_existing_task_titles(company_id):
    titles = set()
    try:
        data = hs_get(f"/engagements/v1/engagements/associated/COMPANY/{company_id}/paged",
                      params={"limit": 100})
        for eng in data.get("results", []):
            if eng.get("engagement", {}).get("type") == "TASK":
                title = eng.get("metadata", {}).get("subject", "")
                titles.add(title)
    except:
        pass
    return titles

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"PetScript → HubSpot Sync  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    ensure_properties()
    hs_companies  = load_hs_companies()
    contacts_map  = load_hs_contacts_by_company()
    clinics       = parse_data_js()

    if not clinics:
        print("No clinics found in data.js — exiting")
        return

    matched   = 0
    unmatched = 0
    updated   = 0
    notes_pushed = 0
    tasks_pushed = 0

    for clinic in clinics:
        name = clinic.get("name") or clinic.get("customer_name") or "Unknown"
        comp, method = match_clinic(clinic, hs_companies, contacts_map)

        if not comp:
            print(f"  ✗ No match: {name}")
            unmatched += 1
            continue

        company_id = comp["id"]
        matched += 1
        print(f"  ✓ {name} → {comp['properties'].get('name')} [{method}]")

        # Update properties
        props = build_update_payload(clinic)
        if props:
            try:
                hs_patch(f"/crm/v3/objects/companies/{company_id}", {"properties": props})
                updated += 1
            except Exception as e:
                print(f"    Update failed: {e}")
            time.sleep(0.15)

        # Push notes
        clinic_key = name
        existing_notes = get_existing_note_bodies(company_id)
        n = push_notes(company_id, clinic_key, existing_notes)
        notes_pushed += n

        # Push tasks
        existing_tasks = get_existing_task_titles(company_id)
        t = push_tasks(company_id, clinic_key, existing_tasks)
        tasks_pushed += t

    print(f"\n{'='*60}")
    print(f"Sync complete")
    print(f"  Matched:       {matched}")
    print(f"  Unmatched:     {unmatched}")
    print(f"  Updated:       {updated}")
    print(f"  Notes pushed:  {notes_pushed}")
    print(f"  Tasks pushed:  {tasks_pushed}")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
