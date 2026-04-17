#!/usr/bin/env python3
"""
PSTOOL — Zoho Invoice nightly sync
Pulls all invoice line items from Zoho Invoice API and rebuilds data.js

Requires env vars:
  ZOHO_CLIENT_ID
  ZOHO_CLIENT_SECRET
  ZOHO_REFRESH_TOKEN
  ZOHO_ORG_ID
"""

import os, sys, json, time, re, math
from datetime import datetime, date, timedelta
from collections import defaultdict

import urllib.request
import urllib.parse
import urllib.error

# ── Config ────────────────────────────────────────────────────
CLIENT_ID     = os.environ['ZOHO_CLIENT_ID']
CLIENT_SECRET = os.environ['ZOHO_CLIENT_SECRET']
REFRESH_TOKEN = os.environ['ZOHO_REFRESH_TOKEN']
ORG_ID        = os.environ.get('ZOHO_ORG_ID', '691451730')
API_BASE      = 'https://www.zohoapis.com/invoice/v3'
EP0           = date(2024, 1, 1)   # epoch anchor — day 0

# ── Token refresh ─────────────────────────────────────────────
def get_access_token():
    params = urllib.parse.urlencode({
        'refresh_token': REFRESH_TOKEN,
        'client_id':     CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type':    'refresh_token',
    }).encode()
    req = urllib.request.Request(
        'https://accounts.zoho.com/oauth/v2/token',
        data=params, method='POST'
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    if 'access_token' not in data:
        raise RuntimeError(f'Token refresh failed: {data}')
    print(f'[auth] got access token, expires in {data.get("expires_in")}s')
    return data['access_token']

# ── Generic paginated GET ──────────────────────────────────────
def zoho_get(token, path, params=None):
    headers = {
        'Authorization': f'Zoho-oauthtoken {token}',
        'X-com-zoho-invoice-organizationid': ORG_ID,
    }
    base = f'{API_BASE}{path}'
    results = []
    page = 1
    while True:
        p = dict(params or {})
        p['page'] = page
        p['per_page'] = 200
        url = base + '?' + urllib.parse.urlencode(p)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f'HTTP {e.code} on {path}: {body[:300]}')
        
        # Zoho wraps results in different keys depending on endpoint
        payload = (data.get('invoices') or data.get('lineitems') or
                   data.get('invoice') or [])
        if isinstance(payload, dict):
            payload = [payload]
        results.extend(payload)
        
        page_context = data.get('page_context', {})
        if not page_context.get('has_more_page', False):
            break
        page += 1
        time.sleep(0.2)   # be polite to the API
    
    return results

# ── Fetch all invoices with line items ────────────────────────
def fetch_all_line_items(token):
    """
    Fetches every SENT/PAID invoice from 2024-01-01 onward.
    Returns list of dicts: {date, customer_name, item_name, quantity, line_total}
    """
    print('[fetch] pulling invoices from 2024-01-01 ...')
    
    # Pull invoices in date-range chunks to avoid timeouts on large accounts
    all_lines = []
    start = date(2024, 1, 1)
    end   = date.today()
    
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=90), end)
        params = {
            'date_start': chunk_start.strftime('%Y-%m-%d'),
            'date_end':   chunk_end.strftime('%Y-%m-%d'),
            'status':     'paid,sent,overdue,viewed',
            'sort_column':'date',
            'sort_order': 'A_Z',
        }
        print(f'  chunk {chunk_start} → {chunk_end}', end=' ', flush=True)
        invoices = zoho_get(token, '/invoices', params)
        print(f'({len(invoices)} invoices)', flush=True)
        
        for inv in invoices:
            inv_date_str = inv.get('date', '')
            customer     = inv.get('customer_name', '').strip()
            inv_id       = inv.get('invoice_id', '')
            if not inv_date_str or not customer:
                continue
            
            # line_items may be inline or need a detail fetch
            line_items = inv.get('line_items', [])
            if not line_items:
                # Fetch detail to get line items
                try:
                    detail = zoho_get(token, f'/invoices/{inv_id}')
                    if detail:
                        line_items = detail[0].get('line_items', []) if isinstance(detail, list) else detail.get('line_items', [])
                except Exception as e:
                    print(f'  [warn] detail fetch failed for {inv_id}: {e}')
                    continue
            
            for li in line_items:
                name  = (li.get('name') or li.get('item_name') or '').strip()
                qty   = float(li.get('quantity', 0) or 0)
                total = float(li.get('item_total') or li.get('line_total') or 0)
                if not name or total <= 0:
                    continue
                all_lines.append({
                    'date':     inv_date_str,
                    'customer': customer,
                    'item':     name,
                    'qty':      qty,
                    'total':    total,
                })
        
        chunk_start = chunk_end + timedelta(days=1)
    
    print(f'[fetch] total line items: {len(all_lines)}')
    return all_lines

# ── Build data structures ──────────────────────────────────────
def epoch_day(d):
    """Days since 2024-01-01"""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return (d - EP0).days

def build_data(lines):
    """
    Returns (C, D, KA, REPS, P, T) matching existing data.js schema.
    
    D[clinicName] = [[yearCode(4/5/6), prodIdx, revenue, epochDay, qty], ...]
    KA[clinicIdx] = [repIdx, email, phone, doctor, address, city_state, lastOrderISO]
    P[idx] = productName
    REPS[idx] = repName
    T[idx] = [prodIdx, something, totalRevenue]  (top 30 products)
    C[idx] = clinicName
    """
    # Build product index
    products = {}   # name -> idx
    P = []
    def get_prod(name):
        if name not in products:
            products[name] = len(P)
            P.append(name)
        return products[name]
    
    # Aggregate: clinic -> list of row tuples
    clinic_rows = defaultdict(list)   # clinicName -> [(yr, pidx, rev, ep, qty)]
    
    today = date.today()
    
    for li in lines:
        try:
            d = date.fromisoformat(li['date'])
        except:
            continue
        yr = d.year
        if yr < 2024:
            continue
        yr_code = yr - 2020   # 2024→4, 2025→5, 2026→6
        ep = epoch_day(d)
        pidx = get_prod(li['item'])
        clinic_rows[li['customer']].append((yr_code, pidx, li['total'], ep, li['qty']))
    
    # Sort clinics alphabetically
    C = sorted(clinic_rows.keys())
    
    # Build KA — we don't have contact info from invoice API alone,
    # so populate what we can and leave contact fields blank (preserved from existing KA if merging)
    # Rep info also not in basic invoice pull — leave as -1 for now
    # (A future enhancement can pull contacts separately)
    KA = []
    REPS = []
    
    for clinic in C:
        rows = clinic_rows[clinic]
        last_ep = max(r[3] for r in rows) if rows else -1
        last_iso = (EP0 + timedelta(days=last_ep)).strftime('%Y-%m-%d') if last_ep >= 0 else ''
        # [repIdx, email, phone, doctor, address, city_state, lastOrderISO]
        KA.append([-1, '', '', '', '', '', last_iso])
    
    # D dict
    D = {clinic: clinic_rows[clinic] for clinic in C}
    
    # Top 30 products by total revenue (2025+2026)
    prod_rev = defaultdict(float)
    for rows in clinic_rows.values():
        for r in rows:
            if r[0] >= 5:   # 2025 or 2026
                prod_rev[r[1]] += r[2]
    
    top30 = sorted(prod_rev.items(), key=lambda x: -x[1])[:30]
    T = [[pidx, 0, rev] for pidx, rev in top30]
    
    return C, D, KA, REPS, P, T

# ── Merge with existing KA contact info ───────────────────────
def merge_existing_contacts(C, KA, existing_js_path='data.js'):
    """
    If an existing data.js exists, preserve contact info (email, phone, doctor,
    address, rep assignments) for clinics we already know about.
    New clinics from Zoho get blank contact fields.
    """
    if not os.path.exists(existing_js_path):
        print('[merge] no existing data.js found, skipping contact merge')
        return KA
    
    print('[merge] loading existing contact data...')
    with open(existing_js_path, 'r', encoding='utf-8') as f:
        js = f.read()
    
    # Extract existing C array
    c_match = re.search(r'var C\s*=\s*(\[.*?\]);', js, re.DOTALL)
    ka_match = re.search(r'var KA\s*=\s*(\[.*?\]);', js, re.DOTALL)
    reps_match = re.search(r'var REPS\s*=\s*(\[.*?\]);', js, re.DOTALL)
    
    if not c_match or not ka_match:
        print('[merge] could not parse existing data.js, skipping merge')
        return KA
    
    try:
        old_C    = json.loads(c_match.group(1))
        old_KA   = json.loads(ka_match.group(1))
        old_REPS = json.loads(reps_match.group(1)) if reps_match else []
    except Exception as e:
        print(f'[merge] JSON parse error: {e}, skipping merge')
        return KA
    
    # Build lookup: clinic name -> old KA entry
    old_ka_map = {}
    for i, name in enumerate(old_C):
        if i < len(old_KA):
            old_ka_map[name] = old_KA[i]
    
    # Merge: for each clinic in new C, if we have old contact info, use it
    merged = []
    for i, name in enumerate(C):
        new_entry = list(KA[i])
        if name in old_ka_map:
            old = old_ka_map[name]
            # Preserve: repIdx[0], email[1], phone[2], doctor[3], address[4], city[5]
            # Update: lastOrderISO[6] from fresh data
            new_entry[0] = old[0] if len(old) > 0 else -1
            new_entry[1] = old[1] if len(old) > 1 else ''
            new_entry[2] = old[2] if len(old) > 2 else ''
            new_entry[3] = old[3] if len(old) > 3 else ''
            new_entry[4] = old[4] if len(old) > 4 else ''
            new_entry[5] = old[5] if len(old) > 5 else ''
            # new_entry[6] stays as freshly computed last order date
        merged.append(new_entry)
    
    print(f'[merge] preserved contact info for {sum(1 for n in C if n in old_ka_map)} / {len(C)} clinics')
    return merged

# ── Serialise to data.js ───────────────────────────────────────
def write_data_js(C, D, KA, REPS, P, T, out_path='data.js'):
    now = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    
    # Compact-encode D
    d_parts = []
    for clinic in C:
        rows = D[clinic]
        # sort by epoch day
        rows_sorted = sorted(rows, key=lambda r: r[3])
        encoded = json.dumps(rows_sorted, separators=(',', ':'))
        clinic_esc = clinic.replace('\\', '\\\\').replace('"', '\\"')
        d_parts.append(f'"{clinic_esc}":{encoded}')
    d_str = '{' + ','.join(d_parts) + '}'
    
    c_str   = json.dumps(C,    separators=(',', ':'))
    ka_str  = json.dumps(KA,   separators=(',', ':'))
    reps_str= json.dumps(REPS, separators=(',', ':'))
    p_str   = json.dumps(P,    separators=(',', ':'))
    t_str   = json.dumps(T,    separators=(',', ':'))
    
    js = f"""// data.js — auto-generated by sync_zoho.py
// Last sync: {now}
// Source: Zoho Invoice (org {ORG_ID})
// Clinics: {len(C)} | Products: {len(P)}

var C={c_str};
var D={d_str};
var KA={ka_str};
var REPS={reps_str};
var P={p_str};
var T={t_str};
"""
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(js)
    
    size_kb = os.path.getsize(out_path) // 1024
    print(f'[write] {out_path} written — {len(C)} clinics, {len(P)} products, {size_kb}KB')

# ── Main ───────────────────────────────────────────────────────
def main():
    print(f'[start] PSTOOL Zoho Invoice sync — {datetime.utcnow().isoformat()}')
    
    token = get_access_token()
    lines = fetch_all_line_items(token)
    
    if not lines:
        print('[error] no line items returned — aborting to avoid overwriting good data')
        sys.exit(1)
    
    C, D, KA, REPS, P, T = build_data(lines)
    
    # Merge existing contact/rep info so we don't lose it
    KA = merge_existing_contacts(C, KA, existing_js_path='data.js')
    
    write_data_js(C, D, KA, REPS, P, T, out_path='data.js')
    
    print(f'[done] sync complete — {len(lines)} line items → {len(C)} clinics')

if __name__ == '__main__':
    main()
