FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user; /app/data must be writable by it for the SQLite file.
RUN useradd --create-home --uid 10001 urumi \
    && mkdir -p /app/data \
    && chown -R urumi:urumi /app

USER urumi

VOLUME ["/app/data"]

EXPOSE 8080

CMD ["python", "main.py"]
