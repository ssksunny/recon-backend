FROM python:3.11-slim

# psycopg2-binary needs libpq at runtime; build-essential covers anything
# else in requirements.txt that needs to compile (kept in the final image
# too, on purpose — this runs on a single free-tier VM, not a fleet where
# image size actually matters).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Both the API and the worker use this same image — see docker-compose.yml,
# which overrides `command` for the worker service. This default is the API.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
