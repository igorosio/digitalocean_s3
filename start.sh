#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -x ".venv/bin/flask" ]]; then
  echo "Errore: ambiente virtuale .venv non trovato. Esegui: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

CERT_DIR="certs"
CERT_FILE="$CERT_DIR/cert.pem"
KEY_FILE="$CERT_DIR/key.pem"

if [[ ! -f "$CERT_FILE" || ! -f "$KEY_FILE" ]]; then
  mkdir -p "$CERT_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -days 365 \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
  chmod 600 "$KEY_FILE"
fi

PORT="${FLASK_PORT:-5000}"
if [[ -f ".env" ]]; then
  ENV_PORT="$(grep -E '^FLASK_PORT=' .env | tail -n1 | cut -d= -f2 | tr -d '[:space:]' || true)"
  if [[ -n "$ENV_PORT" ]]; then
    PORT="$ENV_PORT"
  fi
fi

exec .venv/bin/flask --app app run \
  --host=127.0.0.1 \
  --port="$PORT" #\
  #--cert="$CERT_FILE" \
  #--key="$KEY_FILE"
