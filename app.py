import os
import hmac
import hashlib
import base64
import logging
from datetime import datetime
from flask import Flask, request, jsonify
import requests

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config from environment variables ────────────────────────────────────────
AIRTABLE_TOKEN    = os.environ["AIRTABLE_TOKEN"]
AIRTABLE_BASE_ID  = os.environ["AIRTABLE_BASE_ID"]
SHOPIFY_SECRET    = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")

AT_BASE = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
AT_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type":  "application/json",
}

TABLE_CUSTOMERS   = "Customers"
TABLE_INVENTORIES = "French Inventories"
TABLE_LEADS       = "Lead table"

print("=" * 60)
print("[STARTUP] Shopify → Airtable Abandoned Cart Service")
print(f"[STARTUP] Airtable Base ID : {AIRTABLE_BASE_ID}")
print(f"[STARTUP] Customers table  : {TABLE_CUSTOMERS}")
print(f"[STARTUP] Inventories table: {TABLE_INVENTORIES}")
print(f"[STARTUP] Leads table      : {TABLE_LEADS}")
print(f"[STARTUP] Webhook secret   : {'SET' if SHOPIFY_SECRET else 'NOT SET (verification skipped)'}")
print("=" * 60)


# ══════════════════════════════════════════════════════════════════════════════
# Shopify webhook verification
# ══════════════════════════════════════════════════════════════════════════════
def verify_webhook(raw_body: bytes, hmac_header: str) -> bool:
    if not SHOPIFY_SECRET:
        print("[WEBHOOK] No secret configured — skipping HMAC verification")
        return True
    digest = hmac.new(SHOPIFY_SECRET.encode(), raw_body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode()
    result = hmac.compare_digest(computed, hmac_header or "")
    print(f"[WEBHOOK] HMAC verification: {'PASSED' if result else 'FAILED'}")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Airtable helpers
# ══════════════════════════════════════════════════════════════════════════════

def at_get(table: str, formula: str) -> list:
    url = f"{AT_BASE}/{requests.utils.quote(table)}"
    print(f"[AIRTABLE GET] Table: {table}")
    print(f"[AIRTABLE GET] Formula: {formula}")
    resp = requests.get(url, headers=AT_HEADERS, params={"filterByFormula": formula})
    print(f"[AIRTABLE GET] Status: {resp.status_code}")
    resp.raise_for_status()
    records = resp.json().get("records", [])
    print(f"[AIRTABLE GET] Records found: {len(records)}")
    return records


def at_create(table: str, fields: dict) -> dict:
    url = f"{AT_BASE}/{requests.utils.quote(table)}"
    print(f"[AIRTABLE CREATE] Table: {table}")
    print(f"[AIRTABLE CREATE] Fields: {fields}")
    resp = requests.post(url, headers=AT_HEADERS, json={"fields": fields})
    print(f"[AIRTABLE CREATE] Status: {resp.status_code}")
    if not resp.ok:
        print(f"[AIRTABLE CREATE] Error: {resp.text}")
    resp.raise_for_status()
    record = resp.json()
    print(f"[AIRTABLE CREATE] Created record ID: {record.get('id')}")
    return record


# ══════════════════════════════════════════════════════════════════════════════
# Business logic
# ══════════════════════════════════════════════════════════════════════════════

def find_customer(phone: str, email: str) -> dict | None:
    """
    Name/Mobile/Mail is a FORMULA field (read-only):
      CONCATENATE({Customer Name},"/",{Contact Number},"/",{Mail id})
    Search by actual underlying fields: Contact Number and Mail id.
    """
    print(f"\n[CUSTOMER SEARCH] phone={phone!r}  email={email!r}")
    parts = []
    if phone:
        parts.append(f"{{Contact Number}}='{phone}'")
        stripped = phone.lstrip("+")
        if stripped != phone:
            parts.append(f"{{Contact Number}}='{stripped}'")
        if len(phone) >= 9:
            parts.append(f"RIGHT(SUBSTITUTE({{Contact Number}},' ',''),9)='{phone[-9:]}'")
    if email:
        parts.append(f"LOWER({{Mail id}})='{email.lower()}'")

    if not parts:
        print("[CUSTOMER SEARCH] No contact info — cannot search")
        return None

    formula = f"OR({','.join(parts)})"
    records = at_get(TABLE_CUSTOMERS, formula)

    if records:
        rec = records[0]
        print(f"[CUSTOMER SEARCH] Found: ID={rec['id']}  Name={rec.get('fields',{}).get('Customer Name')}")
        return rec
    print("[CUSTOMER SEARCH] Not found")
    return None


def create_customer(name: str, phone: str, email: str) -> dict:
    """
    Write only editable fields. Name/Mobile/Mail auto-computes.
    """
    print(f"\n[CUSTOMER CREATE] name={name!r}  phone={phone!r}  email={email!r}")
    fields: dict = {}
    if name:  fields["Customer Name"]  = name
    if phone: fields["Contact Number"] = phone
    if email: fields["Mail id"]        = email
    record = at_create(TABLE_CUSTOMERS, fields)
    print(f"[CUSTOMER CREATE] New customer ID: {record.get('id')}")
    return record


def find_product_by_sku(sku: str) -> dict | None:
    print(f"\n[PRODUCT SEARCH] SKU={sku!r}")
    if not sku:
        print("[PRODUCT SEARCH] Empty SKU — skip")
        return None
    records = at_get(TABLE_INVENTORIES, f"{{SKU}}='{sku}'")
    if records:
        rec = records[0]
        print(f"[PRODUCT SEARCH] Found: ID={rec['id']}  Name={rec.get('fields',{}).get('Product Name','?')}")
        return rec
    print(f"[PRODUCT SEARCH] SKU '{sku}' NOT found in {TABLE_INVENTORIES}")
    return None


def create_lead(customer_id: str, product_ids: list[str], abandoned_date: str) -> dict:
    print(f"\n[LEAD CREATE] customer_id={customer_id}")
    print(f"[LEAD CREATE] product_ids={product_ids}")
    print(f"[LEAD CREATE] date={abandoned_date}")
    fields = {
        "Customers":           [{"id": customer_id}],
        "Interested products": [{"id": pid} for pid in product_ids],
        "Lead created date":   abandoned_date,
        "Lead Source":         "Abandoned cart",
    }
    record = at_create(TABLE_LEADS, fields)
    print(f"[LEAD CREATE] Lead ID: {record.get('id')}")
    return record


# ══════════════════════════════════════════════════════════════════════════════
# Webhook route
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/webhook/abandoned-checkout", methods=["POST"])
def abandoned_checkout():
    print("\n" + "=" * 60)
    print(f"[WEBHOOK] Received at {datetime.utcnow().isoformat()}Z")

    # Verify signature
    if not verify_webhook(request.data, request.headers.get("X-Shopify-Hmac-SHA256", "")):
        print("[WEBHOOK] Rejected — HMAC mismatch")
        return jsonify({"error": "Unauthorized"}), 401

    checkout = request.get_json(force=True)
    if not checkout:
        # Skip if checkout was already completed (became an order)
        if checkout.get("completed_at"):
            print(f"[WEBHOOK] Checkout {checkout.get('id')} already completed — skipping")
            return jsonify({"skipped": "checkout already completed"}), 200
        print("[WEBHOOK] No JSON payload")
        return jsonify({"error": "No payload"}), 400  

    print(f"[WEBHOOK] Checkout ID   : {checkout.get('id')}")
    print(f"[WEBHOOK] Checkout token: {checkout.get('token', 'N/A')}")

    # Extract customer
    cust    = checkout.get("customer") or {}
    billing = checkout.get("billing_address") or {}
    first = (cust.get("first_name") or billing.get("first_name") or "").strip()
    last  = (cust.get("last_name")  or billing.get("last_name")  or "").strip()
    name  = f"{first} {last}".strip() or billing.get("name", "Unknown")
    email = (cust.get("email") or checkout.get("email") or "").strip().lower()
    phone = (cust.get("phone") or billing.get("phone") or checkout.get("phone") or "").strip()

    print(f"[EXTRACT] Name : {name!r}")
    print(f"[EXTRACT] Email: {email!r}")
    print(f"[EXTRACT] Phone: {phone!r}")

    if not email and not phone:
        print("[EXTRACT] No contact info — skipping")
        return jsonify({"skipped": "no contact info"}), 200

    # Parse date
    raw_date = checkout.get("created_at", "")
    try:
        abandoned_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        abandoned_date = datetime.utcnow().strftime("%Y-%m-%d")
    print(f"[EXTRACT] Abandoned date: {abandoned_date}")

    # Line items
    line_items = checkout.get("line_items", [])
    print(f"[EXTRACT] Line items ({len(line_items)}):")
    for i, item in enumerate(line_items, 1):
        print(f"  [{i}] title={item.get('title')!r}  sku={item.get('sku')!r}  qty={item.get('quantity')}")

    # STEP 1 — Find or create customer
    print("\n[STEP 1] Customer lookup...")
    customer_record = find_customer(phone, email)
    if customer_record:
        print(f"[STEP 1] Existing customer: {customer_record['id']}")
    else:
        print("[STEP 1] Not found — creating new customer")
        customer_record = create_customer(name, phone, email)
        print(f"[STEP 1] New customer: {customer_record['id']}")
    customer_id = customer_record["id"]

    # STEP 2 — Match SKUs
    print("\n[STEP 2] SKU matching...")
    product_ids: list[str] = []
    unmatched_skus: list[str] = []
    for item in line_items:
        sku = (item.get("sku") or "").strip()
        if not sku:
            print(f"  [SKIP] '{item.get('title')}' has no SKU")
            continue
        prod = find_product_by_sku(sku)
        if prod:
            product_ids.append(prod["id"])
            print(f"  [OK] {sku} -> {prod['id']}")
        else:
            unmatched_skus.append(sku)
            print(f"  [MISS] {sku} not found")
    print(f"[STEP 2] Matched={len(product_ids)}  Unmatched={unmatched_skus}")

    if not product_ids:
        print("[STEP 2] No matched products — lead NOT created")
        return jsonify({
            "warning":        "Customer saved but no SKUs matched in French Inventories",
            "unmatched_skus": unmatched_skus,
            "customer_id":    customer_id,
        }), 200

    # STEP 3 — Create lead
    print("\n[STEP 3] Creating lead...")
    lead = create_lead(customer_id, product_ids, abandoned_date)
    lead_id = lead.get("id")

    print(f"\n[DONE] customer_id={customer_id}  lead_id={lead_id}  products={len(product_ids)}  unmatched={unmatched_skus}")
    print("=" * 60)

    return jsonify({
        "success":         True,
        "customer_id":     customer_id,
        "lead_id":         lead_id,
        "products_linked": len(product_ids),
        "unmatched_skus":  unmatched_skus,
    }), 200

# sync

@app.route("/sync/abandoned-checkouts", methods=["POST"])
def sync_abandoned_checkouts():
    print("\n" + "=" * 60)
    print(f"[SYNC] Started at {datetime.utcnow().isoformat()}Z")

    if not SHOPIFY_STORE or not SHOPIFY_ADMIN_TOKEN:
        print("[SYNC] SHOPIFY_STORE or SHOPIFY_ADMIN_TOKEN not set")
        return jsonify({"error": "SHOPIFY_STORE and SHOPIFY_ADMIN_TOKEN env vars required"}), 500

    body       = request.get_json(force=True) or {}
    max_limit  = body.get("limit", None)   # optional: stop after N checkouts
    since_date = body.get("since", None)   # optional: only after this date e.g. "2026-01-01"

    print(f"[SYNC] max_limit={max_limit}  since_date={since_date}")

    stats = {
        "fetched": 0, "success": 0,
        "skipped_completed": 0, "skipped_no_contact": 0,
        "skipped_no_sku": 0, "duplicate_lead": 0, "errors": 0,
    }
    results = []

    # ── Fetch all open checkouts from Shopify (paginated) ─────────────────────
    url    = f"https://{SHOPIFY_STORE}/admin/api/2024-04/checkouts.json"
    params = {"limit": 250, "status": "open"}
    if since_date:
        params["created_at_min"] = since_date

    page = 1
    done = False

    while url and not done:
        print(f"\n[SYNC] Fetching Shopify page {page}...")
        resp = requests.get(url, headers=SHOPIFY_HEADERS, params=params)

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2))
            print(f"[SYNC] Rate limited — waiting {wait}s")
            time.sleep(wait)
            continue

        if not resp.ok:
            print(f"[SYNC] Shopify error: {resp.status_code} {resp.text}")
            return jsonify({"error": f"Shopify API error: {resp.status_code}", "detail": resp.text}), 500

        checkouts = resp.json().get("checkouts", [])
        print(f"[SYNC] Page {page}: {len(checkouts)} checkouts received")

        for checkout in checkouts:
            stats["fetched"] += 1
            print(f"\n[SYNC] ── Checkout #{checkout.get('id')} ({stats['fetched']}) ──")

            # Check duplicate lead before processing
            cust  = checkout.get("customer") or {}
            phone = (cust.get("phone") or checkout.get("phone") or "").strip()
            email = (cust.get("email") or checkout.get("email") or "").strip().lower()
            existing_customer = find_customer(phone, email) if (phone or email) else None
            if existing_customer and lead_exists_for_customer(existing_customer["id"]):
                print(f"[SYNC] Lead already exists for customer {existing_customer['id']} — skipping")
                stats["duplicate_lead"] += 1
                results.append({"checkout_id": checkout.get("id"), "status": "skipped", "reason": "duplicate lead"})
                continue

            try:
                result = process_single_checkout(checkout)
                results.append(result)

                if result["status"] == "success":
                    stats["success"] += 1
                elif result.get("reason") == "already completed":
                    stats["skipped_completed"] += 1
                elif result.get("reason") == "no contact info":
                    stats["skipped_no_contact"] += 1
                elif result.get("reason") == "no matching SKUs":
                    stats["skipped_no_sku"] += 1

            except Exception as e:
                print(f"[SYNC] ERROR on checkout {checkout.get('id')}: {e}")
                stats["errors"] += 1
                results.append({"checkout_id": checkout.get("id"), "status": "error", "error": str(e)})

            time.sleep(0.3)   # avoid rate limiting

            if max_limit and stats["fetched"] >= max_limit:
                print(f"[SYNC] Reached limit of {max_limit} — stopping")
                done = True
                break

        # Pagination via Link header
        link = resp.headers.get("Link", "")
        url  = None
        params = {}
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
                    break
        page += 1

    print(f"\n[SYNC] Complete — {stats}")
    print("=" * 60)

    return jsonify({
        "sync_complete": True,
        "stats":         stats,
        "results":       results,
    }), 200
# end


@app.route("/health", methods=["GET"])
def health():
    print("[HEALTH] Health check")
    return jsonify({"status": "ok", "service": "shopify-airtable-abandoned-cart"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[STARTUP] Flask running on port {port}")
    app.run(host="0.0.0.0", port=port)
