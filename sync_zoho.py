#!/usr/bin/env python3
"""
PSTOOL — Zoho Invoice nightly sync (MERGE mode)
====================================================
CRITICAL BEHAVIOR:
  - Incremental (default): pulls last 3 days from Zoho, MERGES into existing data.js
  - Full (FULL_SYNC=1): pulls from 2024-01-01, MERGES into existing data.js
  - NEVER overwrites historical data — only ADDS new rows and updates last order dates
  - If Zoho returns 0 lines, aborts without touching data.js

Requires env vars:
  ZOHO_CLIENT_ID
  ZOHO_CLIENT_SECRET
  ZOHO_REFRESH_TOKEN
  ZOHO_ORG_ID  (optional, defaults to 691451730)
"""

import os, sys, json, time, re
from datetime import datetime, date, timedelta
from collections import defaultdict
import urllib.request, urllib.parse, urllib.error

# ── Config ─────────────────────────────────────────────────────
CLIENT_ID     = os.environ['ZOHO_CLIENT_ID']
CLIENT_SECRET = os.environ['ZOHO_CLIENT_SECRET']
REFRESH_TOKEN = os.environ['ZOHO_REFRESH_TOKEN']
ORG_ID        = os.environ.get('ZOHO_ORG_ID', '691451730')
API_BASE      = 'https://www.zohoapis.com/invoice/v3'
EP0           = date(2024, 1, 1)

def epoch_day(d):
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return (d - EP0).days

# ── Token ──────────────────────────────────────────────────────
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
    print(f'[auth] token ok, expires in {data.get("expires_in")}s')
    return data['access_token']

# ── Zoho paginated GET ─────────────────────────────────────────
def zoho_get(token, path, params=None):
    headers = {
        'Authorization': f'Zoho-oauthtoken {token}',
        'X-com-zoho-invoice-organizationid': ORG_ID,
    }
    results = []
    page = 1
    while True:
        p = dict(params or {})
        p['page'] = page
        p['per_page'] = 200
        url = f'{API_BASE}{path}?' + urllib.parse.urlencode(p)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f'HTTP {e.code} on {path}: {body[:300]}')
        payload = (data.get('invoices') or data.get('invoice') or [])
        if isinstance(payload, dict):
            payload = [payload]
        results.extend(payload)
        if not data.get('page_context', {}).get('has_more_page', False):
            break
        page += 1
        time.sleep(0.25)
    return results

# ── Fetch new line items from Zoho ─────────────────────────────
def fetch_new_lines(token, start_date, end_date):
    all_lines = []
    new_refs  = []   # [{'customer':..., 'date':...}] for invoices whose reference contains NEW
    chunk_start = start_date
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=90), end_date)
        params = {
            'date_start':  chunk_start.strftime('%Y-%m-%d'),
            'date_end':    chunk_end.strftime('%Y-%m-%d'),
            'sort_column': 'date',
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
            try:
                inv_total = float(inv.get('total') or 0)
            except (TypeError, ValueError):
                inv_total = 0.0
            ref = (inv.get('reference_number') or '').strip()
            salesperson = (inv.get('salesperson_name') or '').strip()
            line_items = inv.get('line_items', [])
            if not line_items:
                try:
                    detail = zoho_get(token, f'/invoices/{inv_id}')
                    if detail:
                        det = detail[0] if isinstance(detail, list) else detail
                        line_items = det.get('line_items', [])
                        try:
                            inv_total = float(det.get('total') or inv_total)
                        except (TypeError, ValueError):
                            pass
                        if not ref:
                            ref = (det.get('reference_number') or '').strip()
                        if not salesperson:
                            salesperson = (det.get('salesperson_name') or '').strip()
                except Exception as e:
                    print(f'  [warn] detail fetch failed for {inv_id}: {e}')
                    continue
            if 'NEW' in ref.upper():
                new_refs.append({'customer': customer, 'date': inv_date_str})
            line_sum = 0.0
            for li in line_items:
                name  = (li.get('name') or li.get('item_name') or '').strip()
                qty   = float(li.get('quantity', 0) or 0)
                total = float(li.get('item_total') or li.get('line_total') or 0)
                if not name or total <= 0:
                    continue
                line_sum += total
                all_lines.append({
                    'date':     inv_date_str,
                    'customer': customer,
                    'item':     name,
                    'qty':      qty,
                    'total':    total,
                    'rep':      salesperson,
                })
            ship_adj = round(inv_total - line_sum, 2)
            if inv_total > 0 and abs(ship_adj) >= 0.01:
                all_lines.append({
                    'date':     inv_date_str,
                    'customer': customer,
                    'item':     '~Shipping & Adjustments',
                    'qty':      0,
                    'total':    ship_adj,
                    'rep':      salesperson,
                })
        chunk_start = chunk_end + timedelta(days=1)

    print(f'[fetch] total new line items: {len(all_lines)} | NEW-ref invoices: {len(new_refs)}')
    return all_lines, new_refs

# ── Load existing data.js into Python structures ───────────────
def load_existing_data(path='data.js'):
    """
    Returns (C, D, KA, REPS, P, T) from existing data.js.
    D values are lists of lists (mutable).
    Returns None if file doesn't exist or can't be parsed.
    """
    if not os.path.exists(path):
        print(f'[load] {path} not found — will build from scratch')
        return None

    print(f'[load] reading existing {path}...')
    with open(path, 'r', encoding='utf-8') as f:
        js = f.read()

    def extract(var):
        # Match var X = <value>;  where value can be [...] or {...}
        m = re.search(r'var\s+' + var + r'\s*=\s*(\{[\s\S]*?\}|\[[\s\S]*?\]);', js)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except Exception as e:
            print(f'[load] JSON parse error for {var}: {e}')
            return None

    C    = extract('C')
    D    = extract('D')
    KA   = extract('KA')
    REPS = extract('REPS')
    P    = extract('P')
    T    = extract('T')
    NC   = extract('NC')

    if C is None or D is None or P is None:
        print('[load] could not parse required arrays — will build from scratch')
        return None

    # Convert D values to lists of lists (they may be lists of lists already)
    for k in D:
        D[k] = [list(row) for row in D[k]]

    print(f'[load] loaded {len(C)} clinics, {len(P)} products from existing data.js')
    return C, D, KA or [], REPS or [], P, T or [], NC or {}

# ── Merge new lines into existing data structures ──────────────
def merge_new_lines(existing, new_lines, new_refs=None):
    """
    Takes existing (C, D, KA, REPS, P, T) and new_lines list.
    Returns updated (C, D, KA, REPS, P, T) with new rows merged in.

    Key rules:
    - Product names are matched exactly; new products get new P[] indices
    - Existing rows are NEVER deleted
    - Duplicate rows (same clinic + product + epoch_day + revenue) are skipped
    - KA lastOrderISO is updated for any clinic that has new orders
    - New clinics get a blank KA entry
    """
    C_list, D, KA, REPS, P, T, NC = existing
    NC = dict(NC or {})
    for nr in (new_refs or []):
        try:
            ep_nr = epoch_day(date.fromisoformat(nr['date']))
        except Exception:
            continue
        cust = nr['customer']
        if cust not in NC or ep_nr < NC[cust]:
            NC[cust] = ep_nr

    # Build mutable product index
    prod_map = {name: idx for idx, name in enumerate(P)}

    def get_prod(name):
        if name not in prod_map:
            prod_map[name] = len(P)
            P.append(name)
        return prod_map[name]

    # Rep index map (extends REPS for new salespeople)
    rep_map = {name: idx for idx, name in enumerate(REPS)}
    def get_rep(name):
        name = (name or '').strip()
        if not name:
            return -1
        if name not in rep_map:
            rep_map[name] = len(REPS)
            REPS.append(name)
        return rep_map[name]

    # Build set of existing rows per clinic for dedup
    # Key: (yr_code, prod_idx, revenue, epoch_day) — qty can vary, ignore for dedup
    existing_keys = defaultdict(set)
    for clinic, rows in D.items():
        for row in rows:
            existing_keys[clinic].add((row[0], row[1], row[2], row[3]))

    # Track which clinics got new orders (to update lastOrderISO)
    updated_clinics = set()
    new_row_count = 0
    new_clinic_count = 0

    for li in new_lines:
        try:
            d = date.fromisoformat(li['date'])
        except:
            continue
        yr = d.year
        if yr < 2024:
            continue
        yr_code = yr - 2020
        ep      = epoch_day(d)
        pidx    = get_prod(li['item'])
        rev     = li['total']
        qty     = li['qty']
        customer = li['customer']

        key = (yr_code, pidx, rev, ep)
        if key in existing_keys[customer]:
            continue  # already have this row

        # New row — add it
        if customer not in D:
            D[customer] = []
            new_clinic_count += 1
        D[customer].append([yr_code, pidx, rev, ep, qty, get_rep(li.get('rep', ''))])
        existing_keys[customer].add(key)
        updated_clinics.add(customer)
        new_row_count += 1

    print(f'[merge] added {new_row_count} new rows across {len(updated_clinics)} clinics')
    print(f'[merge] {new_clinic_count} brand-new clinics added')

    # Rebuild C[] sorted
    C_new = sorted(D.keys())

    # Rebuild KA — preserve existing, add blanks for new clinics, update lastOrderISO
    old_ka_map = {name: list(KA[i]) for i, name in enumerate(C_list) if i < len(KA)}

    KA_new = []
    for clinic in C_new:
        rows = D[clinic]
        last_ep = max(r[3] for r in rows) if rows else -1
        last_iso = (EP0 + timedelta(days=last_ep)).strftime('%Y-%m-%d') if last_ep >= 0 else ''

        if clinic in old_ka_map:
            entry = old_ka_map[clinic]
            # Update lastOrderISO[6] only if this clinic had new orders
            if clinic in updated_clinics:
                while len(entry) < 7:
                    entry.append('')
                entry[6] = last_iso
            KA_new.append(entry)
        else:
            # New clinic — blank contact info
            KA_new.append([-1, '', '', '', '', '', last_iso])

    # Recompute T[] — top 30 products by 2025+2026 revenue
    prod_rev = defaultdict(float)
    for rows in D.values():
        for r in rows:
            if r[0] >= 5:  # 2025 or 2026
                prod_rev[r[1]] += r[2]
    top30 = sorted(prod_rev.items(), key=lambda x: -x[1])[:30]
    T_new = [[pidx, 0, rev] for pidx, rev in top30]

    return C_new, D, KA_new, REPS, P, T_new, NC

# ── Write data.js ──────────────────────────────────────────────
def write_data_js(C, D, KA, REPS, P, T, NC=None, out_path='data.js'):
    NC = NC or {}
    now = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

    d_parts = []
    for clinic in C:
        rows = sorted(D[clinic], key=lambda r: r[3])
        encoded = json.dumps(rows, separators=(',', ':'))
        clinic_esc = clinic.replace('\\', '\\\\').replace('"', '\\"')
        d_parts.append(f'"{clinic_esc}":{encoded}')

    js = (
        f'// data.js — auto-generated by sync_zoho.py\n'
        f'// Last sync: {now}\n'
        f'// Source: Zoho Invoice (org {ORG_ID})\n'
        f'// Clinics: {len(C)} | Products: {len(P)}\n\n'
        f'var C={json.dumps(C, separators=(",", ":"))};' + '\n'
        f'var D={{{",".join(d_parts)}}};' + '\n'
        f'var KA={json.dumps(KA, separators=(",", ":"))};' + '\n'
        f'var REPS={json.dumps(REPS, separators=(",", ":"))};' + '\n'
        f'var P={json.dumps(P, separators=(",", ":"))};' + '\n'
        f'var T={json.dumps(T, separators=(",", ":"))};' + '\n'
        f'var NC={json.dumps(NC, separators=(",", ":"))};' + '\n'
    )

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(js)

    size_kb = os.path.getsize(out_path) // 1024
    print(f'[write] {out_path} — {len(C)} clinics, {len(P)} products, {size_kb}KB')

# ── Main ───────────────────────────────────────────────────────
def main():
    print(f'[start] PSTOOL Zoho sync (MERGE mode) — {datetime.utcnow().isoformat()}')

    full_sync = os.environ.get('FULL_SYNC', '0') == '1'
    today     = date.today()
    start     = date(2024, 1, 1) if full_sync else today - timedelta(days=3)
    end       = today

    print(f'[mode] {"FULL (2024-01-01 → today)" if full_sync else f"incremental ({start} → {end})"}')

    # Step 1: Load existing data.js
    existing = load_existing_data('data.js')

    # Step 2: Fetch new lines from Zoho
    token     = get_access_token()
    new_lines, new_refs = fetch_new_lines(token, start, end)

    if not new_lines:
        if existing is None:
            print('[error] no existing data and no new lines — aborting')
            sys.exit(1)
        else:
            print('[info] no new lines from Zoho — data.js unchanged, exiting cleanly')
            sys.exit(0)

    # Step 3: If no existing data, build from scratch
    if existing is None:
        print('[build] building from scratch (no existing data.js)')
        prod_map = {}
        P = []
        def get_prod(name):
            if name not in prod_map:
                prod_map[name] = len(P)
                P.append(name)
            return prod_map[name]
        clinic_rows = defaultdict(list)
        for li in new_lines:
            try:
                d = date.fromisoformat(li['date'])
            except:
                continue
            if d.year < 2024:
                continue
            yr_code = d.year - 2020
            ep = epoch_day(d)
            pidx = get_prod(li['item'])
            clinic_rows[li['customer']].append([yr_code, pidx, li['total'], ep, li['qty']])  # rep stamped only in merge path
        C = sorted(clinic_rows.keys())
        D = dict(clinic_rows)
        KA = [[-1, '', '', '', '', '', (EP0 + timedelta(days=max(r[3] for r in D[c]))).strftime('%Y-%m-%d')] for c in C]
        REPS = []
        prod_rev = defaultdict(float)
        for rows in D.values():
            for r in rows:
                if r[0] >= 5:
                    prod_rev[r[1]] += r[2]
        top30 = sorted(prod_rev.items(), key=lambda x: -x[1])[:30]
        T = [[pidx, 0, rev] for pidx, rev in top30]
        NC = {}
        for nr in new_refs:
            try:
                ep_nr = epoch_day(date.fromisoformat(nr['date']))
            except Exception:
                continue
            if nr['customer'] not in NC or ep_nr < NC[nr['customer']]:
                NC[nr['customer']] = ep_nr
    else:
        # Step 4: Merge new lines into existing data
        C, D, KA, REPS, P, T, NC = merge_new_lines(existing, new_lines, new_refs)

    # Step 5: Write merged data.js
    write_data_js(C, D, KA, REPS, P, T, NC, 'data.js')
    print(f'[done] sync complete')

if __name__ == '__main__':
    main()
