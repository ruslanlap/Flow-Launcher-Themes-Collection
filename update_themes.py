import os
import re
import requests

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
    """Extract theme information from comments using extended logic:
       Consider a comment as a theme if it contains either a .xaml link or a GitHub repository link.
    """
    themes = []
    
    for comment in comments:
        author = comment.get("author", {}).get("login", "Unknown")
        body_text = comment.get("bodyText", "")
        body_html = comment.get("bodyHTML", "")
        
        # Шукаємо посилання на .xaml з bodyHTML і bodyText
        xaml_links_html = re.findall(r'href="(https://(?:raw\.githubusercontent\.com|github\.com)/[^"]+\.xaml)"', body_html)
        xaml_links_md = re.findall(r'\((https://(?:raw\.githubusercontent\.com|github\.com)/[^)]+\.xaml)\)', body_html)
        xaml_links_text = re.findall(r'(https?://[^\s]+\.xaml)', body_text)
        xaml_links = list(set(xaml_links_html + xaml_links_md + xaml_links_text))
        
        # Знаходимо всі GitHub посилання з bodyHTML
        repo_links = re.findall(r'href="(https://github\.com/[^"]+)"', body_html)
        # Фільтруємо посилання, що ведуть на issues, pulls, discussions, wiki
        repo_links = [link for link in repo_links if not re.search(r'/(issues|pulls|discussions|wiki)/?$', link)]
        download_link = repo_links[0] if repo_links else ""
        
        # Якщо не знайдено ні посилань на .xaml, ні GitHub посилань – пропускаємо цей коментар
        if not (xaml_links or download_link):
            continue
        
        # Визначаємо назву теми: намагаємося знайти перший рядок із принаймні 3 словами, що не містить '.xaml'
        lines = body_text.strip().split('\n')
        theme_name = None
        if lines:
            for line in lines:
                clean_line = line.strip()
                if clean_line and ".xaml" not in clean_line and len(clean_line.split()) >= 3:
                    theme_name = clean_line
                    break
            if not theme_name:
                theme_name = lines[0].strip()
        
        if not theme_name:
            continue
        
        # Перевіряємо наявність зображень (як ознака preview)
        has_image = "<img" in body_html
        
        # Якщо посилання з репо відсутнє, спробуємо використати перше .xaml посилання як download_link
        if not download_link and xaml_links:
            download_link = xaml_links[0]
        
        # Очистимо назву від небажаних символів
        theme_name = re.sub(r'\[|\]|\(|\)|http.*', '', theme_name).strip()
        
        # Витягуємо імена файлів із знайдених .xaml посилань
        xaml_files = []
        for link in xaml_links:
            file_match = re.search(r'/([^/]+\.xaml)', link)
            if file_match:
                xaml_files.append(file_match.group(1))
        xaml_files_text = " ".join(xaml_files) if xaml_files else f"{theme_name}.xaml *(assumed)*"
        
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
            table_row = f"| {idx} | **{safe_name}** | {safe_xaml} | [Download]({theme['download_link']}) | {theme['author']} | {preview_status} |\n"
        else:
            table_row = f"| {idx} | **{safe_name}** | {safe_xaml} | | {theme['author']} | {preview_status} |\n"
        
        content.append(table_row)
    
    content.append("\n---\n\n")
    content.append("*This README was automatically generated from the discussion posts on GitHub. For further details or updates, please refer to the original [Flow Launcher Theme Gallery discussion](https://github.com/Flow-Launcher/Flow.Launcher/discussions/1438).*\n")
    
    with open(readme_path, "w", encoding="utf-8") as file:
        file.writelines(content)
    
    print(f"README.md updated with {len(themes)} themes in table format.")
    return True

if __name__ == "__main__":
    comments = fetch_discussion_comments()
    themes = extract_theme_info(comments)
    themes.sort(key=lambda x: x['name'].lower())
    update_readme_table(themes)
