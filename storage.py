import os
from supabase import create_client

_cached_client = None


def _client():
    # Reuse a single client (and its underlying HTTP connection pool) across
    # calls. Rebuilding it per download forced a fresh TLS handshake every
    # file, which dominated the runtime of bulk operations like the zip export.
    global _cached_client
    if _cached_client is None:
        url = os.environ['SUPABASE_URL']
        key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
        _cached_client = create_client(url, key)
    return _cached_client


def _bucket():
    return os.environ.get('SUPABASE_BUCKET', 'rapportini')


def to_storage_key(local_path: str) -> str:
    """Convert a local file path to a Supabase Storage bucket key."""
    key = local_path.replace('\\', '/')
    # Strip /tmp/ prefix (Render deployment)
    if key.startswith('/tmp/'):
        return key[5:]
    # Strip project root prefix (local dev, from os.path.abspath)
    root = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/').rstrip('/')
    if key.startswith(root + '/'):
        return key[len(root) + 1:]
    # Strip leading ./
    if key.startswith('./'):
        return key[2:]
    return key


def upload_file(local_path: str, storage_key: str) -> None:
    """Upload a local file to Supabase Storage, overwriting if it exists."""
    with open(local_path, 'rb') as f:
        data = f.read()
    sb = _client()
    bucket = sb.storage.from_(_bucket())
    try:
        bucket.upload(storage_key, data, {'upsert': 'true'})
    except Exception:
        try:
            bucket.remove([storage_key])
        except Exception:
            pass
        bucket.upload(storage_key, data)


def upload_bytes(data: bytes, storage_key: str) -> None:
    """Upload an in-memory blob to Supabase Storage, overwriting if it exists.
    Used for small derived artifacts (e.g. the cached projects-list JSON) that
    never touch local disk."""
    sb = _client()
    bucket = sb.storage.from_(_bucket())
    try:
        bucket.upload(storage_key, data, {'upsert': 'true'})
    except Exception:
        try:
            bucket.remove([storage_key])
        except Exception:
            pass
        bucket.upload(storage_key, data)


def upload_and_remove(local_path: str, storage_key: str) -> None:
    """Upload a file to Supabase, then delete the local copy.

    On Render's free tier the output dirs live under /tmp, which is a tmpfs —
    i.e. RAM that counts against the 512MB instance cap. Generated xlsx/pdf
    files left there accumulate and eat the memory budget across a session, so
    once a file is safely in Supabase (the source of truth that serve_pdf and
    ai_chat both read from) we remove it locally to keep tmpfs near-empty."""
    upload_file(local_path, storage_key)
    try:
        os.remove(local_path)
    except OSError:
        pass


def download_to_bytes(storage_key: str) -> bytes:
    """Download a file from Supabase Storage into memory."""
    sb = _client()
    return sb.storage.from_(_bucket()).download(storage_key)


def list_prefix(prefix: str) -> list:
    """List objects directly under a prefix. Folders have id=None, files have id set."""
    sb = _client()
    return sb.storage.from_(_bucket()).list(prefix, {'limit': 1000, 'offset': 0})


def delete_file(storage_key: str) -> None:
    """Delete a single object from Supabase Storage."""
    sb = _client()
    sb.storage.from_(_bucket()).remove([storage_key])


def object_exists(storage_key: str) -> bool:
    """Return True if the object exists in Supabase Storage."""
    try:
        prefix = '/'.join(storage_key.split('/')[:-1])
        name = storage_key.split('/')[-1]
        items = list_prefix(prefix)
        return any(i.get('name') == name and i.get('id') is not None for i in items)
    except Exception:
        return False
