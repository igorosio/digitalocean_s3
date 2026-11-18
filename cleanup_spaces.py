import os
from datetime import datetime, timezone, timedelta
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Carica le variabili dal file .env
load_dotenv()

# Recupero configurazione
ACCESS_KEY = os.getenv("DO_SPACES_KEY")
SECRET_KEY = os.getenv("DO_SPACES_SECRET")
REGION = os.getenv("DO_SPACES_REGION")
ENDPOINT_URL = os.getenv("DO_SPACES_ENDPOINT")
BUCKET_NAME = os.getenv("DO_SPACES_BUCKET")
FOLDER_PREFIX = os.getenv("DO_SPACES_FOLDER", "").lstrip("/")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", 30))

# Imposta a False per eseguire l'eliminazione effettiva
DRY_RUN = False


def clean_old_files():
    # Verifica che le variabili essenziali siano presenti
    if not all([ACCESS_KEY, SECRET_KEY, ENDPOINT_URL, BUCKET_NAME]):
        raise ValueError("Mancano alcuni parametri obbligatori nel file .env")

    # Normalizza il prefisso della cartella (assicura che termini con '/' se non vuoto)
    prefix = FOLDER_PREFIX
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    # Inizializza il client S3 per DigitalOcean Spaces
    session = boto3.session.Session()
    client = session.client(
        "s3",
        region_name=REGION,
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
    )

    # Calcola la data limite in UTC (S3 restituisce i timestamp in UTC)
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    print(f"Ricerca file in '{BUCKET_NAME}/{prefix}' precedenti al {cutoff_date.strftime('%Y-%m-%d %H:%M:%S UTC')}...")
    if DRY_RUN:
        print("--- MODALITÀ DRY-RUN ATTIVA: Nessun file verrà realmente eliminato ---")

    # Usa il paginator per gestire anche cartelle con più di 1.000 file
    paginator = client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix=prefix)

    objects_to_delete = []
    total_scanned = 0

    try:
        for page in pages:
            if "Contents" not in page:
                continue

            for obj in page["Contents"]:
                key = obj["Key"]
                last_modified = obj["LastModified"]

                # Ignora l'oggetto "cartella" stesso (che su S3 ha dimensione 0 e termina con '/')
                if key.endswith("/"):
                    continue

                total_scanned += 1

                if last_modified < cutoff_date:
                    print(f"[DA ELIMINARE] {key} (Data: {last_modified.strftime('%Y-%m-%d %H:%M:%S')})")
                    objects_to_delete.append({"Key": key})

        print(f"\nFile analizzati: {total_scanned} | File da eliminare: {len(objects_to_delete)}")

        if not objects_to_delete:
            print("Nessun file da eliminare.")
            return

        if DRY_RUN:
            print("Dry-run completato. Imposta DRY_RUN = False nello script per procedere alla cancellazione.")
            return

        # S3 delete_objects accetta massimo 1000 elementi per chiamata
        for i in range(0, len(objects_to_delete), 1000):
            batch = objects_to_delete[i : i + 1000]
            response = client.delete_objects(
                Bucket=BUCKET_NAME,
                Delete={"Objects": batch, "Quiet": False},
            )
            deleted = response.get("Deleted", [])
            errors = response.get("Errors", [])
            print(f"Eliminati {len(deleted)} file.")
            if errors:
                print(f"Errori durante l'eliminazione: {errors}")

    except ClientError as e:
        print(f"Errore API DigitalOcean Spaces: {e}")


if __name__ == "__main__":
    clean_old_files()
