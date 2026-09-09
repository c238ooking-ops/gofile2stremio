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

# Modern Google GenAI Client
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    try:
        from google import genai
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        ai_client = None
else:
    ai_client = None

ROOT_FOLDER_ID = "OBVVp1LI"
ROOT_URL = f"https://gofile.io/d/{ROOT_FOLDER_ID}"

VALID_VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".wmv", ".mov", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".vob", ".ogv", ".3gp",
    ".divx", ".xvid", ".rmvb", ".asf", ".f4v", ".wtv", ".iso"
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
        print("⚡ Refreshing browser session credentials via Chromium...")
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
# ROBUST CRAWLER WITH PINGER RETRY & BACKOFF
# ==========================================

def fetch_folder_page(session_mgr, folder_code, page_num=1, max_retries=5):
    """Fetches a folder page with exponential rate-limit backoff derived from ping.py."""
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
    """Safe, sequential crawl with pacing to prevent rate limits."""
    folders_queue = deque([(root_id, "Root")])
    visited_folders = set()
    all_live_files = {}

    while folders_queue:
        current_folder_id, current_folder_name = folders_queue.popleft()

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
                    if sub_code not in visited_folders and all(sub_code != f[0] for f in folders_queue):
                        folders_queue.append((sub_code, item.get("name", sub_code)))
                else:
                    fname = item.get("name", "")
                    if not is_video_file(fname):
                        continue
                    direct_link = extract_direct_stream_link(item, item_id)
                    if direct_link and item_id not in all_live_files:
                        item["_resolved_link"] = direct_link
                        item["_parent_folder"] = current_folder_name
                        all_live_files[item_id] = item
                        folder_files += 1

            if new_items_on_page == 0 or len(children_items) < 50:
                break

            page_num += 1
            time.sleep(0.5)  # Prevents mid-folder pagination rate limits

        print(f"📁 Scanned [{current_folder_name}]: {folder_files} video files found")
        time.sleep(0.8)  # Inter-folder cooldown

    return all_live_files

# ==========================================
# PARSING & BATCHED AI METADATA
# ==========================================

def normalize(s):
    return re.sub(r"[^\w]", "", (s or "").lower())

def expand_title(title):
    if not title:
        return ""
    clean = re.sub(r"\s+", " ", title).strip()
    return SERIES_ACRONYMS.get(clean.lower(), clean)

def clean_preparse_filename(filename):
    clean_name = re.sub(r"^@[\w\.\-]+(?:\s*-\s*|\s+)", "", filename, flags=re.I)
    clean_name = re.sub(r"\[(?:TTT|CN Dub|Tamil|Hindi|Eng|Dual Audio|HEVC|10bit)[^\]]*\]", "", clean_name, flags=re.I)
    clean_name = re.sub(r"\b(ia)\b", "", clean_name, flags=re.I).strip(" ._-")
    return clean_name

def batch_ai_parse(unresolved_items):
    if not ai_client or not unresolved_items:
        return {}

    items_payload = [{"id": fid, "filename": item.get("name", ""), "parent": item.get("_parent_folder", "")} 
                     for fid, item in unresolved_items]

    prompt = f"""Identify media metadata for these filenames. Clean translation noise and extra tags.
Entries: {json.dumps(items_payload)}

Return ONLY a JSON list:
[
  {{
    "id": "item_id",
    "type": "movie" or "series",
    "title": "English Canonical Title",
    "year": integer or null,
    "season": integer or null,
    "episodes": [integers] or null,
    "edition": "string" or null,
    "quality": "string"
  }}
]"""

    try:
        response = ai_client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        data = json.loads(response.text)
        return {entry["id"]: entry for entry in data if "id" in entry}
    except Exception as e:
        print(f"⚠️ Batch AI parsing failed: {e}")
        return {}

def parse_with_guessit(filename):
    clean_name = clean_preparse_filename(filename)
    g = guessit(clean_name)
    season_pack = re.search(r"\b[sS](\d{1,2})\b(?!\s*[eE]\d+)", clean_name)
    dual_match = re.search(r"(\d{1,2})?(\d{2})\s*\+\s*(?:\d{1,2})?(\d{2})", clean_name)

    raw_title = g.get("title")
    if not raw_title and season_pack:
        raw_title = clean_name[:season_pack.start()].strip(" -._")

    if not raw_title or len(raw_title) <= 2:
        return None

    m_type = "series" if g.get("type") == "episode" or dual_match or season_pack else "movie"
    title = expand_title(raw_title) if raw_title else clean_name

    episodes = []
    edition_tags = []

    if season_pack and not g.get("episode"):
        season = int(season_pack.group(1))
        episodes = [1]
        edition_tags.append(f"Complete Season {season} Pack")
    elif dual_match:
        m_start = re.search(r"\b([1-9]\d{2,3})\s*\+", clean_name)
        if m_start:
            full_first = m_start.group(1)
            ep1 = int(full_first[-2:])
            season = int(full_first[:-2])
            ep2 = int(dual_match.group(2))
            episodes = [ep1, ep2]
            edition_tags.append(f"Ep {ep1}+{ep2}")
    else:
        ep_data = g.get("episode")
        if isinstance(ep_data, list):
            episodes = [int(e) for e in ep_data]
            edition_tags.append(f"Ep {'+'.join(str(e) for e in episodes)}")
        elif ep_data is not None:
            episodes = [int(ep_data)]
        else:
            episodes = [1] if m_type == "series" else []
        season = int(g.get("season")) if g.get("season") is not None else (1 if m_type == "series" else None)

    if g.get("edition"):
        ed = g.get("edition")
        edition_tags.append(ed if isinstance(ed, str) else " / ".join(ed))

    quality = str(g.get("screen_size", "1080p")).upper()
    year = g.get("year")
    if not year:
        ym = re.search(r"\b(19\d\d|20\d\d)\b", clean_name)
        if ym:
            year = int(ym.group(1))

    return {
        "type": m_type,
        "title": title,
        "year": year,
        "season": season,
        "episodes": episodes,
        "edition": " ".join(dict.fromkeys(edition_tags)),
        "quality": quality
    }

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

def search_imdb(title, year):
    clean_q = re.sub(r"[^\w\s]", "", title)
    first_char = clean_q[0].lower() if clean_q else "a"
    url = f"https://v3.sg.media-imdb.com/suggestion/{first_char}/{requests.utils.quote(title)}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        for item in res.get("d", []):
            iid = item.get("id", "")
            if iid.startswith("tt"):
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

    match = search_cinemeta(parsed["title"], parsed.get("year"), parsed.get("type"))
    if match:
        return {"id": match.get("id"), "name": match.get("name"), "poster": match.get("poster", "")}

    return search_imdb(parsed["title"], parsed.get("year"))

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
# MAIN ROUTINE
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

    print(f"📌 Cached matches: {len(pruned_catalog)} | New or Renamed to Index: {len(missing_ids)}\n")

    parsed_items = {}
    unresolved_for_ai = []

    for fid in missing_ids:
        item = all_live_files[fid]
        parsed = parse_with_guessit(item.get("name", ""))
        if parsed:
            parsed_items[fid] = parsed
        else:
            unresolved_for_ai.append((fid, item))

    # Batch AI requests into groups of 20 instead of 4.2s per-item delays
    if unresolved_for_ai and ai_client:
        print(f"🤖 Batching {len(unresolved_for_ai)} complex items to Gemini...")
        for i in range(0, len(unresolved_for_ai), 20):
            batch = unresolved_for_ai[i:i+20]
            ai_results = batch_ai_parse(batch)
            for fid, _ in batch:
                if fid in ai_results:
                    parsed_items[fid] = ai_results[fid]
                else:
                    parsed_items[fid] = {
                        "type": "movie",
                        "title": all_live_files[fid].get("name", ""),
                        "year": None,
                        "season": None,
                        "episodes": [],
                        "edition": "",
                        "quality": "1080P"
                    }

    # Parallelize Stremio/IMDb metadata resolution (independent of Gofile)
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
                print(f"🎬 Synced: {entry['name']} ➔ {entry['title']} ({entry['imdb_id']})")

    final_list = list(pruned_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(final_list, f, indent=2)

    print(f"\n🎉 Catalog update complete! Total entries: {len(final_list)}")

if __name__ == "__main__":
    main()
