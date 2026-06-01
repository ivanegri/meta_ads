"""
models.py — Thin wrapper classes for MongoDB documents.

Instead of SQLAlchemy ORM objects, these classes wrap plain MongoDB dicts
so that all templates and endpoint code can access attributes normally
(e.g. lead.lead_id, mapping.client_name) without any changes.
"""
import json
from datetime import datetime
from bson import ObjectId


def _str_id(doc: dict) -> str:
    """Returns the MongoDB _id as a string."""
    oid = doc.get("_id")
    if isinstance(oid, ObjectId):
        return str(oid)
    return str(oid) if oid else ""


class Lead:
    """Wrapper around a MongoDB 'leads' document."""

    def __init__(self, doc: dict):
        self._doc = doc
        self.id = _str_id(doc)
        self.lead_id = doc.get("lead_id", "")
        self.form_id = doc.get("form_id")
        self.page_id = doc.get("page_id")
        self.ad_id = doc.get("ad_id")
        self.ad_name = doc.get("ad_name")
        self.adset_id = doc.get("adset_id")
        self.adset_name = doc.get("adset_name")
        self.campaign_id = doc.get("campaign_id")
        self.campaign_name = doc.get("campaign_name")
        self.platform = doc.get("platform")
        self.fields_json = doc.get("fields_json")
        self.raw_payload = doc.get("raw_payload")
        self.status = doc.get("status", "received")
        self.forwarded_to = doc.get("forwarded_to")
        self.forward_response = doc.get("forward_response")
        self.error_message = doc.get("error_message")
        self.created_at = doc.get("created_at", datetime.utcnow())
        self.updated_at = doc.get("updated_at", datetime.utcnow())

    def get_fields(self) -> dict:
        """Deserializes the lead's field_json into a dict."""
        if self.fields_json:
            if isinstance(self.fields_json, dict):
                return self.fields_json
            return json.loads(self.fields_json)
        return {}

    def __repr__(self):
        return f"<Lead lead_id={self.lead_id} status={self.status}>"


class InstanceMapping:
    """Wrapper around a MongoDB 'instance_mappings' document."""

    def __init__(self, doc: dict):
        self._doc = doc
        self.id = _str_id(doc)
        self.form_id = doc.get("form_id")
        self.page_id = doc.get("page_id")
        self.client_name = doc.get("client_name", "")
        self.crm_url = doc.get("crm_url", "")
        self.crm_auth_token = doc.get("crm_auth_token")
        self.crm_payload_type = doc.get("crm_payload_type", "raw")  # 'raw' (Approach A) or 'resolved' (Approach B)
        self.active = doc.get("active", True)
        self.created_at = doc.get("created_at", datetime.utcnow())
        self.updated_at = doc.get("updated_at", datetime.utcnow())

    def __repr__(self):
        return f"<InstanceMapping client={self.client_name} form_id={self.form_id}>"


class MetaConnection:
    """Wrapper around a MongoDB 'meta_connections' document."""

    def __init__(self, doc: dict):
        self._doc = doc
        self.id = _str_id(doc)
        self.page_id = doc.get("page_id", "")
        self.page_name = doc.get("page_name", "")
        self.page_access_token = doc.get("page_access_token", "")
        self.user_access_token = doc.get("user_access_token")
        self.connected_by = doc.get("connected_by")
        self.active = doc.get("active", True)
        self.created_at = doc.get("created_at", datetime.utcnow())
        self.updated_at = doc.get("updated_at", datetime.utcnow())

    def __repr__(self):
        return f"<MetaConnection page={self.page_name} id={self.page_id}>"
