import os
import sys
import json
import time
import re
from collections import deque
from difflib import SequenceMatcher
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from playwright.sync_api import sync_playwright
import PTN

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
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
    adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25, max_retries=retries)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    })
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
        print("⚡ Capturing browser session headers via Chromium...")
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
# CRAWLER ENGINE
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
                    sub_name = item.get("name", sub_code)
                    if sub_code not in visited_folders and all(sub_code != f[0] for f in folders_queue):
                        folders_queue.append((sub_code, sub_name, current_path + [sub_name]))
                else:
                    fname = item.get("name", "")
                    if not is_video_file(fname):
                        continue
                    direct_link = extract_direct_stream_link(item, item_id)
                    if direct_link:
                        item["_resolved_link"] = direct_link
                        item["_parent_folder"] = current_folder_name
                        item["_folder_path"] = current_path
                        all_live_files[item_id] = item
                        folder_files += 1

            if new_items_on_page == 0 or len(children_items) < 50:
                break

            page_num += 1
            time.sleep(0.5)

        print(f"📁 Scanned [{current_folder_name}]: {folder_files} files")
        time.sleep(0.8)

    return all_live_files

# ==========================================
# SANITIZATION & MATCHING ENGINE
# ==========================================

def clean_media_string(raw_name):
    """Aggressively strips telegram tags, playlist indexes, codec blocks, audio blocks, and rip tags."""
    base = os.path.splitext(raw_name)[0]

    # Strip telegram tags: '@Tamiltvtoonsofficial -'
    base = re.sub(r"^@[\w\.\-]+(?:\s*-\s*|\s+)", "", base, flags=re.I)

    # Strip playlist index numbers: '03.Iron Man 2', '15.Guardians...', '01 - '
    base = re.sub(r"^\d{1,3}\s*[\.\-]\s*", "", base)

    # Strip bracketed metadata e.g. [Org BD 5.1 Hindi + DDP 5.1 Atmos English], [YTS.MX], [TTT]
    base = re.sub(r"\[.*?\]", " ", base)

    # Strip trailing tags like MSubs, ESubs, ~ TombDoc, -TombDoc
    base = re.sub(r"(?:~|-)?\s*(?:MSubs|ESubs|Sub|TombDoc|FRDS|4kHDHub|YTS).*$", "", base, flags=re.I)

    # Strip video specs, codecs, and audio formats
    base = re.sub(r"\b(1080p|720p|480p|2160p|4k|bluray|web-?dl|webrip|hdtvrip|hdtv|x264|x265|hevc|10bit|open\s+matte|ivi|dsnp|imax|atmos|ddp5?\.?1?|hindi-english|dual\s+audio|aac5?\.?1?|ac3|dts|remux|repack|proper)\b.*", "", base, flags=re.I)

    # Strip isolated release years in parens: '(2010)' -> ''
    base = re.sub(r"[\(\[]\s*(?:19\d\d|20\d\d)\s*[\)\]]", " ", base)

    base = re.sub(r"[-_.]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip(" ~-._")
    return base

def extract_explicit_year(raw_filename):
    # Year in parens or brackets: (2010), [2012]
    match = re.search(r"[\(\[]\s*(19\d\d|20\d\d)\s*[\)\]]", raw_filename)
    if match:
        return int(match.group(1))
    # Year before standard video tags: '2014 1080p', '2012 IMAX'
    match_tag = re.search(r"\b(19\d\d|20\d\d)\b(?=\s*(?:1080p|720p|2160p|4k|bluray|web|imax|dsnp|hdtv))", raw_filename, re.I)
    if match_tag:
        return int(match_tag.group(1))
    return None

def extract_episode_meta(fname):
    """Accurately extracts episode information. Never misidentifies standalone movies."""
    f_lower = fname.lower()
    is_extra = any(tag in f_lower for tag in ["extra", "promo", "interview", "featurette", "bonus", "deleted"])

    # Explicit S01E02 or S01E02-E03
    multi_match = re.search(r"\b[sS](\d{1,2})[eE](\d{1,3})(?:[\-eE](\d{1,3}))?\b", fname)
    if multi_match:
        s = int(multi_match.group(1))
        e1 = int(multi_match.group(2))
        e2 = int(multi_match.group(3)) if multi_match.group(3) else e1
        return {"is_tv": True, "season": s, "episodes": list(range(e1, e2 + 1)), "is_special": False}

    # 1x09 pattern
    x_match = re.search(r"\b(\d{1,2})x(\d{1,3})\b", fname)
    if x_match:
        return {"is_tv": True, "season": int(x_match.group(1)), "episodes": [int(x_match.group(2))], "is_special": False}

    # S04 or Season 4 (Season Pack)
    sp_match = re.search(r"\b(?:[sS]|Season\s*)(\d{1,2})\b(?!\s*[eE]\d+)", fname, re.I)
    if sp_match and not is_extra:
        return {"is_tv": True, "season": int(sp_match.group(1)), "episodes": [1], "is_special": False}

    # E05 / Episode 5
    ep_num = re.search(r"\b(?:[eE]|Episode\s*)(\d{1,3})\b", fname, re.I)
    if ep_num and not is_extra:
        return {"is_tv": True, "season": 1, "episodes": [int(ep_num.group(1))], "is_special": False}

    if is_extra:
        return {"is_tv": True, "season": 0, "episodes": [1], "is_special": True}

    return {"is_tv": False, "season": 1, "episodes": [1], "is_special": False}

def is_folder_explicit_tv_show(folder_path):
    """Verifies if folder hierarchy actually belongs to a TV series."""
    for folder in reversed(folder_path):
        f_lower = folder.lower().strip()
        if f_lower in GENERIC_FOLDERS:
            continue
        # If folder contains explicit season or complete series tag
        if re.search(r"\b(season\s*\d+|series|complete\s*(?:series|collection|pack)|tv\s*shows?)\b", f_lower):
            clean = re.sub(r"[\(\[\{].*?[\)\]\}]", "", folder)
            clean = re.sub(r"\b(the\s+)?(complete|collection|cinemascope|anthology|pack|season\s*\d*|series)\b.*", "", clean, flags=re.I).strip(" ._-")
            if len(clean) >= 2:
                return clean
    return None

def search_tmdb(query, year=None, force_type=None):
    if not TMDB_API_KEY or not query or len(query) < 1:
        return None

    endpoint = "search/tv" if force_type == "tv" else ("search/movie" if force_type == "movie" else "search/multi")
    url = f"https://api.themoviedb.org/3/{endpoint}"

    params = {
        "api_key": TMDB_API_KEY,
        "query": query,
        "include_adult": "false"
    }
    if year:
        if force_type == "tv":
            params["first_air_date_year"] = str(year)
        else:
            params["year"] = str(year)

    try:
        res = HTTP_CLIENT.get(url, params=params, timeout=6).json()
        results = res.get("results", [])

        # Retry without year filter if no results
        if not results and year:
            params.pop("year", None)
            params.pop("first_air_date_year", None)
            res = HTTP_CLIENT.get(url, params=params, timeout=6).json()
            results = res.get("results", [])

        media_hits = [r for r in results if r.get("media_type") in ["movie", "tv"] or force_type]
        if not media_hits:
            return None

        # Prioritize popularity
        media_hits.sort(key=lambda x: x.get("popularity", 0), reverse=True)
        top = media_hits[0]

        m_type = "movie" if (top.get("media_type") == "movie" or force_type == "movie") else "series"
        tmdb_id = top.get("id")

        # Fetch authentic IMDb ID
        lookup_cat = "movie" if m_type == "movie" else "tv"
        ext_url = f"https://api.themoviedb.org/3/{lookup_cat}/{tmdb_id}/external_ids"
        ext_res = HTTP_CLIENT.get(ext_url, params={"api_key": TMDB_API_KEY}, timeout=5).json()
        imdb_id = ext_res.get("imdb_id")

        poster_path = top.get("poster_path")
        poster = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""

        return {
            "type": m_type,
            "imdb_id": imdb_id or f"tmdb:{tmdb_id}",
            "title": top.get("title") or top.get("name") or query,
            "poster": poster
        }
    except Exception:
        return None

def make_stream_entry(fid, item, m_type, imdb_id, title, poster, season=1, episodes=[1], edition="", quality="1080P"):
    fname = item.get("name", fid)
    link = item.get("_resolved_link") or extract_direct_stream_link(item, fid)
    size = item.get("size", 0)
    size_mb = f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"

    if m_type == "series":
        primary_ep = episodes[0] if episodes else 1
        stream_ids = [f"{imdb_id}:{season}:{ep}" for ep in episodes]
        return {
            "file_id": fid,
            "type": "series",
            "imdb_id": imdb_id,
            "title": title,
            "name": fname,
            "season": season,
            "episode": primary_ep,
            "stream_id": stream_ids[0],
            "stream_ids": stream_ids,
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
# MAIN EXECUTION
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
            print(f"📦 Loaded {len(existing_catalog)} entries from local data.json")
        except Exception as e:
            print(f"⚠️ data.json read notice: {e}")

    session_mgr = BrowserSessionManager(ROOT_URL)
    all_live_files = crawl_tree(session_mgr, ROOT_FOLDER_ID)

    print(f"\n📊 Discovered {len(all_live_files)} live video files.")
    if not all_live_files:
        print("❌ 0 files retrieved. Preserving data.json.")
        sys.exit(1)

    final_catalog = {}
    missing_ids = []

    for fid, item in all_live_files.items():
        if fid in existing_catalog:
            cached = existing_catalog[fid]
            if cached.get("name") == item.get("name"):
                cached["link"] = item.get("_resolved_link")
                final_catalog[fid] = cached
                continue
        missing_ids.append(fid)

    print(f"📌 Cached matches: {len(final_catalog)} | Items to resolve: {len(missing_ids)}\n")

    tv_cache = {}

    for fid in missing_ids:
        item = all_live_files[fid]
        raw_name = item.get("name", "")
        folder_path = item.get("_folder_path", ["Root"])
        parent_folder = item.get("_parent_folder", "Root")

        # 1. Parse attributes
        parsed = PTN.parse(raw_name)
        explicit_year = extract_explicit_year(raw_name) or parsed.get("year")
        cleaned_title = clean_media_string(raw_name)
        ep_meta = extract_episode_meta(raw_name)
        quality = parsed.get("resolution") or parsed.get("quality") or "1080P"

        # 2. Check if this file is explicitly a TV Show Episode / Special
        explicit_parent_show = is_folder_explicit_tv_show(folder_path)
        is_tv_entry = ep_meta["is_tv"] or bool(explicit_parent_show)

        if is_tv_entry:
            show_name = explicit_parent_show or cleaned_title
            # Strip season indicators from query: 'Ed Edd n Eddy S04' -> 'Ed Edd n Eddy'
            show_name = re.sub(r"\b(?:[sS]|Season\s*)\d{1,2}.*", "", show_name, flags=re.I).strip()

            if show_name in tv_cache:
                match = tv_cache[show_name]
            else:
                match = search_tmdb(show_name, force_type="tv")
                if match:
                    tv_cache[show_name] = match

            if match and match.get("type") == "series":
                season = ep_meta["season"]
                episodes = ep_meta["episodes"]
                edition = "Special / Extra" if ep_meta["is_special"] else ""

                final_catalog[fid] = make_stream_entry(
                    fid, item, "series", match["imdb_id"], match["title"], match["poster"],
                    season=season, episodes=episodes, edition=edition, quality=str(quality)
                )
                print(f"📺 TV Synced: [{parent_folder}] {raw_name} ➔ {match['title']} S{season:02d}E{episodes[0]:02d} ({match['imdb_id']})")
                continue

        # 3. Standalone Movie Pipeline (Iron Man 2, The Avengers, The Batman, etc.)
        # Handle dual language titles: 'Бойцовский клуб Fight Club'
        movie_queries = [cleaned_title]
        if " " in cleaned_title:
            parts = re.split(r"\s*[-/|]\s*", cleaned_title)
            for p in parts:
                if len(p.strip()) >= 2 and p.strip() not in movie_queries:
                    movie_queries.append(p.strip())

        # Add PTN title if available
        if parsed.get("title") and parsed["title"] not in movie_queries:
            movie_queries.append(parsed["title"])

        match = None
        for q in movie_queries:
            match = search_tmdb(q, year=explicit_year, force_type="movie")
            if match:
                break

        # Fallback without year constraint if strict search missed
        if not match and explicit_year:
            for q in movie_queries:
                match = search_tmdb(q, force_type="movie")
                if match:
                    break

        if match:
            final_catalog[fid] = make_stream_entry(
                fid, item, "movie", match["imdb_id"], match["title"], match["poster"], quality=str(quality)
            )
            print(f"🍿 Movie Synced: {raw_name} ➔ {match['title']} ({match['imdb_id']})")
        else:
            # Fallback
            final_catalog[fid] = make_stream_entry(
                fid, item, "movie", f"gf:{fid}", cleaned_title, "", quality=str(quality)
            )
            print(f"🛡️ Guard Fallback: {raw_name} ➔ '{cleaned_title}' (gf:{fid})")

    output_list = list(final_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output_list, f, indent=2)

    print(f"\n🎉 Catalog build complete! Total indexed: {len(output_list)} entries.")

if __name__ == "__main__":
    main()
