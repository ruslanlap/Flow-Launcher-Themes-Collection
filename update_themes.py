import os
import requests

# Отримання GitHub токену з змінних середовища
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
if not GITHUB_TOKEN:
    raise Exception("GITHUB_TOKEN не встановлено. Додайте його як змінну середовища.")

HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}"}

def fetch_discussions():
    query = """
    {
      repository(owner: "Flow-Launcher", name: "Flow.Launcher") {
        discussions(first: 100) {
          nodes {
            title
            url
          }
        }
      }
    }
    """
    response = requests.post("https://api.github.com/graphql", json={"query": query}, headers=HEADERS)
    if response.status_code == 200:
        data = response.json()
        return data["data"]["repository"]["discussions"]["nodes"]
    else:
        raise Exception(f"Query failed with code {response.status_code}")

def update_readme(discussions):
    readme_path = "README.md"
    try:
        with open(readme_path, "r", encoding="utf-8") as file:
            content = file.read()
    except FileNotFoundError:
        content = ""

    new_section = "\n## Нові теми\n"
    for disc in discussions:
        entry = f"- [{disc['title']}]({disc['url']})\n"
        if entry not in content:
            new_section += entry

    if new_section.strip() and new_section not in content:
        updated_content = content + new_section
        with open(readme_path, "w", encoding="utf-8") as file:
            file.write(updated_content)
        print("README.md оновлено новими темами.")
    else:
        print("Немає нових тем для додавання.")

if __name__ == "__main__":
    discussions = fetch_discussions()
    update_readme(discussions)
