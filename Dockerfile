FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY run.py .
COPY .env.example .

# Permite `uvicorn main:app` a partir do pacote app/
ENV PYTHONPATH=/app/app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]