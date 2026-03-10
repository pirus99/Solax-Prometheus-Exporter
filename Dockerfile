FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py .

# Prometheus metrics endpoint
EXPOSE 9090

CMD ["python", "exporter.py"]
