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

SERIES_ACRONYMS = {
    "bcs": "Better Call Saul",
    "bb": "Breaking Bad",
    "got": "Game of Thrones",
    "hotd": "House of the Dragon",
    "himym": "How I Met Your Mother",
    "tbbt": "The Big Bang Theory",
    "atla": "Avatar: The Last Airbender"
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
# CRAWLER ENGINE WITH BACKOFF
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
# METADATA EXTRACTION & AI
# ==========================================

def normalize(s):
    return re.sub(r"[^\w]", "", (s or "").lower())

def clean_preparse_filename(filename):
    clean_name = re.sub(r"^@[\w\.\-]+(?:\s*-\s*|\s+)", "", filename, flags=re.I)
    clean_name = re.sub(r"\[(?:TTT|CN Dub|Tamil|Hindi|Eng|Dual Audio|HEVC|10bit)[^\]]*\]", "", clean_name, flags=re.I)
    clean_name = re.sub(r"\b(ia)\b", "", clean_name, flags=re.I).strip(" ._-")
    return clean_name

def extract_primary_folder_title(parent_name):
    if not parent_name:
        return ""
    clean = re.sub(r"[\(\[\{].*?[\)\]\}]", "", parent_name)
    clean = re.sub(r"\b(the\s+)?(complete|collection|cinemascope|anthology|pack|season|series|movies|specials|films)\b", "", clean, flags=re.I)
    clean = re.sub(r"\s*-\s*.*", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" ._-")
    if clean.lower() in GENERIC_FOLDERS or len(clean) < 3:
        return ""
    return clean

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

def batch_ai_parse(unresolved_items):
    if not unresolved_items:
        return {}

    items_payload = [{
        "id": fid,
        "filename": item.get("name", ""),
        "parent_folder": item.get("_parent_folder", ""),
        "path_hierarchy": " / ".join(item.get("_folder_path", []))
    } for fid, item in unresolved_items]

    prompt = f"""You are an expert film and television archivist. Determine the true media identity using the folder hierarchy and filename together.

Guidelines:
- If a file is inside a collection folder like 'Tom and Jerry - The Complete CinemaScope Collection (1954–1958)', the overarching franchise/title is 'Tom and Jerry', and the file is an episode/short of that series.
- If a file is an extra or bonus featurette (e.g., 'Ed, Edd n Eddy - EXTRA - Promo...'), the overarching title is the main series ('Ed, Edd n Eddy'), NOT 'Extras'.
- If the item is clearly a standalone movie (e.g., 'The Batman 2022' or '500 Days of Summer 2009' or 'Форсаж 5 (2011)'), classify it as a movie and provide the canonical English title.

Payload:
{json.dumps(items_payload, indent=2)}

Return ONLY a JSON list:
[
  {{
    "id": "file_id",
    "type": "movie" or "series",
    "title": "Canonical Show or Film Title",
    "episode_title": "Episode or short title if part of a series, else null",
    "year": integer or null,
    "season": integer or null,
    "episodes": [integers] or null,
    "edition": "string" or null,
    "quality": "string"
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

def parse_with_heuristics(filename, parent_folder=""):
    clean_name = clean_preparse_filename(filename)
    parent_canonical = extract_primary_folder_title(parent_folder)

    g = guessit(clean_name)
    raw_title = g.get("title")

    season_pack = re.search(r"\b[sS](\d{1,2})\b(?!\s*[eE]\d+)", clean_name)
    dual_match = re.search(r"(\d{1,2})?(\d{2})\s*\+\s*(?:\d{1,2})?(\d{2})", clean_name)
    has_explicit_ep = g.get("episode") is not None or g.get("season") is not None or dual_match or season_pack

    year = g.get("year")
    if not year:
        ym = re.search(r"\b(19\d\d|20\d\d)\b", clean_name)
        if ym:
            year = int(ym.group(1))

    # Standalone movie rule
    if not has_explicit_ep and year and not ("season" in (parent_folder or "").lower()):
        return {
            "type": "movie",
            "title": SERIES_ACRONYMS.get((raw_title or clean_name).lower(), raw_title or clean_name),
            "year": year,
            "season": None,
            "episodes": [],
            "edition": g.get("edition", ""),
            "quality": str(g.get("screen_size", "1080p")).upper()
        }

    # Explicit series rule
    if has_explicit_ep:
        title = parent_canonical if (parent_canonical and not raw_title) else (raw_title or clean_name)
        season = int(g.get("season")) if g.get("season") is not None else 1
        ep_data = g.get("episode")
        episodes = [int(ep_data)] if isinstance(ep_data, int) else ([int(e) for e in ep_data] if isinstance(ep_data, list) else [1])
        return {
            "type": "series",
            "title": SERIES_ACRONYMS.get(title.lower(), title),
            "year": year,
            "season": season,
            "episodes": episodes,
            "edition": g.get("edition", ""),
            "quality": str(g.get("screen_size", "1080p")).upper()
        }

    return None

# ==========================================
# TARGETED METADATA RESOLUTION
# ==========================================

def search_cinemeta(title, year, m_type):
    catalog_type = "series" if m_type == "series" else "movie"
    url = f"https://v3-cinemeta.strem.io/catalog/{catalog_type}/top/search={requests.utils.quote(title)}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        metas = res.get("metas", [])
        for m in metas:
            cand_year = str(m.get("year") or m.get("releaseInfo") or "")
            if year and cand_year and abs(int(cand_year[:4]) - int(year)) <= 1:
                return m
            if not year and normalize(m.get("name", "")) == normalize(title):
                return m
        return metas[0] if metas else None
    except Exception:
        return None

def search_imdb_exact(query, target_year=None, parsed_type="movie"):
    clean_q = re.sub(r"[^\w\s]", "", query).strip()
    if not clean_q or clean_q.lower() in GENERIC_FOLDERS:
        return None
    first_char = clean_q[0].lower()
    url = f"https://v3.sg.media-imdb.com/suggestion/{first_char}/{requests.utils.quote(clean_q)}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        for item in res.get("d", []):
            iid = item.get("id", "")
            if not iid.startswith("tt"):
                continue

            q_type = item.get("q", "")
            item_year = item.get("y")

            if target_year and item_year:
                if abs(int(item_year) - int(target_year)) > 1:
                    continue

            if parsed_type == "movie" and q_type not in ["feature", "movie"]:
                continue
            if parsed_type == "series" and q_type not in ["TV series", "TV mini-series", "TV episode"]:
                continue

            return {
                "id": iid,
                "name": item.get("l"),
                "poster": item.get("i", {}).get("imageUrl", "")
            }
    except Exception:
        pass
    return None

def resolve_meta(parsed):
    if not parsed or not parsed.get("title"):
        return None

    title = parsed["title"]
    year = parsed.get("year")
    m_type = parsed.get("type", "movie")

    if title.lower() in GENERIC_FOLDERS:
        return None

    # Step 1: Strict year-matched Cinemeta search
    match = search_cinemeta(title, year, m_type)
    if match:
        return {"id": match.get("id"), "name": match.get("name"), "poster": match.get("poster", "")}

    # Step 2: Strict type/year IMDb search
    imdb_match = search_imdb_exact(title, target_year=year, parsed_type=m_type)
    if imdb_match:
        return imdb_match

    # Step 3: Compound fallback for franchise shorts/specials
    if parsed.get("episode_title"):
        compound = f"{title} {parsed['episode_title']}"
        compound_match = search_imdb_exact(compound, target_year=year, parsed_type="movie")
        if compound_match:
            return compound_match

    return None

def build_entry(fid, item, parsed, meta):
    fname = item.get("name", fid)
    link = item.get("_resolved_link") or extract_direct_stream_link(item, fid)
    size = item.get("size", 0)
    size_mb = f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"

    imdb_id = meta["id"] if meta else f"gf:{fid}"
    display_title = meta["name"] if meta else parsed["title"]
    poster = meta["poster"] if meta and meta.get("poster") else "https://gofile.io/dist/img/logo-small.png"

    if parsed["type"] == "series":
        season_num = parsed.get("season", 1)
        ep_list = parsed.get("episodes", [1])
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
            "edition": parsed.get("edition", ""),
            "quality": parsed.get("quality", "1080P"),
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
            "edition": parsed.get("edition", ""),
            "quality": parsed.get("quality", "1080P"),
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

    print(f"📌 Cached matches: {len(pruned_catalog)} | Unindexed or Changed: {len(missing_ids)}\n")

    parsed_items = {}
    unresolved_for_ai = []

    for fid in missing_ids:
        item = all_live_files[fid]
        parent_dir = item.get("_parent_folder", "")
        fname = item.get("name", "")

        has_non_ascii = any(ord(c) > 127 for c in fname)
        heuristics_match = parse_with_heuristics(fname, parent_dir)

        if not heuristics_match or has_non_ascii:
            unresolved_for_ai.append((fid, item))
        else:
            parsed_items[fid] = heuristics_match

    # Batch AI processing with parent context
    if unresolved_for_ai and ai_client:
        print(f"🤖 Batch processing {len(unresolved_for_ai)} files through Gemini...")
        for i in range(0, len(unresolved_for_ai), 40):
            batch = unresolved_for_ai[i:i+40]
            ai_results = batch_ai_parse(batch)
            for fid, _ in batch:
                if fid in ai_results:
                    parsed_items[fid] = ai_results[fid]
                else:
                    ref_item = all_live_files[fid]
                    parent_clean = extract_primary_folder_title(ref_item.get("_parent_folder", ""))
                    parsed_items[fid] = {
                        "type": "series" if parent_clean else "movie",
                        "title": parent_clean or ref_item.get("name", ""),
                        "year": None,
                        "season": 1,
                        "episodes": [1],
                        "edition": "",
                        "quality": "1080P"
                    }
            time.sleep(3)

    def resolve_worker(fid):
        item = all_live_files[fid]
        parsed = parsed_items.get(fid)
        meta = resolve_meta(parsed)
        return fid, build_entry(fid, item, parsed, meta)

    if missing_ids:
        print(f"⚡ Resolving Cinemeta/IMDb metadata for {len(missing_ids)} items across 10 threads...")
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(resolve_worker, fid) for fid in missing_ids]
            for f in as_completed(futures):
                fid, entry = f.result()
                pruned_catalog[fid] = entry
                print(f"🎬 Synced: [{all_live_files[fid].get('_parent_folder')}] {entry['name']} ➔ {entry['title']} ({entry['imdb_id']})")

    final_list = list(pruned_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(final_list, f, indent=2)

    print(f"\n🎉 Catalog update complete! Total entries: {len(final_list)}")

if __name__ == "__main__":
    main()
