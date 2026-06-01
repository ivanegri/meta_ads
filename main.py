"""
main.py — FastAPI application: Meta webhook + admin dashboard.
Refactored to use MongoDB (pymongo) instead of SQLAlchemy.
"""
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from typing import Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pymongo.database import Database

import models
import services
from database import get_db, get_database

load_dotenv()

# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Meta Leads Central Hub",
    description="Central que recebe, armazena e distribui leads da Meta para instâncias de CRM dos clientes.",
    version="2.0.0",
)

templates = Jinja2Templates(directory="templates")

META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "changeme")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
META_APP_ID = os.getenv("META_APP_ID", "")

# URL pública do servidor (obrigatória quando o serviço fica atrás de proxy/ngrok)
# Ex: PUBLIC_URL=https://leads.meudominio.com.br
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")


def build_callback_url(request: Request) -> str:
    """
    Retorna a URI de callback completa para o OAuth da Meta.
    Prioridade:
      1. Variável de ambiente PUBLIC_URL (garante HTTPS e domínio correto em produção).
      2. Cabeçalhos de proxy X-Forwarded-Proto e X-Forwarded-Host (para Ngrok/Proxies automaticamente).
      3. URL detectada do request original com HTTPS forçado para domínios públicos.
    """
    if PUBLIC_URL:
        return f"{PUBLIC_URL}/oauth/callback"
    
    # Detecção inteligente por trás de proxies (ex: Ngrok, Cloudflare, Nginx)
    scheme = request.headers.get("x-forwarded-proto", request.base_url.scheme)
    host = request.headers.get("x-forwarded-host", request.base_url.netloc)
    
    # Se for um domínio público (não localhost/127.0.0.1), força HTTPS para evitar problemas de proxy
    if "localhost" not in host and "127.0.0.1" not in host:
        scheme = "https"
        
    return f"{scheme}://{host}/oauth/callback"


def _ensure_indexes(db: Database):
    """Creates MongoDB indexes on first startup for performance."""
    db.leads.create_index("lead_id", unique=True, background=True)
    db.leads.create_index("status", background=True)
    db.leads.create_index("created_at", background=True)
    db.instance_mappings.create_index("form_id", sparse=True, background=True)
    db.instance_mappings.create_index("page_id", sparse=True, background=True)
    db.meta_connections.create_index("page_id", unique=True, background=True)


# Create indexes on startup
@app.on_event("startup")
async def startup_event():
    db = get_database()
    _ensure_indexes(db)
    logger.info("MongoDB indexes ensured.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def verify_meta_signature(payload_body: bytes, signature_header: Optional[str]) -> bool:
    """Validates the HMAC-SHA256 signature sent by Meta."""
    if not META_APP_SECRET or not signature_header:
        return True  # Allow in dev without secret configured
    expected = "sha256=" + hmac.new(
        META_APP_SECRET.encode("utf-8"), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# Webhook Meta
# ---------------------------------------------------------------------------

@app.get("/webhook", tags=["Webhook"])
async def webhook_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Webhook verification endpoint required by Meta."""
    if hub_mode == "subscribe" and hub_verify_token == META_VERIFY_TOKEN:
        logger.info("Webhook verified successfully by Meta.")
        return PlainTextResponse(content=hub_challenge)
    logger.warning("Invalid verification attempt.")
    raise HTTPException(status_code=403, detail="Verificação inválida.")


@app.post("/webhook", tags=["Webhook"])
async def webhook_receive(request: Request, db: Database = Depends(get_db)):
    """Receives lead events from Meta and processes them."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_meta_signature(body, signature):
        logger.warning("Invalid webhook signature.")
        raise HTTPException(status_code=403, detail="Assinatura inválida.")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Payload JSON inválido.")

    logger.info(f"Webhook received: {json.dumps(payload)[:300]}")

    for entry in payload.get("entry", []):
        page_id = entry.get("id")
        for change in entry.get("changes", []):
            if change.get("field") != "leadgen":
                continue
            value = change.get("value", {})
            lead_gen_id = value.get("leadgen_id")
            form_id = value.get("form_id")

            if not lead_gen_id:
                continue

            try:
                services.process_lead_event(
                    db=db,
                    lead_gen_id=str(lead_gen_id),
                    form_id=str(form_id) if form_id else None,
                    page_id=str(page_id) if page_id else None,
                    raw_payload=payload,
                )
            except Exception as e:
                logger.error(f"Error processing lead_gen_id={lead_gen_id}: {e}")

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Dashboard — Leads
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, tags=["Dashboard"])
async def dashboard_leads(
    request: Request,
    db: Database = Depends(get_db),
    page: int = Query(1, ge=1),
    status: str = Query(""),
    search: str = Query(""),
):
    """Main dashboard listing all received leads."""
    per_page = 20
    query_filter = {}

    if status:
        query_filter["status"] = status
    if search:
        query_filter["$or"] = [
            {"lead_id": {"$regex": search, "$options": "i"}},
            {"form_id": {"$regex": search, "$options": "i"}},
            {"page_id": {"$regex": search, "$options": "i"}},
        ]

    total = db.leads.count_documents(query_filter)
    raw_leads = list(
        db.leads.find(query_filter)
        .sort("created_at", -1)
        .skip((page - 1) * per_page)
        .limit(per_page)
    )

    leads_display = []
    for doc in raw_leads:
        lead = models.Lead(doc)
        leads_display.append({
            "id": lead.id,
            "lead_id": lead.lead_id,
            "form_id": lead.form_id,
            "page_id": lead.page_id,
            "status": lead.status,
            "forwarded_to": lead.forwarded_to,
            "fields": lead.get_fields(),
            "created_at": lead.created_at,
            "error_message": lead.error_message,
        })

    stats = {
        "total": db.leads.count_documents({}),
        "forwarded": db.leads.count_documents({"status": "forwarded"}),
        "failed": db.leads.count_documents({"status": "failed"}),
        "skipped": db.leads.count_documents({"status": "skipped"}),
        "received": db.leads.count_documents({"status": "received"}),
    }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "leads": leads_display,
        "stats": stats,
        "page": page,
        "total": total,
        "per_page": per_page,
        "status_filter": status,
        "search": search,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    })


@app.post("/leads/{lead_id}/retry", tags=["Dashboard"])
async def retry_lead(lead_id: str, db: Database = Depends(get_db)):
    """Reprocesses a lead with failed or skipped status."""
    doc = db.leads.find_one({"_id": ObjectId(lead_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Lead não encontrado.")

    raw_payload = doc.get("raw_payload", {})
    db.leads.update_one(
        {"_id": ObjectId(lead_id)},
        {"$set": {"status": "received", "error_message": None, "updated_at": datetime.utcnow()}}
    )
    lead = models.Lead(doc)

    services.process_lead_event(
        db=db,
        lead_gen_id=lead.lead_id,
        form_id=lead.form_id,
        page_id=lead.page_id,
        raw_payload=raw_payload,
    )
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Dashboard — Instance Mappings
# ---------------------------------------------------------------------------

@app.get("/mappings", response_class=HTMLResponse, tags=["Mappings"])
async def list_mappings(request: Request, db: Database = Depends(get_db)):
    """Lists instance mappings and active Meta page connections."""
    mappings = [
        models.InstanceMapping(doc)
        for doc in db.instance_mappings.find().sort("client_name", 1)
    ]
    connections = [
        models.MetaConnection(doc)
        for doc in db.meta_connections.find().sort("page_name", 1)
    ]

    app_id = META_APP_ID
    redirect_uri = ""
    if app_id:
        redirect_uri = build_callback_url(request)

    return templates.TemplateResponse("mappings.html", {
        "request": request,
        "mappings": mappings,
        "connections": connections,
        "app_id": app_id,
        "redirect_uri": redirect_uri
    })


@app.post("/mappings/create", tags=["Mappings"])
async def create_mapping(
    db: Database = Depends(get_db),
    client_name: str = Form(...),
    form_id: str = Form(""),
    page_id: str = Form(""),
    crm_url: str = Form(...),
    crm_auth_token: str = Form(""),
):
    """Creates a new CRM instance mapping."""
    now = datetime.utcnow()
    db.instance_mappings.insert_one({
        "client_name": client_name,
        "form_id": form_id.strip() or None,
        "page_id": page_id.strip() or None,
        "crm_url": crm_url.strip(),
        "crm_auth_token": crm_auth_token.strip() or None,
        "active": True,
        "created_at": now,
        "updated_at": now,
    })
    return RedirectResponse(url="/mappings", status_code=303)


@app.post("/mappings/{mapping_id}/toggle", tags=["Mappings"])
async def toggle_mapping(mapping_id: str, db: Database = Depends(get_db)):
    """Activates or deactivates a mapping."""
    doc = db.instance_mappings.find_one({"_id": ObjectId(mapping_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Mapeamento não encontrado.")
    db.instance_mappings.update_one(
        {"_id": ObjectId(mapping_id)},
        {"$set": {"active": not doc.get("active", True), "updated_at": datetime.utcnow()}}
    )
    return RedirectResponse(url="/mappings", status_code=303)


@app.post("/mappings/{mapping_id}/delete", tags=["Mappings"])
async def delete_mapping(mapping_id: str, db: Database = Depends(get_db)):
    """Removes a mapping."""
    result = db.instance_mappings.delete_one({"_id": ObjectId(mapping_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Mapeamento não encontrado.")
    return RedirectResponse(url="/mappings", status_code=303)


# ---------------------------------------------------------------------------
# Dashboard — OAuth 2.0 (Facebook Login)
# ---------------------------------------------------------------------------

@app.get("/oauth/callback", tags=["OAuth"])
async def oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    db: Database = Depends(get_db)
):
    """
    Receives the Facebook Login return.
    - state present → client onboarding flow (renders page selector).
    - state absent  → admin flow (connects all pages, redirects to /mappings).
    """
    redirect_uri = build_callback_url(request)

    if error:
        logger.error(f"OAuth error from Meta: {error}")
        return templates.TemplateResponse("onboard_landing.html", {
            "request": request,
            "client_name": "cliente",
            "app_id": None,
            "oauth_url": "",
            "error": error
        }, status_code=400)

    if not code:
        raise HTTPException(status_code=400, detail="Código de autorização ausente.")

    user_token = services.exchange_code_for_user_token(code, redirect_uri)
    if not user_token:
        return HTMLResponse(
            content="<h2>Erro</h2><p>Não foi possível obter o token de acesso.</p>",
            status_code=400
        )

    pages = services.fetch_user_pages(user_token)

    # ── CLIENT FLOW: state present ──
    if state:
        mapping_doc = db.instance_mappings.find_one({"_id": ObjectId(state)})
        client_name = mapping_doc.get("client_name", "seu cliente") if mapping_doc else "seu cliente"

        return templates.TemplateResponse("select_page.html", {
            "request": request,
            "client_name": client_name,
            "mapping_id": state,
            "user_access_token": user_token,
            "pages": pages,
        })

    # ── ADMIN FLOW: connect all pages automatically ──
    connected_count = 0
    now = datetime.utcnow()
    for page in pages:
        page_id = page["id"]
        page_name = page["name"]
        page_token = page["access_token"]

        services.subscribe_page_to_app(page_id, page_token)

        db.meta_connections.update_one(
            {"page_id": page_id},
            {"$set": {
                "page_id": page_id,
                "page_name": page_name,
                "page_access_token": page_token,
                "user_access_token": user_token,
                "connected_by": "Admin Central",
                "active": True,
                "updated_at": now,
            }, "$setOnInsert": {"created_at": now}},
            upsert=True
        )
        connected_count += 1

    return RedirectResponse(url=f"/mappings?oauth_success={connected_count}", status_code=303)


# ---------------------------------------------------------------------------
# Client Onboarding
# ---------------------------------------------------------------------------

@app.get("/onboard/{mapping_id}", response_class=HTMLResponse, tags=["Onboarding"])
async def onboard_landing(
    request: Request,
    mapping_id: str,
    db: Database = Depends(get_db)
):
    """Personalized landing page for the client to start Facebook Login."""
    try:
        doc = db.instance_mappings.find_one({"_id": ObjectId(mapping_id)})
    except Exception:
        doc = None

    if not doc:
        return HTMLResponse(content="<h2>Link inválido ou expirado.</h2>", status_code=404)

    app_id = META_APP_ID
    redirect_uri = build_callback_url(request)

    oauth_url = (
        f"https://www.facebook.com/v19.0/dialog/oauth"
        f"?client_id={app_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=pages_show_list,pages_read_engagement,pages_manage_ads,leads_retrieval"
        f"&state={mapping_id}"
    ) if app_id else ""

    return templates.TemplateResponse("onboard_landing.html", {
        "request": request,
        "client_name": doc.get("client_name", ""),
        "app_id": app_id,
        "oauth_url": oauth_url,
    })


@app.post("/onboard/complete", tags=["Onboarding"])
async def onboard_complete(
    request: Request,
    mapping_id: str = Form(...),
    page_id: str = Form(...),
    page_name: str = Form(...),
    page_access_token: str = Form(...),
    user_access_token: str = Form(...),
    db: Database = Depends(get_db)
):
    """
    Completes client onboarding:
    - Registers webhook on the selected page.
    - Saves/updates MetaConnection with page_access_token.
    - Links page_id to the client's InstanceMapping.
    """
    try:
        doc = db.instance_mappings.find_one({"_id": ObjectId(mapping_id)})
    except Exception:
        doc = None

    if not doc:
        return HTMLResponse(content="<h2>Mapeamento não encontrado.</h2>", status_code=404)

    client_name = doc.get("client_name", "")
    now = datetime.utcnow()

    subscribed = services.subscribe_page_to_app(page_id, page_access_token)
    if not subscribed:
        logger.warning(f"Onboarding {client_name}: could not subscribe webhook for page {page_name}.")

    db.meta_connections.update_one(
        {"page_id": page_id},
        {"$set": {
            "page_id": page_id,
            "page_name": page_name,
            "page_access_token": page_access_token,
            "user_access_token": user_access_token,
            "connected_by": client_name,
            "active": True,
            "updated_at": now,
        }, "$setOnInsert": {"created_at": now}},
        upsert=True
    )

    db.instance_mappings.update_one(
        {"_id": ObjectId(mapping_id)},
        {"$set": {"page_id": page_id, "updated_at": now}}
    )

    logger.info(f"Onboarding complete: client={client_name}, page={page_name} ({page_id}), mapping_id={mapping_id}")

    return templates.TemplateResponse("onboard_success.html", {
        "request": request,
        "client_name": client_name,
        "page_name": page_name,
        "page_id": page_id,
    })


@app.post("/connections/{connection_id}/toggle", tags=["Mappings"])
async def toggle_connection(connection_id: str, db: Database = Depends(get_db)):
    """Activates or deactivates a page connection."""
    doc = db.meta_connections.find_one({"page_id": connection_id})
    if not doc:
        try:
            doc = db.meta_connections.find_one({"_id": ObjectId(connection_id)})
        except Exception:
            pass
    if not doc:
        raise HTTPException(status_code=404, detail="Conexão não encontrada.")
    db.meta_connections.update_one(
        {"_id": doc["_id"]},
        {"$set": {"active": not doc.get("active", True), "updated_at": datetime.utcnow()}}
    )
    return RedirectResponse(url="/mappings", status_code=303)


@app.post("/connections/{connection_id}/delete", tags=["Mappings"])
async def delete_connection(connection_id: str, db: Database = Depends(get_db)):
    """Removes a page connection."""
    result = db.meta_connections.delete_one({"page_id": connection_id})
    if result.deleted_count == 0:
        try:
            result = db.meta_connections.delete_one({"_id": ObjectId(connection_id)})
        except Exception:
            pass
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Conexão não encontrada.")
    return RedirectResponse(url="/mappings", status_code=303)


# ---------------------------------------------------------------------------
# REST API (for external integrations)
# ---------------------------------------------------------------------------

@app.get("/api/leads", tags=["API"])
async def api_list_leads(
    db: Database = Depends(get_db),
    status: str = Query(""),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    """REST API to list leads."""
    query_filter = {}
    if status:
        query_filter["status"] = status

    total = db.leads.count_documents(query_filter)
    raw_leads = list(
        db.leads.find(query_filter)
        .sort("created_at", -1)
        .skip(offset)
        .limit(limit)
    )

    return {
        "total": total,
        "results": [
            {
                "id": str(doc.get("_id")),
                "lead_id": doc.get("lead_id"),
                "form_id": doc.get("form_id"),
                "page_id": doc.get("page_id"),
                "status": doc.get("status"),
                "forwarded_to": doc.get("forwarded_to"),
                "fields": doc.get("fields_json", {}),
                "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
            }
            for doc in raw_leads
        ],
    }


@app.get("/api/leads/{lead_id}", tags=["API"])
async def api_get_lead(lead_id: str, db: Database = Depends(get_db)):
    """Returns a specific lead by its Meta lead_id."""
    doc = db.leads.find_one({"lead_id": lead_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Lead não encontrado.")
    lead = models.Lead(doc)
    return {
        "id": lead.id,
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
        "status": lead.status,
        "forwarded_to": lead.forwarded_to,
        "forward_response": lead.forward_response,
        "error_message": lead.error_message,
        "fields": lead.get_fields(),
        "raw_payload": doc.get("raw_payload"),
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "updated_at": lead.updated_at.isoformat() if lead.updated_at else None,
    }
