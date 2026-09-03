from collections import deque
import json
import os
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
        print(f"Browser navigation notice: {e}")
      finally:
        browser.close()

    if not captured["headers"]:
      print("❌ Failed to intercept headers from browser session.")
      sys.exit(1)

    self.session.headers.clear()
    self.session.headers.update(captured["headers"])
    self.last_auth_time = time.time()
    print("✅ Intercepted active browser session headers.")

  def ensure_fresh(self):
    if time.time() - self.last_auth_time > 900:
      self.refresh_credentials()


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
        wait_time = (attempt + 1) * 8
        print(f"⏳ [{status}] Backing off for {wait_time}s...")
        time.sleep(wait_time)
        if status in ["error-auth", "error-token"]:
          session_mgr.refresh_credentials()
      else:
        return res
    except Exception as e:
      time.sleep(3)

  return None


def main():
  session_mgr = SessionManager(ROOT_URL)
  folders_queue = deque([(ROOT_FOLDER_ID, "Root")])
  visited_folders = set()
  all_files = {}

  print("🚀 Scanning Gofile folder structure...")

  while folders_queue:
    current_folder_id, current_folder_name = folders_queue.popleft()
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
        item_type = item.get("type", "")
        item_name = item.get("name", item_id)

        if item_type == "folder":
          folder_code = item.get("code") or item.get("id") or item_id
          if folder_code not in visited_folders:
            folders_queue.append((folder_code, item_name))
        else:
          dl_url = (
              item.get("link")
              or item.get("directDownload")
              or item.get("downloadPage")
          )
          if dl_url and item_id not in all_files:
            size = item.get("size", 0)
            size_mb = (
                f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"
            )
            all_files[item_id] = {
                "id": item_id,
                "name": item_name,
                "link": dl_url,
                "size": size_mb,
            }

      total_pages = data.get("totalChildrenPages", 1)
      if page_num >= total_pages:
        break
      page_num += 1

  file_list = list(all_files.values())
  print(f"\n✅ Total files cataloged: {len(file_list)}")

  with open("data.json", "w", encoding="utf-8") as f:
    json.dump(file_list, f, indent=2)


if __name__ == "__main__":
  main()
