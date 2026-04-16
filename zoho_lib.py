"""
Shared Zoho API + data transformation helpers.

Used by both sync_incremental.py (nightly) and sync_full.py (weekly).
"""
import json
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ACCOUNTS_DOMAIN = os.environ.get("ZOHO_ACCOUNTS_DOMAIN", "accounts.zoho.com")
API_DOMAIN = os.environ.get("ZOHO_API_DOMAIN", "www.zohoapis.com")
EARLIEST_YEAR = int(os.environ.get("EARLIEST_YEAR", "2024"))

CLIENT_ID = os.environ["ZOHO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
ORG_ID = os.environ["ZOHO_ORG_ID"]

EPOCH_BASE = date(2024, 1, 1)
SKIP_TERMS = ["CANNOT COLLECT", "CLOSED", "VACANT", "DO NOT USE"]

REQUEST_DELAY = 0.25
MAX_RETRIES = 4

LAST_SYNC_PATH = Path("last_sync.json")
DATA_JS_PATH = Path("data.js")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class ZohoClient:
    """Keeps a live access token, refreshes when needed, makes GET requests."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_acquired_at: float = 0.0
        self.refresh()

    def refresh(self) -> None:
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
            raise RuntimeError(f"No access_token in response: {payload}")
        self._token = payload["access_token"]
        self._token_acquired_at = time.time()
        print(f"  ✓ Access token acquired (expires in {payload.get('expires_in', '?')}s)", flush=True)

    def _ensure_fresh(self) -> None:
        # Refresh proactively if >50 min old
        if time.time() - self._token_acquired_at > 50 * 60:
            print("  ↻ Refreshing access token (age > 50m)", flush=True)
            self.refresh()

    def get(self, path: str, params: dict | None = None) -> dict:
        self._ensure_fresh()
        url = f"https://{API_DOMAIN}/invoice/v3{path}"
        qp = {"organization_id": ORG_ID, **(params or {})}
        for attempt in range(MAX_RETRIES):
            headers = {"Authorization": f"Zoho-oauthtoken {self._token}"}
            resp = requests.get(url, headers=headers, params=qp, timeout=60)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                print("  ↻ 401 Unauthorized — refreshing token and retrying", flush=True)
                self.refresh()
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = (2 ** attempt) + 1
                print(f"  ! {resp.status_code} on {path}, retry in {wait}s", flush=True)
                time.sleep(wait)
                continue
            resp.raise_for_status()
        raise RuntimeError(f"Exhausted retries for {path}")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch_invoice_list(
    client: ZohoClient,
    *,
    date_start: str | None = None,
    last_modified_after: str | None = None,
) -> list[dict]:
    """
    Paginate the /invoices list endpoint.

    date_start: "YYYY-MM-DD" — filter by invoice date >= this
    last_modified_after: ISO timestamp "YYYY-MM-DDTHH:MM:SS-0000" — filter by
        last_modified_time >= this (for incremental sync)

    If both provided, Zoho applies both.
    """
    invoices: list[dict] = []
    page = 1
    per_page = 200
    base_params = {
        "per_page": per_page,
        "sort_column": "last_modified_time",
        "sort_order": "A",
    }
    if date_start:
        base_params["date_start"] = date_start
    if last_modified_after:
        # Zoho uses the header "If-Modified-Since" for this OR a query param
        # on some endpoints. We use the filter_by / custom param approach:
        base_params["last_modified_time_start"] = last_modified_after

    while True:
        params = {**base_params, "page": page}
        data = client.get("/invoices", params=params)
        batch = data.get("invoices", [])
        invoices.extend(batch)
        ctx = data.get("page_context", {})
        has_more = ctx.get("has_more_page", False)
        print(f"  page {page}: +{len(batch)} (total {len(invoices)})", flush=True)
        if not has_more or not batch:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return invoices


def fetch_invoice_detail(client: ZohoClient, invoice_id: str) -> dict:
    return client.get(f"/invoices/{invoice_id}")["invoice"]


def fetch_all_contacts(client: ZohoClient) -> dict[str, dict]:
    """All customer contacts, keyed by contact_id."""
    print("Fetching contacts...", flush=True)
    contacts: dict[str, dict] = {}
    page = 1
    per_page = 200
    while True:
        data = client.get(
            "/contacts",
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
    return contacts


# ---------------------------------------------------------------------------
# Transformation
# ---------------------------------------------------------------------------
def should_skip_clinic(name: str) -> bool:
    upper = name.upper()
    return any(term in upper for term in SKIP_TERMS)


def epoch_days(iso_date: str) -> int:
    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    return (d - EPOCH_BASE).days


def year_code(year: int) -> int:
    return year - 2020


def normalize_phone(p: str | None) -> str:
    if not p:
        return ""
    p = str(p).strip()
    if p.startswith("'"):
        p = p[1:]
    return p


def derive_doctor(contact: dict) -> str:
    ship = contact.get("shipping_address", {}) or {}
    attn = (ship.get("attention") or "").strip()
    if attn:
        return attn
    first = (contact.get("first_name") or "").strip()
    last = (contact.get("last_name") or "").strip()
    return f"{first} {last}".strip()


def format_address(contact: dict) -> tuple[str, str]:
    addr = contact.get("shipping_address") or contact.get("billing_address") or {}
    street_parts = [addr.get("address", ""), addr.get("street2", "")]
    street = ", ".join(p.strip() for p in street_parts if p and p.strip())
    csz_parts = [
        (addr.get("city") or "").strip(),
        (addr.get("state") or "").strip(),
        (addr.get("zip") or "").strip(),
    ]
    city_state_zip = ", ".join(p for p in csz_parts if p)
    return street, city_state_zip


def extract_rep_name(contact: dict) -> str:
    """
    Try several possible fields for sales rep assignment.

    Zoho custom fields are prefixed cf_. If your org uses a different
    field name, add it to the list below.
    """
    candidates = [
        "cf_sales_rep",
        "cf_sales_representative",
        "cf_rep",
        "cf_sales_person",
        "salesperson_name",
    ]
    for key in candidates:
        val = (contact.get(key) or "").strip()
        if val:
            return val
    return ""


# ---------------------------------------------------------------------------
# data.js read/parse + write
# ---------------------------------------------------------------------------
def _extract_var(js_text: str, var_name: str) -> Any:
    """Parse `var X = <json>;` out of a data.js file."""
    pattern = rf"var\s+{re.escape(var_name)}\s*=\s*"
    match = re.search(pattern, js_text)
    if not match:
        raise ValueError(f"Variable {var_name!r} not found in data.js")
    start = match.end()
    # Find matching terminator: we wrote with json.dumps, single-line,
    # terminated by ";\n". Find next ";\n" or ";" at end.
    depth = 0
    in_string = False
    escape = False
    i = start
    while i < len(js_text):
        ch = js_text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    # scan to ; or end
                    end = i + 1
                    return json.loads(js_text[start:end])
        i += 1
    raise ValueError(f"Malformed data.js, couldn't parse {var_name}")


def parse_existing_data_js(path: Path = DATA_JS_PATH) -> dict:
    """Load the current data.js into Python dicts/lists for merging."""
    if not path.exists():
        return {
            "D": {},  # {clinic_name: [[yr, pidx, rev, ep_days], ...]}
            "P": [],  # product names
            "C": [],  # sorted clinic names
            "T": [],  # top 30
            "KA": [],  # clinic metadata parallel to C
            "REPS": [],  # rep names
        }
    txt = path.read_text(encoding="utf-8")
    return {
        "D": _extract_var(txt, "D"),
        "P": _extract_var(txt, "P"),
        "C": _extract_var(txt, "C"),
        "T": _extract_var(txt, "T"),
        "KA": _extract_var(txt, "KA"),
        "REPS": _extract_var(txt, "REPS"),
    }


def write_data_js(state: dict, path: Path = DATA_JS_PATH, note: str = "") -> None:
    def jdump(obj):
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    txns = sum(len(v) for v in state["D"].values())
    header_lines = [
        f"// data.js — auto-generated at {ts}",
        f"// {len(state['C'])} clinics, {len(state['P'])} products, {txns} transactions",
    ]
    if note:
        header_lines.append(f"// {note}")
    parts = header_lines + [
        f"var D={jdump(state['D'])};",
        f"var P={jdump(state['P'])};",
        f"var C={jdump(state['C'])};",
        f"var T={jdump(state['T'])};",
        f"var KA={jdump(state['KA'])};",
        f"var REPS={jdump(state['REPS'])};",
        "",
    ]
    path.write_text("\n".join(parts), encoding="utf-8")
    size_kb = path.stat().st_size / 1024
    print(f"  ✓ Wrote {path} ({size_kb:.1f} KB)", flush=True)


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------
def process_invoices_into_state(
    state: dict,
    invoices_full: list[dict],
    contacts: dict[str, dict],
    *,
    replace_clinic: bool = False,
) -> dict:
    """
    Fold a batch of detailed invoices into the state dict.

    replace_clinic=True:  for each clinic touched in this batch, drop all
                          of its existing transactions first (used by the
                          weekly full rebuild, and by incremental when we
                          want to replace a day's worth of invoices).
    replace_clinic=False: append transactions to existing clinic bucket.
                          NOTE: this can cause duplicates if the same
                          invoice is processed twice, so the incremental
                          script uses True to be safe.
    """
    D = state["D"]
    products = state["P"]
    product_set = {p: i for i, p in enumerate(products)}

    reps = state["REPS"]
    rep_set = {r: i for i, r in enumerate(reps)}

    def get_product_idx(name: str) -> int:
        if name not in product_set:
            product_set[name] = len(products)
            products.append(name)
        return product_set[name]

    def get_rep_idx(name: str) -> int:
        name = (name or "").strip() or "Unassigned"
        if name not in rep_set:
            rep_set[name] = len(reps)
            reps.append(name)
        return rep_set[name]

    # Build a contact-by-name map for KA lookup
    contacts_by_name: dict[str, dict] = {}
    for c in contacts.values():
        nm = (c.get("contact_name") or "").strip()
        if nm:
            contacts_by_name[nm] = c

    # Group invoices by clinic for cleaner processing
    invoices_by_clinic: dict[str, list[dict]] = {}
    for inv in invoices_full:
        clinic = (inv.get("customer_name") or "").strip()
        if not clinic or should_skip_clinic(clinic):
            continue
        invoices_by_clinic.setdefault(clinic, []).append(inv)

    # Track the latest order date per clinic (for KA last_order field)
    latest_order_by_clinic: dict[str, str] = {}

    for clinic, invs in invoices_by_clinic.items():
        if replace_clinic:
            D[clinic] = []
        bucket = D.setdefault(clinic, [])

        for inv in invs:
            inv_date = inv.get("date")
            if not inv_date:
                continue
            try:
                ep = epoch_days(inv_date)
                yr = year_code(int(inv_date[:4]))
            except Exception:
                continue
            if ep < 0:
                continue

            # Track latest invoice date
            prev = latest_order_by_clinic.get(clinic, "")
            if inv_date > prev:
                latest_order_by_clinic[clinic] = inv_date

            for li in inv.get("line_items", []) or []:
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

    # Rebuild C, KA for touched clinics (and optionally all clinics)
    # For simplicity + correctness: rebuild C and KA from scratch every time.
    # KA needs contact data; for clinics without fresh contact data, we keep
    # the prior KA row.
    old_C = state["C"]
    old_KA = state["KA"]
    old_KA_by_clinic = {old_C[i]: old_KA[i] for i in range(len(old_C))}

    new_C = sorted(D.keys(), key=lambda s: s.lower())
    new_KA: list[list] = []
    for clinic in new_C:
        c = contacts_by_name.get(clinic)
        # Compute last order from merged data (D) if we have transactions
        bucket = D.get(clinic, [])
        last_order = latest_order_by_clinic.get(clinic, "")
        if not last_order and bucket:
            # derive from existing transactions: max epoch_days
            max_ep = max(t[3] for t in bucket)
            last_order = (EPOCH_BASE + _timedelta_days(max_ep)).isoformat()

        if c:
            rep_idx = get_rep_idx(extract_rep_name(c))
            email = (c.get("email") or "").strip()
            phone = normalize_phone(c.get("phone") or c.get("mobile"))
            doctor = derive_doctor(c)
            street, csz = format_address(c)
            new_KA.append([rep_idx, email, phone, doctor, street, csz, last_order])
        elif clinic in old_KA_by_clinic:
            # No fresh contact data for this clinic — preserve old row,
            # but update last_order_date
            row = list(old_KA_by_clinic[clinic])
            if len(row) == 7 and last_order:
                row[6] = last_order
            new_KA.append(row)
        else:
            # Brand new clinic, no contact data fetched — placeholder
            rep_idx = get_rep_idx("")
            new_KA.append([rep_idx, "", "", "", "", "", last_order])

    # Recompute top 30
    product_totals: dict[int, float] = {}
    for bucket in D.values():
        for _, pidx, rev, _ in bucket:
            product_totals[pidx] = product_totals.get(pidx, 0.0) + rev
    top_sorted = sorted(product_totals.items(), key=lambda kv: -kv[1])[:30]
    T = [[pidx, products[pidx], round(rev, 2)] for pidx, rev in top_sorted]

    state["D"] = D
    state["P"] = products
    state["C"] = new_C
    state["KA"] = new_KA
    state["REPS"] = reps
    state["T"] = T
    return state


def _timedelta_days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


# ---------------------------------------------------------------------------
# Sync state file
# ---------------------------------------------------------------------------
def read_last_sync() -> dict:
    if not LAST_SYNC_PATH.exists():
        return {}
    try:
        return json.loads(LAST_SYNC_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def write_last_sync(payload: dict) -> None:
    LAST_SYNC_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def utc_now_iso() -> str:
    """ISO-8601 for Zoho's last_modified_time_start param."""
    # Zoho wants format like: 2024-05-01T00:00:00+0000
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def zoho_format_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+0000")


def parse_iso(s: str) -> datetime:
    # Handle both +0000 and +00:00
    s = s.replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized ISO timestamp: {s}")
