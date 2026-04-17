#!/usr/bin/env python3
"""
Full rebuild: pulls every invoice since EARLIEST_YEAR, regenerates data.js
from scratch. Manual-only — triggered from the Actions tab when you want
a clean reconcile.
Uses concurrent detail-fetching to keep runtime manageable for large invoice
counts. For 60k invoices, expect ~1-2 hours.
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoho_lib import (
    EARLIEST_YEAR,
    REQUEST_DELAY,
    ZohoClient,
    fetch_all_contacts,
    fetch_invoice_detail,
    fetch_invoice_list,
    process_invoices_into_state,
    utc_now_iso,
    write_data_js,
    write_last_sync,
)

# FIX D: 6 → 3 concurrent threads.
# At REQUEST_DELAY=0.65s per detail call, 3 threads = ~4.6 calls/s = 276/min.
# Still fast, still well under 300 concurrent connections, and the delay inside
# each thread (post-call sleep) gives the org-wide bucket room to breathe.
CONCURRENCY = 3

# Pause between submitting each batch of futures to avoid spiking at launch.
BATCH_PAUSE = 0.2


def fetch_detail_safe(client, inv_id, inv_number):
    try:
        result = fetch_invoice_detail(client, inv_id)
        time.sleep(REQUEST_DELAY)  # per-thread pacing
        return result
    except Exception as e:
        print(f"  ! Failed invoice {inv_number}: {e}", flush=True)
        return None


def main() -> int:
    start = time.time()
    print("=" * 60, flush=True)
    print(f"Zoho Full Rebuild (from {EARLIEST_YEAR}-01-01)", flush=True)
    print("=" * 60, flush=True)

    run_started_iso = utc_now_iso()
    client = ZohoClient()

    # All invoices since earliest year
    invoices_list = fetch_invoice_list(client, date_start=f"{EARLIEST_YEAR}-01-01")
    print(f"  ✓ {len(invoices_list)} invoices to process", flush=True)

    # Parallel detail-fetch with per-thread pacing
    print(f"Fetching line items (concurrency={CONCURRENCY})...", flush=True)
    invoices_full = []
    completed = 0
    total = len(invoices_list)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {}
        for inv in invoices_list:
            f = executor.submit(
                fetch_detail_safe,
                client,
                inv["invoice_id"],
                inv.get("invoice_number", "?"),
            )
            futures[f] = inv
            time.sleep(BATCH_PAUSE)  # stagger submission to avoid burst

        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                invoices_full.append(result)
            completed += 1
            if completed % 500 == 0 or completed == total:
                pct = 100 * completed / total
                elapsed_min = (time.time() - start) / 60
                rate = completed / max(time.time() - start, 1)
                remaining = (total - completed) / rate if rate > 0 else 0
                print(
                    f"  {completed}/{total} ({pct:.1f}%) — "
                    f"elapsed {elapsed_min:.1f}m, {rate:.1f} req/s, "
                    f"eta {remaining/60:.1f}m",
                    flush=True,
                )

    print(f"  ✓ {len(invoices_full)} invoices detailed", flush=True)

    contacts = fetch_all_contacts(client)

    print("Building data.js from scratch...", flush=True)
    empty_state = {"D": {}, "P": [], "C": [], "T": [], "KA": [], "REPS": []}
    state = process_invoices_into_state(
        empty_state,
        invoices_full,
        contacts,
        replace_clinic=True,
    )
    write_data_js(state, note=f"full rebuild: {len(invoices_full)} invoices")
    write_last_sync({
        "completed_at": run_started_iso,
        "mode": "full",
        "invoices_processed": len(invoices_full),
    })

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.0f}s ({elapsed/60:.1f} min).", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
