"""
services.py — Lógica de negócio: buscar lead na Meta, mapear instância e encaminhar.
"""
import json
import logging
import os
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from models import Lead, InstanceMapping, MetaConnection

logger = logging.getLogger(__name__)

META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
META_GRAPH_API_VERSION = "v19.0"


# ---------------------------------------------------------------------------
# 1. Buscar detalhes completos do lead na Meta Graph API
# ---------------------------------------------------------------------------

def fetch_lead_details(lead_id: str, db: Session, page_id: Optional[str] = None) -> Optional[dict]:
    """
    Usa a Meta Graph API para buscar todos os campos do lead pelo lead_id.
    Tenta usar o token específico da página conectada via OAuth ou o global no .env.
    Retorna um dict com os dados ou None em caso de erro.
    """
    access_token = META_ACCESS_TOKEN

    if page_id:
        conn = db.query(MetaConnection).filter(
            MetaConnection.page_id == page_id,
            MetaConnection.active == True
        ).first()
        if conn:
            access_token = conn.page_access_token
            logger.info(f"Usando token de acesso da página conectada via OAuth: {conn.page_name}")

    if not access_token:
        logger.warning("Token de acesso não configurado (nem dinâmico OAuth, nem global no .env). Não é possível buscar detalhes do lead.")
        return None

    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{lead_id}"
    params = {
        "fields": "id,created_time,field_data,form_id,ad_id,adset_id,campaign_id,page_id",
        "access_token": access_token,
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Erro HTTP ao buscar lead {lead_id}: {e.response.text}")
    except Exception as e:
        logger.error(f"Erro ao buscar lead {lead_id}: {e}")

    return None


# ---------------------------------------------------------------------------
# 2. Encontrar o mapeamento de instância correto
# ---------------------------------------------------------------------------

def find_mapping(db: Session, form_id: Optional[str], page_id: Optional[str]) -> Optional[InstanceMapping]:
    """
    Busca o mapeamento de instância por form_id (prioridade) ou page_id.
    """
    if form_id:
        mapping = db.query(InstanceMapping).filter(
            InstanceMapping.form_id == form_id,
            InstanceMapping.active == True,
        ).first()
        if mapping:
            return mapping

    if page_id:
        mapping = db.query(InstanceMapping).filter(
            InstanceMapping.page_id == page_id,
            InstanceMapping.active == True,
        ).first()
        return mapping

    return None


# ---------------------------------------------------------------------------
# 3. Salvar lead no banco de dados
# ---------------------------------------------------------------------------

def save_lead(db: Session, lead_data: dict, raw_payload: dict) -> Lead:
    """
    Persiste o lead no banco de dados local.
    lead_data: dados completos vindos da Graph API (ou payload bruto).
    """
    lead_id = lead_data.get("id") or raw_payload.get("leadgen_id", "unknown")

    # Evitar duplicatas
    existing = db.query(Lead).filter(Lead.lead_id == lead_id).first()
    if existing:
        logger.info(f"Lead {lead_id} já existe no banco. Ignorando duplicata.")
        return existing

    # Montar campos do formulário (field_data é uma lista de {name, values})
    fields = {}
    for field in lead_data.get("field_data", []):
        values = field.get("values", [])
        fields[field.get("name")] = values[0] if len(values) == 1 else values

    lead = Lead(
        lead_id=lead_id,
        form_id=lead_data.get("form_id"),
        page_id=lead_data.get("page_id"),
        ad_id=lead_data.get("ad_id"),
        adset_id=lead_data.get("adset_id"),
        campaign_id=lead_data.get("campaign_id"),
        fields_json=json.dumps(fields, ensure_ascii=False),
        raw_payload=json.dumps(raw_payload, ensure_ascii=False),
        status="received",
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    logger.info(f"Lead {lead_id} salvo com id={lead.id}.")
    return lead


# ---------------------------------------------------------------------------
# 4. Encaminhar lead para o CRM da instância correta
# ---------------------------------------------------------------------------

def forward_lead(db: Session, lead: Lead, mapping: InstanceMapping) -> bool:
    """
    Envia o lead via HTTP POST para a URL do CRM do cliente.
    Atualiza o status do lead no banco de dados.
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

    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(mapping.crm_url, json=payload, headers=headers)
            response.raise_for_status()

        lead.status = "forwarded"
        lead.forwarded_to = mapping.crm_url
        lead.forward_response = response.text[:1000]  # Limitar tamanho
        db.commit()
        logger.info(f"Lead {lead.lead_id} encaminhado para {mapping.crm_url} com sucesso.")
        return True

    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:500]}"
        lead.status = "failed"
        lead.forwarded_to = mapping.crm_url
        lead.error_message = error_msg
        db.commit()
        logger.error(f"Erro HTTP ao encaminhar lead {lead.lead_id}: {error_msg}")

    except Exception as e:
        lead.status = "failed"
        lead.forwarded_to = mapping.crm_url
        lead.error_message = str(e)[:500]
        db.commit()
        logger.error(f"Erro ao encaminhar lead {lead.lead_id}: {e}")

    return False


# ---------------------------------------------------------------------------
# 5. Orquestrador principal
# ---------------------------------------------------------------------------

def process_lead_event(db: Session, lead_gen_id: str, form_id: Optional[str], page_id: Optional[str], raw_payload: dict):
    """
    Fluxo completo:
    1. Busca detalhes do lead na Meta Graph API.
    2. Salva no banco.
    3. Identifica o CRM correto.
    4. Encaminha o lead.
    """
    logger.info(f"Processando lead_gen_id={lead_gen_id} form_id={form_id} page_id={page_id}")

    # 1. Buscar detalhes na Meta API
    lead_data = fetch_lead_details(lead_gen_id, db=db, page_id=page_id)
    if not lead_data:
        # Fallback: usar dados do payload bruto
        lead_data = {
            "id": lead_gen_id,
            "form_id": form_id,
            "page_id": page_id,
        }

    # Garantir que form_id e page_id do payload sejam usados se ausentes na API
    if not lead_data.get("form_id"):
        lead_data["form_id"] = form_id
    if not lead_data.get("page_id"):
        lead_data["page_id"] = page_id

    # 2. Salvar no banco
    lead = save_lead(db, lead_data, raw_payload)

    if lead.status != "received":
        return  # Lead já processado anteriormente

    # 3. Encontrar mapeamento
    mapping = find_mapping(db, form_id=lead_data.get("form_id"), page_id=lead_data.get("page_id"))

    if not mapping:
        lead.status = "skipped"
        lead.error_message = f"Nenhum mapeamento encontrado para form_id={lead_data.get('form_id')} page_id={lead_data.get('page_id')}"
        db.commit()
        logger.warning(f"Lead {lead_gen_id} sem mapeamento de instância. Status: skipped.")
        return

    # 4. Encaminhar
    forward_lead(db, lead, mapping)


# ---------------------------------------------------------------------------
# 6. Fluxo OAuth 2.0 (Facebook Login para Empresas)
# ---------------------------------------------------------------------------

def exchange_code_for_user_token(code: str, redirect_uri: str) -> Optional[str]:
    """
    Troca o código temporário do OAuth por um token de usuário de longa duração (60 dias).
    """
    client_id = os.getenv("META_APP_ID", "")
    client_secret = os.getenv("META_APP_SECRET", "")

    if not client_id or not client_secret:
        logger.error("META_APP_ID ou META_APP_SECRET não estão configurados no .env")
        return None

    # Passo 1: Trocar código pelo token de curta duração
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

            # Passo 2: Trocar token de curta duração pelo de longa duração (60 dias)
            long_url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/oauth/access_token"
            long_params = {
                "grant_type": "fb_exchange_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "fb_exchange_token": short_token,
            }
            long_resp = client.get(long_url, params=long_params)
            long_resp.raise_for_status()
            return long_resp.json().get("access_token")

    except Exception as e:
        logger.error(f"Erro ao trocar código por token da Meta: {e}")
        return None


def fetch_user_pages(user_token: str) -> list[dict]:
    """
    Busca todas as páginas administradas pelo usuário com seus respectivos Page Access Tokens.
    """
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/me/accounts"
    params = {
        "access_token": user_token,
        "limit": 100
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            pages = []
            for item in data.get("data", []):
                # Cada item tem: name, id, access_token
                pages.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "access_token": item.get("access_token")
                })
            return pages
    except Exception as e:
        logger.error(f"Erro ao buscar páginas do usuário na Meta: {e}")
        return []


def subscribe_page_to_app(page_id: str, page_token: str) -> bool:
    """
    Inscreve a página do cliente para receber webhooks do nosso app Meta.
    Isso diz à Meta para disparar eventos de leadgen para o nosso webhook configurado.
    """
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{page_id}/subscribed_apps"
    payload = {
        "subscribed_fields": "leadgen",
        "access_token": page_token
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, data=payload)
            resp.raise_for_status()
            logger.info(f"Página {page_id} inscrita no webhook da central com sucesso.")
            return True
    except Exception as e:
        logger.error(f"Erro ao inscrever página {page_id} no webhook: {e}")
        return False
