#!/usr/bin/env python3
"""
PetScript → HubSpot Nightly Sync (FAST VERSION)
- Batch loads all HubSpot companies once
- Skips notes/tasks unless data exists
- Batch updates properties
- Target runtime: < 5 minutes
"""

import os
import re
import json
import time
import requests
from datetime import datetime, timezone
from difflib import SequenceMatcher

HS_TOKEN     = os.environ["HS_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

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

YEAR_MAP     = {4: 2024, 5: 2025, 6: 2026}
CURRENT_YEAR = 2026

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

def hs_patch(path, body):
    r = requests.patch(f"{HS_BASE}{path}", headers=HS_HEADERS, json=body)
    r.raise_for_status()
    return r.json()

def hs_post(path, body):
    r = requests.post(f"{HS_BASE}{path}", headers=HS_HEADERS, json=body)
    r.raise_for_status()
    return r.json()

def sb_get(table, params=None):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, params=params)
    r.raise_for_status()
    return r.json()

def ensure_properties():
    print("Checking custom properties...")
    existing = hs_get("/crm/v3/properties/companies")
    existing_names = {p["name"] for p in existing.get("results", [])}
    for prop in CUSTOM_PROPS:
        if prop["name"] not in existing_names:
            hs_post("/crm/v3/properties/companies", {
                "name": prop["name"], "label": prop["label"],
                "type": prop["type"], "fieldType": prop["fieldType"],
                "groupName": "companyinformation",
            })
            print(f"  + Created {prop['name']}")
            time.sleep(0.3)

def load_hs_companies():
    print("Loading HubSpot companies...")
    companies = {}
    after = None
    while True:
        params = {"limit": 100, "properties": "name"}
        if after:
            params["after"] = after
        data = hs_get("/crm/v3/objects/companies", params=params)
        for c in data.get("results", []):
            name = (c["properties"].get("name") or "").strip()
            if name:
                norm = normalize_name(name)
                if norm not in companies:
                    companies[norm] = c
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.1)
    print(f"  Loaded {len(companies)} companies")
    return companies

def parse_data_js():
    print("Parsing data.js...")
    with open("data.js", "r", encoding="utf-8") as f:
        content = f.read()
    match = re.search(r"var\s+D\s*=\s*(\{)", content)
    if not match:
        print("  ERROR: var D not found")
        return []
    start = match.start(1)
    depth = 0
    end = start
    for i, ch in enumerate(content[start:], start):
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        D = json.loads(content[start:end])
    except json.JSONDecodeError as e:
        print(f"  ERROR: {e}")
        return []

    clinics = []
    for name, rows in D.items():
        rev_by_year = {}
        prod_counts = {}
        for row in rows:
            if len(row) < 3: continue
            yr_code, prod_id, amount = row[0], row[1], float(row[2])
            year = YEAR_MAP.get(yr_code)
            if not year: continue
            rev_by_year[year] = rev_by_year.get(year, 0) + amount
            prod_counts[prod_id] = prod_counts.get(prod_id, 0) + 1
        ytd      = round(rev_by_year.get(CURRENT_YEAR, 0), 2)
        rev_2025 = round(rev_by_year.get(2025, 0), 2)
        rev_2024 = round(rev_by_year.get(2024, 0), 2)
        total_3yr = round(ytd + rev_2025 + rev_2024, 2)
        top_prods = sorted(prod_counts, key=prod_counts.get, reverse=True)[:3]
        clinics.append({
            "name": name, "ytd": ytd, "rev_2025": rev_2025,
            "rev_2024": rev_2024, "total_3yr": total_3yr, "top_drug_ids": top_prods,
        })
    print(f"  Parsed {len(clinics)} clinics")
    return clinics

def load_supabase_notes():
    """Load ALL notes at once — one API call"""
    print("Loading notes from Supabase...")
    try:
        notes = sb_get("clinic_notes", params={"select": "clinic_name,note,rep_name,created_at", "limit": "10000"})
        by_clinic = {}
        for n in (notes or []):
            cn = n.get("clinic_name", "")
            if cn not in by_clinic: by_clinic[cn] = []
            by_clinic[cn].append(n)
        print(f"  Loaded {len(notes or [])} notes for {len(by_clinic)} clinics")
        return by_clinic
    except Exception as e:
        print(f"  Notes load failed: {e}")
        return {}

def load_supabase_tasks():
    """Load ALL tasks at once — one API call"""
    print("Loading tasks from Supabase...")
    try:
        tasks = sb_get("rep_tasks", params={"select": "clinic_name,task,due_date,done,rep_name,created_at", "limit": "10000"})
        by_clinic = {}
        for t in (tasks or []):
            cn = t.get("clinic_name", "")
            if cn not in by_clinic: by_clinic[cn] = []
            by_clinic[cn].append(t)
        print(f"  Loaded {len(tasks or [])} tasks for {len(by_clinic)} clinics")
        return by_clinic
    except Exception as e:
        print(f"  Tasks load failed: {e}")
        return {}

def push_notes_for_clinic(company_id, notes, existing_bodies):
    pushed = 0
    for note in notes:
        body = note.get("note", "")
        rep  = note.get("rep_name", "")
        tagged = f"[PetScript] {rep}: {body}" if rep else f"[PetScript] {body}"
        if not body or tagged in existing_bodies: continue
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
            time.sleep(0.15)
        except Exception as e:
            print(f"    Note push error: {e}")
    return pushed

def push_tasks_for_clinic(company_id, tasks, existing_titles):
    pushed = 0
    for task in tasks:
        title  = task.get("task", "PetScript Task")
        tagged = f"[PetScript] {title}"
        if tagged in existing_titles: continue
        due = task.get("due_date")
        due_ts = None
        if due:
            try:
                due_ts = int(datetime.strptime(str(due)[:10], "%Y-%m-%d")
                             .replace(tzinfo=timezone.utc).timestamp() * 1000)
            except: pass
        try:
            hs_post("/engagements/v1/engagements", {
                "engagement":   {"active": True, "type": "TASK",
                                 "timestamp": due_ts or int(time.time() * 1000)},
                "associations": {"companyIds": [int(company_id)]},
                "metadata":     {
                    "subject": tagged,
                    "status":  "COMPLETED" if task.get("done") else "NOT_STARTED",
                    "body":    f"Rep: {task.get('rep_name','')}" if task.get("rep_name") else "",
                },
            })
            pushed += 1
            time.sleep(0.15)
        except Exception as e:
            print(f"    Task push error: {e}")
    return pushed

def main():
    print(f"\n{'='*60}")
    print(f"PetScript → HubSpot Sync (Fast)  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    ensure_properties()
    hs_companies = load_hs_companies()
    clinics      = parse_data_js()
    notes_by_clinic = load_supabase_notes()
    tasks_by_clinic = load_supabase_tasks()

    if not clinics:
        print("No clinics — exiting")
        return

    matched = unmatched = updated = notes_pushed = tasks_pushed = 0
    unmatched_names = []

    for clinic in clinics:
        name = clinic["name"]
        norm = normalize_name(name)

        # Exact match first
        comp = hs_companies.get(norm)
        method = "exact"

        # Fuzzy match if no exact
        if not comp:
            best_score, best_comp = 0, None
            for hn, hc in hs_companies.items():
                score = similarity(name, hc["properties"].get("name", ""))
                if score > best_score:
                    best_score = score
                    best_comp  = hc
            if best_score >= 0.82:
                comp   = best_comp
                method = f"fuzzy({best_score:.2f})"

        if not comp:
            unmatched += 1
            unmatched_names.append(name)
            continue

        company_id = comp["id"]
        matched += 1

        # Build property update
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
                print(f"  Update failed {name}: {e}")
            time.sleep(0.05)

        # Only push notes/tasks if there are any for this clinic
        clinic_notes = notes_by_clinic.get(name, [])
        clinic_tasks = tasks_by_clinic.get(name, [])

        if clinic_notes:
            try:
                data = hs_get(f"/engagements/v1/engagements/associated/COMPANY/{company_id}/paged",
                              params={"limit": 100})
                existing_bodies = set()
                for eng in data.get("results", []):
                    if eng.get("engagement", {}).get("type") == "NOTE":
                        existing_bodies.add(eng.get("metadata", {}).get("body", ""))
                notes_pushed += push_notes_for_clinic(company_id, clinic_notes, existing_bodies)
            except Exception as e:
                print(f"  Notes error {name}: {e}")

        if clinic_tasks:
            try:
                data = hs_get(f"/engagements/v1/engagements/associated/COMPANY/{company_id}/paged",
                              params={"limit": 100})
                existing_titles = set()
                for eng in data.get("results", []):
                    if eng.get("engagement", {}).get("type") == "TASK":
                        existing_titles.add(eng.get("metadata", {}).get("subject", ""))
                tasks_pushed += push_tasks_for_clinic(company_id, clinic_tasks, existing_titles)
            except Exception as e:
                print(f"  Tasks error {name}: {e}")

    print(f"\n{'='*60}")
    print(f"Sync complete")
    print(f"  Matched:      {matched}")
    print(f"  Unmatched:    {unmatched}")
    print(f"  Updated:      {updated}")
    print(f"  Notes pushed: {notes_pushed}")
    print(f"  Tasks pushed: {tasks_pushed}")
    if unmatched_names[:10]:
        print(f"\nFirst 10 unmatched:")
        for n in unmatched_names[:10]:
            print(f"  - {n}")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
