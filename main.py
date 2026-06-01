"""
main.py — Aplicação FastAPI: webhook Meta + dashboard de administração.
"""
import hashlib
import hmac
import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import models
import services
from database import engine, get_db

load_dotenv()

# ---------------------------------------------------------------------------
# Inicialização
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Criar tabelas no banco se não existirem
models.Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Meta Leads Central Hub",
    description="Central que recebe, armazena e distribui leads da Meta para instâncias de CRM dos clientes.",
    version="1.0.0",
)

templates = Jinja2Templates(directory="templates")

META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "changeme")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
META_APP_ID = os.getenv("META_APP_ID", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def verify_meta_signature(payload_body: bytes, signature_header: Optional[str]) -> bool:
    """Valida a assinatura HMAC-SHA256 enviada pela Meta."""
    if not META_APP_SECRET or not signature_header:
        return True  # Se sem segredo configurado, permite (dev)
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
    """Endpoint de verificação do webhook exigido pela Meta."""
    if hub_mode == "subscribe" and hub_verify_token == META_VERIFY_TOKEN:
        logger.info("Webhook verificado com sucesso pela Meta.")
        return PlainTextResponse(content=hub_challenge)
    logger.warning("Tentativa de verificação inválida.")
    raise HTTPException(status_code=403, detail="Verificação inválida.")


@app.post("/webhook", tags=["Webhook"])
async def webhook_receive(request: Request, db: Session = Depends(get_db)):
    """Recebe eventos de novos leads da Meta e os processa de forma assíncrona."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_meta_signature(body, signature):
        logger.warning("Assinatura do webhook inválida.")
        raise HTTPException(status_code=403, detail="Assinatura inválida.")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Payload JSON inválido.")

    logger.info(f"Webhook recebido: {json.dumps(payload)[:300]}")

    # Iterar sobre os eventos recebidos
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

            # Processar em background (não bloqueia o ACK para a Meta)
            try:
                services.process_lead_event(
                    db=db,
                    lead_gen_id=str(lead_gen_id),
                    form_id=str(form_id) if form_id else None,
                    page_id=str(page_id) if page_id else None,
                    raw_payload=payload,
                )
            except Exception as e:
                logger.error(f"Erro ao processar lead_gen_id={lead_gen_id}: {e}")

    # A Meta exige 200 OK rápido
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Dashboard — Leads
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, tags=["Dashboard"])
async def dashboard_leads(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    status: str = Query(""),
    search: str = Query(""),
):
    """Dashboard principal: lista todos os leads recebidos."""
    per_page = 20
    query = db.query(models.Lead)

    if status:
        query = query.filter(models.Lead.status == status)
    if search:
        query = query.filter(
            models.Lead.lead_id.contains(search)
            | models.Lead.form_id.contains(search)
            | models.Lead.page_id.contains(search)
            | models.Lead.fields_json.contains(search)
        )

    total = query.count()
    leads = query.order_by(models.Lead.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()

    # Deserializar campos para exibição
    leads_display = []
    for lead in leads:
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
        "total": db.query(models.Lead).count(),
        "forwarded": db.query(models.Lead).filter(models.Lead.status == "forwarded").count(),
        "failed": db.query(models.Lead).filter(models.Lead.status == "failed").count(),
        "skipped": db.query(models.Lead).filter(models.Lead.status == "skipped").count(),
        "received": db.query(models.Lead).filter(models.Lead.status == "received").count(),
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
async def retry_lead(lead_id: int, db: Session = Depends(get_db)):
    """Reprocessa um lead com status failed ou skipped."""
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado.")

    raw_payload = json.loads(lead.raw_payload) if lead.raw_payload else {}
    lead.status = "received"
    lead.error_message = None
    db.commit()

    services.process_lead_event(
        db=db,
        lead_gen_id=lead.lead_id,
        form_id=lead.form_id,
        page_id=lead.page_id,
        raw_payload=raw_payload,
    )
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Dashboard — Mapeamentos de Instâncias
# ---------------------------------------------------------------------------

@app.get("/mappings", response_class=HTMLResponse, tags=["Mappings"])
async def list_mappings(request: Request, db: Session = Depends(get_db)):
    """Lista mapeamentos de instâncias e conexões ativas com páginas da Meta."""
    mappings = db.query(models.InstanceMapping).order_by(models.InstanceMapping.client_name).all()
    connections = db.query(models.MetaConnection).order_by(models.MetaConnection.page_name).all()

    # Adicionar variável do App ID para montar a URL de Login no front
    app_id = META_APP_ID
    # Tentar montar redirect_uri baseado no host atual
    redirect_uri = ""
    if app_id:
        redirect_uri = f"{request.base_url.scheme}://{request.base_url.netloc}/oauth/callback"

    return templates.TemplateResponse("mappings.html", {
        "request": request,
        "mappings": mappings,
        "connections": connections,
        "app_id": app_id,
        "redirect_uri": redirect_uri
    })


@app.post("/mappings/create", tags=["Mappings"])
async def create_mapping(
    db: Session = Depends(get_db),
    client_name: str = Form(...),
    form_id: str = Form(""),
    page_id: str = Form(""),
    crm_url: str = Form(...),
    crm_auth_token: str = Form(""),
):
    """Cria um novo mapeamento de instância."""
    mapping = models.InstanceMapping(
        client_name=client_name,
        form_id=form_id.strip() or None,
        page_id=page_id.strip() or None,
        crm_url=crm_url.strip(),
        crm_auth_token=crm_auth_token.strip() or None,
        active=True,
    )
    db.add(mapping)
    db.commit()
    return RedirectResponse(url="/mappings", status_code=303)


@app.post("/mappings/{mapping_id}/toggle", tags=["Mappings"])
async def toggle_mapping(mapping_id: int, db: Session = Depends(get_db)):
    """Ativa ou desativa um mapeamento."""
    mapping = db.query(models.InstanceMapping).filter(models.InstanceMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapeamento não encontrado.")
    mapping.active = not mapping.active
    db.commit()
    return RedirectResponse(url="/mappings", status_code=303)


@app.post("/mappings/{mapping_id}/delete", tags=["Mappings"])
async def delete_mapping(mapping_id: int, db: Session = Depends(get_db)):
    """Remove um mapeamento."""
    mapping = db.query(models.InstanceMapping).filter(models.InstanceMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapeamento não encontrado.")
    db.delete(mapping)
    db.commit()
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
    db: Session = Depends(get_db)
):
    """
    Recebe o retorno do Facebook Login.
    - state presente → fluxo de onboarding do cliente (renderiza seletor de página).
    - state ausente  → fluxo administrativo (conecta todas as páginas, redireciona para /mappings).
    """
    # Montar URL de retorno base
    redirect_uri = f"{request.base_url.scheme}://{request.base_url.netloc}/oauth/callback"

    if error:
        logger.error(f"Erro no retorno do OAuth da Meta: {error}")
        back_url = f"/onboard/{state}" if state else "/mappings"
        return templates.TemplateResponse("onboard_landing.html", {
            "request": request,
            "client_name": "cliente",
            "app_id": None,
            "oauth_url": "",
            "error": error
        }, status_code=400)

    if not code:
        raise HTTPException(status_code=400, detail="Código de autorização ausente.")

    # Trocar código por token de longa duração do usuário
    user_token = services.exchange_code_for_user_token(code, redirect_uri)
    if not user_token:
        return HTMLResponse(
            content="<h2>Erro</h2><p>Não foi possível obter o token de acesso.</p>",
            status_code=400
        )

    # Buscar páginas do usuário
    pages = services.fetch_user_pages(user_token)

    # ── FLUXO CLIENTE: state presente (onboarding de mapping_id específico) ──
    if state:
        mapping_id = state
        mapping = db.query(models.InstanceMapping).filter(
            models.InstanceMapping.id == int(mapping_id)
        ).first()
        client_name = mapping.client_name if mapping else "seu cliente"

        return templates.TemplateResponse("select_page.html", {
            "request": request,
            "client_name": client_name,
            "mapping_id": mapping_id,
            "user_access_token": user_token,
            "pages": pages,
        })

    # ── FLUXO ADMIN: sem state, conecta todas as páginas automaticamente ──
    connected_count = 0
    for page in pages:
        page_id = page["id"]
        page_name = page["name"]
        page_token = page["access_token"]

        services.subscribe_page_to_app(page_id, page_token)

        conn = db.query(models.MetaConnection).filter(models.MetaConnection.page_id == page_id).first()
        if not conn:
            conn = models.MetaConnection(
                page_id=page_id,
                page_name=page_name,
                page_access_token=page_token,
                user_access_token=user_token,
                connected_by="Admin Central",
                active=True
            )
            db.add(conn)
        else:
            conn.page_name = page_name
            conn.page_access_token = page_token
            conn.user_access_token = user_token
            conn.active = True

        db.commit()
        connected_count += 1

    return RedirectResponse(url=f"/mappings?oauth_success={connected_count}", status_code=303)


# ---------------------------------------------------------------------------
# Onboarding de Clientes
# ---------------------------------------------------------------------------

@app.get("/onboard/{mapping_id}", response_class=HTMLResponse, tags=["Onboarding"])
async def onboard_landing(
    request: Request,
    mapping_id: int,
    db: Session = Depends(get_db)
):
    """Landing page personalizada para o cliente iniciar o Facebook Login."""
    mapping = db.query(models.InstanceMapping).filter(
        models.InstanceMapping.id == mapping_id
    ).first()
    if not mapping:
        return HTMLResponse(content="<h2>Link inválido ou expirado.</h2>", status_code=404)

    app_id = META_APP_ID
    redirect_uri = f"{request.base_url.scheme}://{request.base_url.netloc}/oauth/callback"

    # Montar URL do diálogo OAuth com state = mapping_id
    oauth_url = (
        f"https://www.facebook.com/v19.0/dialog/oauth"
        f"?client_id={app_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=pages_show_list,pages_read_engagement,leads_retrieval"
        f"&state={mapping_id}"
    ) if app_id else ""

    return templates.TemplateResponse("onboard_landing.html", {
        "request": request,
        "client_name": mapping.client_name,
        "app_id": app_id,
        "oauth_url": oauth_url,
    })


@app.post("/onboard/complete", tags=["Onboarding"])
async def onboard_complete(
    request: Request,
    mapping_id: int = Form(...),
    page_id: str = Form(...),
    page_name: str = Form(...),
    page_access_token: str = Form(...),
    user_access_token: str = Form(...),
    db: Session = Depends(get_db)
):
    """
    Finaliza o onboarding do cliente:
    - Registra o webhook na página selecionada.
    - Salva/atualiza o MetaConnection com o page_access_token.
    - Vincula o page_id ao InstanceMapping do cliente.
    """
    mapping = db.query(models.InstanceMapping).filter(
        models.InstanceMapping.id == mapping_id
    ).first()
    if not mapping:
        return HTMLResponse(content="<h2>Mapeamento não encontrado.</h2>", status_code=404)

    client_name = mapping.client_name

    # 1. Inscrever webhook na página selecionada
    subscribed = services.subscribe_page_to_app(page_id, page_access_token)
    if not subscribed:
        logger.warning(f"Onboarding {client_name}: não foi possível inscrever webhook na página {page_name}.")

    # 2. Salvar / atualizar MetaConnection
    conn = db.query(models.MetaConnection).filter(models.MetaConnection.page_id == page_id).first()
    if not conn:
        conn = models.MetaConnection(
            page_id=page_id,
            page_name=page_name,
            page_access_token=page_access_token,
            user_access_token=user_access_token,
            connected_by=client_name,
            active=True
        )
        db.add(conn)
    else:
        conn.page_name = page_name
        conn.page_access_token = page_access_token
        conn.user_access_token = user_access_token
        conn.connected_by = client_name
        conn.active = True

    # 3. Vincular page_id ao mapeamento do cliente
    mapping.page_id = page_id
    db.commit()

    logger.info(f"Onboarding concluído: cliente={client_name}, page={page_name} ({page_id}), mapping_id={mapping_id}")

    return templates.TemplateResponse("onboard_success.html", {
        "request": request,
        "client_name": client_name,
        "page_name": page_name,
        "page_id": page_id,
    })


@app.post("/connections/{connection_id}/toggle", tags=["Mappings"])
async def toggle_connection(connection_id: int, db: Session = Depends(get_db)):
    """Ativa ou desativa uma conexão de página."""
    conn = db.query(models.MetaConnection).filter(models.MetaConnection.page_id == str(connection_id)).first()
    if not conn:
        # Fallback se for ID numérico
        conn = db.query(models.MetaConnection).filter(models.MetaConnection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Conexão não encontrada.")
    conn.active = not conn.active
    db.commit()
    return RedirectResponse(url="/mappings", status_code=303)


@app.post("/connections/{connection_id}/delete", tags=["Mappings"])
async def delete_connection(connection_id: int, db: Session = Depends(get_db)):
    """Remove uma conexão de página."""
    conn = db.query(models.MetaConnection).filter(models.MetaConnection.page_id == str(connection_id)).first()
    if not conn:
        # Fallback se for ID numérico
        conn = db.query(models.MetaConnection).filter(models.MetaConnection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Conexão não encontrada.")
    db.delete(conn)
    db.commit()
    return RedirectResponse(url="/mappings", status_code=303)


# ---------------------------------------------------------------------------
# API REST (para integração externa)
# ---------------------------------------------------------------------------

@app.get("/api/leads", tags=["API"])
async def api_list_leads(
    db: Session = Depends(get_db),
    status: str = Query(""),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    """API REST para listar leads."""
    query = db.query(models.Lead)
    if status:
        query = query.filter(models.Lead.status == status)
    total = query.count()
    leads = query.order_by(models.Lead.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "results": [
            {
                "id": l.id,
                "lead_id": l.lead_id,
                "form_id": l.form_id,
                "page_id": l.page_id,
                "status": l.status,
                "forwarded_to": l.forwarded_to,
                "fields": l.get_fields(),
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in leads
        ],
    }


@app.get("/api/leads/{lead_id}", tags=["API"])
async def api_get_lead(lead_id: str, db: Session = Depends(get_db)):
    """Retorna um lead específico pelo seu lead_id da Meta."""
    lead = db.query(models.Lead).filter(models.Lead.lead_id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado.")
    return {
        "id": lead.id,
        "lead_id": lead.lead_id,
        "form_id": lead.form_id,
        "page_id": lead.page_id,
        "ad_id": lead.ad_id,
        "adset_id": lead.adset_id,
        "campaign_id": lead.campaign_id,
        "status": lead.status,
        "forwarded_to": lead.forwarded_to,
        "forward_response": lead.forward_response,
        "error_message": lead.error_message,
        "fields": lead.get_fields(),
        "raw_payload": json.loads(lead.raw_payload) if lead.raw_payload else None,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "updated_at": lead.updated_at.isoformat() if lead.updated_at else None,
    }
