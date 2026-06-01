"""
database.py — Configuração do cliente MongoDB (pymongo).
"""
import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27018/")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "meta_leads_hub")

_client: MongoClient = None


def get_client() -> MongoClient:
    """Retorna o cliente Mongo singleton (cria na primeira chamada)."""
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    return _client


def get_database():
    """Retorna a instância do database MongoDB."""
    return get_client()[MONGO_DB_NAME]


def get_db():
    """Dependência FastAPI: retorna o database MongoDB."""
    return get_database()
