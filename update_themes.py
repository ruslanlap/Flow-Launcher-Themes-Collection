import os
import re
import requests
import unicodedata  # Для "нормалізації" рядків, якщо там є приховані символи

# Get GitHub token from environment variables
GITHUB_TOKEN = os.environ.get('PAT_TOKEN')
if not GITHUB_TOKEN:
    raise Exception("PAT_TOKEN not set. Add it as an environment variable.")

HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}"}


def fetch_discussion_comments():
    """Fetch comments from the Flow Launcher Theme Gallery discussion"""
    query = """
    {
      repository(owner: "Flow-Launcher", name: "Flow.Launcher") {
        discussion(number: 1438) {
          title
          comments(first: 100) {
            nodes {
              author {
                login
              }
              bodyText
              bodyHTML
              url
              createdAt
            }
          }
        }
      }
    }
    """
    response = requests.post("https://api.github.com/graphql", json={"query": query}, headers=HEADERS)
    if response.status_code == 200:
        data = response.json()
        return data["data"]["repository"]["discussion"]["comments"]["nodes"]
    else:
        raise Exception(f"Query failed with code {response.status_code}: {response.text}")


def extract_theme_info(comments):
    """Extract theme information from comments with unique .xaml files per theme."""
    themes = []
    seen_names = set()

    for comment in comments:
        author = comment.get("author", {}).get("login", "Unknown")
        body_text = comment.get("bodyText", "")
        body_html = comment.get("bodyHTML", "")

        # 1) Збираємо всі посилання на .xaml
        #    - HTML-посилання (href="...")
        #    - Markdown-посилання ((...))
        #    - Прямі лінки в тексті (https://...)
        xaml_links_html = re.findall(
            r'href="(https://(?:raw\.githubusercontent\.com|github\.com)/[^"]+\.xaml)"',
            body_html,
            flags=re.IGNORECASE
        )
        xaml_links_md = re.findall(
            r'(https://(?:raw\.githubusercontent\.com|github\.com)/[^)]+\.xaml)',
            body_html,
            flags=re.IGNORECASE
        )
        xaml_links_text = re.findall(
            r'(https?://[^\s]+\.xaml)',
            body_text,
            flags=re.IGNORECASE
        )

        # Усуваємо дублікати лінків (set())
        xaml_links = list(set(xaml_links_html + xaml_links_md + xaml_links_text))

        # 2) Шукаємо GitHub-посилання (репозиторії)
        repo_links = re.findall(r'href="(https://github\.com/[^"]+)"', body_html)
        # Відкидаємо посилання на issues, pulls, discussions, wiki
        repo_links = [
            link for link in repo_links
            if not re.search(r'/(issues|pulls|discussions|wiki)/?$', link)
        ]
        download_link = repo_links[0] if repo_links else ""

        # Якщо немає .xaml лінків і немає репо-посилань — пропускаємо
        if not (xaml_links or download_link):
            continue

        # 3) Визначаємо назву теми (перший рядок із >= 3 словами, без ".xaml")
        lines = body_text.strip().split('\n')
        theme_name = None
        if lines:
            for line in lines:
                clean_line = line.strip()
                if clean_line and ".xaml" not in clean_line and len(clean_line.split()) >= 3:
                    theme_name = clean_line
                    break
            if not theme_name:
                # Якщо не знайшли "адекватний" рядок, візьмемо перший
                theme_name = lines[0].strip()

        if not theme_name:
            continue

        # Видаляємо з назви теми всі зайві штуки, типу посилань і т.д.
        theme_name = re.sub(r'||||http.*', '', theme_name, flags=re.IGNORECASE).strip()

        # 4) Перевірка на унікальність назви теми (не дублюємо саму тему)
        if theme_name.lower() in seen_names:
            continue
        seen_names.add(theme_name.lower())

        # 5) Чи є прев’ю (картинка)
        has_image = "<img" in body_html

        # Якщо немає download_link, але є .xaml-посилання, візьмемо перше
        if not download_link and xaml_links:
            download_link = xaml_links[0]

        # 6) Унікальні імена .xaml файлів (без прихованих символів)
        xaml_files_set = set()
        for link in xaml_links:
            file_match = re.search(r'/([^/]+\.xaml)', link, flags=re.IGNORECASE)
            if file_match:
                # file_name — остання частина шляху
                file_name = file_match.group(1)
                # приберемо пробіли, нульової ширини символи та інше
                file_name = file_name.strip()
                file_name = unicodedata.normalize("NFKC", file_name)
                xaml_files_set.add(file_name)

        # Відсортуємо, щоб виглядало акуратніше
        xaml_files_list = sorted(xaml_files_set)
        # Якщо нема жодного .xaml, припустимо, що воно {theme_name}.xaml
        if xaml_files_list:
            xaml_files_text = " ".join(xaml_files_list)
        else:
            xaml_files_text = f"{theme_name}.xaml *(assumed)*"

        # 7) Додаємо зібрані дані в масив результатів
        themes.append({
            "name": theme_name,
            "xaml_files": xaml_files_text,
            "download_link": download_link,
            "author": author,
            "has_image": has_image
        })

    return themes


def update_readme_table(themes):
    """Update the README.md with theme information in a table including numbering"""
    readme_path = "README.md"

    content = [
        "# 🎨 Flow Launcher Themes Collection\n",
        "\n",
        "This README aggregates theme submissions shared in the [Flow Launcher Theme Gallery discussion](https://github.com/Flow-Launcher/Flow.Launcher/discussions/1438).\n",
        "\n",
        f"📦 **Total Themes:** {len(themes)}\n",
        "\n",
        "| 🔢 # | 🎨 Theme | 🗂 XAML File(s) | 📥 Download | ✍️ Author | 🖼️ Preview |\n",
        "|------|----------|------------------|--------------|------------|-----------|\n"
    ]

    for idx, theme in enumerate(themes, start=1):
        preview_status = "✅" if theme['has_image'] else ""
        safe_name = theme['name'].replace('|', '\\|')
        safe_xaml = theme['xaml_files'].replace('|', '\\|')

        if theme['download_link']:
            table_row = (
                f"| {idx} | **{safe_name}** | {safe_xaml} | "
                f"[Download]({theme['download_link']}) | {theme['author']} | {preview_status} |\n"
            )
        else:
            table_row = (
                f"| {idx} | **{safe_name}** | {safe_xaml} | "
                f"| {theme['author']} | {preview_status} |\n"
            )

        content.append(table_row)

    content.append("\n---\n\n")
    content.append(
        "*This README was automatically generated from the discussion posts on GitHub. "
        "For further details or updates, please refer to the original "
        "[Flow Launcher Theme Gallery discussion](https://github.com/Flow-Launcher/Flow.Launcher/discussions/1438).*\n"
    )

    with open(readme_path, "w", encoding="utf-8") as file:
        file.writelines(content)

    print(f"README.md updated with {len(themes)} themes in table format.")
    return True


if __name__ == "__main__":
    comments = fetch_discussion_comments()
    themes = extract_theme_info(comments)
    # Сортуємо теми за алфавітом
    themes.sort(key=lambda x: x['name'].lower())
    update_readme_table(themes)
