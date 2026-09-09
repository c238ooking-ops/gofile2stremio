import os
import sys
import json
import time
import re
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from guessit import guessit

# Gemini GenAI Client
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

def create_http_session():
    """High-performance session with connection pooling and automated backoff."""
    session = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25, max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    })
    return session

HTTP_CLIENT = create_http_session()

# ==========================================
# FAST NATIVE GOFILE AUTH (NO BROWSER)
# ==========================================

class FastSessionManager:
    def __init__(self):
        self.session = create_http_session()
        self.last_auth_time = 0
        self.token = None
        self.refresh_credentials()

    def refresh_credentials(self):
        print("⚡ Requesting native Gofile guest authentication token...")
        try:
            res = self.session.post("https://api.gofile.io/accounts", timeout=15).json()
            if res.get("status") == "ok":
                self.token = res["data"]["token"]
                self.session.headers.update({"Authorization": f"Bearer {self.token}"})
                self.last_auth_time = time.time()
                print("✅ Successfully authorized without browser.")
                return
        except Exception as e:
            print(f"⚠️ Guest account creation failed: {e}")

        # Fallback to direct token header
        self.session.headers.update({"Authorization": "Bearer anonymous"})
        self.last_auth_time = time.time()

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
# PARALLEL CRAWLER PIPELINE
# ==========================================

def fetch_folder_contents(session_mgr, folder_id):
    """Exhaustively fetches all pages of a folder."""
    all_children = {}
    page_num = 1

    while True:
        session_mgr.ensure_fresh()
        api_url = f"https://api.gofile.io/contents/{folder_id}?page={page_num}&pageSize=100&sortField=createTime&sortDirection=-1"
        try:
            res = session_mgr.session.get(api_url, timeout=20).json()
            status = res.get("status")
            if status != "ok":
                break

            data = res.get("data", {})
            children = data.get("children", {})
            if not children:
                break

            if isinstance(children, dict):
                all_children.update(children)
                count = len(children)
            else:
                for c in children:
                    cid = c.get("id") or c.get("file_id")
                    if cid:
                        all_children[cid] = c
                count = len(children)

            if count < 50:
                break
            page_num += 1
        except Exception:
            break

    return all_children

def crawl_tree(session_mgr, root_id):
    """Crawls folders concurrently across multiple threads."""
    folder_queue = Queue()
    folder_queue.put((root_id, "Root"))

    visited_folders = {root_id}
    all_live_files = {}

    def worker():
        while True:
            try:
                fid, fname = folder_queue.get_nowait()
            except Exception:
                break

            children = fetch_folder_contents(session_mgr, fid)
            folder_file_count = 0

            for item_id, item in children.items():
                if not item:
                    continue

                if item.get("type") == "folder":
                    sub_code = item.get("code") or item.get("id") or item_id
                    if sub_code not in visited_folders:
                        visited_folders.add(sub_code)
                        folder_queue.put((sub_code, item.get("name", sub_code)))
                else:
                    iname = item.get("name", "")
                    if not is_video_file(iname):
                        continue
                    direct_link = extract_direct_stream_link(item, item_id)
                    if direct_link:
                        item["_resolved_link"] = direct_link
                        item["_parent_folder"] = fname
                        all_live_files[item_id] = item
                        folder_file_count += 1

            folder_queue.task_done()

    # Parallelize directory discovery
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker) for _ in range(5)]
        folder_queue.join()
        for f in futures:
            f.cancel()

    return all_live_files

# ==========================================
# PARSER & BATCHED AI METADATA
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
    """Processes multiple tough filenames in a single Gemini call."""
    if not ai_client or not unresolved_items:
        return {}

    items_payload = [{"id": fid, "filename": item.get("name", ""), "parent": item.get("_parent_folder", "")} 
                     for fid, item in unresolved_items]

    prompt = f"""Identify the media metadata for these filenames. Clean foreign translations and release tags.
Entries: {json.dumps(items_payload)}

Return ONLY a JSON list matching this structure:
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
        print(f"⚠️ Batch AI parsing skipped: {e}")
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
        return None  # Needs AI rescue

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

# ==========================================
# METADATA RESOLVER (CINEMETA / IMDB)
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

    # Cinemeta handles ~90% of requests in Stremio-ready format
    match = search_cinemeta(parsed["title"], parsed.get("year"), parsed.get("type"))
    if match:
        return {"id": match.get("id"), "name": match.get("name"), "poster": match.get("poster", "")}

    # Fallback to IMDB
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

    session_mgr = FastSessionManager()
    start_crawl = time.time()
    all_live_files = crawl_tree(session_mgr, ROOT_FOLDER_ID)
    print(f"⏱️ Crawled {len(all_live_files)} files in {time.time() - start_crawl:.2f}s")

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

    print(f"📌 Cached: {len(pruned_catalog)} | Unindexed / Changed: {len(missing_ids)}")

    # Two-stage parsing: fast local GuessIt followed by batch AI for failures
    parsed_items = {}
    unresolved_for_ai = []

    for fid in missing_ids:
        item = all_live_files[fid]
        parsed = parse_with_guessit(item.get("name", ""))
        if parsed:
            parsed_items[fid] = parsed
        else:
            unresolved_for_ai.append((fid, item))

    # Batch process unresolved files in chunks of 20
    if unresolved_for_ai and ai_client:
        print(f"🤖 Batch processing {len(unresolved_for_ai)} complex filenames with Gemini...")
        for i in range(0, len(unresolved_for_ai), 20):
            batch = unresolved_for_ai[i:i+20]
            ai_results = batch_ai_parse(batch)
            for fid, _ in batch:
                if fid in ai_results:
                    parsed_items[fid] = ai_results[fid]
                else:
                    # Final fallback
                    parsed_items[fid] = {
                        "type": "movie",
                        "title": all_live_files[fid].get("name", ""),
                        "year": None,
                        "season": None,
                        "episodes": [],
                        "edition": "",
                        "quality": "1080P"
                    }

    # Parallel resolve metadata using pooled workers
    def resolve_worker(fid):
        item = all_live_files[fid]
        parsed = parsed_items.get(fid)
        meta = resolve_meta(parsed)
        return fid, build_entry(fid, item, parsed, meta)

    if missing_ids:
        print(f"⚡ Resolving metadata for {len(missing_ids)} items across 12 threads...")
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(resolve_worker, fid) for fid in missing_ids]
            for f in as_completed(futures):
                fid, entry = f.result()
                pruned_catalog[fid] = entry

    final_list = list(pruned_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(final_list, f, indent=2)

    print(f"\n🎉 Sync completed! Output catalog size: {len(final_list)} entries.")

if __name__ == "__main__":
    main()
