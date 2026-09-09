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

KNOWN_FRANCHISE_SHOWS = {
    "tom and jerry": "tt0032138",
    "looney tunes": "tt0021064",
    "mickey mouse": "tt0020170",
    "popeye": "tt0023783",
    "ed edd n eddy": "tt0217935",
    "oggy and the cockroaches": "tt0212686"
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
# VERSION, CUT & EPISODE DETECTORS
# ==========================================

def extract_versions_and_cuts(raw_name):
    """Detects cuts, editions, and variants to display inside Stremio's stream picker."""
    cuts = []
    f_lower = raw_name.lower()

    if "open matte" in f_lower or "openmatte" in f_lower:
        cuts.append("Open Matte")
    if "imax" in f_lower:
        cuts.append("IMAX")
    if "director's cut" in f_lower or "directors cut" in f_lower:
        cuts.append("Director's Cut")
    if "extended" in f_lower:
        cuts.append("Extended")
    if "theatrical" in f_lower:
        cuts.append("Theatrical")
    if "unrated" in f_lower:
        cuts.append("Unrated")
    if "remastered" in f_lower:
        cuts.append("Remastered")
    if "dual audio" in f_lower or "hindi-english" in f_lower or "multi" in f_lower:
        cuts.append("Dual Audio")
    if "criterion" in f_lower:
        cuts.append("Criterion")

    return " | ".join(cuts) if cuts else ""

def extract_episode_meta_comprehensive(fname):
    """Parses SxxExx, 1x09, 401, 401a, 401b, multi-episodes, and specials."""
    f_lower = fname.lower()
    is_extra = any(tag in f_lower for tag in ["extra", "promo", "interview", "featurette", "bonus", "deleted"])

    # Rule 1: S04E01a / S04E01b or S04E01-E02
    se_ab_match = re.search(r"\b[sS](\d{1,2})[eE](\d{1,3})([a-zA-Z])?\b", fname)
    if se_ab_match:
        s = int(se_ab_match.group(1))
        e = int(se_ab_match.group(2))
        part = f"Part {se_ab_match.group(3).upper()}" if se_ab_match.group(3) else ""
        return {"is_tv": True, "season": s, "episodes": [e], "is_special": False, "part_tag": part}

    # Rule 2: 1x09 pattern
    x_match = re.search(r"\b(\d{1,2})x(\d{1,3})([a-zA-Z])?\b", fname)
    if x_match:
        part = f"Part {x_match.group(3).upper()}" if x_match.group(3) else ""
        return {"is_tv": True, "season": int(x_match.group(1)), "episodes": [int(x_match.group(2))], "is_special": False, "part_tag": part}

    # Rule 3: 3 or 4-digit shorthand: '401', '401a', '401b' (Season 4 Episode 1)
    # Must not be a standard release year (19xx / 20xx)
    shorthand_match = re.search(r"\b([1-9])(\d{2})([a-zA-Z])?\b", fname)
    if shorthand_match:
        full_num = int(shorthand_match.group(1) + shorthand_match.group(2))
        # Ignore if it looks like a year (e.g. 1984, 2012)
        if not (1900 <= full_num <= 2035):
            s = int(shorthand_match.group(1))
            e = int(shorthand_match.group(2))
            part = f"Part {shorthand_match.group(3).upper()}" if shorthand_match.group(3) else ""
            return {"is_tv": True, "season": s, "episodes": [e], "is_special": False, "part_tag": part}

    # Rule 4: Season Pack 'S04', 'Season 4'
    sp_match = re.search(r"\b(?:[sS]|Season\s*)(\d{1,2})\b(?!\s*[eE]\d+)", fname, re.I)
    if sp_match and not is_extra:
        return {"is_tv": True, "season": int(sp_match.group(1)), "episodes": [1], "is_special": False, "part_tag": "Season Pack"}

    # Rule 5: Specials / Extras
    if is_extra:
        return {"is_tv": True, "season": 0, "episodes": [1], "is_special": True, "part_tag": "Special / Extra"}

    return {"is_tv": False, "season": 1, "episodes": [1], "is_special": False, "part_tag": ""}

def check_parent_franchise_override(folder_path):
    """Checks if file is inside a known cartoon/theatrical short collection (Tom & Jerry, etc.)."""
    full_path_str = " ".join(folder_path).lower()
    for franchise_name, imdb_id in KNOWN_FRANCHISE_SHOWS.items():
        if franchise_name in full_path_str:
            return franchise_name.title(), imdb_id
    return None, None

def clean_media_string(raw_name):
    """Strips telegram prefixes, playlist indexes, codec blocks, audio blocks, and rip tags."""
    base = os.path.splitext(raw_name)[0]

    # Strip telegram tags: '@Tamiltvtoonsofficial -'
    base = re.sub(r"^@[\w\.\-]+(?:\s*-\s*|\s+)", "", base, flags=re.I)

    # Strip playlist index numbers: '03.Iron Man 2', '15.Guardians...', '01 - '
    base = re.sub(r"^\d{1,3}\s*[\.\-]\s*", "", base)

    # Strip bracketed metadata e.g. [Org BD 5.1 Hindi...], [YTS.MX], [TTT]
    base = re.sub(r"\[.*?\]", " ", base)

    # Strip trailing metadata and release tags
    base = re.sub(r"(?:~|-)?\s*(?:MSubs|ESubs|Sub|TombDoc|FRDS|4kHDHub|YTS|Garshasp).*$", "", base, flags=re.I)

    # Strip codecs, resolutions, audio profiles
    base = re.sub(r"\b(1080p|720p|480p|2160p|4k|bluray|web-?dl|webrip|hdtvrip|hdtv|x264|x265|hevc|10bit|open\s+matte|ivi|dsnp|imax|atmos|ddp5?\.?1?|hindi-english|dual\s+audio|aac5?\.?1?|ac3|dts|remux|repack|proper|ds4k)\b.*", "", base, flags=re.I)

    # Strip isolated release years in parens: '(2010)' -> ''
    base = re.sub(r"[\(\[]\s*(?:19\d\d|20\d\d)\s*[\)\]]", " ", base)

    base = re.sub(r"[-_.]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip(" ~-._")
    return base

def extract_explicit_year(raw_filename):
    match = re.search(r"[\(\[]\s*(19\d\d|20\d\d)\s*[\)\]]", raw_filename)
    if match:
        return int(match.group(1))
    match_tag = re.search(r"\b(19\d\d|20\d\d)\b(?=\s*(?:1080p|720p|2160p|4k|bluray|web|imax|dsnp|hdtv|480p))", raw_filename, re.I)
    if match_tag:
        return int(match_tag.group(1))
    return None

# ==========================================
# STRICT TMDB RESOLUTION
# ==========================================

def search_tmdb_strict(query, year=None, force_type=None):
    """Searches TMDB and validates the primary title to avoid false-positive jumps like Alpha -> John Wick."""
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

        # CRITICAL FIX FOR SHORT / SINGLE-WORD TITLES (e.g. 'Alpha'):
        # If query is a single word, require the title to actually match or contain that word
        q_clean = query.lower().strip()
        filtered_hits = []
        for r in media_hits:
            t = (r.get("title") or r.get("name") or "").lower()
            orig = (r.get("original_title") or r.get("original_name") or "").lower()
            if q_clean == t or q_clean == orig:
                filtered_hits.insert(0, r)  # Perfect exact match
            elif q_clean in t or q_clean in orig:
                filtered_hits.append(r)
            elif SequenceMatcher(None, q_clean, t).ratio() >= 0.70:
                filtered_hits.append(r)

        candidates = filtered_hits if filtered_hits else media_hits
        top = candidates[0]

        m_type = "movie" if (top.get("media_type") == "movie" or force_type == "movie") else "series"
        tmdb_id = top.get("id")

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

def make_stream_entry(fid, item, m_type, imdb_id, title, poster, season=1, episodes=[1], version_tag="", quality="1080P"):
    fname = item.get("name", fid)
    link = item.get("_resolved_link") or extract_direct_stream_link(item, fid)
    size = item.get("size", 0)
    size_mb = f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"

    # Assemble comprehensive stream title for the Stremio player drawer
    details = [quality]
    if version_tag:
        details.append(version_tag)
    details.append(size_mb)
    stream_description = " | ".join(details)

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
            "edition": version_tag,
            "quality": quality,
            "description": stream_description,
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
            "edition": version_tag,
            "quality": quality,
            "description": stream_description,
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
    movie_cache = {}
    short_seq_counter = {}

    for fid in missing_ids:
        item = all_live_files[fid]
        raw_name = item.get("name", "")
        folder_path = item.get("_folder_path", ["Root"])
        parent_folder = item.get("_parent_folder", "Root")

        # 1. Parse attributes, cuts, and versions
        parsed = PTN.parse(raw_name)
        explicit_year = extract_explicit_year(raw_name) or parsed.get("year")
        cleaned_title = clean_media_string(raw_name)
        version_cut_tag = extract_versions_and_cuts(raw_name)
        ep_meta = extract_episode_meta_comprehensive(raw_name)
        quality = parsed.get("resolution") or parsed.get("quality") or "1080P"

        if ep_meta.get("part_tag"):
            version_cut_tag = f"{version_cut_tag} | {ep_meta['part_tag']}".strip(" |")

        # 2. Case: Theatrical Short Franchise Override (Tom and Jerry, Looney Tunes, etc.)
        franchise_title, franchise_imdb = check_parent_franchise_override(folder_path)
        if franchise_title and franchise_imdb:
            short_seq_counter.setdefault(franchise_imdb, 1)
            seq_num = short_seq_counter[franchise_imdb]
            short_seq_counter[franchise_imdb] += 1

            short_label = f"Short: {cleaned_title}"
            combined_tag = f"{version_cut_tag} | {short_label}".strip(" |")

            # Route shorts to Season 0 (Specials) so they stack inside the show card
            final_catalog[fid] = make_stream_entry(
                fid, item, "series", franchise_imdb, franchise_title,
                "https://image.tmdb.org/t/p/w500/bLkWl7J4bO043s3rY8U14Xw0ZfU.jpg",
                season=0, episodes=[seq_num], version_tag=combined_tag, quality=str(quality)
            )
            print(f"🐭 Franchise Short Anchored: [{franchise_title}] {raw_name} ➔ S00E{seq_num:03d} ({franchise_imdb})")
            continue

        # 3. Case: Verified TV Show Episode / Special / Season Pack
        if ep_meta["is_tv"]:
            # Find parent show name from folder or cleaned title
            candidate_show = cleaned_title
            for folder in reversed(folder_path):
                if folder.lower() not in GENERIC_FOLDERS and not folder.lower().startswith("season"):
                    candidate_show = re.sub(r"[\(\[\{].*?[\)\]\}]", "", folder).strip()
                    break

            # Strip trailing season tags
            show_query = re.sub(r"\b(?:[sS]|Season\s*)\d{1,2}.*", "", candidate_show, flags=re.I).strip()

            if show_query in tv_cache:
                match = tv_cache[show_query]
            else:
                match = search_tmdb_strict(show_query, force_type="tv")
                if match:
                    tv_cache[show_query] = match

            if match and match.get("type") == "series":
                season = ep_meta["season"]
                episodes = ep_meta["episodes"]

                final_catalog[fid] = make_stream_entry(
                    fid, item, "series", match["imdb_id"], match["title"], match["poster"],
                    season=season, episodes=episodes, version_tag=version_cut_tag, quality=str(quality)
                )
                print(f"📺 TV Synced: [{parent_folder}] {raw_name} ➔ {match['title']} S{season:02d}E{episodes[0]:02d} ({match['imdb_id']})")
                continue

        # 4. Case: Movies (Historical, Classic, Modern, In-Theatres, Multi-Language)
        movie_queries = [cleaned_title]
        if " " in cleaned_title:
            parts = re.split(r"\s*[-/|]\s*", cleaned_title)
            for p in parts:
                if len(p.strip()) >= 2 and p.strip() not in movie_queries:
                    movie_queries.append(p.strip())

        if parsed.get("title") and parsed["title"] not in movie_queries:
            movie_queries.append(parsed["title"])

        match = None
        cache_key = f"{movie_queries[0]}_{explicit_year}"
        if cache_key in movie_cache:
            match = movie_cache[cache_key]
        else:
            for q in movie_queries:
                match = search_tmdb_strict(q, year=explicit_year, force_type="movie")
                if match:
                    break

            if not match and explicit_year:
                for q in movie_queries:
                    match = search_tmdb_strict(q, force_type="movie")
                    if match:
                        break

            if match:
                movie_cache[cache_key] = match

        if match:
            final_catalog[fid] = make_stream_entry(
                fid, item, "movie", match["imdb_id"], match["title"], match["poster"],
                version_tag=version_cut_tag, quality=str(quality)
            )
            print(f"🍿 Movie Synced: {raw_name} ➔ {match['title']} ({match['imdb_id']}) [{version_cut_tag or 'Standard'}]")
        else:
            # Fallback
            final_catalog[fid] = make_stream_entry(
                fid, item, "movie", f"gf:{fid}", cleaned_title, "",
                version_tag=version_cut_tag, quality=str(quality)
            )
            print(f"🛡️ Guard Fallback: {raw_name} ➔ '{cleaned_title}' (gf:{fid})")

    output_list = list(final_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output_list, f, indent=2)

    print(f"\n🎉 Catalog build complete! Total indexed: {len(output_list)} entries.")

if __name__ == "__main__":
    main()
