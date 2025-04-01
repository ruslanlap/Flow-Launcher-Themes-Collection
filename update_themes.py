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
    """Extract theme information from comments"""
    themes = []
    
    for comment in comments:
        author = comment.get("author", {}).get("login", "Unknown")
        body_text = comment.get("bodyText", "")
        body_html = comment.get("bodyHTML", "")
        comment_url = comment.get("url", "")
        created_at = comment.get("createdAt", "")
        
        # Look for theme name at the beginning of the post (either as heading or first line)
        lines = body_text.strip().split('\n')
        theme_name = None
        
        # Check for heading patterns
        heading_match = re.search(r'^#+\s*(.+?)$', lines[0].strip()) if lines else None
        if heading_match:
            theme_name = heading_match.group(1).strip()
        # If no heading, take the first non-empty line
        elif lines and lines[0].strip():
            theme_name = lines[0].strip()
            
        if not theme_name:
            continue
            
        # Check if post contains images (likely theme previews)
        has_image = "<img" in body_html
        
        # Find GitHub repo links
        repo_links = re.findall(r'href="(https://github\.com/[^"]+)"', body_html)
        download_link = repo_links[0] if repo_links else ""
        
        # Remove any links from theme name
        theme_name = re.sub(r'\[|\]|\(|\)|http.*', '', theme_name).strip()
        
        themes.append({
            "name": theme_name,
            "author": author,
            "download_link": download_link,
            "comment_url": comment_url,
            "created_at": created_at,
            "has_image": has_image
        })
    
    return themes

def update_readme(themes):
    """Update the README.md with theme information"""
    readme_path = "README.md"
    
    # Create a new README structure
    content = [
        "# Flow Launcher Themes Collection\n",
        "\n",
        "This README aggregates theme submissions shared in the [Flow Launcher Theme Gallery discussion](https://github.com/Flow-Launcher/Flow.Launcher/discussions/1438).\n",
        "\n",
        "## Available Themes\n",
        "\n"
    ]
    
    # Add theme entries
    for theme in themes:
        theme_entry = f"### {theme['name']}\n\n"
        
        if theme['download_link']:
            theme_entry += f"- **Download**: [{theme['name']} Theme]({theme['download_link']})\n"
        
        theme_entry += f"- **Author**: {theme['author']}\n"
        theme_entry += f"- **Discussion**: [View original post]({theme['comment_url']})\n"
        
        if theme['has_image']:
            theme_entry += f"- *Includes theme preview*\n"
        
        theme_entry += "\n"
        content.append(theme_entry)
    
    # Add footer
    content.append("\n---\n\n")
    content.append("*This README was automatically generated from the discussion posts on GitHub. For further details or updates, please refer to the original [Flow Launcher Theme Gallery discussion](https://github.com/Flow-Launcher/Flow.Launcher/discussions/1438).*\n")
    
    # Write to README
    with open(readme_path, "w", encoding="utf-8") as file:
        file.writelines(content)
    
    print(f"README.md updated with {len(themes)} themes.")
    return True

if __name__ == "__main__":
    comments = fetch_discussion_comments()
    themes = extract_theme_info(comments)
    # Sort themes by name
    themes.sort(key=lambda x: x['name'].lower())
    update_readme(themes)
