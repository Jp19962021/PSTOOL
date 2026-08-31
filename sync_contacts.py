#!/usr/bin/env python3
"""
sync_contacts.py — refresh clinic contact info (rep, email, phone, doctor,
address) in data.js from Zoho contacts.

Pages ALL customer contacts (~38 calls for ~7,500 clinics — cheap), matches
by clinic name, and updates KA entries. Fills blanks and refreshes stale
values; preserves lastOrderISO. Run nightly after the invoice sync, or
manually as a catch-up.
"""
from zoho_lib import (
    ZohoClient,
    fetch_all_contacts,
    parse_existing_data_js,
    write_data_js,
    extract_rep_name,
    normalize_phone,
    derive_doctor,
    format_address,
)


def main():
    state = parse_existing_data_js()
    if not state.get('C'):
        print('[error] data.js missing or empty — run invoice sync first')
        return

    client = ZohoClient()
    contacts = fetch_all_contacts(client)

    by_name = {}
    for c in contacts.values():
        nm = (c.get('contact_name') or '').strip()
        if nm:
            by_name[nm] = c

    reps = state['REPS']
    rep_map = {name: idx for idx, name in enumerate(reps)}

    def get_rep_idx(name):
        name = (name or '').strip()
        if not name:
            return -1
        if name not in rep_map:
            rep_map[name] = len(reps)
            reps.append(name)
        return rep_map[name]

    C = state['C']
    KA = state['KA']
    while len(KA) < len(C):
        KA.append([-1, '', '', '', '', '', ''])

    filled = 0
    refreshed = 0
    unmatched = 0
    for i, clinic in enumerate(C):
        c = by_name.get(clinic)
        if not c:
            unmatched += 1
            continue
        row = list(KA[i])
        while len(row) < 7:
            row.append('')
        was_blank = (row[0] == -1 and not row[1] and not row[2])

        rep_idx = get_rep_idx(extract_rep_name(c))
        email = (c.get('email') or '').strip()
        phone = normalize_phone(c.get('phone') or c.get('mobile'))
        doctor = derive_doctor(c)
        street, csz = format_address(c)

        changed = False
        if rep_idx != -1 and row[0] != rep_idx:
            row[0] = rep_idx; changed = True
        for idx, val in ((1, email), (2, phone), (3, doctor), (4, street), (5, csz)):
            if val and row[idx] != val:
                row[idx] = val; changed = True

        if changed:
            KA[i] = row
            if was_blank:
                filled += 1
            else:
                refreshed += 1

    state['KA'] = KA
    state['REPS'] = reps
    print(f'[contacts] filled {filled} blank clinics, refreshed {refreshed}, '
          f'{unmatched} names not found in Zoho contacts')
    write_data_js(state, note=f'contact refresh: +{filled} filled, {refreshed} updated')


if __name__ == '__main__':
    main()
