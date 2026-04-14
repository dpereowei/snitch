FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Create non-root user
RUN useradd -m -u 1000 snitch
USER snitch

CMD ["python", "main.py"]
