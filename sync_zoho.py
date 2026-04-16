#!/usr/bin/env python3
"""
Zoho Invoice -> data.js sync for the Clinic Intelligence Tool.

Pulls all invoices from Zoho Invoice via OAuth2, transforms them into the
compact format that index.html expects, and writes data.js to the repo root.

Required environment variables (set as GitHub Actions secrets):
  ZOHO_CLIENT_ID       - From Zoho self-client
  ZOHO_CLIENT_SECRET   - From Zoho self-client
  ZOHO_REFRESH_TOKEN   - From the OAuth handshake (one-time generation)
  ZOHO_ORG_ID          - Your Zoho Invoice organization ID

Optional:
  ZOHO_ACCOUNTS_DOMAIN - Default: accounts.zoho.com
  ZOHO_API_DOMAIN      - Default: www.zohoapis.com
  EARLIEST_YEAR        - Default: 2024
  OUTPUT_PATH          - Default: data.js (in current directory)
"""

import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ACCOUNTS_DOMAIN = os.environ.get("ZOHO_ACCOUNTS_DOMAIN", "accounts.zoho.com")
API_DOMAIN = os.environ.get("ZOHO_API_DOMAIN", "www.zohoapis.com")
EARLIEST_YEAR = int(os.environ.get("EARLIEST_YEAR", "2024"))
OUTPUT_PATH = Path(os.environ.get("OUTPUT_PATH", "data.js"))

CLIENT_ID = os.environ["ZOHO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
ORG_ID = os.environ["ZOHO_ORG_ID"]

EPOCH_BASE = date(2024, 1, 1)

SKIP_TERMS = ["CANNOT COLLECT", "CLOSED", "VACANT", "DO NOT USE"]

# Rate limiting: Zoho limits ~100 req/min on free tier. Be conservative.
REQUEST_DELAY = 0.5  # seconds between paginated calls
MAX_RETRIES = 4


# ---------------------------------------------------------------------------
# Zoho API helpers
# ---------------------------------------------------------------------------
def get_access_token() -> str:
    """Trade the long-lived refresh token for a 1-hour access token."""
    print("Requesting access token...", flush=True)
    resp = requests.post(
        f"https://{ACCOUNTS_DOMAIN}/oauth/v2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "access_token" not in payload:
        raise RuntimeError(f"Failed to get access token: {payload}")
    print(f"  ✓ Access token acquired (expires in {payload.get('expires_in', '?')}s)", flush=True)
    return payload["access_token"]


def zoho_get(path: str, access_token: str, params: dict | None = None) -> dict:
    """GET against the Zoho Invoice API with retry on 429/5xx."""
    url = f"https://{API_DOMAIN}/invoice/v3{path}"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    qp = {"organization_id": ORG_ID, **(params or {})}

    for attempt in range(MAX_RETRIES):
        resp = requests.get(url, headers=headers, params=qp, timeout=60)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = (2 ** attempt) + 1
            print(f"  ! {resp.status_code} on {path}, retrying in {wait}s", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Exhausted retries for {path}")


def fetch_all_invoices(access_token: str) -> list[dict]:
    """Page through every invoice from EARLIEST_YEAR to today."""
    invoices: list[dict] = []
    page = 1
    per_page = 200
    date_after = f"{EARLIEST_YEAR}-01-01"

    print(f"Fetching invoices since {date_after}...", flush=True)
    while True:
        data = zoho_get(
            "/invoices",
            access_token,
            params={
                "page": page,
                "per_page": per_page,
                "date_start": date_after,
                "sort_column": "date",
                "sort_order": "A",
            },
        )
        batch = data.get("invoices", [])
        invoices.extend(batch)
        ctx = data.get("page_context", {})
        has_more = ctx.get("has_more_page", False)
        print(f"  page {page}: +{len(batch)} (total {len(invoices)})", flush=True)
        if not has_more or not batch:
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    print(f"  ✓ {len(invoices)} invoices total", flush=True)
    return invoices


def fetch_invoice_detail(invoice_id: str, access_token: str) -> dict:
    """Fetch a single invoice with its line items."""
    return zoho_get(f"/invoices/{invoice_id}", access_token)["invoice"]


def fetch_all_contacts(access_token: str) -> dict[str, dict]:
    """Fetch every customer contact, keyed by contact_id."""
    print("Fetching contacts...", flush=True)
    contacts: dict[str, dict] = {}
    page = 1
    per_page = 200
    while True:
        data = zoho_get(
            "/contacts",
            access_token,
            params={"page": page, "per_page": per_page, "contact_type": "customer"},
        )
        batch = data.get("contacts", [])
        for c in batch:
            contacts[c["contact_id"]] = c
        ctx = data.get("page_context", {})
        print(f"  page {page}: +{len(batch)} (total {len(contacts)})", flush=True)
        if not ctx.get("has_more_page", False) or not batch:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    print(f"  ✓ {len(contacts)} contacts", flush=True)
    return contacts


def fetch_contact_detail(contact_id: str, access_token: str) -> dict:
    """Fetch a single contact for full address + persons."""
    return zoho_get(f"/contacts/{contact_id}", access_token)["contact"]


# ---------------------------------------------------------------------------
# Transformation: Zoho records -> compact data.js arrays
# ---------------------------------------------------------------------------
def should_skip_clinic(name: str) -> bool:
    upper = name.upper()
    return any(term in upper for term in SKIP_TERMS)


def epoch_days(iso_date: str) -> int:
    """ISO date string (YYYY-MM-DD) -> days since 2024-01-01."""
    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    return (d - EPOCH_BASE).days


def year_code(year: int) -> int:
    """Year -> single-digit code (2024->4, 2025->5, 2026->6, ...)."""
    return year - 2020


def normalize_phone(p: str | None) -> str:
    if not p:
        return ""
    p = str(p).strip()
    # CSVs sometimes have leading apostrophe to force-text in Excel
    if p.startswith("'"):
        p = p[1:]
    return p


def derive_doctor(contact: dict) -> str:
    """shipping_attention preferred, else first+last name on contact."""
    ship = contact.get("shipping_address", {}) or {}
    attn = (ship.get("attention") or "").strip()
    if attn:
        return attn
    first = (contact.get("first_name") or "").strip()
    last = (contact.get("last_name") or "").strip()
    full = f"{first} {last}".strip()
    return full


def format_address(contact: dict) -> tuple[str, str]:
    """Return (street, city_state_zip) using shipping address with billing fallback."""
    addr = contact.get("shipping_address") or contact.get("billing_address") or {}
    street_parts = [addr.get("address", ""), addr.get("street2", "")]
    street = ", ".join(p.strip() for p in street_parts if p and p.strip())
    csz_parts = [
        addr.get("city", "").strip(),
        addr.get("state", "").strip(),
        addr.get("zip", "").strip(),
    ]
    city_state_zip = ", ".join(p for p in csz_parts if p)
    return street, city_state_zip


def build_data_js(invoices_full: list[dict], contacts: dict[str, dict]) -> str:
    """
    Construct the final data.js content with these globals:
      D     - {clinic_name: [[yr_code, prod_idx, revenue, epoch_days], ...]}
      P     - [product_name, ...]                                product index
      C     - [clinic_name, ...] sorted lowercase                clinic index
      KA    - parallel to C: [rep_idx, email, phone, doctor, street, csz, last_order_date]
      REPS  - [rep_name, ...]                                    rep index
      T     - [[prod_idx, name, total_revenue], ...]             top 30 sellers
    """
    # ---- index products -----------------------------------------------------
    product_set: dict[str, int] = {}
    products: list[str] = []

    def get_product_idx(name: str) -> int:
        if name not in product_set:
            product_set[name] = len(products)
            products.append(name)
        return product_set[name]

    # ---- index reps ---------------------------------------------------------
    rep_set: dict[str, int] = {}
    reps: list[str] = []

    def get_rep_idx(name: str) -> int:
        name = (name or "").strip() or "Unassigned"
        if name not in rep_set:
            rep_set[name] = len(reps)
            reps.append(name)
        return rep_set[name]

    # ---- accumulate transactions + product totals --------------------------
    # D_raw[clinic_name] = list of [yr_code, prod_idx, revenue, epoch_days]
    D_raw: dict[str, list[list]] = {}
    product_totals: dict[int, float] = {}
    last_order_by_clinic: dict[str, str] = {}  # ISO date string

    for inv in invoices_full:
        clinic = (inv.get("customer_name") or "").strip()
        if not clinic or should_skip_clinic(clinic):
            continue
        inv_date = inv.get("date")  # YYYY-MM-DD
        if not inv_date:
            continue
        try:
            ep = epoch_days(inv_date)
            yr = year_code(int(inv_date[:4]))
        except Exception:
            continue
        if ep < 0:  # earlier than EPOCH_BASE
            continue

        bucket = D_raw.setdefault(clinic, [])
        line_items = inv.get("line_items", []) or []
        for li in line_items:
            pname = (li.get("name") or li.get("description") or "").strip()
            if not pname:
                continue
            try:
                rev = float(li.get("item_total") or li.get("total") or 0)
            except (TypeError, ValueError):
                rev = 0.0
            if rev == 0:
                continue
            pidx = get_product_idx(pname)
            bucket.append([yr, pidx, round(rev, 2), ep])
            product_totals[pidx] = product_totals.get(pidx, 0.0) + rev

        # track latest order date per clinic
        prev = last_order_by_clinic.get(clinic, "")
        if inv_date > prev:
            last_order_by_clinic[clinic] = inv_date

    # ---- sorted clinic list + parallel KA array ----------------------------
    clinics_sorted = sorted(D_raw.keys(), key=lambda s: s.lower())

    # Build a contact-by-name map for lookup (fall back if missing)
    contacts_by_name: dict[str, dict] = {}
    for c in contacts.values():
        nm = (c.get("contact_name") or "").strip()
        if nm:
            contacts_by_name[nm] = c

    KA: list[list] = []
    for clinic in clinics_sorted:
        c = contacts_by_name.get(clinic, {})
        rep_name = (c.get("cf_sales_rep") or c.get("salesperson_name") or "").strip()
        rep_idx = get_rep_idx(rep_name)
        email = (c.get("email") or "").strip()
        phone = normalize_phone(c.get("phone") or c.get("mobile"))
        doctor = derive_doctor(c)
        street, csz = format_address(c)
        last_order = last_order_by_clinic.get(clinic, "")
        KA.append([rep_idx, email, phone, doctor, street, csz, last_order])

    # ---- D as dict keyed by clinic name (matches existing tool) ------------
    D = {clinic: D_raw[clinic] for clinic in clinics_sorted}

    # ---- top 30 sellers -----------------------------------------------------
    top_sorted = sorted(product_totals.items(), key=lambda kv: -kv[1])[:30]
    T = [[pidx, products[pidx], round(rev, 2)] for pidx, rev in top_sorted]

    # ---- write the JS file --------------------------------------------------
    def jdump(obj) -> str:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    header = (
        f"// data.js — auto-generated by sync_zoho.py at {timestamp}\n"
        f"// {len(clinics_sorted)} clinics, {len(products)} products, "
        f"{sum(len(v) for v in D.values())} transactions\n"
    )

    parts = [
        header,
        f"var D={jdump(D)};",
        f"var P={jdump(products)};",
        f"var C={jdump(clinics_sorted)};",
        f"var T={jdump(T)};",
        f"var KA={jdump(KA)};",
        f"var REPS={jdump(reps)};",
        "",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    start = time.time()
    print("=" * 60, flush=True)
    print("Zoho Invoice -> data.js sync", flush=True)
    print("=" * 60, flush=True)

    access_token = get_access_token()

    # Pull every invoice (header info: date, customer)
    invoices = fetch_all_invoices(access_token)

    # Pull line items for each. The list endpoint doesn't include line_items,
    # so we have to detail-fetch. This is the slowest step.
    print(f"Fetching line items for {len(invoices)} invoices...", flush=True)
    invoices_full = []
    for i, inv in enumerate(invoices, 1):
        try:
            invoices_full.append(fetch_invoice_detail(inv["invoice_id"], access_token))
        except Exception as e:
            print(f"  ! Failed invoice {inv.get('invoice_number', '?')}: {e}", flush=True)
            continue
        if i % 50 == 0:
            print(f"  detailed {i}/{len(invoices)}", flush=True)
            # refresh token if we've been running >50 minutes
            if time.time() - start > 50 * 60:
                print("  refreshing access token (long-running job)", flush=True)
                access_token = get_access_token()
        time.sleep(REQUEST_DELAY)
    print(f"  ✓ {len(invoices_full)} invoices detailed", flush=True)

    # Pull contacts for the KA array
    contacts = fetch_all_contacts(access_token)

    # Transform and write
    print("Building data.js...", flush=True)
    js = build_data_js(invoices_full, contacts)
    OUTPUT_PATH.write_text(js, encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"  ✓ Wrote {OUTPUT_PATH} ({size_kb:.1f} KB)", flush=True)

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.0f}s.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
