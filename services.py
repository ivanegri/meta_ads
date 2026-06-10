"""
services.py — Business logic: fetch lead from Meta, map instance, and forward.
Refactored to use MongoDB (pymongo) instead of SQLAlchemy.
"""
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx
from pymongo.database import Database

from models import Lead, InstanceMapping, MetaConnection

logger = logging.getLogger(__name__)

META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
META_GRAPH_API_VERSION = "v19.0"


# ---------------------------------------------------------------------------
# 1. Fetch complete lead details from Meta Graph API
# ---------------------------------------------------------------------------

def fetch_lead_details(lead_id: str, db: Database, page_id: Optional[str] = None) -> Optional[dict]:
    """
    Uses the Meta Graph API to fetch all fields of a lead by lead_id.
    Tries to use the page-specific OAuth token first, falls back to global .env token.
    """
    access_token = META_ACCESS_TOKEN

    if page_id:
        conn_doc = db.meta_connections.find_one({"page_id": page_id, "active": True})
        if conn_doc:
            access_token = conn_doc.get("page_access_token", access_token)
            logger.info(f"Using OAuth page token for: {conn_doc.get('page_name')}")

    if not access_token:
        logger.warning("No access token configured. Cannot fetch lead details.")
        return None

    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{lead_id}"
    params = {
        "fields": "id,created_time,field_data,form_id,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,platform",
        "access_token": access_token,
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            res = response.json()
            if page_id and isinstance(res, dict):
                res["page_id"] = page_id
            return res
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error fetching lead {lead_id}: {e.response.text}")
    except Exception as e:
        logger.error(f"Error fetching lead {lead_id}: {e}")

    return None


# ---------------------------------------------------------------------------
# 2. Find the correct instance mapping
# ---------------------------------------------------------------------------

def find_mapping(db: Database, form_id: Optional[str], page_id: Optional[str]) -> Optional[InstanceMapping]:
    """
    Searches for a mapping by form_id (priority) then page_id.
    """
    if form_id:
        doc = db.instance_mappings.find_one({"form_id": form_id, "active": True})
        if doc:
            return InstanceMapping(doc)

    if page_id:
        doc = db.instance_mappings.find_one({"page_id": page_id, "active": True})
        if doc:
            return InstanceMapping(doc)

    return None


# ---------------------------------------------------------------------------
# 3. Save lead to MongoDB
# ---------------------------------------------------------------------------

def save_lead(db: Database, lead_data: dict, raw_payload: dict) -> Lead:
    """
    Persists the lead in the MongoDB 'leads' collection.
    Avoids duplicates via lead_id unique index.
    """
    lead_id = lead_data.get("id") or raw_payload.get("leadgen_id", "unknown")

    existing = db.leads.find_one({"lead_id": lead_id})
    if existing:
        logger.info(f"Lead {lead_id} already exists. Skipping duplicate.")
        return Lead(existing)

    # Parse field_data list into a flat dict
    fields = {}
    for field in lead_data.get("field_data", []):
        values = field.get("values", [])
        fields[field.get("name")] = values[0] if len(values) == 1 else values

    now = datetime.utcnow()
    doc = {
        "lead_id": lead_id,
        "form_id": lead_data.get("form_id"),
        "page_id": lead_data.get("page_id"),
        "ad_id": lead_data.get("ad_id"),
        "ad_name": lead_data.get("ad_name"),
        "adset_id": lead_data.get("adset_id"),
        "adset_name": lead_data.get("adset_name"),
        "campaign_id": lead_data.get("campaign_id"),
        "campaign_name": lead_data.get("campaign_name"),
        "platform": lead_data.get("platform"),
        "fields_json": fields,  # Stored as a native Mongo dict
        "raw_payload": raw_payload,
        "status": "received",
        "forwarded_to": None,
        "forward_response": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }

    result = db.leads.insert_one(doc)
    doc["_id"] = result.inserted_id
    lead = Lead(doc)
    logger.info(f"Lead {lead_id} saved with _id={result.inserted_id}.")
    return lead


# ---------------------------------------------------------------------------
# 4. Forward lead to the correct CRM
# ---------------------------------------------------------------------------

def forward_lead(db: Database, lead: Lead, mapping: InstanceMapping) -> bool:
    """
    Sends the lead via HTTP POST to the client's CRM URL.
    Updates the lead status in MongoDB.
    """
    # Architecture support: Choose payload type based on crm_payload_type ('raw' or 'resolved')
    crm_payload_type = getattr(mapping, "crm_payload_type", "raw")
    
    if crm_payload_type == "raw" and lead.raw_payload:
        payload = lead.raw_payload
        logger.info(f"Forwarding lead {lead.lead_id} in RAW Meta webhook format.")
    else:
        payload = {
            "lead_id": lead.lead_id,
            "form_id": lead.form_id,
            "page_id": lead.page_id,
            "ad_id": lead.ad_id,
            "ad_name": lead.ad_name,
            "adset_id": lead.adset_id,
            "adset_name": lead.adset_name,
            "campaign_id": lead.campaign_id,
            "campaign_name": lead.campaign_name,
            "platform": lead.platform,
            "fields": lead.get_fields(),
            "received_at": lead.created_at.isoformat() if lead.created_at else None,
        }
        logger.info(f"Forwarding lead {lead.lead_id} in RESOLVED custom JSON format.")

    # Serialize body precisely to a compact JSON string to match signature byte-by-byte
    payload_str = json.dumps(payload, separators=(',', ':'))

    headers = {"Content-Type": "application/json"}
    
    # Generate X-Hub-Signature-256 using META_APP_SECRET for security parity with Meta
    meta_app_secret = os.getenv("META_APP_SECRET", "")
    if meta_app_secret:
        sig = hmac.new(
            meta_app_secret.encode("utf-8"),
            payload_str.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={sig}"
        logger.debug("Added X-Hub-Signature-256 signature header.")

    if mapping.crm_auth_token:
        headers["Authorization"] = f"Bearer {mapping.crm_auth_token}"

    now = datetime.utcnow()

    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(mapping.crm_url, content=payload_str, headers=headers)
            response.raise_for_status()

        db.leads.update_one(
            {"lead_id": lead.lead_id},
            {"$set": {
                "status": "forwarded",
                "forwarded_to": mapping.crm_url,
                "forward_response": response.text[:1000],
                "updated_at": now,
            }}
        )
        logger.info(f"Lead {lead.lead_id} forwarded to {mapping.crm_url}.")
        return True

    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:500]}"
        db.leads.update_one(
            {"lead_id": lead.lead_id},
            {"$set": {"status": "failed", "forwarded_to": mapping.crm_url, "error_message": error_msg, "updated_at": now}}
        )
        logger.error(f"HTTP error forwarding lead {lead.lead_id}: {error_msg}")

    except Exception as e:
        db.leads.update_one(
            {"lead_id": lead.lead_id},
            {"$set": {"status": "failed", "forwarded_to": mapping.crm_url, "error_message": str(e)[:500], "updated_at": now}}
        )
        logger.error(f"Error forwarding lead {lead.lead_id}: {e}")

    return False


# ---------------------------------------------------------------------------
# 5. Main orchestrator
# ---------------------------------------------------------------------------

def process_lead_event(db: Database, lead_gen_id: str, form_id: Optional[str], page_id: Optional[str], raw_payload: dict):
    """
    Full pipeline:
    1. Fetch lead details from Meta Graph API.
    2. Save to MongoDB.
    3. Find the correct CRM mapping.
    4. Forward the lead.
    """
    logger.info(f"Processing lead_gen_id={lead_gen_id} form_id={form_id} page_id={page_id}")

    lead_data = fetch_lead_details(lead_gen_id, db=db, page_id=page_id)
    if not lead_data:
        lead_data = {"id": lead_gen_id, "form_id": form_id, "page_id": page_id}

    if not lead_data.get("form_id"):
        lead_data["form_id"] = form_id
    if not lead_data.get("page_id"):
        lead_data["page_id"] = page_id

    lead = save_lead(db, lead_data, raw_payload)

    if lead.status != "received":
        return  # Already processed

    mapping = find_mapping(db, form_id=lead_data.get("form_id"), page_id=lead_data.get("page_id"))

    if not mapping:
        db.leads.update_one(
            {"lead_id": lead_gen_id},
            {"$set": {
                "status": "skipped",
                "error_message": f"No mapping found for form_id={lead_data.get('form_id')} page_id={lead_data.get('page_id')}",
                "updated_at": datetime.utcnow(),
            }}
        )
        logger.warning(f"Lead {lead_gen_id} has no matching mapping. Status: skipped.")
        return

    forward_lead(db, lead, mapping)


# ---------------------------------------------------------------------------
# 6. Facebook OAuth 2.0 flow helpers
# ---------------------------------------------------------------------------

def exchange_code_for_user_token(code: str, redirect_uri: str) -> Optional[str]:
    """Exchanges the temporary OAuth code for a long-lived user access token (60 days)."""
    client_id = os.getenv("META_APP_ID", "")
    client_secret = os.getenv("META_APP_SECRET", "")

    if not client_id or not client_secret:
        logger.error("META_APP_ID or META_APP_SECRET not set in .env")
        return None

    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/oauth/access_token"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "client_secret": client_secret,
        "code": code,
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params=params)
            if not resp.is_success:
                logger.error(f"Erro na troca de code por token (step 1). Status={resp.status_code} | Body={resp.text} | redirect_uri={redirect_uri}")
                return None
            short_token = resp.json().get("access_token")
            if not short_token:
                logger.error(f"Token de curta duração ausente na resposta: {resp.json()}")
                return None

            long_params = {
                "grant_type": "fb_exchange_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "fb_exchange_token": short_token,
            }
            long_resp = client.get(url, params=long_params)
            if not long_resp.is_success:
                logger.error(f"Erro na troca por token de longa duração (step 2). Status={long_resp.status_code} | Body={long_resp.text}")
                return None
            return long_resp.json().get("access_token")

    except Exception as e:
        logger.error(f"Exceção ao trocar OAuth code por token da Meta: {e}")
        return None


def fetch_user_pages(user_token: str) -> list[dict]:
    """Fetches all pages administered by the user along with their Page Access Tokens."""
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/me/accounts"
    params = {"access_token": user_token, "limit": 100}
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return [
                {"id": item.get("id"), "name": item.get("name"), "access_token": item.get("access_token")}
                for item in resp.json().get("data", [])
            ]
    except Exception as e:
        logger.error(f"Error fetching user pages from Meta: {e}")
        return []


def subscribe_page_to_app(page_id: str, page_token: str) -> bool:
    """Subscribes a page to receive leadgen webhook events from our Meta app."""
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{page_id}/subscribed_apps"
    payload = {"subscribed_fields": "leadgen", "access_token": page_token}
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, data=payload)
            resp.raise_for_status()
            logger.info(f"Page {page_id} subscribed to webhook successfully.")
            return True
    except Exception as e:
        logger.error(f"Error subscribing page {page_id} to webhook: {e}")
        return False


# ---------------------------------------------------------------------------
# 7. Lead Review & Recovery (anti-missed-webhook safety net)
# ---------------------------------------------------------------------------

def build_simulated_raw_payload(lead_data: dict, page_id: str) -> dict:
    """
    Builds a raw webhook-format payload from a lead fetched directly via Graph API.
    This ensures the CRM forwarding logic (including X-Hub-Signature-256) works
    identically to a live webhook call.
    """
    return {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": int(time.time()),
                "changes": [
                    {
                        "field": "leadgen",
                        "value": {
                            "leadgen_id": lead_data.get("id"),
                            "form_id": lead_data.get("form_id"),
                            "page_id": page_id,
                            "created_time": int(
                                datetime.fromisoformat(
                                    lead_data["created_time"].replace("Z", "+00:00")
                                ).timestamp()
                            ) if lead_data.get("created_time") else int(time.time()),
                        }
                    }
                ]
            }
        ]
    }


def fetch_forms_for_page(page_id: str, access_token: str) -> list[dict]:
    """
    Fetches all active lead-gen forms associated with a Facebook page.
    Returns a list of dicts: [{"id": "...", "name": "..."}, ...]
    """
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{page_id}/leadgen_forms"
    params = {
        "fields": "id,name,status",
        "access_token": access_token,
        "limit": 100,
    }
    forms = []
    try:
        with httpx.Client(timeout=15.0) as client:
            while url:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                forms.extend(data.get("data", []))
                # Handle pagination
                paging = data.get("paging", {})
                url = paging.get("next")
                params = {}  # next URL already contains all query params
    except Exception as e:
        logger.error(f"Error fetching forms for page {page_id}: {e}")
    return forms


def review_and_recover_leads(
    db: Database,
    hours: int = 6,
    trigger: str = "auto",
) -> dict:
    """
    Safety-net job that polls Meta's Graph API to find leads that may have been
    missed by the real-time webhook (network blips, server downtime, etc.).

    For each active MetaConnection:
      1. Lists all lead-gen forms for the page.
      2. Fetches leads created in the last `hours` hours.
      3. Skips leads already in MongoDB (dedup by lead_id).
      4. For new leads: saves + forwards using the EXACT same pipeline as webhook.

    Results are written to the `review_logs` collection for audit and dashboard display.

    Args:
        db:      MongoDB database instance.
        hours:   How many hours back to search (default 6).
        trigger: Who triggered this run ("auto", "manual", "cli").

    Returns:
        dict with keys: leads_found_in_meta, recovered_leads, skipped_duplicates, errors.
    """
    started_at = datetime.utcnow()
    logger.info(f"[LeadReview] Starting review job. trigger={trigger} window={hours}h")

    # Time window: leads created after this timestamp
    since_ts = int((datetime.utcnow() - timedelta(hours=hours)).timestamp())

    leads_found = 0
    recovered = 0
    duplicates = 0
    errors = 0

    # Fetch all active connections (pages with OAuth tokens)
    connections = list(db.meta_connections.find({"active": True}))
    if not connections:
        logger.warning("[LeadReview] No active MetaConnections found. Nothing to review.")
        _save_review_log(db, started_at, trigger, hours, 0, 0, 0, 0, "no_connections")
        return {
            "leads_found_in_meta": 0,
            "recovered_leads": 0,
            "skipped_duplicates": 0,
            "errors": 0,
            "note": "No active Meta connections configured.",
        }

    for conn_doc in connections:
        page_id = conn_doc.get("page_id")
        page_name = conn_doc.get("page_name", page_id)
        access_token = conn_doc.get("page_access_token", META_ACCESS_TOKEN)

        if not access_token:
            logger.warning(f"[LeadReview] No token for page {page_name}. Skipping.")
            errors += 1
            continue

        logger.info(f"[LeadReview] Checking page: {page_name} ({page_id})")

        forms = fetch_forms_for_page(page_id, access_token)
        if not forms:
            logger.info(f"[LeadReview] No forms found for page {page_name}.")
            continue

        for form in forms:
            form_id = form.get("id")
            form_name = form.get("name", form_id)

            # Fetch leads for this form created after the time window
            graph_url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{form_id}/leads"
            params = {
                "fields": "id,created_time,field_data,form_id,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,platform",
                "access_token": access_token,
                "filtering": json.dumps([{"field": "time_created", "operator": "GREATER_THAN", "value": since_ts}]),
                "limit": 100,
            }

            try:
                with httpx.Client(timeout=20.0) as client:
                    next_url: Optional[str] = graph_url
                    next_params = params

                    while next_url:
                        resp = client.get(next_url, params=next_params)
                        resp.raise_for_status()
                        page_data = resp.json()

                        for lead_data in page_data.get("data", []):
                            leads_found += 1
                            lead_id = lead_data.get("id")

                            # ── Deduplication: skip if already in DB ──
                            existing = db.leads.find_one({"lead_id": lead_id})
                            if existing:
                                duplicates += 1
                                logger.debug(f"[LeadReview] Lead {lead_id} already in DB. Skipping.")
                                continue

                            # ── New lead found! Inject page_id and process. ──
                            lead_data["page_id"] = page_id
                            if not lead_data.get("form_id"):
                                lead_data["form_id"] = form_id

                            raw_payload = build_simulated_raw_payload(lead_data, page_id)

                            try:
                                process_lead_event(
                                    db=db,
                                    lead_gen_id=lead_id,
                                    form_id=lead_data.get("form_id", form_id),
                                    page_id=page_id,
                                    raw_payload=raw_payload,
                                )
                                recovered += 1
                                logger.info(
                                    f"[LeadReview] ✅ Recovered lead {lead_id} from form '{form_name}'"
                                )
                            except Exception as e:
                                errors += 1
                                logger.error(
                                    f"[LeadReview] Error processing recovered lead {lead_id}: {e}"
                                )

                        # Pagination
                        paging = page_data.get("paging", {})
                        next_url = paging.get("next")
                        next_params = {}  # next URL already contains all params

            except Exception as e:
                errors += 1
                logger.error(f"[LeadReview] Error fetching leads for form {form_id}: {e}")

    finished_at = datetime.utcnow()
    duration_s = (finished_at - started_at).total_seconds()

    logger.info(
        f"[LeadReview] Finished. found={leads_found} recovered={recovered} "
        f"duplicates={duplicates} errors={errors} duration={duration_s:.1f}s"
    )

    _save_review_log(
        db, started_at, trigger, hours,
        leads_found, recovered, duplicates, errors, "ok"
    )

    return {
        "leads_found_in_meta": leads_found,
        "recovered_leads": recovered,
        "skipped_duplicates": duplicates,
        "errors": errors,
        "duration_seconds": duration_s,
    }


def _save_review_log(
    db: Database,
    started_at: datetime,
    trigger: str,
    hours: int,
    leads_found: int,
    recovered: int,
    duplicates: int,
    errors: int,
    status: str,
):
    """Persists a review execution record into the review_logs collection."""
    try:
        db.review_logs.insert_one({
            "started_at": started_at,
            "finished_at": datetime.utcnow(),
            "trigger": trigger,          # 'auto', 'manual', 'cli'
            "window_hours": hours,
            "leads_found_in_meta": leads_found,
            "recovered_leads": recovered,
            "skipped_duplicates": duplicates,
            "errors": errors,
            "status": status,
        })
    except Exception as e:
        logger.error(f"[LeadReview] Failed to save review log: {e}")
