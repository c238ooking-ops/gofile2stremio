import os
import sys
import json
import time
import re
from collections import deque
import requests
from playwright.sync_api import sync_playwright

ROOT_FOLDER_ID = "OBVVp1LI"
ROOT_URL = f"https://gofile.io/d/{ROOT_FOLDER_ID}"

SEQUEL_TAGS = {"2", "3", "4", "5", "6", "ii", "iii", "iv", "v", "part", "chapter", "returns", "reloaded"}

SERIES_ACRONYMS = {
    "bcs": "Better Call Saul",
    "bb": "Breaking Bad",
    "got": "Game of Thrones",
    "hotd": "House of the Dragon",
    "himym": "How I Met Your Mother",
    "tbbt": "The Big Bang Theory",
    "atla": "Avatar: The Last Airbender"
}

class SessionManager:
    def __init__(self, root_url):
        self.root_url = root_url
        self.session = requests.Session()
        self.last_auth_time = 0
        self.refresh_credentials()

    def refresh_credentials(self):
        print("🌐 Launching Chromium to capture session credentials...")
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

# ================= STRING CLEANING & PARSING =================

def normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def extract_edition(raw_name):
    lower = raw_name.lower()
    tags = []
    if "open matte" in lower or "open.matte" in lower: tags.append("Open Matte")
    if "imax" in lower: tags.append("IMAX")
    if "extended" in lower: tags.append("Extended Cut")
    if "director" in lower and "cut" in lower: tags.append("Director's Cut")
    if "theatrical" in lower: tags.append("Theatrical")
    if "workprint" in lower: tags.append("Workprint")
    if "35mm" in lower: tags.append("35mm Scan")
    if "remux" in lower: tags.append("REMUX")
    if "unrated" in lower: tags.append("Unrated")
    return " • ".join(tags) if tags else ""

def extract_quality(raw_name):
    m = re.search(r"\b(2160p|4k|1440p|1080p|720p|480p)\b", raw_name, re.I)
    return m.group(1).upper() if m else "1080P"

def clean_title_string(s):
    s = re.sub(r"^(\d{1,3}[\.\-\s_]+|\[\d{1,3}\][\.\-\s_]*)", "", s)
    s = re.sub(r"[\[\(\{].*?[\]\)\}]", " ", s)
    s = re.sub(r"[\~|\-]\s*[\w\.\-]+$", "", s)
    s = re.sub(r"\b(msubs|subs|esub|dual audio|hindi|english|atmos|ddp5\.1|dd5\.1|5\.1|7\.1|truehd|dts\-hd|dts|aac|ac3)\b", "", s, flags=re.I)
    s = re.sub(r"\b(10bit|8bit|bluray|bdrip|brrip|webrip|web\-dl|hdrip|dvdrip|remux|x265|x264|hevc|h264|h265|avc)\b", "", s, flags=re.I)
    s = re.sub(r"\b(2160p|4k|1440p|1080p|720p|480p|uhd|ia)\b", "", s, flags=re.I)
    s = re.sub(r"\b(open matte|imax|extended|director\'?s cut|unrated|theatrical)\b", "", s, flags=re.I)
    s = re.sub(r"[\._\-~+:]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def expand_title(title):
    cleaned = clean_title_string(title)
    return SERIES_ACRONYMS.get(cleaned.lower(), cleaned)

KNOWN_SERIES_SPECIALS = {
    "jingle jingle jangle": {"title": "Ed, Edd n Eddy", "season": 0, "episode": 1, "edition": "Christmas Special"},
    "boo haw haw": {"title": "Ed, Edd n Eddy", "season": 0, "episode": 2, "edition": "Halloween Special"},
    "hanky panky hullabaloo": {"title": "Ed, Edd n Eddy", "season": 0, "episode": 3, "edition": "Valentine's Special"},
    "the big picture show": {"title": "Ed, Edd n Eddy", "season": 0, "episode": 4, "edition": "Movie Finale"}
}

def parse_filename(filename, parent_folder=None):
    clean = re.sub(r"\.[^/.]+$", "", filename)
    clean = re.sub(r"\b(ia)\b", "", clean, flags=re.I).strip(" ._-")
    clean_lower = clean.lower()

    # 1. Holiday Specials / Known Series Lookup
    for key, spec in KNOWN_SERIES_SPECIALS.items():
        if key in clean_lower:
            return {
                "type": "series",
                "title": spec["title"],
                "year": None,
                "season": spec["season"],
                "episode": spec["episode"],
                "part": spec["edition"]
            }

    # 2. Leading Season x Episode (e.g., "1x10. Marco.ia", "S01E10 - Marco")
    lead_match = re.match(r"^(\d{1,2})x(\d{1,2})([a-zA-Z])?[\s._\-]+(.*)", clean, re.I) or \
                 re.match(r"^[sS](\d{1,2})[eE](\d{1,2})([a-zA-Z])?[\s._\-]+(.*)", clean, re.I)
    if lead_match:
        season = int(lead_match.group(1))
        episode = int(lead_match.group(2))
        part_tag = f"Part {lead_match.group(3).upper()}" if lead_match.group(3) else ""
        ep_name_raw = lead_match.group(4) if len(lead_match.groups()) >= 4 else ""

        derived_title = ""
        if parent_folder and parent_folder.lower() not in ["root", "downloads", "series", "movies"]:
            clean_folder = re.sub(r"\b(season\s*\d+|s\d+)\b", "", parent_folder, flags=re.I).strip(" ._-")
            derived_title = expand_title(clean_folder)

        if not derived_title and ep_name_raw:
            derived_title = clean_title_string(ep_name_raw)

        if derived_title:
            return {
                "type": "series",
                "title": derived_title,
                "year": None,
                "season": season,
                "episode": episode,
                "part": part_tag
            }

    # 3. Dual/Combined Episodes (e.g., 520+521, 603+604)
    dual_match = re.search(r"(\d{1,2})?(\d{2})\s*\+\s*(?:\d{1,2})?(\d{2})", clean)
    if dual_match:
        m_start = re.search(r"\b([1-9]\d{2,3})\s*\+", clean)
        if m_start:
            full_first = m_start.group(1)
            ep = int(full_first[-2:])
            season = int(full_first[:-2])
            title_part = clean[:m_start.start()].strip(" -_")
            cleaned_title = expand_title(title_part)
            if cleaned_title:
                return {
                    "type": "series",
                    "title": cleaned_title,
                    "year": None,
                    "season": season,
                    "episode": ep,
                    "part": f"Ep {full_first}+{dual_match.group(3)}"
                }

    # 4. Standard Series Match (e.g., S01E10, 1x10 preceded by show title)
    std_match = re.search(r"(.*?)\s*[sS](\d{1,2})[eE](\d{1,2})([a-zA-Z])?\b", clean, re.I) or \
                re.search(r"(.*?)\s*(\d{1,2})x(\d{1,2})([a-zA-Z])?\b", clean, re.I)
    if std_match:
        raw_title = std_match.group(1).strip(" -_")
        title = expand_title(raw_title) if raw_title else (expand_title(parent_folder) if parent_folder else "")
        if title:
            part_tag = f"Part {std_match.group(4).upper()}" if std_match.group(4) else ""
            return {
                "type": "series",
                "title": title,
                "year": None,
                "season": int(std_match.group(2)),
                "episode": int(std_match.group(3)),
                "part": part_tag
            }

    # 5. Shorthand notation with sub-letters (e.g., 425a, 425b, 306a, 505b)
    COMMON_NON_EPISODES = {480, 720, 1080, 2160, 1440, 640, 448, 384, 320, 256, 224, 192, 128, 300, 101}
    m_short = re.search(r"(?:^|[\s._\-])([1-9]\d{2,3})([a-zA-Z])?(?:[\s._\-]|$)", clean)
    if m_short:
        val = int(m_short.group(1))
        sub_letter = m_short.group(2)
        if (not (1920 <= val <= 2035) or sub_letter) and (val not in COMMON_NON_EPISODES or sub_letter or "ed" in clean_lower):
            raw_str = m_short.group(1)
            ep = int(raw_str[-2:])
            season = int(raw_str[:-2])
            if 1 <= ep <= 50 and 1 <= season <= 40:
                title_part = clean[:m_short.start()].strip(" -_")
                cleaned_title = expand_title(title_part)
                if len(cleaned_title) >= 2:
                    part_tag = f"Part {sub_letter.upper()}" if sub_letter else ""
                    return {
                        "type": "series",
                        "title": cleaned_title,
                        "year": None,
                        "season": season,
                        "episode": ep,
                        "part": part_tag
                    }

    # 6. Standalone Movie Fallback
    year = None
    year_match = re.search(r"\b(19\d\d|20\d\d)\b", clean)
    if year_match:
        year = int(year_match.group(1))
        title_raw = clean[:year_match.start()].strip()
    else:
        title_raw = clean

    return {
        "type": "movie",
        "title": expand_title(title_raw),
        "year": year,
        "part": ""
    }

# ================= SEARCH & RESOLVER ENGINE =================

def score_candidate(cand_title, cand_year, target_title, target_year):
    try:
        c_year = int(cand_year) if cand_year else None
    except:
        c_year = None
    t_year = int(target_year) if target_year else None

    if t_year and c_year:
        if abs(c_year - t_year) > 1:
            return -1

    norm_cand = normalize(cand_title)
    norm_target = normalize(target_title)

    cand_words = set(re.findall(r"\w+", cand_title.lower()))
    target_words = set(re.findall(r"\w+", target_title.lower()))
    for tag in SEQUEL_TAGS:
        if tag in cand_words and tag not in target_words:
            return -1

    score = 0
    if norm_cand == norm_target:
        score += 100
    elif norm_cand.startswith(norm_target):
        score += 50
    elif norm_target in norm_cand:
        score += 20
    else:
        return -1

    if t_year and c_year:
        if c_year == t_year:
            score += 60
        elif abs(c_year - t_year) == 1:
            score += 30

    return score

def search_cinemeta(title, year, m_type):
    catalog_type = "series" if m_type == "series" else "movie"
    url = f"https://v3-cinemeta.strem.io/catalog/{catalog_type}/top/search={requests.utils.quote(title)}.json"
    try:
        res = requests.get(url, timeout=7).json()
        metas = res.get("metas", [])
        best_item, highest_score = None, 0
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
        best_item, highest_score = None, 0
        for item in items:
            iid = item.get("id", "")
            if not iid.startswith("tt"):
                continue
            q_type = item.get("q", "")

            # If IMDb recognizes this query as an individual TV episode
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

            if q_type not in ["feature", "TV series", "TV mini-series", "movie"]:
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

    # 1. Primary lookup via Cinemeta (series titles and movies)
    match = search_cinemeta(parsed["title"], parsed["year"], parsed["type"])
    if match:
        return {"id": match["id"], "name": match["name"], "poster": match.get("poster", "")}

    # 2. Lookup via IMDb Suggestion Index (detects episode titles and exact shows)
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
                "id": imdb_match.get("id") if imdb_match.get("id", "").startswith("tt") else f"gf:series",
                "name": parent_query,
                "poster": imdb_match.get("poster", "")
            }
        return imdb_match

    return None

# ================= RUNNER & DATABASE SYNC =================

def fetch_folder_page(session_mgr, folder_id, page_num=1, max_retries=4):
    api_url = f"https://api.gofile.io/contents/{folder_id}?page={page_num}&pageSize=100&sortField=createTime&sortDirection=-1"
    for attempt in range(max_retries):
        session_mgr.ensure_fresh()
        try:
            res = session_mgr.session.get(api_url, timeout=25).json()
            status = res.get("status")
            if status == "ok":
                return res
            elif status in ["error-rateLimit", "error-auth", "error-token"]:
                time.sleep((attempt + 1) * 6)
                if status in ["error-auth", "error-token"]:
                    session_mgr.refresh_credentials()
            else:
                return None
        except:
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

def main():
    existing_catalog = {}
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                for item in json.load(f):
                    fid = item.get("file_id")
                    if fid:
                        existing_catalog[fid] = item
            print(f"📦 Loaded {len(existing_catalog)} baseline entries from local data.json")
        except Exception as e:
            print(f"⚠️ Could not read data.json: {e}")

    session_mgr = SessionManager(ROOT_URL)
    folders_queue = deque([(ROOT_FOLDER_ID, "Root")])
    visited_folders = set()
    all_live_files = {}

    print("🚀 Crawling Gofile directory tree...")
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

            for item_id, item in children.items():
                if item.get("type") == "folder":
                    sub_code = item.get("code") or item.get("id") or item_id
                    if sub_code not in visited_folders and all(sub_code != f[0] for f in folders_queue):
                        folders_queue.append((sub_code, item.get("name", sub_code)))
                else:
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

        print(f"📂 Scanned [{current_folder_name}]: {folder_files} files")

    print(f"\n🔎 Total live files currently on Gofile: {len(all_live_files)}")

    if len(all_live_files) == 0:
        print("❌ Error: 0 files retrieved from Gofile. Preserving data.json and aborting.")
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
                print(f"🔄 Detected rename: '{entry.get('name')}' ➜ '{fresh_name}'. Queuing for re-index...")
                renamed_count += 1
                continue

            if fresh_link:
                entry["link"] = fresh_link
            pruned_catalog[fid] = entry
        else:
            pruned_count += 1
            print(f"🗑️ Pruned deleted file: {entry.get('name')}")

    missing_ids = [fid for fid in all_live_files if fid not in pruned_catalog]
    print(f"⚡ Preserved: {len(pruned_catalog)} | Pruned: {pruned_count} | Renamed/New to Index: {len(missing_ids)}\n")

    added_count = 0
    meta_cache = {}

    for fid in missing_ids:
        item = all_live_files[fid]
        fname = item.get("name", fid)
        link = item.get("_resolved_link") or extract_direct_stream_link(item, fid)
        size = item.get("size", 0)
        size_mb = f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"

        base_edition = extract_edition(fname)
        quality = extract_quality(fname)
        parsed = parse_filename(fname, parent_folder=item.get("_parent_folder"))

        edition_parts = []
        if parsed.get("part"):
            edition_parts.append(parsed["part"])
        if base_edition:
            edition_parts.append(base_edition)
        edition = " • ".join(edition_parts)

        cache_key = f"{parsed['type']}:{parsed['title']}:{parsed['year']}"
        if cache_key not in meta_cache:
            meta_cache[cache_key] = resolve_metadata(parsed)

        meta = meta_cache[cache_key]
        imdb_id = meta["id"] if meta else f"gf:{fid}"
        display_title = meta["name"] if meta else parsed["title"]
        poster = meta["poster"] if meta and meta.get("poster") else "https://gofile.io/dist/img/logo-small.png"

        if parsed["type"] == "series":
            pruned_catalog[fid] = {
                "file_id": fid,
                "type": "series",
                "imdb_id": imdb_id,
                "title": display_title,
                "name": fname,
                "season": parsed["season"],
                "episode": parsed["episode"],
                "stream_id": f"{imdb_id}:{parsed['season']}:{parsed['episode']}",
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
                "poster": poster,
                "edition": edition,
                "quality": quality,
                "size": size_mb,
                "link": link
            }

        added_count += 1
        edition_str = f" [{edition}]" if edition else ""
        print(f"➕ Matched: '{fname}' ➜ '{display_title}' ({imdb_id}){edition_str} (Direct Stream: {link})")

    final_list = list(pruned_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(final_list, f, indent=2)

    print(f"\n🎉 Finished! Total entries: {len(final_list)} (Added/Updated: {added_count}, Removed: {pruned_count})")

if __name__ == "__main__":
    main()
