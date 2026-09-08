import os
import sys
import json
import time
import re
from collections import deque
import requests
from playwright.sync_api import sync_playwright
from guessit import guessit

# Optional Gemini AI fallback for messy/complex filenames
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        ai_model = genai.GenerativeModel("gemini-1.5-flash")
    except Exception as e:
        print(f"⚠️ Failed to initialize Gemini API: {e}")
        ai_model = None
else:
    ai_model = None

# Track last AI call time to strictly cap requests below the 15 RPM free tier limit
LAST_AI_CALL_TIME = 0

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

def is_video_file(filename):
    if not filename or "." not in filename:
        return False
    ext = os.path.splitext(filename)[1].lower()
    return ext in VALID_VIDEO_EXTENSIONS

class SessionManager:
    def __init__(self, root_url):
        self.root_url = root_url
        self.session = requests.Session()
        self.last_auth_time = 0
        self.refresh_credentials()

    def refresh_credentials(self):
        print("⚡ Launching Chromium to capture session credentials...")
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
            print("❌ Failed to intercept headers from browser session.")
            sys.exit(1)

        self.session.headers.clear()
        self.session.headers.update(captured["headers"])
        self.last_auth_time = time.time()
        print("✅ Intercepted session headers.")

    def ensure_fresh(self):
        if time.time() - self.last_auth_time > 900:
            self.refresh_credentials()

def fetch_folder_page(session_mgr, folder_id, page_num=1, max_retries=4):
    api_url = f"https://api.gofile.io/contents/{folder_id}?page={page_num}&pageSize=100&sortField=createTime&sortDirection=-1"
    for attempt in range(max_retries):
        session_mgr.ensure_fresh()
        try:
            res = session_mgr.session.get(api_url, timeout=25).json()
            status = res.get("status")
            if status == "ok":
                return res
            print(f"⚠️ Gofile API non-ok status: {status} on folder {folder_id} (Attempt {attempt + 1})")
            if status in ["error-rateLimit", "error-auth", "error-token"]:
                time.sleep((attempt + 1) * 6)
                if status in ["error-auth", "error-token"]:
                    session_mgr.refresh_credentials()
            else:
                return res
        except Exception as e:
            print(f"⚠️ Network error connecting to Gofile API: {e}")
            time.sleep(3)
    return None

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

def normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def expand_title(title):
    if not title:
        return ""
    clean = re.sub(r"\s+", " ", title).strip()
    return SERIES_ACRONYMS.get(clean.lower(), clean)

def ai_parse_filename(filename, parent_folder=None):
    """Uses Gemini 1.5 Flash with strict free-tier rate-limiting (max 14 calls/min)."""
    global LAST_AI_CALL_TIME
    if not ai_model:
        return None

    # Enforce minimum 4.2-second pause between AI calls
    elapsed = time.time() - LAST_AI_CALL_TIME
    if elapsed < 4.2:
        time.sleep(4.2 - elapsed)

    prompt = f"""You are an expert media library classifier. Given a video filename and its parent folder path, determine its canonical media details.
Filename: "{filename}"
Parent Folder: "{parent_folder or 'Unknown'}"

Instructions:
1. Strip all Telegram channels, release groups, and noisy prefixes (e.g. '@Tamiltvtoonsofficial -', '[TTT]', 'x265 10bit', etc.).
2. Theatrical shorts (e.g., Tom and Jerry shorts like "Tops with Pops (1957)", "Down Beat Bear", "Tot Watchers") are standalone movies (type: "movie").
3. For full season packs (e.g. S01, S03 without an episode number), type is "series", season is the number, episodes is [1], and edition is "Season Pack".
4. For extras/promos/unreleased clips, type is "series", season is 0, episodes is [0], and edition is the specific extra info.
5. If it contains multi-episodes (e.g. E01-E02 or 520+521), put all episode numbers into the episodes array.

Return ONLY a valid JSON object with these keys:
{{
  "type": "movie" or "series",
  "title": "Canonical title string (e.g. Oggy and the Cockroaches, Tops with Pops, Ed, Edd n Eddy)",
  "year": integer or null,
  "season": integer or null,
  "episodes": [list of integers] or null,
  "edition": "any edition/cut/extra tags (e.g. Season Pack, Promo, Open Matte)" or null,
  "quality": "e.g. 1080P, 720P, 480P"
}}"""

    for attempt in range(3):
        try:
            LAST_AI_CALL_TIME = time.time()
            response = ai_model.generate_content(
                prompt,
                generation_config={"response_mime_type": "application/json"}
            )
            data = json.loads(response.text)
            if data.get("title"):
                return {
                    "type": data.get("type", "movie"),
                    "title": expand_title(data.get("title")),
                    "year": data.get("year"),
                    "season": data.get("season"),
                    "episodes": data.get("episodes") or ([data.get("season")] if data.get("type") == "series" else []),
                    "edition": data.get("edition") or "",
                    "quality": data.get("quality") or "1080P"
                }
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "quota" in err_str.lower():
                print(f"⏳ Free quota backoff for '{filename}' (retry in 10s)...")
                time.sleep(10)
            else:
                print(f"⚠️ Gemini AI parse error for '{filename}': {e}")
                break
    return None

def parse_filename(filename, parent_folder=None):
    clean_name = re.sub(r"^@[\w\.\-]+(?:\s*-\s*|\s+)", "", filename, flags=re.I)
    clean_name = re.sub(r"\[(?:TTT|CN Dub|Tamil|Hindi|Eng|Dual Audio|HEVC|10bit)[^\]]*\]", "", clean_name, flags=re.I)
    clean_name = re.sub(r"\b(ia)\b", "", clean_name, flags=re.I).strip(" ._-")

    # If it has messy handles or promos, send directly to Gemini AI first
    if ai_model and ("@" in filename or "- extra -" in filename.lower() or "promo" in filename.lower() or "unreleased" in filename.lower()):
        ai_res = ai_parse_filename(filename, parent_folder)
        if ai_res:
            return ai_res

    # GuessIt parsing
    g = guessit(clean_name)
    season_pack = re.search(r"\b[sS](\d{1,2})\b(?!\s*[eE]\d+)", clean_name)
    dual_match = re.search(r"(\d{1,2})?(\d{2})\s*\+\s*(?:\d{1,2})?(\d{2})", clean_name)

    raw_title = g.get("title")
    if not raw_title and season_pack:
        raw_title = clean_name[:season_pack.start()].strip(" -._")

    # If title is missing or suspiciously short, fall back to AI
    if (not raw_title or len(raw_title) <= 2) and ai_model:
        ai_res = ai_parse_filename(filename, parent_folder)
        if ai_res:
            return ai_res

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

def score_candidate(cand_title, cand_year, target_title, target_year):
    try:
        c_year = int(cand_year) if cand_year else None
        t_year = int(target_year) if target_year else None
    except:
        c_year, t_year = None, None

    if t_year and c_year and abs(c_year - t_year) > 1:
        return -1

    norm_cand = normalize(cand_title)
    norm_target = normalize(target_title)

    score = 0
    if norm_cand == norm_target:
        score = 100
    elif norm_cand.startswith(norm_target) or norm_target.startswith(norm_cand):
        score = 60
    elif norm_target in norm_cand or norm_cand in norm_target:
        score = 30
    else:
        return -1

    if t_year and c_year:
        if c_year == t_year:
            score += 50
        elif abs(c_year - t_year) <= 1:
            score += 25

    return score

def search_cinemeta(title, year, m_type):
    catalog_type = "series" if m_type == "series" else "movie"
    url = f"https://v3-cinemeta.strem.io/catalog/{catalog_type}/top/search={requests.utils.quote(title)}.json"
    try:
        res = requests.get(url, timeout=7).json()
        metas = res.get("metas", [])
        best_item, highest_score = None, -1
        for m in metas:
            cand_year = m.get("year") or m.get("releaseInfo")
            score = score_candidate(m.get("name", ""), cand_year, title, year)
            if score > highest_score:
                highest_score = score
                best_item = m
        return best_item
    except:
        return None

def search_imdb(title, year, parsed_type="movie"):
    norm_q = normalize(title)
    if not norm_q:
        return None
    url = f"https://v3.sg.media-imdb.com/suggestion/{norm_q[0]}/{requests.utils.quote(title)}.json"
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=7).json()
        items = res.get("d", [])
        best_item, highest_score = None, -1
        for item in items:
            iid = item.get("id", "")
            if not iid.startswith("tt"):
                continue

            q_type = item.get("q", "")
            if q_type == "TV episode" and parsed_type == "series":
                parent_title = item.get("series", {}).get("l") or item.get("series", {}).get("title")
                parent_id = item.get("series", {}).get("id")
                if not parent_title and "yr" in item:
                    parent_title = item.get("s", "").split(",")[0].strip()
                if parent_title:
                    return {
                        "id": parent_id or f"parent_search:{parent_title}",
                        "name": parent_title,
                        "poster": item.get("i", {}).get("imageUrl", ""),
                        "is_episode_hit": True
                    }

            if q_type not in ["feature", "TV series", "TV mini-series", "movie", "short"]:
                continue

            score = score_candidate(item.get("l", ""), item.get("y"), title, year)
            if score > highest_score:
                highest_score = score
                best_item = {
                    "id": iid,
                    "name": item.get("l"),
                    "year": item.get("y"),
                    "poster": item.get("i", {}).get("imageUrl", "")
                }
        return best_item
    except:
        return None

def resolve_metadata(parsed):
    if not parsed["title"]:
        return None

    match = search_cinemeta(parsed["title"], parsed["year"], parsed["type"])
    if match:
        return {"id": match["id"], "name": match["name"], "poster": match.get("poster", "")}

    imdb_match = search_imdb(parsed["title"], parsed["year"], parsed["type"])
    if imdb_match:
        if imdb_match.get("is_episode_hit"):
            parent_query = imdb_match["name"]
            show_match = search_cinemeta(parent_query, None, "series") or search_imdb(parent_query, None, "series")
            if show_match:
                return {
                    "id": show_match.get("id"),
                    "name": show_match.get("name") or parent_query,
                    "poster": show_match.get("poster", "") or imdb_match.get("poster", "")
                }
            return {
                "id": imdb_match.get("id") if imdb_match.get("id", "").startswith("tt") else "gf:series",
                "name": parent_query,
                "poster": imdb_match.get("poster", "")
            }
        return imdb_match

    if parsed["type"] == "movie" and parsed.get("year"):
        cin_movie = search_cinemeta(parsed["title"], parsed["year"], "movie")
        if cin_movie:
            return {"id": cin_movie["id"], "name": cin_movie["name"], "poster": cin_movie.get("poster", "")}

    return None

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

    session_mgr = SessionManager(ROOT_URL)
    folders_queue = deque([(ROOT_FOLDER_ID, "Root")])
    visited_folders = set()
    all_live_files = {}

    print(f"🔎 Crawling Gofile root: {ROOT_FOLDER_ID} ({ROOT_URL})...")
    while folders_queue:
        current_folder_id, current_folder_name = folders_queue.popleft()
        if current_folder_id in visited_folders:
            continue
        visited_folders.add(current_folder_id)

        page_num = 1
        folder_files = 0
        while True:
            res = fetch_folder_page(session_mgr, current_folder_id, page_num)
            if not res or res.get("status") != "ok":
                break
            data = res.get("data", {})
            children = data.get("children", {})
            if not children:
                break

            children_items = children.items() if isinstance(children, dict) else [(c.get("id") or c.get("file_id"), c) for c in children]

            for item_id, item in children_items:
                if not item:
                    continue
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

            if len(children) < 50:
                break
            page_num += 1
            time.sleep(0.5)

        print(f"📁 Scanned [{current_folder_name}]: {folder_files} video files")

    print(f"\n📊 Total live video files currently on Gofile: {len(all_live_files)}")
    if len(all_live_files) == 0:
        print("❌ Error: 0 video files retrieved from Gofile. Preserving data.json and aborting.")
        sys.exit(1)

    pruned_catalog = {}
    pruned_count = 0
    renamed_count = 0

    for fid, entry in existing_catalog.items():
        if fid in all_live_files:
            fresh_item = all_live_files[fid]
            fresh_name = fresh_item.get("name", fid)
            fresh_link = fresh_item.get("_resolved_link") or extract_direct_stream_link(fresh_item, fid)

            if entry.get("name") != fresh_name:
                print(f"🔄 Detected rename: '{entry.get('name')}' ➔ '{fresh_name}'. Queuing for re-index...")
                renamed_count += 1
                continue

            if fresh_link:
                entry["link"] = fresh_link
            pruned_catalog[fid] = entry
        else:
            pruned_count += 1
            print(f"🗑️ Pruned deleted/non-video file: {entry.get('name')}")

    missing_ids = [fid for fid in all_live_files if fid not in pruned_catalog]
    print(f"\n📌 Preserved: {len(pruned_catalog)} | Pruned: {pruned_count} | Renamed/New to Index: {len(missing_ids)}\n")

    added_count = 0
    meta_cache = {}

    for fid in missing_ids:
        item = all_live_files[fid]
        fname = item.get("name", fid)
        link = item.get("_resolved_link") or extract_direct_stream_link(item, fid)
        size = item.get("size", 0)
        size_mb = f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"

        parsed = parse_filename(fname, parent_folder=item.get("_parent_folder"))
        cache_key = f"{parsed['type']}:{parsed['title']}:{parsed['year']}"

        if cache_key not in meta_cache:
            meta_cache[cache_key] = resolve_metadata(parsed)

        meta = meta_cache[cache_key]
        imdb_id = meta["id"] if meta else f"gf:{fid}"
        display_title = meta["name"] if meta else parsed["title"]
        poster = meta["poster"] if meta and meta.get("poster") else "https://gofile.io/dist/img/logo-small.png"

        edition = parsed.get("edition", "")
        quality = parsed.get("quality", "1080P")

        if parsed["type"] == "series":
            season_num = parsed.get("season", 1)
            ep_list = parsed.get("episodes", [1])
            primary_ep = ep_list[0] if ep_list else 1
            stream_ids = [f"{imdb_id}:{season_num}:{ep}" for ep in ep_list]

            pruned_catalog[fid] = {
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
            pruned_catalog[fid] = {
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

        added_count += 1
        edition_str = f"[{edition}]" if edition else ""
        print(f"🎬 Matched: {fname} ➔ {display_title} ({imdb_id}) {edition_str} [Direct Stream: {link}]")

    final_list = list(pruned_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(final_list, f, indent=2)

    print(f"\n🎉 Finished! Total entries: {len(final_list)} (Added/Updated: {added_count}, Removed: {pruned_count})")

if __name__ == "__main__":
    main()
