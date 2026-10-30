import re
import urllib.parse
import urllib.request
import base64
import logging
from typing import List, Dict, Optional, Any

logger = logging.getLogger("cloud_storage")

GENERIC_FOLDER_NAMES = {"фото", "photo", "photos", "images", "исходники", "материалы", "files", "документы"}

def clean_suggested_pool_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    cleaned = re.sub(r'^(?:задание|задача)\s+', '', name.strip(), flags=re.I)
    return cleaned if cleaned else name

def extract_share_token(url: str) -> Optional[str]:
    """Extracts the public share token from a Cloud / Nextcloud URL."""
    # Pattern: /s/<token> or /index.php/s/<token>
    match = re.search(r'/s/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    return None

def extract_sku_from_path(path: str) -> Optional[str]:
    """Extracts potential SKU from a directory path (e.g., '/фото/130754OUT-99' -> '130754OUT-99')."""
    parts = [p.strip() for p in urllib.parse.unquote(path).strip("/").split("/") if p.strip()]
    if not parts:
        return None
    candidate = parts[-1]
    if candidate.lower() not in GENERIC_FOLDER_NAMES and len(candidate) >= 3:
        return candidate
    return None

def build_direct_sku_url(parent_url: str, sku: str) -> str:
    """
    Builds a direct public link to a specific SKU folder that opens without requiring login.
    If parent_url already points to this SKU folder, returns parent_url without duplication.
    """
    if not parent_url:
        return ""
    
    parsed = urllib.parse.urlparse(parent_url)
    token = extract_share_token(parent_url)
    if not token:
        # Fallback to direct path append
        return parent_url.rstrip("/") + "/" + urllib.parse.quote(sku)

    base_link = f"{parsed.scheme}://{parsed.netloc}/index.php/s/{token}"
    qs = urllib.parse.parse_qs(parsed.query)
    current_dir = qs.get("dir", [""])[0].rstrip("/")

    # Check if current_dir already ends with sku
    if current_dir:
        last_segment = urllib.parse.unquote(current_dir).split("/")[-1].strip()
        if last_segment.lower() == sku.lower():
            return parent_url

    if current_dir:
        new_dir = f"{current_dir}/{sku}"
    else:
        new_dir = f"/{sku}"

    query = urllib.parse.urlencode({"dir": new_dir})
    return f"{base_link}?{query}"

def scan_cloud_share(share_url: str) -> Dict[str, Any]:
    """
    Scans Nextcloud/WebDAV cloud share:
    - If URL contains subfolders -> type 'pool', lists all subfolder SKUs.
    - If URL points to a specific SKU folder (with files) -> type 'single', returns single SKU.
    """
    token = extract_share_token(share_url)
    if not token:
        return {"type": "pool", "is_single": False, "folders": [], "single_sku": None, "suggested_pool_name": None}

    parsed = urllib.parse.urlparse(share_url)
    qs = urllib.parse.parse_qs(parsed.query)
    sub_dir = qs.get("dir", [""])[0].strip("/")
    sku_from_path = extract_sku_from_path(sub_dir)

    encoded_sub_dir = urllib.parse.quote(sub_dir) if sub_dir else ""
    webdav_url = f"{parsed.scheme}://{parsed.netloc}/public.php/webdav"
    if encoded_sub_dir:
        webdav_url += f"/{encoded_sub_dir}"

    auth_header = base64.b64encode(f"{token}:".encode()).decode()
    req = urllib.request.Request(
        webdav_url,
        headers={
            "Authorization": f"Basic {auth_header}",
            "Depth": "1"
        },
        method="PROPFIND"
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            responses = re.findall(r'<d:response>.*?</d:response>', content, re.DOTALL)
            folders = []
            has_files = False
            current_folder_name = urllib.parse.unquote(sub_dir).split("/")[-1] if sub_dir else ""

            for r_block in responses:
                href_match = re.search(r'<d:href>(.*?)</d:href>', r_block)
                if not href_match:
                    continue
                unquoted = urllib.parse.unquote(href_match.group(1)).rstrip("/")
                name = unquoted.split("/")[-1]
                if not name or name == current_folder_name or name in ("webdav", token):
                    continue

                is_collection = bool(re.search(r'<d:collection\s*/>|<d:collection></d:collection>', r_block))
                if is_collection:
                    folders.append(name)
                else:
                    has_files = True

            folders = sorted(list(set(folders)))

            # Case 1: Subfolders found -> pool of SKUs
            if folders:
                candidate = current_folder_name if current_folder_name and current_folder_name.lower() not in GENERIC_FOLDER_NAMES else None
                return {
                    "type": "pool",
                    "is_single": False,
                    "folders": folders,
                    "single_sku": None,
                    "suggested_pool_name": clean_suggested_pool_name(candidate)
                }

            # Case 2: No subfolders, but folder path is a specific SKU or contains files
            if sku_from_path:
                return {
                    "type": "single",
                    "is_single": True,
                    "folders": [sku_from_path],
                    "single_sku": sku_from_path,
                    "suggested_pool_name": sku_from_path
                }

            return {
                "type": "pool",
                "is_single": False,
                "folders": [],
                "single_sku": None,
                "suggested_pool_name": None
            }

    except Exception as e:
        logger.error(f"Error scanning cloud share: {e}")
        # Fallback: if network fails or PROPFIND 404, but path clearly contains SKU
        if sku_from_path:
            return {
                "type": "single",
                "is_single": True,
                "folders": [sku_from_path],
                "single_sku": sku_from_path,
                "suggested_pool_name": sku_from_path
            }
        return {"type": "pool", "is_single": False, "folders": [], "single_sku": None, "suggested_pool_name": None}

def fetch_subfolders_from_cloud(share_url: str) -> List[str]:
    """Backward compatibility helper."""
    return scan_cloud_share(share_url).get("folders", [])
