#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import json
import hashlib
import unicodedata
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Tuple
import requests

# ---------------------------
# Config
# ---------------------------

OWNER = "Flow-Launcher"
REPO = "Flow.Launcher"
DISCUSSION_NUMBER = 1438
README_PATH = "README.md"

# Пріоритет токенів: GITHUB_TOKEN (завжди є в Actions) -> PAT_TOKEN (опціонально)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("PAT_TOKEN")
if not GITHUB_TOKEN:
    raise SystemExit("❌ No token found. Provide GITHUB_TOKEN (preferred) or PAT_TOKEN.")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

GRAPHQL_API = "https://api.github.com/graphql"

# ---------------------------
# Models
# ---------------------------

@dataclass
class Theme:
    name: str
    xaml_files: List[str]
    download_link: str
    author: str
    has_image: bool

# ---------------------------
# Helpers
# ---------------------------

def _normalize_text(s: str) -> str:
    """Trim, collapse spaces, normalize unicode width/compatibility."""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\u200b", "").strip()
    # Убрати зайві пробіли
    s = re.sub(r"\s+", " ", s)
    return s

def _to_raw_github_url(url: str) -> str:
    """
    Перетворює github.com/.../blob/<branch>/path/file.xaml -> raw.githubusercontent.com/.../<branch>/path/file.xaml
    Якщо вже raw або не підпадає під шаблон — повертає як є.
    """
    if "raw.githubusercontent.com" in url:
        return url
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*\.xaml)$", url)
    if m:
        owner, repo, branch, path = m.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    return url

def _extract_urls_from_text(text: str) -> List[str]:
    # Простенький екстрактор URL з plain text
    return re.findall(r"https?://[^\s)>\]}\"']+", text or "", flags=re.IGNORECASE)

def _extract_href_urls_from_html(html: str) -> List[str]:
    # Витягуємо href="..."; не HTML-парсер, але працює для простих кейсів
    return re.findall(r'href="([^"]+)"', html or "", flags=re.IGNORECASE)

def _dedupe_preserve_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

# ---------------------------
# GitHub GraphQL fetch (with pagination & retries)
# ---------------------------

def _graphql(query: str, variables: Optional[Dict] = None, max_retries: int = 5, backoff: float = 1.2) -> Dict:
    for attempt in range(1, max_retries + 1):
        resp = requests.post(GRAPHQL_API, headers=HEADERS, json={"query": query, "variables": variables or {}})
        # Rate-limit handling
        if resp.status_code == 200:
            data = resp.json()
            if "errors" in data and data["errors"]:
                # Якщо “secondary rate limit”, дамо бекофф
                err_str = json.dumps(data["errors"], ensure_ascii=False)
                if "rate limit" in err_str.lower():
                    time.sleep(backoff * attempt)
                    continue
                raise RuntimeError(f"GraphQL errors: {err_str}")
            return data
        elif resp.status_code in (502, 503, 504, 429):
            time.sleep(backoff * attempt)
            continue
        else:
            raise RuntimeError(f"GraphQL HTTP {resp.status_code}: {resp.text}")
    raise RuntimeError("Exceeded retries for GraphQL request.")

def fetch_all_discussion_comments(owner: str, repo: str, number: int) -> List[Dict]:
    """
    Отримує ВСІ коментарі з обговорення (з пагінацією).
    Повертає nodes (author/login, bodyText, bodyHTML, url, createdAt).
    """
    query = """
    query($owner:String!, $repo:String!, $number:Int!, $after:String) {
      repository(owner:$owner, name:$repo) {
        discussion(number: $number) {
          title
          comments(first: 100, after: $after) {
            nodes {
              author { login }
              bodyText
              bodyHTML
              url
              createdAt
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
      }
    }
    """
    comments: List[Dict] = []
    after = None
    while True:
        data = _graphql(query, {"owner": owner, "repo": repo, "number": number, "after": after})
        node = data["data"]["repository"]["discussion"]
        if not node:
            break
        batch = node["comments"]["nodes"] or []
        comments.extend(batch)
        pi = node["comments"]["pageInfo"]
        if pi["hasNextPage"]:
            after = pi["endCursor"]
        else:
            break
    return comments

# ---------------------------
# Extraction logic
# ---------------------------

def extract_theme_info(comments: List[Dict]) -> List[Theme]:
    """
    Витягує теми:
      - name: перший “людський” рядок (>=3 слова), без .xaml; fallback — перший рядок
      - xaml_files: унікальні .xaml із bodyHTML/bodyText; нормалізація + raw URLs
      - download_link: перше валідне посилання (repo або raw), preference: репозиторій
      - author: login або "Unknown"
      - has_image: за наявністю <img ...> у bodyHTML
    """
    themes: List[Theme] = []
    seen_names = set()

    for c in comments:
        author = (c.get("author") or {}).get("login") or "Unknown"
        body_text = c.get("bodyText") or ""
        body_html = c.get("bodyHTML") or ""

        # 1) Збираємо всі потенційні лінки
        urls = _dedupe_preserve_order(_extract_href_urls_from_html(body_html) + _extract_urls_from_text(body_text))

        # 2) Виділяємо .xaml
        xaml_links = []
        for u in urls:
            if u.lower().endswith(".xaml"):
                xaml_links.append(_to_raw_github_url(u))

        # 3) Репо-посилання (для “Download” краще показати репозиторій, якщо є)
        repo_links = []
        for u in urls:
            if re.match(r"^https://github\.com/[^/]+/[^/]+/?$", u):
                repo_links.append(u)

        # 4) Якщо нема ні XAML, ні репо — пропускаємо
        if not xaml_links and not repo_links:
            continue

        # 5) Визначаємо назву теми (>=3 слова без .xaml)
        lines = [ln.strip() for ln in (body_text or "").splitlines() if ln.strip()]
        theme_name = None
        for ln in lines:
            candidate = _normalize_text(ln)
            if ".xaml" in candidate.lower():
                continue
            if len(candidate.split()) >= 3:
                theme_name = candidate
                break
        if not theme_name and lines:
            theme_name = _normalize_text(lines[0])

        if not theme_name:
            # Справді нічого осмисленого
            continue

        # Прибрати будь-які raw URL з назви (інколи люди вставляють рядок з лінком)
        theme_name = re.sub(r"https?://\S+", "", theme_name).strip()
        theme_key = theme_name.lower()

        if theme_key in seen_names:
            # Уникнути дублів теми за ім’ям
            continue
        seen_names.add(theme_key)

        # 6) has_image
        has_image = "<img" in (body_html or "").lower()

        # 7) download_link: репо має пріоритет, інакше перший xaml
        download_link = repo_links[0] if repo_links else (xaml_links[0] if xaml_links else "")

        # 8) XAML file names (унікальні, нормалізовані)
        xaml_files_set = set()
        for link in xaml_links:
            m = re.search(r"/([^/]+\.xaml)$", link, flags=re.IGNORECASE)
            if m:
                file_name = _normalize_text(m.group(1))
                xaml_files_set.add(file_name)

        xaml_files = sorted(xaml_files_set)

        # Якщо зовсім немає .xaml — припустимо назву
        if not xaml_files:
            assumed = f"{theme_name}.xaml"
            xaml_files = [assumed]

        themes.append(Theme(
            name=theme_name,
            xaml_files=xaml_files,
            download_link=download_link,
            author=author,
            has_image=has_image,
        ))

    return themes

# ---------------------------
# README rendering (idempotent)
# ---------------------------

def render_readme(themes: List[Theme]) -> str:
    """Генерує повний README (легко замінити на інкрементальну секцію за маркерами)."""
    lines = []
    lines.append("# 🎨 Flow Launcher Themes Collection\n\n")
    lines.append(
        "This README aggregates theme submissions shared in the "
        "[Flow Launcher Theme Gallery discussion]"
        "(https://github.com/Flow-Launcher/Flow.Launcher/discussions/1438).\n\n"
    )
    lines.append(f"📦 **Total Themes:** {len(themes)}\n\n")
    lines.append("| 🔢 # | 🎨 Theme | 🗂 XAML File(s) | 📥 Download | ✍️ Author | 🖼️ Preview |\n")
    lines.append("|-----:|----------|------------------|-------------|-----------|-----------|\n")

    for idx, t in enumerate(themes, start=1):
        preview = "✅" if t.has_image else ""
        safe_name = t.name.replace("|", "\\|")
        xaml_join = ", ".join(t.xaml_files).replace("|", "\\|")
        download_cell = f"[Download]({t.download_link})" if t.download_link else ""
        author_cell = t.author

        lines.append(
            f"| {idx} | **{safe_name}** | {xaml_join} | {download_cell} | {author_cell} | {preview} |\n"
        )

    lines.append("\n---\n\n")
    lines.append(
        "*This README was automatically generated from the discussion posts on GitHub. "
        "For updates, please refer to the original "
        "[Flow Launcher Theme Gallery discussion]"
        "(https://github.com/Flow-Launcher/Flow.Launcher/discussions/1438).*\n"
    )
    return "".join(lines)

def write_if_changed(path: str, new_content: str) -> bool:
    """
    Пише файл тільки якщо змінився контент. Повертає True, якщо оновлено.
    Це зменшує шум у коммітах і пришвидшує ран.
    """
    old = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            old = f.read()
    if _sha256(old) == _sha256(new_content):
        print("ℹ️ README unchanged (no write).")
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)
    print("✅ README updated.")
    return True

# ---------------------------
# Main
# ---------------------------

def main() -> int:
    print(f"⬇️ Fetching comments for {OWNER}/{REPO} discussion #{DISCUSSION_NUMBER} ...")
    comments = fetch_all_discussion_comments(OWNER, REPO, DISCUSSION_NUMBER)
    print(f"📄 Received {len(comments)} comment(s). Parsing themes...")

    themes = extract_theme_info(comments)
    # Сортуємо за іменем (стабільно)
    themes.sort(key=lambda t: t.name.lower())

    # (опційно) Вивести короткий підсумок у лог
    print(f"🎯 Parsed {len(themes)} theme(s). Example:\n" +
          json.dumps(asdict(themes[0]) if themes else {}, ensure_ascii=False, indent=2))

    readme = render_readme(themes)
    updated = write_if_changed(README_PATH, readme)
    print("🏁 Done.")
    # Повернути 0 завжди; CI-логіка вирішує, комітити чи створювати PR
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
