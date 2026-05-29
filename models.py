"""
models.py — Modelos SQLAlchemy para o banco de dados local.
"""
import json
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean
from database import Base


class Lead(Base):
    """Armazena cada lead recebido da Meta com todos os seus dados."""
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)

    # Identificadores da Meta
    lead_id = Column(String(64), unique=True, index=True, nullable=False)
    form_id = Column(String(64), index=True, nullable=True)
    page_id = Column(String(64), index=True, nullable=True)
    ad_id = Column(String(64), nullable=True)
    adset_id = Column(String(64), nullable=True)
    campaign_id = Column(String(64), nullable=True)

    # Dados do lead em JSON (campos customizados do formulário)
    fields_json = Column(Text, nullable=True)  # JSON serializado

    # Payload bruto completo enviado pela Meta
    raw_payload = Column(Text, nullable=True)  # JSON serializado

    # Controle de status e encaminhamento
    status = Column(String(32), default="received")  # received | forwarded | failed | skipped
    forwarded_to = Column(String(255), nullable=True)  # URL do CRM que recebeu
    forward_response = Column(Text, nullable=True)  # Resposta HTTP do CRM
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def get_fields(self) -> dict:
        """Desserializa os campos do lead de JSON para dict."""
        if self.fields_json:
            return json.loads(self.fields_json)
        return {}

    def __repr__(self):
        return f"<Lead id={self.id} lead_id={self.lead_id} status={self.status}>"


class InstanceMapping(Base):
    """
    Mapeamento de form_id ou page_id para a URL da instância de CRM do cliente.
    Prioridade: form_id > page_id (mais específico vence).
    """
    __tablename__ = "instance_mappings"

    id = Column(Integer, primary_key=True, index=True)

    # Identificador da Meta
    form_id = Column(String(64), unique=True, index=True, nullable=True)
    page_id = Column(String(64), index=True, nullable=True)

    # Dados da instância do cliente
    client_name = Column(String(255), nullable=False)
    crm_url = Column(String(512), nullable=False)        # Ex: https://crm-cliente.com/api/leads
    crm_auth_token = Column(String(512), nullable=True)  # Token Bearer opcional

    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<InstanceMapping client={self.client_name} form_id={self.form_id}>"


class MetaConnection(Base):
    """
    Armazena os tokens de acesso de longa duração das páginas integradas
    via fluxo de OAuth 2.0 (Facebook Login).
    """
    __tablename__ = "meta_connections"

    id = Column(Integer, primary_key=True, index=True)

    # Identificadores da Página Meta
    page_id = Column(String(64), unique=True, index=True, nullable=False)
    page_name = Column(String(255), nullable=False)
    page_access_token = Column(String(512), nullable=False)  # Page access token (no expiration)

    # Token do Usuário que fez a conexão (opcional para rastreabilidade)
    user_access_token = Column(String(512), nullable=True)   # User long-lived token
    connected_by = Column(String(255), nullable=True)        # Nome do usuário / administrador que conectou

    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<MetaConnection page={self.page_name} id={self.page_id}>"
