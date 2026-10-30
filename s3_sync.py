import os
import boto3
from botocore.client import Config
from config import BASE_DIR

S3_KEY_ID = os.getenv("S3_KEY_ID", "")
S3_SECRET = os.getenv("S3_SECRET", "")
S3_BUCKET = os.getenv("S3_BUCKET", "tg-crm-data-storage-amz")
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "https://storage.yandexcloud.net")

import time

DB_FILE = BASE_DIR / "crm.db"
_last_etag = None
_last_check_time = 0.0

def get_s3_client():
    if not S3_KEY_ID or not S3_SECRET or not S3_BUCKET:
        return None
    session = boto3.session.Session()
    return session.client(
        service_name="s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY_ID,
        aws_secret_access_key=S3_SECRET,
        config=Config(signature_version="s3v4")
    )

def download_db():
    global _last_etag
    if DB_FILE.exists() and DB_FILE.stat().st_size > 0:
        print("crm.db already exists locally on SSD, using local database.")
        return True
    client = get_s3_client()
    if not client:
        return False
    try:
        print(f"Downloading crm.db from S3 bucket {S3_BUCKET}...")
        client.download_file(S3_BUCKET, "crm.db", str(DB_FILE))
        print("crm.db downloaded successfully from S3!")
        try:
            head = client.head_object(Bucket=S3_BUCKET, Key="crm.db")
            _last_etag = head.get("ETag")
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"No existing crm.db in S3 or error downloading: {e}")
        return False

def upload_db():
    global _last_etag
    client = get_s3_client()
    if not client:
        return False
    try:
        if DB_FILE.exists():
            client.upload_file(str(DB_FILE), S3_BUCKET, "crm.db")
            print("crm.db uploaded to S3 successfully!")
            try:
                head = client.head_object(Bucket=S3_BUCKET, Key="crm.db")
                _last_etag = head.get("ETag")
            except Exception:
                pass
            return True
    except Exception as e:
        print(f"Failed to upload crm.db to S3: {e}")
        return False

def check_and_sync_db():
    """Checks S3 ETag (throttled to once per 5 seconds). If S3 has a newer file, reloads it."""
    global _last_etag, _last_check_time
    now = time.time()
    if now - _last_check_time < 5.0:
        return
    _last_check_time = now
    client = get_s3_client()
    if not client:
        return
    try:
        head = client.head_object(Bucket=S3_BUCKET, Key="crm.db")
        remote_etag = head.get("ETag")
        if _last_etag and remote_etag and remote_etag != _last_etag:
            print(f"S3 ETag changed from {_last_etag} to {remote_etag}. Reloading crm.db...")
            client.download_file(S3_BUCKET, "crm.db", str(DB_FILE))
            _last_etag = remote_etag
    except Exception:
        pass
