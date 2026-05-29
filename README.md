# Meta Leads Central Hub 📡

Central que recebe, armazena e distribui leads da Meta (Facebook/Instagram) para as instâncias de CRM de cada cliente.

## Arquitetura

```
Meta Ads
   │
   ▼ POST /webhook
┌─────────────────────────────┐
│   Meta Leads Central Hub    │
│  ┌───────────────────────┐  │
│  │  Webhook Receiver     │  │
│  │  (FastAPI)            │  │
│  └────────┬──────────────┘  │
│           │                 │
│  ┌────────▼──────────────┐  │
│  │  Meta Graph API       │  │ ◄── Busca dados completos do lead
│  └────────┬──────────────┘  │
│           │                 │
│  ┌────────▼──────────────┐  │
│  │  SQLite (histórico)   │  │ ◄── Persiste todos os leads
│  └────────┬──────────────┘  │
│           │                 │
│  ┌────────▼──────────────┐  │
│  │  Dispatcher           │  │
│  │  (form_id → CRM URL)  │  │
│  └────────┬──────────────┘  │
└───────────┼─────────────────┘
            │
     ┌──────┼──────┐
     ▼      ▼      ▼
   CRM-A  CRM-B  CRM-C
```

## Instalação

```bash
# 1. Clone / acesse o diretório
cd /home/ivan/Documentos/HeadOffice/Meta_ads

# 2. Crie o ambiente virtual
python3 -m venv .venv
source .venv/bin/activate

# 3. Instale as dependências
pip install -r requirements.txt

# 4. Configure as variáveis de ambiente
cp .env.example .env
# Edite o .env com seus dados reais

# 5. Inicie o servidor
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## Como Rodar via Docker 🐳

Você pode construir e rodar o container localmente de forma simples:

```bash
# 1. Construa a imagem
docker build -t meta-leads-hub .

# 2. Rode o container passando o arquivo .env e montando o volume para persistência do SQLite
docker run -d \
  -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/meta_leads.db:/app/meta_leads.db \
  --name meta-leads-hub-instance \
  meta-leads-hub
```

> **Nota:** Certifique-se de criar o arquivo `.env` local antes de rodar o container, ou passe as variáveis de ambiente com `-e NOME_VARIAVEL=valor`.

## Configuração do .env

| Variável | Descrição |
|---|---|
| `META_VERIFY_TOKEN` | Token secreto que você define e configura no painel Meta |
| `META_ACCESS_TOKEN` | Token de acesso da Graph API para buscar dados completos do lead |
| `META_APP_SECRET` | Segredo do App Meta para validar assinatura HMAC dos webhooks |
| `DATABASE_URL` | Caminho SQLite (default: `sqlite:///./meta_leads.db`) |

## Configurar o Webhook na Meta

1. Acesse [developers.facebook.com](https://developers.facebook.com/)
2. Vá em **Seu App → Webhooks → Página**
3. Cole a URL do webhook: `https://SEU_DOMINIO/webhook`
4. Token de verificação: o mesmo que `META_VERIFY_TOKEN` no `.env`
5. Assine o campo **`leadgen`**

> Para testar localmente, use [ngrok](https://ngrok.com/): `ngrok http 8000`

## Configurar Mapeamentos

Acesse `http://localhost:8000/mappings` e clique em **"+ Novo Mapeamento"**.

- **Form ID**: ID do formulário de lead da Meta (mais específico, recomendado)
- **Page ID**: ID da página do Facebook (fallback)
- **URL do CRM**: Endpoint que receberá um POST com os dados do lead
- **Token**: Bearer token de autenticação opcional

## Payload enviado ao CRM

```json
{
  "lead_id": "123456789",
  "form_id": "987654321",
  "page_id": "111222333",
  "ad_id": "444555666",
  "adset_id": "777888999",
  "campaign_id": "000111222",
  "fields": {
    "full_name": "João Silva",
    "email": "joao@exemplo.com",
    "phone_number": "+5511999999999"
  },
  "received_at": "2024-01-15T10:30:00"
}
```

## API REST

| Método | Endpoint | Descrição |
|---|---|---|
| `GET` | `/` | Dashboard web de leads |
| `GET` | `/mappings` | Gerenciar mapeamentos |
| `GET/POST` | `/webhook` | Endpoint Meta Webhook |
| `GET` | `/api/leads` | Listar leads (JSON) |
| `GET` | `/api/leads/{lead_id}` | Detalhes de um lead |
| `POST` | `/leads/{id}/retry` | Reenviar lead com erro |
| `GET` | `/docs` | Documentação Swagger |

## Testes

```bash
# Com o servidor rodando em outra aba:
python tests/test_webhook.py
```
