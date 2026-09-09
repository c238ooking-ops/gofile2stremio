import os
import sys
import json
import time
import re
from collections import deque
from difflib import SequenceMatcher
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
# BROWSER SESSION MANAGER
# ==========================================

class BrowserSessionManager:
    def __init__(self, root_url):
        self.root_url = root_url
        self.session = requests.Session()
        self.last_auth_time = 0
        self.refresh_credentials()

    def refresh_credentials(self):
        print("⚡ Capturing authenticated Gofile session headers via Chromium...")
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
                print(f"Browser notice: {e}")
            finally:
                browser.close()

        if not captured["headers"]:
            print("❌ Failed to intercept browser session headers.")
            sys.exit(1)

        self.session.headers.clear()
        self.session.headers.update(captured["headers"])
        self.last_auth_time = time.time()
        print("✅ Session credentials captured.")

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
# CRAWLER ENGINE (TOP-DOWN STRUCTURE CAPTURE)
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
                print(f"   ⏳ Rate limited on [{folder_code}]. Pausing {cool_off}s...")
                time.sleep(cool_off)
            elif status in ["error-auth", "error-token"]:
                session_mgr.refresh_credentials()
                time.sleep(2)
            else:
                return res
        except Exception:
            time.sleep(3)

    return None

def crawl_tree(session_mgr, root_id):
    """Crawls folders and groups items by folder container."""
    folders_queue = deque([(root_id, "Root", ["Root"])])
    visited_folders = set()
    
    # folder_id -> { "name": ..., "path": [...], "files": { fid: item } }
    folder_containers = {}

    while folders_queue:
        current_folder_id, current_folder_name, current_path = folders_queue.popleft()

        if current_folder_id in visited_folders:
            continue
        visited_folders.add(current_folder_id)

        container_files = {}
        page_num = 1
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
                    if direct_link:
                        item["_resolved_link"] = direct_link
                        item["_parent_folder"] = current_folder_name
                        item["_folder_path"] = current_path
                        container_files[item_id] = item

            if new_items_on_page == 0 or len(children_items) < 50:
                break

            page_num += 1
            time.sleep(0.5)

        if container_files:
            folder_containers[current_folder_id] = {
                "name": current_folder_name,
                "path": current_path,
                "files": container_files
            }
            print(f"📁 Scanned [{current_folder_name}]: {len(container_files)} video files")

        time.sleep(0.8)

    return folder_containers

# ==========================================
# TOP-DOWN CONTAINER RESOLVER
# ==========================================

def call_gemini(prompt):
    if not ai_client:
        return None
    for model_name in CANDIDATE_AI_MODELS:
        try:
            res = ai_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={"response_mime_type": "application/json"}
            )
            if res and res.text:
                return res.text
        except Exception as e:
            if "404" in str(e) or "NOT_FOUND" in str(e):
                continue
            print(f"⚠️ Gemini {model_name} error: {e}")
            break
    return None

def resolve_folder_container(folder_name, folder_path, sample_filenames):
    """Resolves whether a folder is a Series, a Movie collection, or loose files."""
    clean_name = folder_name.strip()
    if clean_name.lower() in GENERIC_FOLDERS or clean_name == "Root":
        return {"category": "loose"}

    prompt = f"""You are a media archivist organizing a Stremio catalog.
Analyze this directory and its contents to identify the overarching media identity.

Directory Name: "{folder_name}"
Full Path: "{" / ".join(folder_path)}"
Sample Files inside this folder:
{json.dumps(sample_filenames[:12], indent=2)}

Determine:
1. Is this directory an entire SERIES/FRANCHISE containing episodes, shorts, or extras? (e.g. 'Tom and Jerry', 'Ed, Edd n Eddy', 'Breaking Bad')
   -> category: 'series'
2. Is this directory for a single MOVIE (which may have sample clips or just the movie file)?
   -> category: 'movie'
3. Is it a loose folder of unrelated mixed films/videos?
   -> category: 'loose'

Return JSON:
{{
  "category": "series" | "movie" | "loose",
  "canonical_title": "Clean English Title of Show or Movie",
  "imdb_id": "ttXXXXXXX or null",
  "year": integer or null
}}"""

    res_text = call_gemini(prompt)
    if res_text:
        try:
            data = json.loads(res_text)
            if data.get("category"):
                return data
        except Exception:
            pass

    return {"category": "loose"}

def fetch_series_episodes_from_cinemeta(imdb_id):
    """Fetches the official episode list for a series from Cinemeta."""
    url = f"https://v3-cinemeta.strem.io/meta/series/{imdb_id}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=6).json()
        meta = res.get("meta", {})
        videos = meta.get("videos", [])
        episodes_map = []
        for v in videos:
            episodes_map.append({
                "season": v.get("season"),
                "episode": v.get("episode") or v.get("number"),
                "title": (v.get("title") or v.get("name") or "").lower(),
                "id": v.get("id")  # usually tt...:s:e
            })
        return meta, episodes_map
    except Exception:
        return {}, []

def fetch_movie_meta_from_cinemeta(imdb_id):
    url = f"https://v3-cinemeta.strem.io/meta/movie/{imdb_id}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        return res.get("meta", {})
    except Exception:
        return {}

def fuzzy_find_episode(fname, episodes_map):
    """Matches a filename to the best official show episode by title similarity."""
    clean_f = re.sub(r"[\(\[\{].*?[\)\]\}]", "", fname)
    clean_f = re.sub(r"[^\w\s]", " ", clean_f).strip().lower()

    # Look for explicit SxxExx in filename first
    se_match = re.search(r"\b[sS](\d{1,2})[eE](\d{1,3})\b", fname)
    if se_match:
        s_num = int(se_match.group(1))
        e_num = int(se_match.group(2))
        return s_num, e_num

    best_ratio = 0
    best_ep = None

    for ep in episodes_map:
        ep_title = ep.get("title", "")
        if not ep_title or len(ep_title) < 3:
            continue
        
        # Substring or sequence match
        if ep_title in clean_f:
            ratio = len(ep_title) / max(len(clean_f), 1) + 0.5
        else:
            ratio = SequenceMatcher(None, clean_f, ep_title).ratio()

        if ratio > best_ratio:
            best_ratio = ratio
            best_ep = ep

    if best_ep and best_ratio >= 0.55:
        return best_ep["season"], best_ep["episode"]

    return None, None

# ==========================================
# LOOSE FILE RESOLVER (MOVIES & UNGROUPED)
# ==========================================

def batch_ai_loose_files(unresolved_items):
    if not unresolved_items:
        return {}

    items_payload = [{
        "id": fid,
        "filename": item.get("name", ""),
        "parent_folder": item.get("_parent_folder", "")
    } for fid, item in unresolved_items]

    prompt = f"""Identify the exact English media title and IMDb ID for these standalone files.
Handle foreign titles correctly (e.g., 'Форсаж 5' -> 'Fast Five', IMDb: tt1596343).

Files:
{json.dumps(items_payload, indent=2)}

Return ONLY JSON:
[
  {{
    "id": "item_id",
    "type": "movie" or "series",
    "title": "Canonical English Title",
    "imdb_id": "ttXXXXXXX or null",
    "year": integer or null,
    "season": integer or null,
    "episodes": [integers] or null
  }}
]"""

    res_text = call_gemini(prompt)
    if res_text:
        try:
            data = json.loads(res_text)
            return {entry["id"]: entry for entry in data if "id" in entry}
        except Exception:
            pass
    return {}

# ==========================================
# ENTRY BUILDER
# ==========================================

def make_stream_entry(fid, item, m_type, imdb_id, title, poster, season=1, episode=1, edition="", quality="1080P"):
    fname = item.get("name", fid)
    link = item.get("_resolved_link") or extract_direct_stream_link(item, fid)
    size = item.get("size", 0)
    size_mb = f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"

    if m_type == "series":
        stream_id = f"{imdb_id}:{season}:{episode}"
        return {
            "file_id": fid,
            "type": "series",
            "imdb_id": imdb_id,
            "title": title,
            "name": fname,
            "season": season,
            "episode": episode,
            "stream_id": stream_id,
            "stream_ids": [stream_id],
            "poster": poster or "https://gofile.io/dist/img/logo-small.png",
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
            "title": title,
            "name": fname,
            "stream_id": imdb_id,
            "stream_ids": [imdb_id],
            "poster": poster or "https://gofile.io/dist/img/logo-small.png",
            "edition": edition,
            "quality": quality,
            "size": size_mb,
            "link": link
        }

# ==========================================
# MAIN ROUTINE
# ==========================================

def main():
    existing_catalog = {}
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                for entry in json.load(f):
                    fid = entry.get("file_id")
                    if fid:
                        existing_catalog[fid] = entry
            print(f"📦 Loaded {len(existing_catalog)} entries from data.json")
        except Exception as e:
            print(f"⚠️ data.json read warning: {e}")

    session_mgr = BrowserSessionManager(ROOT_URL)
    folder_containers = crawl_tree(session_mgr, ROOT_FOLDER_ID)

    all_scanned_files = {}
    for c in folder_containers.values():
        all_scanned_files.update(c["files"])

    print(f"\n📊 Discovered {len(all_scanned_files)} live video files across {len(folder_containers)} folders.")
    if not all_scanned_files:
        print("❌ 0 files found. Preserving data.json.")
        sys.exit(1)

    final_catalog = {}
    loose_files_to_resolve = []

    # Phase 1: Process Containers Top-Down
    print("\n🏗️ Processing folder containers top-down...")
    for folder_id, container in folder_containers.items():
        fname = container["name"]
        files = container["files"]
        sample_names = [f.get("name", "") for f in list(files.values())[:10]]

        # Determine container identity
        identity = resolve_folder_container(fname, container["path"], sample_names)
        category = identity.get("category", "loose")

        if category == "series" and identity.get("imdb_id"):
            series_id = identity["imdb_id"]
            series_title = identity.get("canonical_title", fname)
            print(f"📺 Series Container Identified: [{fname}] ➔ {series_title} ({series_id})")

            series_meta, ep_map = fetch_series_episodes_from_cinemeta(series_id)
            poster = series_meta.get("poster", "")
            extra_counter = 1

            for fid, item in files.items():
                iname = item.get("name", "")
                is_extra = any(tag in iname.lower() for tag in ["extra", "promo", "interview", "featurette", "bonus"])

                if is_extra:
                    # Specials slot (Season 0)
                    final_catalog[fid] = make_stream_entry(
                        fid, item, "series", series_id, series_title, poster,
                        season=0, episode=extra_counter, edition="Special / Extra"
                    )
                    extra_counter += 1
                else:
                    s_num, e_num = fuzzy_find_episode(iname, ep_map)
                    if s_num is not None and e_num is not None:
                        final_catalog[fid] = make_stream_entry(
                            fid, item, "series", series_id, series_title, poster,
                            season=s_num, episode=e_num
                        )
                    else:
                        # Fallback to incremental special if title unlisted in official season
                        final_catalog[fid] = make_stream_entry(
                            fid, item, "series", series_id, series_title, poster,
                            season=0, episode=extra_counter, edition=f"Short: {iname[:25]}"
                        )
                        extra_counter += 1

        elif category == "movie" and identity.get("imdb_id"):
            movie_id = identity["imdb_id"]
            movie_title = identity.get("canonical_title", fname)
            print(f"🎬 Movie Container Identified: [{fname}] ➔ {movie_title} ({movie_id})")
            m_meta = fetch_movie_meta_from_cinemeta(movie_id)
            poster = m_meta.get("poster", "")

            for fid, item in files.items():
                final_catalog[fid] = make_stream_entry(
                    fid, item, "movie", movie_id, movie_title, poster
                )
        else:
            # Loose or collection folder -> process file-by-file
            for fid, item in files.items():
                loose_files_to_resolve.append((fid, item))

    # Phase 2: Resolve Loose / Standalone Files
    print(f"\n🔍 Resolving {len(loose_files_to_resolve)} standalone / loose files...")
    unresolved_for_ai = []

    for fid, item in loose_files_to_resolve:
        # Check cache first
        if fid in existing_catalog:
            cached = existing_catalog[fid]
            if cached.get("name") == item.get("name"):
                cached["link"] = item.get("_resolved_link")
                final_catalog[fid] = cached
                continue

        iname = item.get("name", "")
        has_non_ascii = any(ord(c) > 127 for c in iname)
        g = guessit(iname)
        title = g.get("title")
        year = g.get("year")
        has_ep = g.get("episode") is not None or g.get("season") is not None

        # Clean Hollywood movies with year
        if not has_non_ascii and not has_ep and year and title:
            # Query Cinemeta directly for the movie
            m_url = f"https://v3-cinemeta.strem.io/catalog/movie/top/search={requests.utils.quote(title)}.json"
            try:
                res = HTTP_CLIENT.get(m_url, timeout=5).json()
                metas = res.get("metas", [])
                matched_meta = None
                for m in metas:
                    m_year = str(m.get("year") or "")
                    if abs(int(m_year[:4]) - int(year)) <= 1:
                        matched_meta = m
                        break
                if matched_meta:
                    final_catalog[fid] = make_stream_entry(
                        fid, item, "movie", matched_meta["id"], matched_meta["name"], matched_meta.get("poster")
                    )
                    continue
            except Exception:
                pass

        unresolved_for_ai.append((fid, item))

    # Batch AI for complex loose items (Russian titles, unparsed releases)
    if unresolved_for_ai and ai_client:
        print(f"🤖 Batching {len(unresolved_for_ai)} loose items to Gemini...")
        for i in range(0, len(unresolved_for_ai), 35):
            batch = unresolved_for_ai[i:i+35]
            ai_results = batch_ai_loose_files(batch)
            for fid, item in batch:
                if fid in ai_results:
                    res = ai_results[fid]
                    m_type = res.get("type", "movie")
                    imdb_id = res.get("imdb_id") or f"gf:{fid}"
                    title = res.get("title", item.get("name", fid))
                    poster = "https://gofile.io/dist/img/logo-small.png"

                    if imdb_id.startswith("tt"):
                        c_meta = fetch_movie_meta_from_cinemeta(imdb_id) if m_type == "movie" else fetch_series_episodes_from_cinemeta(imdb_id)[0]
                        poster = c_meta.get("poster", poster)

                    season = res.get("season") or 1
                    episodes = res.get("episodes") or [1]
                    final_catalog[fid] = make_stream_entry(
                        fid, item, m_type, imdb_id, title, poster, season=season, episode=episodes[0]
                    )
                else:
                    final_catalog[fid] = make_stream_entry(
                        fid, item, "movie", f"gf:{fid}", item.get("name", fid), ""
                    )
            time.sleep(2)

    # Save finalized catalog
    output_list = list(final_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output_list, f, indent=2)

    print(f"\n🎉 Catalog build complete! Total indexed: {len(output_list)} entries.")

if __name__ == "__main__":
    main()
