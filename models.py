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


class AdDetail:
    """Wrapper around a MongoDB 'ads' document."""

    def __init__(self, doc: dict):
        self._doc = doc
        self.id = _str_id(doc)
        self.ad_id = doc.get("ad_id", "")
        self.ad_name = doc.get("ad_name", "")
        self.status = doc.get("status", "")
        self.effective_status = doc.get("effective_status", "")
        self.adset_id = doc.get("adset_id", "")
        self.campaign_id = doc.get("campaign_id", "")
        self.start_time = doc.get("start_time")          # ISO-8601 string from Meta e.g. "2024-01-15T10:00:00+0000"
        self.stop_time = doc.get("stop_time")            # None = open-ended / no end date
        self.meta_created_time = doc.get("meta_created_time")
        self.creative_id = doc.get("creative_id")
        self.creative_title = doc.get("creative_title")
        self.creative_body = doc.get("creative_body")
        self.creative_image_url = doc.get("creative_image_url")
        self.creative_thumbnail_url = doc.get("creative_thumbnail_url")
        self.call_to_action = doc.get("call_to_action")
        self.page_id = doc.get("page_id")
        self.created_at = doc.get("created_at", datetime.utcnow())
        self.updated_at = doc.get("updated_at", datetime.utcnow())

    def __repr__(self):
        return f"<AdDetail ad_id={self.ad_id} name={self.ad_name}>"



class AdSetDetail:
    """Wrapper around a MongoDB 'adsets' document."""

    def __init__(self, doc: dict):
        self._doc = doc
        self.id = _str_id(doc)
        self.adset_id = doc.get("adset_id", "")
        self.adset_name = doc.get("adset_name", "")
        self.status = doc.get("status", "")
        self.campaign_id = doc.get("campaign_id", "")
        self.daily_budget = doc.get("daily_budget")
        self.lifetime_budget = doc.get("lifetime_budget")
        self.targeting = doc.get("targeting") or {}
        self.optimization_goal = doc.get("optimization_goal")
        self.billing_event = doc.get("billing_event")
        self.bid_strategy = doc.get("bid_strategy")
        self.start_time = doc.get("start_time")
        self.end_time = doc.get("end_time")
        self.created_at = doc.get("created_at", datetime.utcnow())
        self.updated_at = doc.get("updated_at", datetime.utcnow())

    def formatted_targeting(self) -> dict:
        """Returns a human-readable summary of audience targeting specifications."""
        t = self.targeting if isinstance(self.targeting, dict) else {}
        parts = []

        # Age & Gender
        age_min = t.get("age_min", 18)
        age_max = t.get("age_max", 65)
        genders = t.get("genders", [])
        gender_str = "Todos os gêneros"
        if genders == [1]:
            gender_str = "Homens"
        elif genders == [2]:
            gender_str = "Mulheres"
        parts.append(f"{gender_str}, {age_min} a {age_max}+ anos")

        # Geo Locations
        geos = t.get("geo_locations", {})
        countries = geos.get("countries", [])
        regions = [r.get("name") or r.get("key") for r in geos.get("regions", []) if isinstance(r, dict)]
        cities = [c.get("name") for c in geos.get("cities", []) if isinstance(c, dict)]
        geo_parts = countries + regions + cities
        if geo_parts:
            parts.append(f"Localização: {', '.join(geo_parts)}")

        # Interests / Flexible spec
        flex = t.get("flexible_spec", [])
        interests = []
        for group in flex:
            if isinstance(group, dict):
                for item in group.get("interests", []) + group.get("behaviors", []) + group.get("demographics", []):
                    if isinstance(item, dict) and item.get("name"):
                        interests.append(item["name"])
        if interests:
            parts.append(f"Interesses ({len(interests)}): {', '.join(interests[:8])}" + ("..." if len(interests) > 8 else ""))

        # Custom Audiences
        custom_auds = t.get("custom_audiences", [])
        if custom_auds:
            names = [a.get("name") for a in custom_auds if isinstance(a, dict) and a.get("name")]
            if names:
                parts.append(f"Públicos Personalizados: {', '.join(names)}")
            else:
                parts.append(f"{len(custom_auds)} Público(s) Personalizado(s)")

        # Platforms
        platforms = t.get("publisher_platforms", [])
        if platforms:
            parts.append(f"Plataformas: {', '.join(platforms)}")

        return {
            "summary": " | ".join(parts) if parts else "Sem filtros de público especificados",
            "age_min": age_min,
            "age_max": age_max,
            "genders": gender_str,
            "geo_locations": geo_parts,
            "interests": interests,
            "publisher_platforms": platforms,
            "raw": t,
        }

    def __repr__(self):
        return f"<AdSetDetail adset_id={self.adset_id} name={self.adset_name}>"


class CampaignDetail:
    """Wrapper around a MongoDB 'campaigns' document."""

    def __init__(self, doc: dict):
        self._doc = doc
        self.id = _str_id(doc)
        self.campaign_id = doc.get("campaign_id", "")
        self.campaign_name = doc.get("campaign_name", "")
        self.status = doc.get("status", "")
        self.objective = doc.get("objective", "")
        self.daily_budget = doc.get("daily_budget")
        self.lifetime_budget = doc.get("lifetime_budget")
        self.buying_type = doc.get("buying_type")
        self.created_at = doc.get("created_at", datetime.utcnow())
        self.updated_at = doc.get("updated_at", datetime.utcnow())

    def __repr__(self):
        return f"<CampaignDetail campaign_id={self.campaign_id} name={self.campaign_name}>"


class AdInsight:
    """Wrapper around a MongoDB 'insights' document."""

    def __init__(self, doc: dict):
        self._doc = doc
        self.id = _str_id(doc)
        self.object_id = doc.get("object_id", "")
        self.object_type = doc.get("object_type", "ad")  # ad, adset, campaign, account
        self.spend = doc.get("spend", 0.0)
        self.impressions = doc.get("impressions", 0)
        self.clicks = doc.get("clicks", 0)
        self.reach = doc.get("reach", 0)
        self.frequency = doc.get("frequency", 0.0)
        self.cpc = doc.get("cpc", 0.0)
        self.cpm = doc.get("cpm", 0.0)
        self.ctr = doc.get("ctr", 0.0)
        self.conversions = doc.get("conversions", 0)
        self.date_preset = doc.get("date_preset", "maximum")
        self.date_start = doc.get("date_start")
        self.date_stop = doc.get("date_stop")
        self.created_at = doc.get("created_at", datetime.utcnow())
        self.updated_at = doc.get("updated_at", datetime.utcnow())

    def __repr__(self):
        return f"<AdInsight object_id={self.object_id} spend={self.spend} clicks={self.clicks}>"


class AdAccountDetail:
    """Wrapper around a MongoDB 'ad_accounts' document."""

    def __init__(self, doc: dict):
        self._doc = doc
        self.id = _str_id(doc)
        self.account_id = doc.get("account_id", "")
        self.name = doc.get("name", "")
        self.account_status = doc.get("account_status")
        self.currency = doc.get("currency", "BRL")
        self.timezone_name = doc.get("timezone_name")
        self.created_at = doc.get("created_at", datetime.utcnow())
        self.updated_at = doc.get("updated_at", datetime.utcnow())

    def __repr__(self):
        return f"<AdAccountDetail account_id={self.account_id} name={self.name}>"

