#!/usr/bin/env python3
"""
sync_backfill_fast.py — one-shot backfill of shipping/adjustments + NEW refs.

Instead of fetching 73k invoice details, this fetches only the invoice LIST
(~730 paged calls for 2024 -> today). For each clinic+day it compares the sum
of invoice totals against the product line revenue already in data.js; the
difference is written as a single '~Shipping & Adjustments' row. Reference
numbers containing NEW populate NC.

Idempotent: existing ship rows are replaced, not stacked. Run once, done.
"""
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from sync_zoho import (
    load_existing_data,
    write_data_js,
    get_access_token,
    zoho_get,
    epoch_day,
    EP0,
)

SHIP_NAME = '~Shipping & Adjustments'
START = date(2024, 1, 1)


def fetch_invoice_headers(token):
    """Paged invoice list 2024 -> today. Returns list of
    {'customer','date','total','ref'} — no detail calls."""
    headers = []
    chunk_start = START
    today = date.today()
    while chunk_start <= today:
        chunk_end = min(chunk_start + timedelta(days=90), today)
        params = {
            'date_start': chunk_start.strftime('%Y-%m-%d'),
            'date_end': chunk_end.strftime('%Y-%m-%d'),
            'sort_column': 'date',
        }
        print(f'  chunk {chunk_start} -> {chunk_end}', end=' ', flush=True)
        invoices = zoho_get(token, '/invoices', params)
        print(f'({len(invoices)} invoices)', flush=True)
        for inv in invoices:
            cust = (inv.get('customer_name') or '').strip()
            d = inv.get('date', '')
            if not cust or not d:
                continue
            try:
                total = float(inv.get('total') or 0)
            except (TypeError, ValueError):
                total = 0.0
            headers.append({
                'customer': cust,
                'date': d,
                'total': total,
                'ref': (inv.get('reference_number') or '').strip(),
                'rep': (inv.get('salesperson_name') or '').strip(),
            })
        chunk_start = chunk_end + timedelta(days=1)
    print(f'[fetch] {len(headers)} invoice headers')
    return headers


def main():
    existing = load_existing_data('data.js')
    if existing is None:
        print('[error] data.js missing — aborting')
        sys.exit(1)
    C, D, KA, REPS, P, T, NC = existing
    NC = dict(NC or {})

    token = get_access_token()
    headers = fetch_invoice_headers(token)

    # aggregate invoice totals by clinic+epoch day; collect NEW refs;
    # pick the rep of the largest invoice per clinic+day
    inv_totals = defaultdict(float)
    day_rep = {}
    day_rep_amt = {}
    rep_map = {name: idx for idx, name in enumerate(REPS)}
    def rep_idx(name):
        name = (name or '').strip()
        if not name:
            return -1
        if name not in rep_map:
            rep_map[name] = len(REPS)
            REPS.append(name)
        return rep_map[name]
    for h in headers:
        try:
            ep = epoch_day(date.fromisoformat(h['date']))
        except Exception:
            continue
        if ep < 0:
            continue
        key = (h['customer'], ep)
        inv_totals[key] += h['total']
        if h['rep'] and h['total'] >= day_rep_amt.get(key, -1):
            day_rep[key] = rep_idx(h['rep'])
            day_rep_amt[key] = h['total']
        if 'NEW' in h['ref'].upper():
            if h['customer'] not in NC or ep < NC[h['customer']]:
                NC[h['customer']] = ep

    # product index for ship rows
    prod_map = {name: idx for idx, name in enumerate(P)}
    if SHIP_NAME in prod_map:
        ship_idx = prod_map[SHIP_NAME]
    else:
        ship_idx = len(P)
        P.append(SHIP_NAME)

    # per clinic: strip old ship rows, sum product lines by day, write diffs
    added = 0
    total_ship = 0.0
    for clinic in list(D.keys()):
        rows = [r for r in D[clinic] if r[1] != ship_idx]
        line_sum_by_ep = defaultdict(float)
        yr_by_ep = {}
        for r in rows:
            line_sum_by_ep[r[3]] += r[2]
            yr_by_ep[r[3]] = r[0]
        # stamp rep-at-invoice-time onto every row (6th element)
        for r in rows:
            ri = day_rep.get((clinic, r[3]), -1)
            if len(r) >= 6:
                r[5] = ri
            else:
                while len(r) < 5:
                    r.append(0)
                r.append(ri)
        for ep, lsum in line_sum_by_ep.items():
            inv_total = inv_totals.get((clinic, ep))
            if inv_total is None or inv_total <= 0:
                continue
            diff = round(inv_total - lsum, 2)
            if abs(diff) >= 0.01:
                rows.append([yr_by_ep[ep], ship_idx, diff, ep, 0, day_rep.get((clinic, ep), -1)])
                added += 1
                total_ship += diff
        rows.sort(key=lambda r: r[3])
        D[clinic] = rows

    print(f'[backfill] added {added} ship/adj rows, net ${total_ship:,.2f}')
    print(f'[backfill] NC now tracks {len(NC)} NEW-ref clinics')

    write_data_js(C, D, KA, REPS, P, T, NC, 'data.js')
    print('[backfill-done]')


if __name__ == '__main__':
    main()
