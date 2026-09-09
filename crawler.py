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

KNOWN_YEAR_TITLES = {
    "2012", "1984", "1917", "2001", "2010", "1408", "300", "21", "22",
    "blade runner 2049", "cyberpunk 2077", "death race 2000", "godzilla 2000"
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
# SANITIZATION & TITLE DISCOVERY ENGINE
# ==========================================

def extract_explicit_year(raw_filename):
    """Isolates true release year while protecting titles like '2012 (2009)' or '1984'."""
    # Priority 1: Explicitly bracketed year e.g. (2011), [1999]
    bracketed = re.search(r"[\(\[]\s*(19\d\d|20\d\d)\s*[\)\]]", raw_filename)
    if bracketed:
        return int(bracketed.group(1))

    # Priority 2: Year before rip tags e.g. '... 2017 IMAX ...'
    tag_year = re.search(r"\b(19\d\d|20\d\d)\b(?=\s+(?:1080p|720p|2160p|4k|bluray|web|hdtv))", raw_filename, re.I)
    if tag_year:
        return int(tag_year.group(1))

    return None

def clean_media_string(raw_name):
    """Strips telegram prefixes, index numbers, audio details, and codecs without destroying numbers."""
    base = os.path.splitext(raw_name)[0]
    
    # Strip telegram tags like '@Tamiltvtoonsofficial -'
    base = re.sub(r"^@[\w\.\-]+(?:\s*-\s*|\s+)", "", base, flags=re.I)
    
    # Strip playlist index markers: '15.Guardians...', '01 - ...'
    base = re.sub(r"^\d{1,3}\s*[\.\-]\s*", "", base)

    # Strip release noise in brackets
    base = re.sub(r"\[(?:TTT|CN Dub|Tamil|Hindi|Eng|Dual Audio|HEVC|10bit|Open Matte|YTS|MNHD-FRDS-4kHDHub)[^\]]*\]", "", base, flags=re.I)
    base = re.sub(r"\[.*?\]", "", base)

    # Strip resolution, rip, and audio specs
    base = re.sub(r"\b(1080p|720p|480p|2160p|4k|bluray|web-?dl|webrip|hdtvrip|hdtv|x264|x265|hevc|10bit|open\s+matte|ivi|dsnp|imax|atmos|ddp5?\.?1?|hindi-english|dual\s+audio|aac5?\.?1?|ac3|dts|remux|repack|proper)\b", "", base, flags=re.I)
    
    # Clean formatting
    base = re.sub(r"[-_.]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    return base

def generate_search_candidates(cleaned_name, raw_filename):
    """Produces targeted search queries for dual-language, year-titled, and standard movies."""
    candidates = []

    # Strip explicit bracketed year from title candidates so search stays clean
    clean_no_bracket_year = re.sub(r"[\(\[]\s*(19\d\d|20\d\d)\s*[\)\]]", "", cleaned_name).strip()

    # Check for dual-language split e.g., 'Бойцовский клуб Fight Club'
    parts = re.split(r"\s*[-/|]\s*", clean_no_bracket_year)
    for part in parts:
        p = part.strip()
        if len(p) >= 2 and p.lower() not in GENERIC_FOLDERS:
            candidates.append(p)

    # Add whole cleaned string
    if clean_no_bracket_year and clean_no_bracket_year not in candidates:
        candidates.append(clean_no_bracket_year)

    # PTN parsed title candidate
    parsed = PTN.parse(raw_filename)
    ptn_title = parsed.get("title")
    if ptn_title and ptn_title not in candidates:
        candidates.append(ptn_title)

    return candidates, parsed

def find_parent_series_identity(folder_path):
    """Walks the full folder tree from deep to root to find the genuine series name."""
    for folder in reversed(folder_path):
        f_lower = folder.lower().strip()
        if f_lower in GENERIC_FOLDERS or f_lower.startswith("season") or f_lower.startswith("s0") or f_lower.startswith("s1"):
            continue
        # Strip collection descriptors: 'Tom and Jerry - The Complete CinemaScope Collection (1954–1958)'
        clean = re.sub(r"[\(\[\{].*?[\)\]\}]", "", folder)
        clean = re.sub(r"\b(the\s+)?(complete|collection|cinemascope|anthology|pack|season|series|movies|specials)\b.*", "", clean, flags=re.I)
        clean = clean.strip(" ._-")
        if len(clean) >= 2:
            return clean
    return None

def extract_episode_meta(fname):
    """Extracts season/episode slots, multi-episodes (101+102), and specials."""
    f_lower = fname.lower()
    is_extra = any(tag in f_lower for tag in ["extra", "promo", "interview", "featurette", "bonus", "deleted"])

    # Explicit S01E02 or S01E02-E03
    multi_match = re.search(r"\b[sS](\d{1,2})[eE](\d{1,3})(?:[\-eE](\d{1,3}))?\b", fname)
    if multi_match:
        s = int(multi_match.group(1))
        e1 = int(multi_match.group(2))
        e2 = int(multi_match.group(3)) if multi_match.group(3) else e1
        return {"type": "episode", "season": s, "episodes": list(range(e1, e2 + 1))}

    # 1x09 pattern
    x_match = re.search(r"\b(\d{1,2})x(\d{1,3})\b", fname)
    if x_match:
        return {"type": "episode", "season": int(x_match.group(1)), "episodes": [int(x_match.group(2))]}

    # Season Pack: S04, Season 4 (without trailing Exx)
    sp_match = re.search(r"\b[sS](\d{1,2})\b(?!\s*[eE]\d+)", fname)
    if sp_match and not is_extra:
        return {"type": "season_pack", "season": int(sp_match.group(1)), "episodes": [1]}

    # Plain episode number: E05, Ep 5
    ep_num = re.search(r"\b[eE](?:pisode)?\s*(\d{1,3})\b", fname)
    if ep_num:
        return {"type": "episode", "season": 1, "episodes": [int(ep_num.group(1))]}

    if is_extra:
        return {"type": "special", "season": 0, "episodes": [1]}

    return {"type": "unknown", "season": 1, "episodes": [1]}

# ==========================================
# TMDB RESOLUTION ENGINE
# ==========================================

def search_tmdb_flexible(candidates, release_year=None, force_type=None):
    """Executes query resolution against TMDB with year awareness and multi-language support."""
    if not TMDB_API_KEY:
        return None

    endpoint = "search/tv" if force_type == "tv" else ("search/movie" if force_type == "movie" else "search/multi")
    url = f"https://api.themoviedb.org/3/{endpoint}"

    for query in candidates:
        if not query or len(query) < 1:
            continue

        params = {
            "api_key": TMDB_API_KEY,
            "query": query,
            "include_adult": "false"
        }

        # Check if the query itself is a year title (e.g. '2012', '1984')
        is_title_number = query.strip().isdigit() and len(query.strip()) <= 4
        if release_year and not is_title_number:
            if force_type == "movie":
                params["year"] = str(release_year)
            elif force_type == "tv":
                params["first_air_date_year"] = str(release_year)

        try:
            res = HTTP_CLIENT.get(url, params=params, timeout=6).json()
            results = res.get("results", [])

            # Retry without release year if 0 hits
            if not results and release_year and not is_title_number:
                params.pop("year", None)
                params.pop("first_air_date_year", None)
                res = HTTP_CLIENT.get(url, params=params, timeout=6).json()
                results = res.get("results", [])

            media_hits = [r for r in results if r.get("media_type") in ["movie", "tv"] or force_type]
            if not media_hits:
                continue

            # Prioritize by popularity to beat low-budget lookalikes
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
            continue

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

    # In-memory series cache to eliminate redundant network calls
    show_identity_cache = {}

    for fid in missing_ids:
        item = all_live_files[fid]
        raw_name = item.get("name", "")
        folder_path = item.get("_folder_path", ["Root"])
        parent_folder = item.get("_parent_folder", "Root")

        # Step 1: Pre-process strings and extract release attributes
        explicit_year = extract_explicit_year(raw_name)
        cleaned_str = clean_media_string(raw_name)
        candidates, ptn_data = generate_search_candidates(cleaned_str, raw_name)
        ep_info = extract_episode_meta(raw_name)
        quality = ptn_data.get("resolution") or ptn_data.get("quality") or "1080P"

        # Step 2: Determine if this item is part of a parent show
        parent_show_name = find_parent_series_identity(folder_path)
        is_episodic = ep_info["type"] in ["episode", "season_pack", "special"]

        show_match = None

        # If a parent show was located in the folder ancestry OR file has SxxExx markers
        if parent_show_name or is_episodic:
            show_query = parent_show_name or candidates[0]
            # Strip season indicators from show query: 'Ed Edd n Eddy S04' -> 'Ed Edd n Eddy'
            show_query = re.sub(r"\b[sS]\d{1,2}.*", "", show_query).strip()

            if show_query in show_identity_cache:
                show_match = show_identity_cache[show_query]
            else:
                show_match = search_tmdb_flexible([show_query], force_type="tv")
                if show_match:
                    show_identity_cache[show_query] = show_match

        # Handled as Series / Episodes / Specials
        if show_match and show_match.get("type") == "series":
            series_id = show_match["imdb_id"]
            series_title = show_match["title"]
            poster = show_match["poster"]

            season = ep_info["season"]
            episodes = ep_info["episodes"]
            edition = "Special / Extra" if ep_info["type"] == "special" else ("Season Pack" if ep_info["type"] == "season_pack" else "")

            final_catalog[fid] = make_stream_entry(
                fid, item, "series", series_id, series_title, poster,
                season=season, episodes=episodes, edition=edition, quality=str(quality)
            )
            print(f"📺 TV Synced: [{parent_folder}] {raw_name} ➔ {series_title} S{season:02d}E{episodes[0]:02d} ({series_id})")
            continue

        # Step 3: Standalone Movies (2012, 1984, Fight Club, Guardians of the Galaxy Vol. 2)
        movie_match = search_tmdb_flexible(candidates, release_year=explicit_year, force_type="movie")
        
        # Fallback multi-search if strict movie lookup produced 0 results
        if not movie_match:
            movie_match = search_tmdb_flexible(candidates, release_year=explicit_year)

        if movie_match:
            m_type = movie_match.get("type")
            m_id = movie_match.get("imdb_id")
            m_title = movie_match.get("title")
            poster = movie_match.get("poster")

            final_catalog[fid] = make_stream_entry(
                fid, item, m_type, m_id, m_title, poster, quality=str(quality)
            )
            print(f"🍿 Movie Synced: {raw_name} ➔ {m_title} ({m_id})")
        else:
            # Clean Local Fallback: Prevents metadata corruption
            final_catalog[fid] = make_stream_entry(
                fid, item, "movie", f"gf:{fid}", cleaned_str, "", quality=str(quality)
            )
            print(f"🛡️ Guard Protected: {raw_name} ➔ '{cleaned_str}' (gf:{fid})")

    output_list = list(final_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output_list, f, indent=2)

    print(f"\n🎉 Catalog build complete! Total indexed: {len(output_list)} entries.")

if __name__ == "__main__":
    main()
