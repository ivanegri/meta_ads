"""
main.py — FastAPI application: Meta webhook + admin dashboard.
Refactored to use MongoDB (pymongo) instead of SQLAlchemy.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from typing import Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Form
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
    db.ads.create_index("ad_id", unique=True, background=True)
    db.adsets.create_index("adset_id", unique=True, background=True)
    db.campaigns.create_index("campaign_id", unique=True, background=True)
    db.insights.create_index([("object_id", 1), ("date_preset", 1)], background=True)
    db.ad_accounts.create_index("account_id", unique=True, background=True)



# Create indexes on startup
@app.on_event("startup")
async def startup_event():
    db = get_database()
    _ensure_indexes(db)
    # Also ensure index on review_logs for fast last-record lookup
    db.review_logs.create_index("started_at", background=True)
    logger.info("MongoDB indexes ensured.")
    # Start the background lead review scheduler (every 6 hours)
    asyncio.create_task(schedule_lead_reviews())


async def schedule_lead_reviews():
    """
    Background coroutine that runs the lead review job every 6 hours.
    Wrapped in a try/except so any failure never crashes the main server.
    """
    INTERVAL_SECONDS = 6 * 60 * 60  # 6 hours
    # Small initial delay to let the server fully start
    await asyncio.sleep(30)
    while True:
        try:
            logger.info("[LeadReview] Background scheduler triggered.")
            db = get_database()
            results = await asyncio.to_thread(
                services.review_and_recover_leads,
                db,
                6,
                "auto",
            )
            logger.info(f"[LeadReview] Scheduler done: {results}")
        except Exception as e:
            logger.error(f"[LeadReview] Scheduler error (non-fatal): {e}")
        await asyncio.sleep(INTERVAL_SECONDS)


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

            # Reconstruct clean, single-lead raw webhook event payload for perfect signature mirroring
            single_lead_payload = {
                "object": payload.get("object", "page"),
                "entry": [
                    {
                        "id": page_id,
                        "time": entry.get("time", int(datetime.utcnow().timestamp())),
                        "changes": [
                            {
                                "field": "leadgen",
                                "value": value
                            }
                        ]
                    }
                ]
            }

            try:
                services.process_lead_event(
                    db=db,
                    lead_gen_id=str(lead_gen_id),
                    form_id=str(form_id) if form_id else None,
                    page_id=str(page_id) if page_id else None,
                    raw_payload=single_lead_payload,
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

    # Fetch last review log for status bar display
    last_review_doc = db.review_logs.find_one(sort=[("started_at", -1)])
    last_review = None
    if last_review_doc:
        last_review = {
            "started_at": last_review_doc.get("started_at"),
            "trigger": last_review_doc.get("trigger", "auto"),
            "recovered_leads": last_review_doc.get("recovered_leads", 0),
            "skipped_duplicates": last_review_doc.get("skipped_duplicates", 0),
            "errors": last_review_doc.get("errors", 0),
            "status": last_review_doc.get("status", "ok"),
        }

    # Read one-time review result from query params (after manual trigger redirect)
    review_result = None
    recovered_param = request.query_params.get("review_recovered")
    if recovered_param is not None:
        review_result = {
            "recovered_leads": int(recovered_param),
            "skipped_duplicates": int(request.query_params.get("review_duplicates", 0)),
            "leads_found_in_meta": int(request.query_params.get("review_found", 0)),
            "errors": int(request.query_params.get("review_errors", 0)),
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
        "last_review": last_review,
        "review_result": review_result,
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


@app.post("/admin/review-leads", tags=["Dashboard"])
async def manual_review_leads(
    db: Database = Depends(get_db),
    hours: int = Form(24),
):
    """
    Manually triggers the lead review & recovery job from the dashboard.
    Redirects back to / with the result summary as query parameters.
    """
    try:
        results = await asyncio.to_thread(
            services.review_and_recover_leads,
            db,
            hours,
            "manual",
        )
    except Exception as e:
        logger.error(f"Manual review error: {e}")
        results = {"recovered_leads": 0, "skipped_duplicates": 0, "leads_found_in_meta": 0, "errors": 1}

    redirect_url = (
        f"/?review_recovered={results.get('recovered_leads', 0)}"
        f"&review_duplicates={results.get('skipped_duplicates', 0)}"
        f"&review_found={results.get('leads_found_in_meta', 0)}"
        f"&review_errors={results.get('errors', 0)}"
    )
    return RedirectResponse(url=redirect_url, status_code=303)


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
    crm_payload_type: str = Form("raw"),
):
    """Creates a new CRM instance mapping."""
    now = datetime.utcnow()
    db.instance_mappings.insert_one({
        "client_name": client_name,
        "form_id": form_id.strip() or None,
        "page_id": page_id.strip() or None,
        "crm_url": crm_url.strip(),
        "crm_auth_token": crm_auth_token.strip() or None,
        "crm_payload_type": crm_payload_type.strip(),
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
    error_description: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    db: Database = Depends(get_db)
):
    """
    Receives the Facebook Login return.
    - state present → client onboarding flow (renders page selector).
    - state absent  → admin flow (connects all pages, redirects to /mappings).
    """
    redirect_uri = build_callback_url(request)
    logger.info(f"OAuth callback recebido. redirect_uri={redirect_uri} | state={'presente' if state else 'ausente'} | code={'presente' if code else 'ausente'} | error={error}")

    # Validar state se presente
    mapping_doc = None
    if state:
        try:
            mapping_doc = db.instance_mappings.find_one({"_id": ObjectId(state)})
        except Exception:
            mapping_doc = None
        if not mapping_doc:
            return HTMLResponse(
                content="<h2>Erro: Mapeamento não encontrado ou link de onboarding inválido.</h2>",
                status_code=404
            )

    back_url = f"/onboard/{state}" if state else "/mappings"
    back_label = "Voltar para Integração" if state else "Voltar para Mapeamentos"

    if error:
        error_msg = f"{error}: {error_description}" if error_description else error
        logger.error(f"Erro OAuth da Meta: {error_msg}")
        return HTMLResponse(
            content=f"""
            <h2>Erro de autorização da Meta</h2>
            <p><strong>Erro:</strong> {error_msg}</p>
            <p><a href='{back_url}'>{back_label}</a></p>
            """,
            status_code=400
        )

    if not code:
        raise HTTPException(status_code=400, detail="Código de autorização ausente.")

    user_token = services.exchange_code_for_user_token(code, redirect_uri)
    if not user_token:
        logger.error(f"Falha ao trocar o code pelo token. redirect_uri utilizado: {redirect_uri}")
        return HTMLResponse(
            content=f"""
            <h2>Erro: Falha ao obter token de acesso</h2>
            <p>A troca do código de autorização falhou. O motivo mais comum é um <strong>redirect_uri incompatível</strong>.</p>
            <p><strong>redirect_uri utilizado nesta chamada:</strong><br>
            <code style='background:#eee;padding:4px 8px;border-radius:4px;'>{redirect_uri}</code></p>
            <p>Verifique se esse endereço está cadastrado exatamente igual nas <strong>Configurações do App Meta → Produtos → Facebook Login → URIs de redirecionamento OAuth válidos</strong>.</p>
            <p>Se estiver rodando atrás de um proxy/ngrok, configure a variável de ambiente <code>PUBLIC_URL</code> no seu <code>.env</code>.</p>
            <p><a href='{back_url}'>{back_label}</a></p>
            """,
            status_code=400
        )

    pages = services.fetch_user_pages(user_token)
    logger.info(f"OAuth: {len(pages)} página(s) retornada(s) pela Meta para este usuário.")

    # ── CLIENT FLOW: state present ──
    if state:
        client_name = mapping_doc.get("client_name", "seu cliente")

        return templates.TemplateResponse("select_page.html", {
            "request": request,
            "client_name": client_name,
            "mapping_id": state,
            "user_access_token": user_token,
            "pages": pages,
        })

    # ── ADMIN FLOW: connect all pages automatically ──
    if not pages:
        logger.warning("OAuth admin: nenhuma página retornada pela Meta. Verifique se o usuário é administrador de alguma Página do Facebook.")
        return HTMLResponse(
            content="""
            <h2>Nenhuma página encontrada</h2>
            <p>A autenticação foi bem-sucedida, mas a Meta não retornou nenhuma Página do Facebook para este usuário.</p>
            <p>Possíveis causas:</p>
            <ul>
                <li>O usuário autenticado não é administrador de nenhuma Página do Facebook.</li>
                <li>A permissão <strong>pages_show_list</strong> não foi concedida ou não está aprovada no App.</li>
                <li>O App está em modo de desenvolvimento e o usuário não é um testador cadastrado.</li>
            </ul>
            <p><a href='/mappings'>Voltar para Mapeamentos</a></p>
            """,
            status_code=200
        )

    connected_count = 0
    failed_pages = []
    now = datetime.utcnow()
    for page in pages:
        page_id = page["id"]
        page_name = page["name"]
        page_token = page["access_token"]

        subscribed = services.subscribe_page_to_app(page_id, page_token)
        if not subscribed:
            logger.warning(f"Falha ao inscrever webhook para página {page_name} ({page_id}).")
            failed_pages.append(page_name)

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
        logger.info(f"Página conectada: {page_name} ({page_id}) | webhook_ok={not page_name in failed_pages}")

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
        f"&scope=pages_show_list,pages_read_engagement,pages_manage_metadata,pages_manage_ads,leads_retrieval,business_management,ads_read"
        f"&response_type=code"
        f"&auth_type=rerequest"
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
    # Fetch enriched metadata if available in Mongo
    ad_doc = db.ads.find_one({"ad_id": lead.ad_id}) if lead.ad_id else None
    insight_doc = db.insights.find_one({"object_id": lead.ad_id}) if lead.ad_id else None

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
        "ad_details": ad_doc if ad_doc else None,
        "ad_insights": insight_doc if insight_doc else None,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "updated_at": lead.updated_at.isoformat() if lead.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Dashboard — Ads & Insights
# ---------------------------------------------------------------------------

@app.get("/ads", response_class=HTMLResponse, tags=["Dashboard"])
async def dashboard_ads(
    request: Request,
    db: Database = Depends(get_db),
    page: int = Query(1, ge=1),
    search: str = Query(""),
    page_filter: str = Query(""),
    status_filter: str = Query(""),
    group_by: str = Query("page", description="Group ads by 'page', 'campaign', or 'adset'"),
):
    """Dashboard page for listing ads grouped by Page, Campaign, or AdSet (Público)."""
    per_page = 20
    query_filter = {}
    if search:
        query_filter["$or"] = [
            {"ad_id": {"$regex": search, "$options": "i"}},
            {"ad_name": {"$regex": search, "$options": "i"}},
            {"campaign_id": {"$regex": search, "$options": "i"}},
        ]
    if page_filter:
        clean_pfilter = page_filter.replace("act_", "")
        query_filter["$or"] = [
            {"page_id": page_filter},
            {"account_id": clean_pfilter},
        ]
    if status_filter:
        query_filter["$or"] = [
            {"status": status_filter},
            {"effective_status": status_filter},
        ]

    # Pre-cache pages, ad accounts, campaigns, and adsets for fast lookup
    page_name_map: dict[str, str] = {}
    for conn in db.meta_connections.find({}, {"page_id": 1, "page_name": 1}):
        pid = conn.get("page_id")
        pname = conn.get("page_name") or pid or "Desconhecido"
        if pid:
            page_name_map[pid] = pname

    for acc in db.ad_accounts.find({}, {"account_id": 1, "name": 1}):
        acc_id = str(acc.get("account_id"))
        acc_name = acc.get("name")
        if acc_id and acc_name and acc_id not in page_name_map:
            page_name_map[acc_id] = f"{acc_name} ({acc_id})"

    campaigns_map: dict[str, dict] = {}
    for cdoc in db.campaigns.find():
        cid = cdoc.get("campaign_id")
        if cid:
            campaigns_map[cid] = {
                "name": cdoc.get("campaign_name") or f"Campanha {cid}",
                "status": cdoc.get("status"),
                "objective": cdoc.get("objective"),
            }

    adsets_map: dict[str, dict] = {}
    for sdoc in db.adsets.find():
        sid = sdoc.get("adset_id")
        if sid:
            s_obj = models.AdSetDetail(sdoc)
            adsets_map[sid] = {
                "name": s_obj.adset_name or f"Conjunto {sid}",
                "status": s_obj.status,
                "targeting": s_obj.formatted_targeting(),
                "optimization_goal": s_obj.optimization_goal,
            }

    total = db.ads.count_documents(query_filter)
    sort_field = "page_id" if group_by == "page" else ("campaign_id" if group_by == "campaign" else "adset_id")
    raw_ads = list(
        db.ads.find(query_filter)
        .sort([(sort_field, 1), ("updated_at", -1)])
        .skip((page - 1) * per_page)
        .limit(per_page)
    )

    groups: dict[str, dict] = {}

    for doc in raw_ads:
        ad = models.AdDetail(doc)
        insight_doc = db.insights.find_one({"object_id": ad.ad_id, "date_preset": "maximum"})
        if not insight_doc:
            insight_doc = db.insights.find_one({"object_id": ad.ad_id})
        insight = models.AdInsight(insight_doc) if insight_doc else None
        leads_count = db.leads.count_documents({"ad_id": ad.ad_id})

        campaign_info = campaigns_map.get(ad.campaign_id, {
            "name": ad.campaign_id or "Sem Campanha",
            "status": None,
            "objective": None
        })
        adset_info = adsets_map.get(ad.adset_id, {
            "name": ad.adset_id or "Sem Conjunto",
            "status": None,
            "targeting": {"summary": "Sem dados de público", "raw": {}},
            "optimization_goal": None
        })

        spend = insight.spend if insight else 0.0
        cpl = round(spend / leads_count, 2) if leads_count > 0 else 0.0

        ad_dict = {
            "ad_id": ad.ad_id,
            "ad_name": ad.ad_name,
            "status": ad.status,
            "effective_status": ad.effective_status,
            "adset_id": ad.adset_id,
            "adset_name": adset_info["name"],
            "targeting_summary": adset_info["targeting"]["summary"],
            "targeting": adset_info["targeting"],
            "campaign_id": ad.campaign_id,
            "campaign_name": campaign_info["name"],
            "campaign_objective": campaign_info.get("objective"),
            "start_time": ad.start_time,
            "stop_time": ad.stop_time,
            "meta_created_time": ad.meta_created_time,
            "creative_id": ad.creative_id,
            "creative_title": ad.creative_title,
            "creative_body": ad.creative_body,
            "creative_image_url": ad.creative_image_url,
            "creative_thumbnail_url": ad.creative_thumbnail_url,
            "call_to_action": ad.call_to_action,
            "page_id": ad.page_id,
            "page_name": page_name_map.get(ad.page_id, ad.page_id or "Sem Página"),
        }

        insight_dict = None
        if insight:
            insight_dict = {
                "object_id": insight.object_id,
                "spend": insight.spend,
                "impressions": insight.impressions,
                "clicks": insight.clicks,
                "reach": insight.reach,
                "frequency": insight.frequency,
                "cpc": insight.cpc,
                "cpm": insight.cpm,
                "ctr": insight.ctr,
                "conversions": insight.conversions,
                "date_start": insight.date_start,
                "date_stop": insight.date_stop,
                "cpl": cpl,
            }

        # Determine group key and label
        if group_by == "campaign":
            g_key = ad.campaign_id or "__no_campaign__"
            g_name = f"Campanha: {campaign_info['name']}"
            g_sub = f"ID: {ad.campaign_id}" if ad.campaign_id else ""
        elif group_by == "adset":
            g_key = ad.adset_id or "__no_adset__"
            g_name = f"Público / Conjunto: {adset_info['name']}"
            g_sub = f"Targeting: {adset_info['targeting']['summary']}"
        else: # "page"
            g_key = ad.page_id or doc.get("account_id") or "__unknown__"
            g_name = page_name_map.get(g_key, f"Conta/Página {g_key}" if g_key != "__unknown__" else "Sem Página/Conta")
            g_sub = f"ID: {g_key}"

        if g_key not in groups:
            groups[g_key] = {
                "group_key": g_key,
                "group_name": g_name,
                "group_sub": g_sub,
                "page_id": ad.page_id,
                "page_name": page_name_map.get(ad.page_id, "Sem Página"),
                "ads": [],
                "total_leads": 0,
                "total_spend": 0.0,
            }

        groups[g_key]["ads"].append({
            "ad": ad_dict,
            "insight": insight_dict,
            "leads_count": leads_count,
            "cpl": cpl,
        })
        groups[g_key]["total_leads"] += leads_count
        if insight_dict:
            groups[g_key]["total_spend"] += insight_dict["spend"]

    all_pages = [
        {"page_id": pid, "page_name": name}
        for pid, name in sorted(page_name_map.items(), key=lambda x: x[1])
    ]

    sync_started = bool(request.query_params.get("sync_started"))
    sync_result = None
    synced_param = request.query_params.get("sync_ads")
    if synced_param is not None:
        sync_result = {
            "ads_synced": int(synced_param),
            "insights_synced": int(request.query_params.get("sync_insights", 0)),
            "campaigns_synced": int(request.query_params.get("sync_campaigns", 0)),
        }

    return templates.TemplateResponse("ads.html", {
        "request": request,
        "ads_by_page": list(groups.values()),
        "all_pages": all_pages,
        "page_filter": page_filter,
        "status_filter": status_filter,
        "group_by": group_by,
        "page": page,
        "total": total,
        "per_page": per_page,
        "search": search,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "sync_result": sync_result,
        "sync_started": sync_started,
    })



@app.post("/admin/sync-ads", tags=["Dashboard"])
async def manual_sync_ads(background_tasks: BackgroundTasks, db: Database = Depends(get_db)):
    """Manually triggers full Meta Ads & Insights synchronization in background."""
    background_tasks.add_task(services.sync_all_meta_objects, db)
    return RedirectResponse(url="/ads?sync_started=1", status_code=303)


# ---------------------------------------------------------------------------
# REST API (Ads, Campaigns, Insights, AdAccounts)
# ---------------------------------------------------------------------------

@app.get("/api/ads", tags=["API"])
async def api_list_ads(
    db: Database = Depends(get_db),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    """REST API to list all cached Ads with their creatives and insights."""
    total = db.ads.count_documents({})
    raw_ads = list(
        db.ads.find()
        .sort("updated_at", -1)
        .skip(offset)
        .limit(limit)
    )

    results = []
    for doc in raw_ads:
        ad = models.AdDetail(doc)
        insight_doc = db.insights.find_one({"object_id": ad.ad_id})
        results.append({
            "ad_id": ad.ad_id,
            "ad_name": ad.ad_name,
            "status": ad.status,
            "effective_status": ad.effective_status,
            "adset_id": ad.adset_id,
            "campaign_id": ad.campaign_id,
            "creative": {
                "id": ad.creative_id,
                "title": ad.creative_title,
                "body": ad.creative_body,
                "image_url": ad.creative_image_url,
                "thumbnail_url": ad.creative_thumbnail_url,
                "call_to_action": ad.call_to_action,
            },
            "insights": {
                "spend": insight_doc.get("spend", 0.0) if insight_doc else 0.0,
                "impressions": insight_doc.get("impressions", 0) if insight_doc else 0,
                "clicks": insight_doc.get("clicks", 0) if insight_doc else 0,
                "reach": insight_doc.get("reach", 0) if insight_doc else 0,
                "cpc": insight_doc.get("cpc", 0.0) if insight_doc else 0.0,
                "ctr": insight_doc.get("ctr", 0.0) if insight_doc else 0.0,
                "conversions": insight_doc.get("conversions", 0) if insight_doc else 0,
            } if insight_doc else None,
            "updated_at": ad.updated_at.isoformat() if ad.updated_at else None,
        })

    return {"total": total, "results": results}


@app.get("/api/ads/{ad_id}", tags=["API"])
async def api_get_ad(ad_id: str, db: Database = Depends(get_db)):
    """Returns detailed information and creative data for a specific ad_id."""
    doc = db.ads.find_one({"ad_id": ad_id})
    if not doc:
        # Try fetching live from Meta
        doc = services.fetch_ad_details(ad_id, db)
    if not doc:
        raise HTTPException(status_code=404, detail="Anúncio não encontrado.")

    ad = models.AdDetail(doc)
    insight_doc = db.insights.find_one({"object_id": ad.ad_id})
    leads_count = db.leads.count_documents({"ad_id": ad.ad_id})

    return {
        "ad_id": ad.ad_id,
        "ad_name": ad.ad_name,
        "status": ad.status,
        "effective_status": ad.effective_status,
        "adset_id": ad.adset_id,
        "campaign_id": ad.campaign_id,
        "creative": {
            "id": ad.creative_id,
            "title": ad.creative_title,
            "body": ad.creative_body,
            "image_url": ad.creative_image_url,
            "thumbnail_url": ad.creative_thumbnail_url,
            "call_to_action": ad.call_to_action,
        },
        "leads_captured": leads_count,
        "insights": insight_doc if insight_doc else None,
        "updated_at": ad.updated_at.isoformat() if ad.updated_at else None,
    }


@app.get("/api/ads/{ad_id}/insights", tags=["API"])
async def api_get_ad_insights(
    ad_id: str,
    date_preset: str = Query("maximum"),
    db: Database = Depends(get_db),
):
    """Fetches performance insights (spend, clicks, impressions, conversions) for an ad."""
    insight_doc = db.insights.find_one({"object_id": ad_id, "date_preset": date_preset})
    if not insight_doc:
        insight_doc = services.fetch_object_insights(ad_id, db, object_type="ad", date_preset=date_preset)
    if not insight_doc:
        raise HTTPException(status_code=404, detail="Métricas de anúncios não encontradas.")
    return {
        "object_id": insight_doc.get("object_id"),
        "object_type": insight_doc.get("object_type"),
        "spend": insight_doc.get("spend", 0.0),
        "impressions": insight_doc.get("impressions", 0),
        "clicks": insight_doc.get("clicks", 0),
        "reach": insight_doc.get("reach", 0),
        "frequency": insight_doc.get("frequency", 0.0),
        "cpc": insight_doc.get("cpc", 0.0),
        "cpm": insight_doc.get("cpm", 0.0),
        "ctr": insight_doc.get("ctr", 0.0),
        "conversions": insight_doc.get("conversions", 0),
        "date_preset": insight_doc.get("date_preset"),
        "date_start": insight_doc.get("date_start"),
        "date_stop": insight_doc.get("date_stop"),
    }


@app.get("/api/campaigns/{campaign_id}", tags=["API"])
async def api_get_campaign(campaign_id: str, db: Database = Depends(get_db)):
    """Returns details and metrics for a campaign."""
    doc = db.campaigns.find_one({"campaign_id": campaign_id})
    if not doc:
        doc = services.fetch_campaign_details(campaign_id, db)
    if not doc:
        raise HTTPException(status_code=404, detail="Campanha não encontrada.")

    campaign = models.CampaignDetail(doc)
    insight_doc = db.insights.find_one({"object_id": campaign_id})
    leads_count = db.leads.count_documents({"campaign_id": campaign_id})

    return {
        "campaign_id": campaign.campaign_id,
        "campaign_name": campaign.campaign_name,
        "status": campaign.status,
        "objective": campaign.objective,
        "daily_budget": campaign.daily_budget,
        "lifetime_budget": campaign.lifetime_budget,
        "buying_type": campaign.buying_type,
        "leads_captured": leads_count,
        "insights": insight_doc if insight_doc else None,
        "updated_at": campaign.updated_at.isoformat() if campaign.updated_at else None,
    }


@app.get("/api/adaccounts", tags=["API"])
async def api_list_ad_accounts(db: Database = Depends(get_db)):
    """Lists cached Ad Accounts."""
    accounts = list(db.ad_accounts.find().sort("name", 1))
    return {
        "total": len(accounts),
        "accounts": [
            {
                "account_id": doc.get("account_id"),
                "name": doc.get("name"),
                "account_status": doc.get("account_status"),
                "currency": doc.get("currency"),
                "timezone_name": doc.get("timezone_name"),
            }
            for doc in accounts
        ]
    }


# ---------------------------------------------------------------------------
# REST API — Metrics (designed for external AI analysis)
# ---------------------------------------------------------------------------

@app.get("/api/metrics", tags=["API"])
async def api_metrics_by_page(
    db: Database = Depends(get_db),
    page_id: str = Query("", description="Filter by a specific Facebook Page ID"),
    campaign_id: str = Query("", description="Filter by a specific Campaign ID"),
    adset_id: str = Query("", description="Filter by a specific AdSet/Audience ID"),
    status: str = Query("", description="Filter ads by status (ACTIVE, PAUSED, ARCHIVED, DELETED...)"),
    date_preset: str = Query("maximum", description="Insights date preset (maximum, last_30d, last_7d, today...)"),
    group_by: str = Query("page", description="Group results by 'page', 'campaign', or 'adset'"),
    include_creatives: bool = Query(False, description="Include ad creative details (title, body, image_url)"),
    include_targeting: bool = Query(True, description="Include audience targeting specifications (age, gender, interests, geo)"),
):
    """
    Returns full ad performance metrics and hierarchy details designed for external AI analysis.

    Each group (by Page, Campaign, or AdSet) contains:
    - Group metadata & totals (spend, leads, CPL, impressions, clicks)
    - Full list of ads with associated Campaign Name, AdSet Name, Audience Targeting, Dates, and Performance Metrics

    **Filters & Parameters:**
    - `page_id`: Filter by page
    - `campaign_id`: Filter by campaign
    - `adset_id`: Filter by audience/adset
    - `status`: Filter by status (ACTIVE, PAUSED, etc.)
    - `group_by`: Group by `page`, `campaign`, or `adset`
    - `include_creatives`: Set `true` to include ad copy and image URLs
    - `include_targeting`: Set `true` to include audience targeting (age, gender, interests, geo, custom audiences)
    """
    # Pre-cache lookup maps
    page_name_map: dict[str, str] = {}
    for conn in db.meta_connections.find({}, {"page_id": 1, "page_name": 1}):
        pid = conn.get("page_id")
        if pid:
            page_name_map[pid] = conn.get("page_name") or pid

    campaigns_map: dict[str, dict] = {}
    for cdoc in db.campaigns.find():
        cid = cdoc.get("campaign_id")
        if cid:
            campaigns_map[cid] = {
                "campaign_name": cdoc.get("campaign_name") or f"Campanha {cid}",
                "status": cdoc.get("status"),
                "objective": cdoc.get("objective"),
            }

    adsets_map: dict[str, dict] = {}
    for sdoc in db.adsets.find():
        sid = sdoc.get("adset_id")
        if sid:
            s_obj = models.AdSetDetail(sdoc)
            adsets_map[sid] = {
                "adset_name": s_obj.adset_name or f"Conjunto {sid}",
                "status": s_obj.status,
                "targeting": s_obj.formatted_targeting(),
            }

    # Build query
    ad_filter: dict = {}
    if page_id:
        ad_filter["page_id"] = page_id
    if campaign_id:
        ad_filter["campaign_id"] = campaign_id
    if adset_id:
        ad_filter["adset_id"] = adset_id
    if status:
        ad_filter["$or"] = [{"status": status.upper()}, {"effective_status": status.upper()}]

    sort_key = "page_id" if group_by == "page" else ("campaign_id" if group_by == "campaign" else "adset_id")
    all_ads = list(db.ads.find(ad_filter).sort([(sort_key, 1), ("ad_name", 1)]))

    groups: dict[str, dict] = {}

    for ad_doc in all_ads:
        ad = models.AdDetail(ad_doc)

        insight_doc = db.insights.find_one({"object_id": ad.ad_id, "date_preset": date_preset})
        if not insight_doc and date_preset != "maximum":
            insight_doc = db.insights.find_one({"object_id": ad.ad_id})

        insight = models.AdInsight(insight_doc) if insight_doc else None
        leads_count = db.leads.count_documents({"ad_id": ad.ad_id})

        campaign_info = campaigns_map.get(ad.campaign_id, {
            "campaign_name": ad.campaign_id or "Sem Campanha",
            "status": None,
            "objective": None
        })
        adset_info = adsets_map.get(ad.adset_id, {
            "adset_name": ad.adset_id or "Sem Conjunto",
            "status": None,
            "targeting": {"summary": "Sem dados de público", "raw": {}}
        })

        spend = insight.spend if insight else 0.0
        cpl = round(spend / leads_count, 2) if leads_count > 0 else 0.0

        ad_entry: dict = {
            "ad_id": ad.ad_id,
            "ad_name": ad.ad_name,
            "status": ad.status,
            "effective_status": ad.effective_status,
            "start_time": ad.start_time,
            "stop_time": ad.stop_time,
            "campaign": {
                "campaign_id": ad.campaign_id,
                "campaign_name": campaign_info["campaign_name"],
                "objective": campaign_info["objective"],
            },
            "adset": {
                "adset_id": ad.adset_id,
                "adset_name": adset_info["adset_name"],
                "targeting_summary": adset_info["targeting"]["summary"],
            },
            "leads_captured": leads_count,
            "cpl": cpl,
            "metrics": {
                "spend": spend,
                "impressions": insight.impressions if insight else 0,
                "clicks": insight.clicks if insight else 0,
                "reach": insight.reach if insight else 0,
                "frequency": insight.frequency if insight else 0.0,
                "ctr": insight.ctr if insight else 0.0,
                "cpc": insight.cpc if insight else 0.0,
                "cpm": insight.cpm if insight else 0.0,
                "conversions": insight.conversions if insight else 0,
                "date_preset": insight.date_preset if insight else date_preset,
                "date_start": insight.date_start if insight else None,
                "date_stop": insight.date_stop if insight else None,
            } if insight else None,
        }

        if include_targeting:
            ad_entry["adset"]["targeting_details"] = adset_info["targeting"]

        if include_creatives:
            ad_entry["creative"] = {
                "title": ad.creative_title,
                "body": ad.creative_body,
                "image_url": ad.creative_image_url,
                "thumbnail_url": ad.creative_thumbnail_url,
                "call_to_action": ad.call_to_action,
            }

        # Determine grouping
        if group_by == "campaign":
            g_id = ad.campaign_id or "__no_campaign__"
            g_title = campaign_info["campaign_name"]
        elif group_by == "adset":
            g_id = ad.adset_id or "__no_adset__"
            g_title = adset_info["adset_name"]
        else: # "page"
            g_id = ad.page_id or "__unknown__"
            g_title = page_name_map.get(g_id, g_id if g_id != "__unknown__" else "Sem Página")

        if g_id not in groups:
            groups[g_id] = {
                "group_id": g_id,
                "group_name": g_title,
                "page_id": ad.page_id,
                "page_name": page_name_map.get(ad.page_id, "Sem Página"),
                "totals": {
                    "ads_count": 0,
                    "total_leads": 0,
                    "total_spend": 0.0,
                    "total_impressions": 0,
                    "total_clicks": 0,
                    "avg_cpl": 0.0,
                },
                "ads": [],
            }

        groups[g_id]["ads"].append(ad_entry)
        groups[g_id]["totals"]["ads_count"] += 1
        groups[g_id]["totals"]["total_leads"] += leads_count
        if insight:
            groups[g_id]["totals"]["total_spend"] += insight.spend
            groups[g_id]["totals"]["total_impressions"] += insight.impressions
            groups[g_id]["totals"]["total_clicks"] += insight.clicks

    result = list(groups.values())
    for g in result:
        tot = g["totals"]
        tot["total_spend"] = round(tot["total_spend"], 2)
        tot["avg_cpl"] = round(tot["total_spend"] / tot["total_leads"], 2) if tot["total_leads"] > 0 else 0.0

    total_leads_all = sum(g["totals"]["total_leads"] for g in result)
    total_spend_all = round(sum(g["totals"]["total_spend"] for g in result), 2)

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "filters": {
            "page_id": page_id or None,
            "campaign_id": campaign_id or None,
            "adset_id": adset_id or None,
            "status": status or None,
            "date_preset": date_preset,
            "group_by": group_by,
            "include_creatives": include_creatives,
            "include_targeting": include_targeting,
        },
        "summary": {
            "total_groups": len(result),
            "total_ads": sum(g["totals"]["ads_count"] for g in result),
            "total_leads": total_leads_all,
            "total_spend": total_spend_all,
            "avg_cpl": round(total_spend_all / total_leads_all, 2) if total_leads_all > 0 else 0.0,
            "total_impressions": sum(g["totals"]["total_impressions"] for g in result),
            "total_clicks": sum(g["totals"]["total_clicks"] for g in result),
        },
        "groups": result,
    }

