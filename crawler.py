import os
import sys
import json
import time
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from playwright.sync_api import sync_playwright
from guessit import guessit

# Google GenAI Client
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    try:
        from google import genai
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        ai_client = None
else:
    ai_client = None

CANDIDATE_AI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash-latest"
]

ROOT_FOLDER_ID = "OBVVp1LI"
ROOT_URL = f"https://gofile.io/d/{ROOT_FOLDER_ID}"

VALID_VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".wmv", ".mov", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".vob", ".ogv", ".3gp",
    ".divx", ".xvid", ".rmvb", ".asf", ".f4v", ".wtv", ".iso"
}

GENERIC_FOLDERS = {
    "extras", "featurettes", "bonus", "specials", "behind the scenes",
    "season", "root", "all items", "downloads", "movies", "tv shows", "unknown"
}

def create_pooled_session():
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retries)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

HTTP_CLIENT = create_pooled_session()

# ==========================================
# RELIABLE BROWSER SESSION MANAGER
# ==========================================

class BrowserSessionManager:
    def __init__(self, root_url):
        self.root_url = root_url
        self.session = requests.Session()
        self.last_auth_time = 0
        self.refresh_credentials()

    def refresh_credentials(self):
        print("⚡ Refreshing browser credentials via Chromium...")
        captured = {"headers": {}}
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720}
            )
            page = context.new_page()

            def intercept_request(request):
                if "contents/" in request.url:
                    captured["headers"] = dict(request.headers)

            page.on("request", intercept_request)

            try:
                page.goto(self.root_url, wait_until="networkidle", timeout=45000)
                time.sleep(2)
            except Exception as e:
                print(f"Browser navigation notice: {e}")
            finally:
                browser.close()

        if not captured["headers"]:
            print("❌ Failed to intercept browser session headers.")
            sys.exit(1)

        self.session.headers.clear()
        self.session.headers.update(captured["headers"])
        self.last_auth_time = time.time()
        print("✅ Session credentials captured successfully.")

    def ensure_fresh(self):
        if time.time() - self.last_auth_time > 900:
            self.refresh_credentials()

def is_video_file(filename):
    if not filename or "." not in filename:
        return False
    ext = os.path.splitext(filename)[1].lower()
    return ext in VALID_VIDEO_EXTENSIONS

def extract_direct_stream_link(item, fid):
    raw_link = item.get("directDownload") or item.get("link")
    server = item.get("server")
    fname = item.get("name", fid)

    if raw_link and "/d/" in raw_link and server:
        return f"https://{server}.gofile.io/download/web/{fid}/{requests.utils.quote(fname)}"
    if raw_link and not raw_link.startswith("https://gofile.io/d/"):
        return raw_link
    if server:
        return f"https://{server}.gofile.io/download/web/{fid}/{requests.utils.quote(fname)}"
    return raw_link or item.get("downloadPage")

# ==========================================
# CRAWLER ENGINE WITH RETRY & BACKOFF
# ==========================================

def fetch_folder_page(session_mgr, folder_code, page_num=1, max_retries=5):
    api_url = f"https://api.gofile.io/contents/{folder_code}?page={page_num}&pageSize=50"

    for attempt in range(max_retries):
        session_mgr.ensure_fresh()
        try:
            res = session_mgr.session.get(api_url, timeout=20).json()
            status = res.get("status")

            if status == "ok":
                return res
            elif status in ["error-rateLimit", "429"]:
                cool_off = 10 + (attempt * 8)
                print(f"   ⏳ Rate limited on [{folder_code}]. Pausing {cool_off}s (Attempt {attempt+1}/{max_retries})...")
                time.sleep(cool_off)
            elif status in ["error-auth", "error-token"]:
                print("   🔑 Token expired, refreshing credentials...")
                session_mgr.refresh_credentials()
                time.sleep(2)
            else:
                return res
        except Exception:
            time.sleep(3)

    return None

def crawl_tree(session_mgr, root_id):
    folders_queue = deque([(root_id, "Root", ["Root"])])
    visited_folders = set()
    all_live_files = {}

    while folders_queue:
        current_folder_id, current_folder_name, current_path = folders_queue.popleft()

        if current_folder_id in visited_folders:
            continue
        visited_folders.add(current_folder_id)

        page_num = 1
        folder_files = 0
        seen_in_folder = set()

        while True:
            res = fetch_folder_page(session_mgr, current_folder_id, page_num)
            if not res or res.get("status") != "ok":
                break

            data = res.get("data", {})
            children = data.get("children", {})
            if not children:
                break

            children_items = children.items() if isinstance(children, dict) else [(c.get("id") or c.get("file_id"), c) for c in children]
            new_items_on_page = 0

            for item_id, item in children_items:
                if not item or item_id in seen_in_folder:
                    continue
                seen_in_folder.add(item_id)
                new_items_on_page += 1

                if item.get("type") == "folder":
                    sub_code = item.get("code") or item.get("id") or item_id
                    folder_name = item.get("name", sub_code)
                    if sub_code not in visited_folders and all(sub_code != f[0] for f in folders_queue):
                        folders_queue.append((sub_code, folder_name, current_path + [folder_name]))
                else:
                    fname = item.get("name", "")
                    if not is_video_file(fname):
                        continue
                    direct_link = extract_direct_stream_link(item, item_id)
                    if direct_link and item_id not in all_live_files:
                        item["_resolved_link"] = direct_link
                        item["_parent_folder"] = current_folder_name
                        item["_folder_path"] = current_path
                        all_live_files[item_id] = item
                        folder_files += 1

            if new_items_on_page == 0 or len(children_items) < 50:
                break

            page_num += 1
            time.sleep(0.5)

        print(f"📁 Scanned [{current_folder_name}]: {folder_files} video files found")
        time.sleep(0.8)

    return all_live_files

# ==========================================
# PARSER & GROUNDED AI ENGINE
# ==========================================

def normalize(s):
    return re.sub(r"[^\w]", "", (s or "").lower())

def clean_preparse_filename(filename):
    clean_name = re.sub(r"^@[\w\.\-]+(?:\s*-\s*|\s+)", "", filename, flags=re.I)
    clean_name = re.sub(r"\[(?:TTT|CN Dub|Tamil|Hindi|Eng|Dual Audio|HEVC|10bit|YTS\.[A-Z]+)[^\]]*\]", "", clean_name, flags=re.I)
    clean_name = re.sub(r"\b(ia)\b", "", clean_name, flags=re.I).strip(" ._-")
    return clean_name

def call_gemini_with_fallback(prompt):
    if not ai_client:
        return None

    for model_name in CANDIDATE_AI_MODELS:
        try:
            response = ai_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={"response_mime_type": "application/json"}
            )
            if response and response.text:
                return response.text
        except Exception as e:
            err_str = str(e)
            if "404" in err_str or "NOT_FOUND" in err_str:
                continue
            print(f"⚠️ Gemini request failed on {model_name}: {e}")
            break

    return None

def batch_ai_parse_grounded(unresolved_items):
    """Feeds full folder ancestry to Gemini and directly asks for canonical IMDb IDs."""
    if not unresolved_items:
        return {}

    items_payload = [{
        "id": fid,
        "filename": item.get("name", ""),
        "immediate_folder": item.get("_parent_folder", ""),
        "full_folder_path": " / ".join(item.get("_folder_path", []))
    } for fid, item in unresolved_items]

    prompt = f"""You are an authoritative media archivist. Identify the canonical IMDb ID and metadata for these files.
Analyze the 'full_folder_path' and 'filename' together like a human would.

CRITICAL RULES:
1. Foreign Titles: e.g. 'Форсаж 5 (2011)' is Fast Five (tt1596343). Translate and return the authentic franchise IMDb ID.
2. Collections & Shorts: Files inside 'Tom and Jerry - The Complete CinemaScope Collection (1954–1958)' (like 'Muscle Beach Tom', 'Royal Cat Nap', 'Feedin the Kiddie') are shorts of the franchise series 'Tom and Jerry' (tt0032138). Set type='series', title='Tom and Jerry', imdb_id='tt0032138'.
3. Extras / Promos: Files like 'Ed, Edd n Eddy - EXTRA - Promo...' belong to the TV series 'Ed, Edd n Eddy' (tt0217935), NOT 'Extras' or 'Big Picture Show'. Set type='series', title='Ed, Edd n Eddy', imdb_id='tt0217935'.
4. Standalone Movies: 'The Batman 2022' -> tt1877830. '500 Days of Summer 2009' -> tt1022603. Set type='movie'.
5. Always provide the canonical 'imdb_id' starting with 'tt' if known.

Payload:
{json.dumps(items_payload, indent=2)}

Return ONLY a JSON list:
[
  {{
    "id": "item_id",
    "type": "movie" or "series",
    "imdb_id": "ttXXXXXXX or null",
    "title": "Canonical Show or Film Title",
    "year": integer or null,
    "season": integer or null,
    "episodes": [integers] or null,
    "edition": "string or null",
    "quality": "1080P/720P/etc"
  }}
]"""

    response_text = call_gemini_with_fallback(prompt)
    if not response_text:
        return {}

    try:
        data = json.loads(response_text)
        return {entry["id"]: entry for entry in data if "id" in entry}
    except Exception as e:
        print(f"⚠️ Could not parse JSON from Gemini response: {e}")
        return {}

# ==========================================
# CINEMETA DIRECT METADATA MATCHER
# ==========================================

def get_cinemeta_meta(imdb_id, m_type):
    """Directly grabs official Stremio poster and metadata by IMDb ID."""
    catalog_type = "series" if m_type == "series" else "movie"
    url = f"https://v3-cinemeta.strem.io/meta/{catalog_type}/{imdb_id}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        meta = res.get("meta")
        if meta:
            return {
                "id": meta.get("imdb_id") or imdb_id,
                "name": meta.get("name"),
                "poster": meta.get("poster", "")
            }
    except Exception:
        pass
    return None

def search_cinemeta_exact(title, year, m_type):
    catalog_type = "series" if m_type == "series" else "movie"
    url = f"https://v3-cinemeta.strem.io/catalog/{catalog_type}/top/search={requests.utils.quote(title)}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        metas = res.get("metas", [])
        for m in metas:
            cand_year = str(m.get("year") or m.get("releaseInfo") or "")
            if year and cand_year and abs(int(cand_year[:4]) - int(year)) <= 1:
                return {"id": m.get("imdb_id") or m.get("id"), "name": m.get("name"), "poster": m.get("poster", "")}
            if not year and normalize(m.get("name", "")) == normalize(title):
                return {"id": m.get("imdb_id") or m.get("id"), "name": m.get("name"), "poster": m.get("poster", "")}
        if metas and not year:
            return {"id": metas[0].get("imdb_id") or metas[0].get("id"), "name": metas[0].get("name"), "poster": metas[0].get("poster", "")}
    except Exception:
        pass
    return None

def build_entry(fid, item, parsed, meta):
    fname = item.get("name", fid)
    link = item.get("_resolved_link") or extract_direct_stream_link(item, fid)
    size = item.get("size", 0)
    size_mb = f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"

    imdb_id = (meta and meta.get("id")) or parsed.get("imdb_id") or f"gf:{fid}"
    display_title = (meta and meta.get("name")) or parsed.get("title") or fname
    poster = (meta and meta.get("poster")) or "https://gofile.io/dist/img/logo-small.png"

    edition = parsed.get("edition") or ""
    quality = parsed.get("quality") or "1080P"

    if parsed.get("type") == "series":
        season_num = parsed.get("season") or 1
        ep_list = parsed.get("episodes") or [1]
        primary_ep = ep_list[0] if ep_list else 1
        stream_ids = [f"{imdb_id}:{season_num}:{ep}" for ep in ep_list]

        return {
            "file_id": fid,
            "type": "series",
            "imdb_id": imdb_id,
            "title": display_title,
            "name": fname,
            "season": season_num,
            "episode": primary_ep,
            "stream_id": stream_ids[0],
            "stream_ids": stream_ids,
            "poster": poster,
            "edition": edition,
            "quality": quality,
            "size": size_mb,
            "link": link
        }
    else:
        return {
            "file_id": fid,
            "type": "movie",
            "imdb_id": imdb_id,
            "title": display_title,
            "name": fname,
            "stream_id": imdb_id,
            "stream_ids": [imdb_id],
            "poster": poster,
            "edition": edition,
            "quality": quality,
            "size": size_mb,
            "link": link
        }

# ==========================================
# MAIN
# ==========================================

def main():
    existing_catalog = {}
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                for item in json.load(f):
                    fid = item.get("file_id")
                    fname = item.get("name", "")
                    if fid and is_video_file(fname):
                        existing_catalog[fid] = item
            print(f"📦 Loaded {len(existing_catalog)} valid video entries from local data.json")
        except Exception as e:
            print(f"⚠️ Could not read data.json: {e}")

    session_mgr = BrowserSessionManager(ROOT_URL)
    all_live_files = crawl_tree(session_mgr, ROOT_FOLDER_ID)

    print(f"\n📊 Discovered {len(all_live_files)} live video files on Gofile.")
    if not all_live_files:
        print("❌ Error: 0 video files retrieved from Gofile. Preserving data.json.")
        sys.exit(1)

    pruned_catalog = {}
    missing_ids = []

    for fid, item in all_live_files.items():
        if fid in existing_catalog:
            cached = existing_catalog[fid]
            if cached.get("name") == item.get("name"):
                cached["link"] = item.get("_resolved_link")
                pruned_catalog[fid] = cached
                continue
        missing_ids.append(fid)

    print(f"📌 Cached matches: {len(pruned_catalog)} | Items to Index/Re-index: {len(missing_ids)}\n")

    parsed_items = {}
    unresolved_for_ai = []

    # Heuristic pass for standard western standalone releases
    for fid in missing_ids:
        item = all_live_files[fid]
        fname = item.get("name", "")
        parent_name = item.get("_parent_folder", "").lower()
        has_non_ascii = any(ord(c) > 127 for c in fname)

        clean_name = clean_preparse_filename(fname)
        g = guessit(clean_name)
        title = g.get("title")
        year = g.get("year")
        has_ep = g.get("episode") is not None or g.get("season") is not None

        # If it's a collection folder, extras folder, non-ascii, or lacks a clear year/title, send to Gemini
        if has_non_ascii or any(tag in parent_name for tag in ["collection", "extras", "cinemascope", "anthology"]) or not title or not year:
            unresolved_for_ai.append((fid, item))
        elif not has_ep and year:
            parsed_items[fid] = {
                "type": "movie",
                "title": title,
                "year": year,
                "season": None,
                "episodes": [],
                "edition": g.get("edition", ""),
                "quality": str(g.get("screen_size", "1080p")).upper()
            }
        else:
            unresolved_for_ai.append((fid, item))

    # Grounded batch processing via Gemini
    if unresolved_for_ai and ai_client:
        print(f"🤖 Batch processing {len(unresolved_for_ai)} complex/contextual files through Gemini...")
        for i in range(0, len(unresolved_for_ai), 35):
            batch = unresolved_for_ai[i:i+35]
            ai_results = batch_ai_parse_grounded(batch)
            for fid, _ in batch:
                if fid in ai_results:
                    parsed_items[fid] = ai_results[fid]
                else:
                    ref_item = all_live_files[fid]
                    parsed_items[fid] = {
                        "type": "movie",
                        "title": ref_item.get("name", ""),
                        "year": None,
                        "season": None,
                        "episodes": [],
                        "edition": "",
                        "quality": "1080P"
                    }
            time.sleep(2)

    # Direct Metadata Resolution
    def resolve_worker(fid):
        item = all_live_files[fid]
        parsed = parsed_items.get(fid, {})
        imdb_id = parsed.get("imdb_id")
        m_type = parsed.get("type", "movie")
        meta = None

        if imdb_id and imdb_id.startswith("tt"):
            meta = get_cinemeta_meta(imdb_id, m_type)
        if not meta and parsed.get("title"):
            meta = search_cinemeta_exact(parsed["title"], parsed.get("year"), m_type)

        return fid, build_entry(fid, item, parsed, meta)

    if missing_ids:
        print(f"⚡ Resolving Cinemeta metadata for {len(missing_ids)} items across 12 threads...")
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(resolve_worker, fid) for fid in missing_ids]
            for f in as_completed(futures):
                fid, entry = f.result()
                pruned_catalog[fid] = entry
                print(f"🎬 Synced: {entry['name']} ➔ {entry['title']} ({entry['imdb_id']})")

    final_list = list(pruned_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(final_list, f, indent=2)

    print(f"\n🎉 Catalog update complete! Total entries: {len(final_list)}")

if __name__ == "__main__":
    main()
