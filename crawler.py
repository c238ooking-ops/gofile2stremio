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
# SANITIZATION, EDITIONS & EPISODE DETECTORS
# ==========================================

def extract_versions_and_cuts(raw_name):
    cuts = []
    # Replace separators with spaces for reliable detection
    f_norm = re.sub(r"[-_.]+", " ", raw_name.lower())

    if "open matte" in f_norm or "openmatte" in f_norm:
        cuts.append("Open Matte")
    if "imax" in f_norm:
        cuts.append("IMAX")
    if "director's cut" in f_norm or "directors cut" in f_norm:
        cuts.append("Director's Cut")
    if "extended" in f_norm:
        cuts.append("Extended")
    if "theatrical" in f_norm:
        cuts.append("Theatrical")
    if "unrated" in f_norm:
        cuts.append("Unrated")
    if "remastered" in f_norm:
        cuts.append("Remastered")
    if "dual audio" in f_norm or "hindi-english" in f_norm or "multi" in f_norm:
        cuts.append("Dual Audio")
    if "criterion" in f_norm:
        cuts.append("Criterion")

    return " | ".join(cuts) if cuts else ""

def clean_media_string(raw_name):
    """Accurately isolates the core movie or show title without trailing noise or years."""
    base = os.path.splitext(raw_name)[0]

    # Convert all periods and underscores into spaces first
    base = re.sub(r"[-_.]+", " ", base)

    # Strip telegram tags: '@Tamiltvtoonsofficial -'
    base = re.sub(r"^@[\w\.\-]+(?:\s*-\s*|\s+)", "", base, flags=re.I)

    # Strip playlist index numbers: '06 The Avengers', '14 Doctor Strange'
    base = re.sub(r"^\d{1,3}\s*[\.\-]?\s*", "", base)

    # Strip bracketed metadata
    base = re.sub(r"\[.*?\]", " ", base)

    # Strip years in parens or brackets: '(2012)' -> ''
    base = re.sub(r"[\(\[]\s*(?:19\d\d|20\d\d)\s*[\)\]]", " ", base)

    # If filename has an isolated year like '2017' followed by tags, slice everything after it
    ym = re.search(r"\b(19\d\d|20\d\d)\b", base)
    if ym and not any(k in base.lower() for k in ["blade runner 2049", "2012", "1984"]):
        base = base[:ym.start()].strip(" -_.")

    # Strip Open Matte, IMAX, Web-DL, HMax, Codecs, and Audio tags
    base = re.sub(r"\b(open\s*matte|openmatte|imax|web-?dl|webrip|hmax|hdtvrip|hdtv|bluray|dsnp|ds4k|1080p|720p|480p|2160p|4k|[hx]\.?26[45]|hevc|10bit|ivi|atmos|ddp5?\.?1?|hindi-english|dual\s+audio|aac5?\.?1?|ac3|dts|remux|repack|proper|org\s+bd|org\s+ddp|msubs|esubs|tombdoc|frds|garshasp|yts)\b.*", "", base, flags=re.I)

    base = re.sub(r"\s+", " ", base)
    return base.strip(" ~-._")

def extract_explicit_year(raw_filename):
    match = re.search(r"[\(\[]\s*(19\d\d|20\d\d)\s*[\)\]]", raw_filename)
    if match:
        return int(match.group(1))
    match_tag = re.search(r"\b(19\d\d|20\d\d)\b(?=\s*(?:1080p|720p|2160p|4k|bluray|web|imax|dsnp|hdtv|480p))", raw_filename, re.I)
    if match_tag:
        return int(match_tag.group(1))
    return None

def extract_episode_meta_comprehensive(fname):
    """Accurately extracts episodic tags and specials with clean show anchors."""
    # Pre-clean channel names and playlist indices
    clean_f = re.sub(r"^@[\w\.\-]+(?:\s*-\s*|\s+)", "", fname, flags=re.I)
    clean_f = re.sub(r"^\d{1,3}\s*[\.\-]?\s*", "", clean_f)
    f_lower = clean_f.lower()
    is_extra = any(tag in f_lower for tag in ["extra", "promo", "interview", "featurette", "bonus", "deleted", "bloopers"])

    if is_extra:
        extra_anchor = re.split(r"[-_]\s*(?:extra|promo|interview|featurette|bonus|deleted|bloopers)\b", clean_f, flags=re.I)[0]
        extra_anchor = clean_media_string(extra_anchor)
        return {
            "is_tv": True, "season": 0, "episodes": [1], "is_special": True,
            "part_tag": "Special / Extra", "anchor": extra_anchor
        }

    # Match S01 E01-E02, S01E01-E02, S01 E01, S04 E11-E12
    se_match = re.search(r"\b[sS](\d{1,2})\s*[-_ ]?\s*[eE](\d{1,3})(?:\s*[-_eE]\s*(\d{1,3}))?([a-zA-Z])?\b", clean_f)
    if se_match:
        s = int(se_match.group(1))
        e1 = int(se_match.group(2))
        e2 = int(se_match.group(3)) if se_match.group(3) else e1
        part_char = se_match.group(4)
        part = f"Part {part_char.upper()}" if (part_char and part_char.lower() not in ['p', 'k']) else ""
        anchor = clean_media_string(clean_f[:se_match.start()])
        return {"is_tv": True, "season": s, "episodes": list(range(e1, e2 + 1)), "is_special": False, "part_tag": part, "anchor": anchor}

    # Match 1x09 or 1x09-10
    x_match = re.search(r"\b(\d{1,2})x(\d{1,3})(?:-(\d{1,3}))?([a-zA-Z])?\b", clean_f)
    if x_match:
        s = int(x_match.group(1))
        e1 = int(x_match.group(2))
        e2 = int(x_match.group(3)) if x_match.group(3) else e1
        part_char = x_match.group(4)
        part = f"Part {part_char.upper()}" if (part_char and part_char.lower() not in ['p', 'k']) else ""
        anchor = clean_media_string(clean_f[:x_match.start()])
        return {"is_tv": True, "season": s, "episodes": list(range(e1, e2 + 1)), "is_special": False, "part_tag": part, "anchor": anchor}

    # Match Season Pack: 'S04', 'Season 4', 'S03'
    sp_match = re.search(r"\b(?:[sS]|Season\s*)(\d{1,2})\b(?!\s*[eE]\d+)", clean_f, re.I)
    if sp_match:
        anchor = clean_media_string(clean_f[:sp_match.start()])
        return {"is_tv": True, "season": int(sp_match.group(1)), "episodes": [1], "is_special": False, "part_tag": "Season Pack", "anchor": anchor}

    return {"is_tv": False, "season": 1, "episodes": [1], "is_special": False, "part_tag": "", "anchor": ""}

def check_parent_franchise_override(folder_path):
    full_path_str = " ".join(folder_path).lower()
    for franchise_name, imdb_id in KNOWN_FRANCHISE_SHOWS.items():
        if franchise_name in full_path_str:
            return franchise_name.title(), imdb_id
    return None, None

# ==========================================
# STRICT TMDB RESOLUTION
# ==========================================

def search_tmdb_strict(query, year=None, force_type=None):
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

        if not results and year:
            params.pop("year", None)
            params.pop("first_air_date_year", None)
            res = HTTP_CLIENT.get(url, params=params, timeout=6).json()
            results = res.get("results", [])

        media_hits = [r for r in results if r.get("media_type") in ["movie", "tv"] or force_type]
        if not media_hits:
            return None

        # Filter exact or high-ratio match for titles
        q_clean = query.lower().strip()
        filtered_hits = []
        for r in media_hits:
            t = (r.get("title") or r.get("name") or "").lower()
            orig = (r.get("original_title") or r.get("original_name") or "").lower()
            if q_clean == t or q_clean == orig:
                filtered_hits.insert(0, r)
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

        # 2. Case: Theatrical Short Franchise Override (Tom and Jerry, etc.)
        franchise_title, franchise_imdb = check_parent_franchise_override(folder_path)
        if franchise_title and franchise_imdb:
            short_seq_counter.setdefault(franchise_imdb, 1)
            seq_num = short_seq_counter[franchise_imdb]
            short_seq_counter[franchise_imdb] += 1

            short_label = f"Short: {cleaned_title}"
            combined_tag = f"{version_cut_tag} | {short_label}".strip(" |")

            final_catalog[fid] = make_stream_entry(
                fid, item, "series", franchise_imdb, franchise_title,
                "https://image.tmdb.org/t/p/w500/bLkWl7J4bO043s3rY8U14Xw0ZfU.jpg",
                season=0, episodes=[seq_num], version_tag=combined_tag, quality=str(quality)
            )
            print(f"🐭 Franchise Short Anchored: [{franchise_title}] {raw_name} ➔ S00E{seq_num:03d} ({franchise_imdb})")
            continue

        # 3. Case: TV Show Episode / Special / Extra / Season Pack
        if ep_meta["is_tv"]:
            show_query = ep_meta.get("anchor")
            if not show_query:
                for folder in reversed(folder_path):
                    if folder.lower() not in GENERIC_FOLDERS and not folder.lower().startswith("season"):
                        show_query = clean_media_string(folder)
                        break

            if not show_query:
                show_query = cleaned_title

            show_query = re.sub(r"\b(?:[sS]|Season\s*)\d{1,2}.*", "", show_query, flags=re.I).strip()

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

        # 4. Case: Movies (The Avengers, Doctor Strange, Guardians of the Galaxy, etc.)
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
