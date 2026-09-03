from collections import deque
import json
import os
import re
import sys
import time
from playwright.sync_api import sync_playwright
import requests

ROOT_FOLDER_ID = "OBVVp1LI"
ROOT_URL = f"https://gofile.io/d/{ROOT_FOLDER_ID}"


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
          args=[
              "--no-sandbox",
              "--disable-setuid-sandbox",
              "--disable-dev-shm-usage",
          ],
      )
      context = browser.new_context(
          user_agent=(
              "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
              " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
          ),
          viewport={"width": 1280, "height": 720},
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
      print("❌ Failed to intercept headers from browser session.")
      sys.exit(1)

    self.session.headers.clear()
    self.session.headers.update(captured["headers"])
    self.last_auth_time = time.time()
    print("✅ Intercepted session headers.")

  def ensure_fresh(self):
    if time.time() - self.last_auth_time > 900:
      self.refresh_credentials()


def clean_title(raw_name):
  name = re.sub(r"\.(mkv|mp4|avi|mov)$", "", raw_name, flags=re.I)
  name = name.replace(".", " ").replace("_", " ").replace("-", " ")
  name = re.sub(
      r"\b(1080p|720p|2160p|4k|uhd|webrip|web-dl|bluray|x264|x265|hevc|aac)\b.*",
      "",
      name,
      flags=re.I,
  )
  return name.strip()


def resolve_imdb(query, is_series=False):
  """Queries Cinemeta catalog to match a clean name to an IMDb ID."""
  media_type = "series" if is_series else "movie"
  url = f"https://v3-cinemeta.strem.io/catalog/{media_type}/top/search={requests.utils.quote(query)}.json"
  try:
    res = requests.get(url, timeout=5).json()
    metas = res.get("metas", [])
    if metas:
      return metas[0].get("id"), metas[0].get("name"), metas[0].get("poster")
  except Exception:
    pass
  return None, None, None


def fetch_folder_with_backoff(session_mgr, folder_id, page_num=1, max_retries=4):
  api_url = f"https://api.gofile.io/contents/{folder_id}?page={page_num}&pageSize=100&sortField=createTime&sortDirection=-1"
  for attempt in range(max_retries):
    session_mgr.ensure_fresh()
    try:
      res = session_mgr.session.get(api_url, timeout=25).json()
      status = res.get("status")
      if status == "ok":
        return res
      elif status in ["error-rateLimit", "error-auth", "error-token"]:
        time.sleep((attempt + 1) * 8)
        if status in ["error-auth", "error-token"]:
          session_mgr.refresh_credentials()
      else:
        return res
    except Exception:
      time.sleep(3)
  return None


def main():
  session_mgr = SessionManager(ROOT_URL)
  folders_queue = deque([(ROOT_FOLDER_ID, "Root")])
  visited_folders = set()
  raw_files = []

  print("🚀 Scanning Gofile folders...")
  while folders_queue:
    current_folder_id, _ = folders_queue.popleft()
    if current_folder_id in visited_folders:
      continue
    visited_folders.add(current_folder_id)

    page_num = 1
    while True:
      res = fetch_folder_with_backoff(session_mgr, current_folder_id, page_num)
      if not res or res.get("status") != "ok":
        break

      data = res.get("data", {})
      children = data.get("children", {})
      if not children:
        break

      for item_id, item in children.items():
        if item.get("type") == "folder":
          code = item.get("code") or item.get("id") or item_id
          if code not in visited_folders:
            folders_queue.append((code, item.get("name", code)))
        else:
          link = (
              item.get("link")
              or item.get("directDownload")
              or item.get("downloadPage")
          )
          if link:
            raw_files.append({
                "id": item_id,
                "name": item.get("name", item_id),
                "link": link,
            })

      if page_num >= data.get("totalChildrenPages", 1):
        break
      page_num += 1

  print(f"📦 Processing metadata for {len(raw_files)} files...")
  structured_catalog = []
  imdb_cache = {}

  series_regex = re.compile(
      r"[sS](\d{1,2})[eE](\d{1,2})|(\d{1,2})x(\d{1,2})", re.I
  )

  for file in raw_files:
    fname = file["name"]
    match = series_regex.search(fname)

    if match:
      # It's an episode in a series
      s = int(match.group(1) or match.group(3))
      e = int(match.group(2) or match.group(4))
      series_raw = fname[: match.start()]
      title_query = clean_title(series_raw)

      if title_query not in imdb_cache:
        imdb_cache[title_query] = resolve_imdb(title_query, is_series=True)

      imdb_id, show_name, poster = imdb_cache[title_query]
      structured_catalog.append({
          "file_id": file["id"],
          "type": "series",
          "imdb_id": imdb_id or f"gf:{file['id']}",
          "title": show_name or title_query,
          "season": s,
          "episode": e,
          "stream_id": (
              f"{imdb_id}:{s}:{e}" if imdb_id else f"gf:{file['id']}:{s}:{e}"
          ),
          "poster": poster or "https://gofile.io/dist/img/logo-small.png",
          "link": file["link"],
      })
    else:
      # It's a standalone movie
      title_query = clean_title(fname)
      if title_query not in imdb_cache:
        imdb_cache[title_query] = resolve_imdb(title_query, is_series=False)

      imdb_id, movie_name, poster = imdb_cache[title_query]
      structured_catalog.append({
          "file_id": file["id"],
          "type": "movie",
          "imdb_id": imdb_id or f"gf:{file['id']}",
          "title": movie_name or title_query,
          "stream_id": imdb_id or f"gf:{file['id']}",
          "poster": poster or "https://gofile.io/dist/img/logo-small.png",
          "link": file["link"],
      })

  with open("data.json", "w", encoding="utf-8") as f:
    json.dump(structured_catalog, f, indent=2)

  print(f"✅ Generated data.json with {len(structured_catalog)} matched entries.")


if __name__ == "__main__":
  main()
