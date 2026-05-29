# Usar a imagem oficial do Python slim para menor tamanho
FROM python:3.12-slim

# Definir variáveis de ambiente
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Definir o diretório de trabalho no container
WORKDIR /app

# Instalar dependências de sistema se necessário (SQLite já vem por padrão no Python)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar dependências do Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar os arquivos do projeto
COPY . .

# Expor a porta que a aplicação roda
EXPOSE 8000

# Comando para rodar a aplicação usando Uvicorn
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
