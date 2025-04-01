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
    """Extract theme information from comments"""
    themes = []
    
    for comment in comments:
        author = comment.get("author", {}).get("login", "Unknown")
        body_text = comment.get("bodyText", "")
        body_html = comment.get("bodyHTML", "")
        comment_url = comment.get("url", "")
        
        # Look for theme name
        theme_name_match = re.search(r'(?:^|\n)#+\s*(.+?)(?:\n|$)', body_text)
        theme_name = theme_name_match.group(1).strip() if theme_name_match else None
        
        if not theme_name:
            continue
            
        # Look for XAML file links
        xaml_links = re.findall(r'href="(https://[^"]+\.xaml)"', body_html)
        xaml_links += re.findall(r'\((https://[^)]+\.xaml)\)', body_html)
        
        # Find XAML file names
        xaml_names = []
        for link in xaml_links:
            name_match = re.search(r'/([^/]+\.xaml)', link)
            if name_match:
                xaml_names.append(name_match.group(1))
            else:
                xaml_names.append(f"{theme_name}.xaml *(assumed)*")
        
        if not xaml_links:
            # Look for repository links
            repo_links = re.findall(r'href="(https://github\.com/[^"]+)"', body_html)
            repo_links = [link for link in repo_links if not link.endswith('.xaml')]
            
            if repo_links:
                xaml_links = [repo_links[0]]
                xaml_names = [f"{theme_name}.xaml *(assumed)*"]
        
        if xaml_links:
            themes.append({
                "name": theme_name,
                "xaml_files": " ".join(xaml_names),
                "download_link": xaml_links[0],
                "author": author
            })
    
    return themes

def update_readme_table(themes):
    """Update the README.md table with new themes"""
    readme_path = "README.md"
    
    try:
        with open(readme_path, "r", encoding="utf-8") as file:
            content = file.readlines()
    except FileNotFoundError:
        # Create a new README.md if it doesn't exist
        content = [
            "# Flow Launcher Themes Collection\n",
            "\n",
            "This README aggregates theme submissions (XAML files) shared in the [Flow Launcher Theme Gallery discussion](https://github.com/Flow-Launcher/Flow.Launcher/discussions/1438). Use the table below to quickly access each theme's XAML file.\n",
            "\n",
            "| Theme | XAML File(s) | Download Link | Author |\n",
            "|-------|--------------|---------------|--------|\n"
        ]
    
    # Find the beginning and end of the table
    table_start = -1
    table_end = -1
    
    for i, line in enumerate(content):
        if "| Theme " in line and "| XAML File" in line:
            table_start = i
        elif table_start > -1 and line.strip() == "":
            table_end = i
            break
    
    if table_end == -1:
        table_end = len(content)
    
    if table_start == -1:
        # Table not found, add it
        content.extend([
            "| Theme | XAML File(s) | Download Link | Author |\n",
            "|-------|--------------|---------------|--------|\n"
        ])
        table_start = len(content) - 2
        table_end = len(content)
    
    # Extract existing themes from the table
    existing_themes = {}
    for i in range(table_start + 2, table_end):
        if i < len(content) and "|" in content[i]:
            parts = content[i].split("|")
            if len(parts) >= 5:
                theme_name = parts[1].strip().strip("*").strip()
                existing_themes[theme_name.lower()] = content[i]
    
    # Add new themes
    new_rows = []
    for theme in themes:
        theme_name = theme["name"].strip()
        if theme_name.lower() not in existing_themes:
            new_row = f"| **{theme_name}** | {theme['xaml_files']} | [Download]({theme['download_link']}) | {theme['author']} |\n"
            new_rows.append(new_row)
            existing_themes[theme_name.lower()] = new_row
    
    # If there are new themes, update the README
    if new_rows:
        # Collect all table rows
        table_rows = list(existing_themes.values())
        
        # Update content
        updated_content = content[:table_start + 2] + table_rows + ([] if table_end == len(content) else content[table_end:])
        
        with open(readme_path, "w", encoding="utf-8") as file:
            file.writelines(updated_content)
        
        print(f"README.md updated with {len(new_rows)} new themes.")
        return True
    else:
        print("No new themes to add.")
        return False

if __name__ == "__main__":
    comments = fetch_discussion_comments()
    themes = extract_theme_info(comments)
    update_readme_table(themes)