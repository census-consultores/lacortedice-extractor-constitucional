FROM python:3.12-slim

WORKDIR /app

# Dependencias del sistema para PyMuPDF y psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Crear carpetas de resultados (temporales en el contenedor)
RUN mkdir -p resultados/pdfs/resolucion \
             resultados/words/resolucion \
             resultados/logs/resolucion \
             resultados/excel

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
