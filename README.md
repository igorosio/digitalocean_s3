# DigitalOcean Spaces Browser & Cleanup (`do_s3`)

Strumenti Python per gestire e consultare un bucket DigitalOcean Spaces (S3-compatibile):

1. **Web App Flask** (`app.py`): browser web per navigare nel bucket, caricare/scaricare file e cartelle ed editare documenti Markdown con esportazione in PDF.
2. **Script di pulizia** (`cleanup_spaces.py`): rimozione automatica dei file più vecchi di `RETENTION_DAYS` giorni all'interno di `DO_SPACES_FOLDER`.

---

## Funzionalità della Web App

- **Navigazione & Ricerca**: esplorazione completa dalla root del bucket con breadcrumb, ordinamento per nome/dimensione/data, filtro nella cartella corrente e paginazione S3 (`ContinuationToken`).
- **Anteprima inline & Download**:
  - Anteprima in modale per immagini, PDF e file di testo/log/codice (fino a 512 KB).
  - Apertura inline e download diretto tramite presigned URL temporanei (scadenza 300s).
- **Upload & Cartelle**:
  - Caricamento multiplo tramite modale o **drag-and-drop** diretto sulla tabella, con richiesta di conferma in caso di sovrascrittura.
  - Creazione di nuove cartelle (`Nuova cartella`).
  - Download ricorsivo di intere cartelle in archivio `.zip` (`/download-folder`, limite predefinito 500 MB).
- **Editor WYSIWYG Markdown & PDF**:
  - Creazione di nuovi file `.md` o modifica di file `.md` esistenti con **Toast UI Editor** (modalità WYSIWYG e Markdown).
  - Salvataggio diretto nel bucket e generazione PDF lato server (`markdown` + `weasyprint`) per download locale o salvataggio nel bucket.
- **Statistiche & Spazio Bucket**: barra superiore con calcolo asincrono della dimensione totale del bucket, numero di file e percentuale di utilizzo su `250 GiB` (configurabile con `BUCKET_QUOTA_GIB`, cache 5 minuti con refresh manuale). *Nota: il traffico outbound (1 TiB/mese) non è esposto dalle API di DigitalOcean Spaces ed è consultabile dal pannello Billing.*
- **Interfaccia**: Bootstrap 5.3 + Font Awesome 6.5 sui toni di grigio con toggle tema Chiaro/Scuro salvato in `localStorage` e footer con riferimenti DigitalOcean, GitHub e LinkedIn.

---

## Sicurezza

- **Protezione CSRF**: token CSRF automatico per sessione (`HttpOnly`, `SameSite=Lax`) verificato tramite header `X-CSRF-Token` su tutte le richieste `POST`/`PUT`/`DELETE`/`PATCH`.
- **Protezione SSRF & HTML in WeasyPrint**: la generazione PDF blocca URL `file://`, `http://` e IP privati/locali (consentendo solo `data:` URI e `https://` pubblici) e rimuove tag/attributi HTML pericolosi prima del rendering.
- **Limiti & File attivi**: limite massimo di upload configurabile (`MAX_UPLOAD_MB`, default `100` MB) e download forzato (`attachment`) su `/open` per file potenzialmente attivi (`.html`, `.svg`, `.xml`, `.js`, ecc.).
- **Validazione Path & Zip Slip**: sanificazione di prefissi, chiavi S3 e percorsi interni agli archivi `.zip` contro path traversal (`..`).
- **Security Headers & Errori**: header `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Content-Security-Policy` e messaggi di errore sanificati lato client.

---

## Configurazione (`.env`)

Crea un file `.env` nella root del progetto con le seguenti variabili:

```env
DO_SPACES_KEY=la_tua_access_key
DO_SPACES_SECRET=la_tua_secret_key
DO_SPACES_REGION=fra1
DO_SPACES_ENDPOINT=https://fra1.digitaloceanspaces.com
DO_SPACES_BUCKET=igorosio
DO_SPACES_FOLDER=backups/
RETENTION_DAYS=30

# Opzionali per la Web App
# APP_USERNAME=admin
# APP_PASSWORD=password_sicura
# FLASK_PORT=5000
# FLASK_DEBUG=false
# FLASK_SECRET_KEY=chiave_segreta_casuale
# MAX_UPLOAD_MB=100
# MAX_FOLDER_ZIP_BYTES=524288000
# BUCKET_QUOTA_GIB=250
```

> Se `APP_USERNAME` e `APP_PASSWORD` sono impostati, l'applicazione abilita automaticamente HTTP Basic Auth.

---

## Installazione e Avvio

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Avvio Web App Flask (su http://127.0.0.1:5000)
python app.py
```

Per eseguire lo script di pulizia:

```bash
python cleanup_spaces.py
```
