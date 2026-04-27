#!/usr/bin/env python3
"""
PetScript → HubSpot Nightly Sync
- Parses data.js compact format (var D = {clinic: [[year,prod,amt,day],...]}
- Matches clinics to HubSpot Companies via exact → fuzzy name
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

HS_BASE    = "https://api.hubapi.com"
HS_HEADERS = {
    "Authorization": f"Bearer {HS_TOKEN}",
    "Content-Type":  "application/json",
}
SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

# Year encoding in data.js: 4=2024, 5=2025, 6=2026
YEAR_MAP     = {4: 2024, 5: 2025, 6: 2026}
CURRENT_YEAR = 2026

# ── Custom properties ─────────────────────────────────────────────────────────
CUSTOM_PROPS = [
    {"name": "ps_ytd_revenue",     "label": "PS YTD Revenue",     "type": "number", "fieldType": "number"},
    {"name": "ps_2025_revenue",    "label": "PS 2025 Revenue",    "type": "number", "fieldType": "number"},
    {"name": "ps_2024_revenue",    "label": "PS 2024 Revenue",    "type": "number", "fieldType": "number"},
    {"name": "ps_3yr_total",       "label": "PS 3-Year Total",    "type": "number", "fieldType": "number"},
    {"name": "ps_last_order_date", "label": "PS Last Order Date", "type": "date",   "fieldType": "date"},
    {"name": "ps_top_drugs",       "label": "PS Top Drugs",       "type": "string", "fieldType": "textarea"},
    {"name": "ps_assigned_rep",    "label": "PS Assigned Rep",    "type": "string", "fieldType": "text"},
    {"name": "ps_invoice_count",   "label": "PS Invoice Count",   "type": "number", "fieldType": "number"},
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def normalize_name(n):
    n = str(n).lower().strip()
    n = re.sub(r"[^\w\s]", "", n)
    n = re.sub(r"\s+", " ", n)
    return n

def similarity(a, b):
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()

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
    existing      = hs_get("/crm/v3/properties/companies")
    existing_names = {p["name"] for p in existing.get("results", [])}
    for prop in CUSTOM_PROPS:
        if prop["name"] in existing_names:
            print(f"  ✓ {prop['name']} exists")
            continue
        hs_post("/crm/v3/properties/companies", {
            "name":        prop["name"],
            "label":       prop["label"],
            "type":        prop["type"],
            "fieldType":   prop["fieldType"],
            "groupName":   "companyinformation",
            "description": "Synced from PetScript PSTOOL",
        })
        print(f"  + Created {prop['name']}")
        time.sleep(0.3)

# ── Step 2: Parse data.js ─────────────────────────────────────────────────────
def parse_data_js():
    print("Parsing data.js...")
    with open("data.js", "r", encoding="utf-8") as f:
        content = f.read()

    # Extract var D = { ... } — grab from "var D=" to next top-level "var X="
    match = re.search(r"var\s+D\s*=\s*(\{)", content)
    if not match:
        print("  ERROR: var D not found in data.js")
        return []

    start = match.start(1)
    depth = 0
    end   = start
    for i, ch in enumerate(content[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    raw = content[start:end]
    try:
        D = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  ERROR parsing var D JSON: {e}")
        return []

    clinics = []
    for name, rows in D.items():
        rev_by_year = {}
        prod_counts = {}

        for row in rows:
            if len(row) < 3:
                continue
            yr_code = row[0]
            prod_id = row[1]
            amount  = float(row[2])
            year    = YEAR_MAP.get(yr_code)
            if not year:
                continue
            rev_by_year[year] = rev_by_year.get(year, 0) + amount
            prod_counts[prod_id] = prod_counts.get(prod_id, 0) + 1

        ytd      = round(rev_by_year.get(CURRENT_YEAR, 0), 2)
        rev_2025 = round(rev_by_year.get(2025, 0), 2)
        rev_2024 = round(rev_by_year.get(2024, 0), 2)
        total_3yr = round(ytd + rev_2025 + rev_2024, 2)
        top_prods = sorted(prod_counts, key=prod_counts.get, reverse=True)[:3]

        clinics.append({
            "name":         name,
            "ytd":          ytd,
            "rev_2025":     rev_2025,
            "rev_2024":     rev_2024,
            "total_3yr":    total_3yr,
            "top_drug_ids": top_prods,
        })

    print(f"  Parsed {len(clinics)} clinics")
    return clinics

# ── Step 3: Load HubSpot companies ───────────────────────────────────────────
def load_hs_companies():
    print("Loading HubSpot companies...")
    companies = []
    after = None
    while True:
        params = {"limit": 100, "properties": "name,phone,domain,address"}
        if after:
            params["after"] = after
        data  = hs_get("/crm/v3/objects/companies", params=params)
        companies.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.15)
    print(f"  Loaded {len(companies)} companies")
    return companies

# ── Step 4: Match clinic → HubSpot company ───────────────────────────────────
def match_clinic(clinic_name, hs_companies):
    norm = normalize_name(clinic_name)

    # 1. Exact
    for comp in hs_companies:
        if normalize_name(comp["properties"].get("name", "")) == norm:
            return comp, "exact"

    # 2. Fuzzy (≥82% similarity)
    best_score, best_comp = 0, None
    for comp in hs_companies:
        score = similarity(clinic_name, comp["properties"].get("name", ""))
        if score > best_score:
            best_score = score
            best_comp  = comp

    if best_score >= 0.82:
        return best_comp, f"fuzzy({best_score:.2f})"

    return None, None

# ── Step 5: Engagements (notes + tasks) ──────────────────────────────────────
def get_existing_bodies(company_id, eng_type):
    bodies = set()
    try:
        data = hs_get(
            f"/engagements/v1/engagements/associated/COMPANY/{company_id}/paged",
            params={"limit": 100}
        )
        for eng in data.get("results", []):
            if eng.get("engagement", {}).get("type") == eng_type:
                meta = eng.get("metadata", {})
                bodies.add(meta.get("body") or meta.get("subject") or "")
    except:
        pass
    return bodies

def push_notes(company_id, clinic_name):
    pushed = 0
    try:
        notes = sb_get("notes", params={"select": "*", "customer_name": f"eq.{clinic_name}"})
    except Exception as e:
        print(f"    Notes fetch error: {e}")
        return 0

    existing = get_existing_bodies(company_id, "NOTE")
    for note in (notes or []):
        body   = note.get("content") or note.get("text") or note.get("note") or ""
        tagged = f"[PetScript] {body}"
        if not body or tagged in existing:
            continue
        created = note.get("created_at", "")
        try:
            ts = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1000)
        except:
            ts = int(time.time() * 1000)
        try:
            hs_post("/engagements/v1/engagements", {
                "engagement":   {"active": True, "type": "NOTE", "timestamp": ts},
                "associations": {"companyIds": [int(company_id)]},
                "metadata":     {"body": tagged},
            })
            pushed += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"    Note push error: {e}")
    return pushed

def push_tasks(company_id, clinic_name):
    pushed = 0
    try:
        tasks = sb_get("tasks", params={"select": "*", "customer_name": f"eq.{clinic_name}"})
    except Exception as e:
        print(f"    Tasks fetch error: {e}")
        return 0

    existing = get_existing_bodies(company_id, "TASK")
    for task in (tasks or []):
        title  = task.get("title") or task.get("task") or "PetScript Task"
        tagged = f"[PetScript] {title}"
        if tagged in existing:
            continue
        due    = task.get("due_date")
        due_ts = None
        if due:
            try:
                due_ts = int(datetime.strptime(str(due)[:10], "%Y-%m-%d")
                             .replace(tzinfo=timezone.utc).timestamp() * 1000)
            except:
                pass
        try:
            hs_post("/engagements/v1/engagements", {
                "engagement":   {"active": True, "type": "TASK",
                                 "timestamp": due_ts or int(time.time() * 1000)},
                "associations": {"companyIds": [int(company_id)]},
                "metadata":     {
                    "subject": tagged,
                    "status":  "NOT_STARTED",
                    "body":    task.get("description") or "",
                },
            })
            pushed += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"    Task push error: {e}")
    return pushed

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"PetScript → HubSpot Sync  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    ensure_properties()
    hs_companies    = load_hs_companies()
    clinics         = parse_data_js()

    if not clinics:
        print("No clinics parsed — exiting")
        return

    matched         = 0
    unmatched       = 0
    updated         = 0
    notes_pushed    = 0
    tasks_pushed    = 0
    unmatched_names = []

    for clinic in clinics:
        name = clinic["name"]
        comp, method = match_clinic(name, hs_companies)

        if not comp:
            unmatched += 1
            unmatched_names.append(name)
            continue

        company_id = comp["id"]
        matched += 1
        print(f"  ✓ {name} → {comp['properties'].get('name')} [{method}]")

        props = {}
        if clinic["ytd"]:       props["ps_ytd_revenue"]  = clinic["ytd"]
        if clinic["rev_2025"]:  props["ps_2025_revenue"] = clinic["rev_2025"]
        if clinic["rev_2024"]:  props["ps_2024_revenue"] = clinic["rev_2024"]
        if clinic["total_3yr"]: props["ps_3yr_total"]    = clinic["total_3yr"]
        if clinic["top_drug_ids"]:
            props["ps_top_drugs"] = ", ".join(str(d) for d in clinic["top_drug_ids"])

        if props:
            try:
                hs_patch(f"/crm/v3/objects/companies/{company_id}", {"properties": props})
                updated += 1
            except Exception as e:
                print(f"    Update failed: {e}")
            time.sleep(0.15)

        notes_pushed += push_notes(company_id, name)
        tasks_pushed += push_tasks(company_id, name)

    print(f"\n{'='*60}")
    print(f"Sync complete")
    print(f"  Matched:      {matched}")
    print(f"  Unmatched:    {unmatched}")
    print(f"  Updated:      {updated}")
    print(f"  Notes pushed: {notes_pushed}")
    print(f"  Tasks pushed: {tasks_pushed}")
    if unmatched_names:
        print(f"\nUnmatched clinics (first 20):")
        for n in unmatched_names[:20]:
            print(f"  - {n}")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
