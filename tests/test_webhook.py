#!/usr/bin/env python3
"""
test_webhook.py — Simula requisições do Meta webhook para testes locais.
Execute: python tests/test_webhook.py
"""
import json
import sys
import httpx

BASE_URL = "http://localhost:8000"
VERIFY_TOKEN = "seu_token_secreto_aqui"  # Deve bater com META_VERIFY_TOKEN no .env


def test_verification():
    """Testa o GET /webhook (verificação da Meta)."""
    print("🔍 Testando verificação do webhook...")
    resp = httpx.get(f"{BASE_URL}/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": VERIFY_TOKEN,
        "hub.challenge": "CHALLENGE_12345",
    })
    assert resp.status_code == 200, f"Esperado 200, recebido {resp.status_code}"
    assert resp.text == "CHALLENGE_12345", f"Challenge errado: {resp.text}"
    print("  ✓ Verificação OK\n")


def test_invalid_token():
    """Testa que um token inválido retorna 403."""
    print("🔒 Testando token inválido...")
    resp = httpx.get(f"{BASE_URL}/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "token_errado",
        "hub.challenge": "CHALLENGE_99999",
    })
    assert resp.status_code == 403, f"Esperado 403, recebido {resp.status_code}"
    print("  ✓ Bloqueio de token inválido OK\n")


def test_receive_lead():
    """Simula um POST do Meta com um evento de novo lead."""
    print("📥 Simulando recebimento de lead...")
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "111222333444555",  # page_id
                "time": 1700000000,
                "changes": [
                    {
                        "field": "leadgen",
                        "value": {
                            "leadgen_id": "TEST_LEAD_ID_001",
                            "form_id": "FORM_ID_TEST_001",
                            "page_id": "111222333444555",
                            "created_time": 1700000000,
                        }
                    }
                ]
            }
        ]
    }
    resp = httpx.post(f"{BASE_URL}/webhook", json=payload)
    print(f"  Status: {resp.status_code}")
    print(f"  Resposta: {resp.text}")
    assert resp.status_code == 200
    print("  ✓ Lead recebido OK\n")


def test_api_list_leads():
    """Testa a API REST de listagem de leads."""
    print("📋 Listando leads via API REST...")
    resp = httpx.get(f"{BASE_URL}/api/leads")
    assert resp.status_code == 200
    data = resp.json()
    print(f"  Total de leads no banco: {data['total']}")
    if data["results"]:
        print(f"  Último lead: {json.dumps(data['results'][0], indent=2, ensure_ascii=False)}")
    print("  ✓ API de leads OK\n")


if __name__ == "__main__":
    print("=" * 50)
    print("  Meta Leads Hub — Testes de Webhook")
    print("=" * 50 + "\n")
    try:
        test_verification()
        test_invalid_token()
        test_receive_lead()
        test_api_list_leads()
        print("🎉 Todos os testes passaram!")
    except AssertionError as e:
        print(f"\n❌ Falha: {e}")
        sys.exit(1)
    except httpx.ConnectError:
        print("\n⚠️  Servidor não está rodando. Inicie com: uvicorn main:app --reload")
        sys.exit(1)
