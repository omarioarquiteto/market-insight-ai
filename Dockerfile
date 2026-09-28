FROM python:3.10-slim

WORKDIR /app

# A iqoptionapi atual é instalada diretamente do GitHub.
# A imagem slim não traz Git por padrão.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY run.py .
COPY .env.example .

# Permite `uvicorn main:app` a partir do pacote app/
ENV PYTHONPATH=/app/app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
