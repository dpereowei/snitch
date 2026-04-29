FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy entire application
COPY . .

# Create non-root user
RUN useradd -m -u 1000 snitch
USER snitch

# Set PYTHONPATH to current directory for module imports
ENV PYTHONPATH=/app

CMD ["python", "main.py"]
