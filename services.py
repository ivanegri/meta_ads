"""
services.py — Business logic: fetch lead from Meta, map instance, and forward.
Refactored to use MongoDB (pymongo) instead of SQLAlchemy.
"""
import json
import logging
import os
from datetime import datetime
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
        "fields": "id,created_time,field_data,form_id,ad_id,adset_id,campaign_id",
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
        "adset_id": lead_data.get("adset_id"),
        "campaign_id": lead_data.get("campaign_id"),
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
    payload = {
        "lead_id": lead.lead_id,
        "form_id": lead.form_id,
        "page_id": lead.page_id,
        "ad_id": lead.ad_id,
        "adset_id": lead.adset_id,
        "campaign_id": lead.campaign_id,
        "fields": lead.get_fields(),
        "received_at": lead.created_at.isoformat() if lead.created_at else None,
    }

    headers = {"Content-Type": "application/json"}
    if mapping.crm_auth_token:
        headers["Authorization"] = f"Bearer {mapping.crm_auth_token}"

    now = datetime.utcnow()

    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(mapping.crm_url, json=payload, headers=headers)
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
            resp.raise_for_status()
            short_token = resp.json().get("access_token")

            long_params = {
                "grant_type": "fb_exchange_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "fb_exchange_token": short_token,
            }
            long_resp = client.get(url, params=long_params)
            long_resp.raise_for_status()
            return long_resp.json().get("access_token")

    except Exception as e:
        logger.error(f"Error exchanging OAuth code for Meta token: {e}")
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
