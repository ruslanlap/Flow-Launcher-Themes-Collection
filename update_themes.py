import os
import re
import json
import time
import logging
import hashlib
import requests
import unicodedata
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, asdict
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET


# Configuration
CONFIG = {
    "max_retries": 3,
    "request_timeout": 30,
    "cache_duration": 3600,  # 1 hour
    "rate_limit_delay": 1.0,  # seconds between requests
    "max_comments": 500,  # increased from 100
    "concurrent_workers": 5,
    "backup_readme": True,
    "validate_xaml": True,
    "generate_stats": True,
    "auto_categorize": True
}

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('theme_collector.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class ThemeInfo:
    """Enhanced theme information structure"""
    name: str
    xaml_files: List[str]
    download_link: str
    author: str
    has_image: bool
    created_at: str
    comment_url: str
    category: str = "Other"
    tags: List[str] = None
    version: str = "1.0"
    stars: int = 0
    last_updated: str = ""
    xaml_valid: bool = True
    preview_images: List[str] = None
    description: str = ""
    file_size: Optional[int] = None
    
    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if self.preview_images is None:
            self.preview_images = []


class ThemeCollector:
    def __init__(self):
        self.github_token = self._get_github_token()
        self.headers = {"Authorization": f"Bearer {self.github_token}"}
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.cache_file = "themes_cache.json"
        self.stats = {
            "total_themes": 0,
            "authors": {},
            "categories": {},
            "tags": {},
            "last_updated": datetime.now().isoformat()
        }

    def _get_github_token(self) -> str:
        """Get GitHub token with better error handling"""
        token = os.environ.get('PAT_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if not token:
            raise Exception(
                "GitHub token not found. Set PAT_TOKEN or GITHUB_TOKEN environment variable."
            )
        return token

    def _make_request(self, url: str, json_data: Dict = None, method: str = "GET") -> requests.Response:
        """Make HTTP request with retry logic and rate limiting"""
        for attempt in range(CONFIG["max_retries"]):
            try:
                time.sleep(CONFIG["rate_limit_delay"])
                
                if method == "POST":
                    response = self.session.post(
                        url, 
                        json=json_data, 
                        timeout=CONFIG["request_timeout"]
                    )
                else:
                    response = self.session.get(url, timeout=CONFIG["request_timeout"])
                
                if response.status_code == 200:
                    return response
                elif response.status_code == 403:  # Rate limit
                    wait_time = 60 * (attempt + 1)
                    logger.warning(f"Rate limited. Waiting {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logger.warning(f"Request failed with status {response.status_code}")
                    
            except requests.exceptions.RequestException as e:
                logger.error(f"Request attempt {attempt + 1} failed: {e}")
                if attempt == CONFIG["max_retries"] - 1:
                    raise
                time.sleep(2 ** attempt)  # Exponential backoff
        
        raise Exception(f"Failed to make request after {CONFIG['max_retries']} attempts")

    def fetch_discussion_comments(self) -> List[Dict]:
        """Fetch comments with pagination support"""
        all_comments = []
        has_next_page = True
        cursor = None
        
        logger.info("Fetching discussion comments...")
        
        while has_next_page and len(all_comments) < CONFIG["max_comments"]:
            query = self._build_graphql_query(cursor)
            
            try:
                response = self._make_request(
                    "https://api.github.com/graphql", 
                    {"query": query}, 
                    "POST"
                )
                data = response.json()
                
                if "errors" in data:
                    logger.error(f"GraphQL errors: {data['errors']}")
                    break
                
                comments_data = data["data"]["repository"]["discussion"]["comments"]
                comments = comments_data["nodes"]
                all_comments.extend(comments)
                
                page_info = comments_data["pageInfo"]
                has_next_page = page_info["hasNextPage"]
                cursor = page_info["endCursor"]
                
                logger.info(f"Fetched {len(comments)} comments (total: {len(all_comments)})")
                
            except Exception as e:
                logger.error(f"Error fetching comments: {e}")
                break
        
        logger.info(f"Total comments fetched: {len(all_comments)}")
        return all_comments

    def _build_graphql_query(self, cursor: Optional[str] = None) -> str:
        """Build GraphQL query with cursor support"""
        after_clause = f', after: "{cursor}"' if cursor else ""
        
        return f"""
        {{
          repository(owner: "Flow-Launcher", name: "Flow.Launcher") {{
            discussion(number: 1438) {{
              title
              comments(first: 50{after_clause}) {{
                pageInfo {{
                  hasNextPage
                  endCursor
                }}
                nodes {{
                  author {{
                    login
                  }}
                  bodyText
                  bodyHTML
                  url
                  createdAt
                  updatedAt
                }}
              }}
            }}
          }}
        }}
        """

    def extract_theme_info(self, comments: List[Dict]) -> List[ThemeInfo]:
        """Enhanced theme extraction with categorization and validation"""
        themes = []
        seen_names = set()
        
        logger.info("Extracting theme information...")
        
        # Process comments with concurrent validation
        with ThreadPoolExecutor(max_workers=CONFIG["concurrent_workers"]) as executor:
            future_to_comment = {
                executor.submit(self._process_comment, comment): comment 
                for comment in comments
            }
            
            for future in as_completed(future_to_comment):
                try:
                    theme = future.result()
                    if theme and theme.name.lower() not in seen_names:
                        themes.append(theme)
                        seen_names.add(theme.name.lower())
                        self._update_stats(theme)
                except Exception as e:
                    logger.error(f"Error processing comment: {e}")
        
        logger.info(f"Extracted {len(themes)} unique themes")
        return themes

    def _process_comment(self, comment: Dict) -> Optional[ThemeInfo]:
        """Process individual comment to extract theme info"""
        try:
            author = comment.get("author", {}).get("login", "Unknown")
            body_text = comment.get("bodyText", "")
            body_html = comment.get("bodyHTML", "")
            comment_url = comment.get("url", "")
            created_at = comment.get("createdAt", "")

            # Extract links and info
            xaml_links = self._extract_xaml_links(body_html, body_text)
            repo_links = self._extract_repo_links(body_html)
            preview_images = self._extract_images(body_html)
            
            if not xaml_links and not repo_links:
                return None

            # Extract theme name with better logic
            theme_name = self._extract_theme_name(body_text)
            if not theme_name:
                return None

            # Clean theme name
            theme_name = self._clean_theme_name(theme_name)
            
            # Get download link
            download_link = repo_links[0] if repo_links else (xaml_links[0] if xaml_links else "")
            
            # Extract version info
            version = self._extract_version(body_text)
            
            # Get XAML file names
            xaml_files = self._get_xaml_filenames(xaml_links)
            
            # Auto-categorize and tag
            category, tags = self._categorize_theme(theme_name, body_text, xaml_files)
            
            # Validate XAML if enabled
            xaml_valid = True
            if CONFIG["validate_xaml"] and xaml_links:
                xaml_valid = self._validate_xaml_links(xaml_links)
            
            # Extract description
            description = self._extract_description(body_text)

            return ThemeInfo(
                name=theme_name,
                xaml_files=xaml_files,
                download_link=download_link,
                author=author,
                has_image=len(preview_images) > 0,
                created_at=created_at,
                comment_url=comment_url,
                category=category,
                tags=tags,
                version=version,
                preview_images=preview_images,
                description=description,
                xaml_valid=xaml_valid
            )
            
        except Exception as e:
            logger.error(f"Error processing comment: {e}")
            return None

    def _extract_xaml_links(self, body_html: str, body_text: str) -> List[str]:
        """Extract all XAML file links"""
        xaml_links = []
        
        # HTML links
        xaml_links.extend(re.findall(
            r'href="(https://(?:raw\.githubusercontent\.com|github\.com)/[^"]+\.xaml)"',
            body_html, flags=re.IGNORECASE
        ))
        
        # Markdown links
        xaml_links.extend(re.findall(
            r'(https://(?:raw\.githubusercontent\.com|github\.com)/[^)]+\.xaml)',
            body_html, flags=re.IGNORECASE
        ))
        
        # Direct text links
        xaml_links.extend(re.findall(
            r'(https?://[^\s]+\.xaml)',
            body_text, flags=re.IGNORECASE
        ))
        
        return list(set(xaml_links))  # Remove duplicates

    def _extract_repo_links(self, body_html: str) -> List[str]:
        """Extract GitHub repository links"""
        repo_links = re.findall(r'href="(https://github\.com/[^"]+)"', body_html)
        # Filter out issues, pulls, discussions, wiki
        return [
            link for link in repo_links
            if not re.search(r'/(issues|pulls|discussions|wiki|blob)/?', link)
        ]

    def _extract_images(self, body_html: str) -> List[str]:
        """Extract preview images"""
        img_patterns = [
            r'<img[^>]+src="([^"]+)"',
            r'!\[[^\]]*\]\(([^)]+\.(png|jpg|jpeg|gif|webp))\)',
        ]
        
        images = []
        for pattern in img_patterns:
            images.extend(re.findall(pattern, body_html, flags=re.IGNORECASE))
        
        # Flatten and clean
        flat_images = []
        for img in images:
            if isinstance(img, tuple):
                flat_images.append(img[0])
            else:
                flat_images.append(img)
        
        return list(set(flat_images))

    def _extract_theme_name(self, body_text: str) -> Optional[str]:
        """Enhanced theme name extraction"""
        lines = body_text.strip().split('\n')
        
        # Try different strategies
        strategies = [
            self._extract_header_name,
            self._extract_bold_name,
            self._extract_first_meaningful_line
        ]
        
        for strategy in strategies:
            name = strategy(body_text, lines)
            if name:
                return name
        
        return None

    def _extract_header_name(self, body_text: str, lines: List[str]) -> Optional[str]:
        """Extract from markdown headers"""
        for line in lines:
            if re.match(r'^#+\s+(.+)', line):
                return re.match(r'^#+\s+(.+)', line).group(1).strip()
        return None

    def _extract_bold_name(self, body_text: str, lines: List[str]) -> Optional[str]:
        """Extract from bold text"""
        bold_pattern = r'\*\*([^*]+)\*\*'
        matches = re.findall(bold_pattern, body_text)
        for match in matches:
            if len(match.split()) >= 2 and ".xaml" not in match.lower():
                return match.strip()
        return None

    def _extract_first_meaningful_line(self, body_text: str, lines: List[str]) -> Optional[str]:
        """Extract first meaningful line"""
        for line in lines:
            clean_line = line.strip()
            if (clean_line and 
                ".xaml" not in clean_line.lower() and 
                len(clean_line.split()) >= 2 and
                not clean_line.startswith('http') and
                not clean_line.startswith('[')):
                return clean_line
        
        # Fallback to first non-empty line
        for line in lines:
            if line.strip():
                return line.strip()
        
        return None

    def _clean_theme_name(self, name: str) -> str:
        """Clean and normalize theme name"""
        # Remove URLs, pipes, and other artifacts
        name = re.sub(r'https?://\S+', '', name)
        name = re.sub(r'\|+', '', name)
        name = re.sub(r'[`*_~]', '', name)  # Remove markdown formatting
        name = re.sub(r'\s+', ' ', name).strip()
        
        # Normalize unicode
        name = unicodedata.normalize("NFKC", name)
        
        return name

    def _extract_version(self, body_text: str) -> str:
        """Extract version information"""
        version_patterns = [
            r'[vV](\d+\.\d+(?:\.\d+)?)',
            r'version\s+(\d+\.\d+(?:\.\d+)?)',
            r'(\d+\.\d+(?:\.\d+)?)\s+version'
        ]
        
        for pattern in version_patterns:
            match = re.search(pattern, body_text, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        
        return "1.0"  # Default version

    def _get_xaml_filenames(self, xaml_links: List[str]) -> List[str]:
        """Extract XAML filenames from links"""
        filenames = []
        for link in xaml_links:
            match = re.search(r'/([^/]+\.xaml)', link, flags=re.IGNORECASE)
            if match:
                filename = match.group(1)
                filename = unicodedata.normalize("NFKC", filename.strip())
                filenames.append(filename)
        
        return sorted(list(set(filenames)))

    def _categorize_theme(self, name: str, body_text: str, xaml_files: List[str]) -> tuple:
        """Auto-categorize theme and extract tags"""
        text_content = f"{name} {body_text}".lower()
        
        # Categories
        categories = {
            "Dark": ["dark", "night", "black", "shadow", "midnight"],
            "Light": ["light", "bright", "white", "clean", "minimal"],
            "Colorful": ["colorful", "rainbow", "vibrant", "neon", "color"],
            "Material": ["material", "md", "google"],
            "Windows": ["windows", "win10", "win11", "fluent", "acrylic"],
            "Gaming": ["gaming", "game", "rgb", "led"],
            "Professional": ["professional", "business", "office", "corporate"],
            "Retro": ["retro", "vintage", "classic", "old", "80s", "90s"]
        }
        
        # Find category
        category = "Other"
        for cat, keywords in categories.items():
            if any(keyword in text_content for keyword in keywords):
                category = cat
                break
        
        # Extract tags
        tags = []
        tag_keywords = {
            "animated": ["animated", "animation"],
            "transparent": ["transparent", "transparency", "alpha"],
            "rounded": ["rounded", "round", "curved"],
            "flat": ["flat", "minimalist"],
            "gradient": ["gradient", "fade"],
            "blur": ["blur", "blurred", "acrylic"]
        }
        
        for tag, keywords in tag_keywords.items():
            if any(keyword in text_content for keyword in keywords):
                tags.append(tag)
        
        return category, tags

    def _validate_xaml_links(self, xaml_links: List[str]) -> bool:
        """Validate XAML links accessibility"""
        for link in xaml_links[:3]:  # Check first 3 links to avoid too many requests
            try:
                response = self._make_request(link)
                if response.status_code != 200:
                    return False
                
                # Basic XAML validation
                content = response.text
                if not content.strip().startswith('<') or 'resourcedictionary' not in content.lower():
                    logger.warning(f"Invalid XAML content in {link}")
                    return False
                    
            except Exception as e:
                logger.warning(f"Could not validate XAML link {link}: {e}")
                return False
        
        return True

    def _extract_description(self, body_text: str) -> str:
        """Extract theme description"""
        lines = body_text.strip().split('\n')
        
        # Look for description after title
        description_lines = []
        skip_first = True
        
        for line in lines:
            clean_line = line.strip()
            if not clean_line:
                continue
                
            if skip_first:
                skip_first = False
                continue
                
            # Stop at links or code blocks
            if (clean_line.startswith('http') or 
                clean_line.startswith('```') or 
                '.xaml' in clean_line.lower()):
                break
                
            description_lines.append(clean_line)
            
            # Limit description length
            if len(' '.join(description_lines)) > 200:
                break
        
        return ' '.join(description_lines)[:200] + ('...' if len(' '.join(description_lines)) > 200 else '')

    def _update_stats(self, theme: ThemeInfo) -> None:
        """Update statistics"""
        # Authors
        if theme.author not in self.stats["authors"]:
            self.stats["authors"][theme.author] = 0
        self.stats["authors"][theme.author] += 1
        
        # Categories
        if theme.category not in self.stats["categories"]:
            self.stats["categories"][theme.category] = 0
        self.stats["categories"][theme.category] += 1
        
        # Tags
        for tag in theme.tags:
            if tag not in self.stats["tags"]:
                self.stats["tags"][tag] = 0
            self.stats["tags"][tag] += 1

    def generate_enhanced_readme(self, themes: List[ThemeInfo]) -> None:
        """Generate enhanced README with all features"""
        logger.info("Generating enhanced README...")
        
        # Backup existing README
        if CONFIG["backup_readme"] and os.path.exists("README.md"):
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"README_backup_{timestamp}.md"
            os.rename("README.md", backup_name)
            logger.info(f"Backed up README to {backup_name}")
        
        # Sort themes
        themes.sort(key=lambda x: (x.category, x.name.lower()))
        self.stats["total_themes"] = len(themes)
        
        content = self._build_readme_content(themes)
        
        # Write README
        with open("README.md", "w", encoding="utf-8") as file:
            file.write(content)
        
        # Save stats
        with open("theme_stats.json", "w", encoding="utf-8") as file:
            json.dump(self.stats, file, indent=2, ensure_ascii=False)
        
        logger.info(f"Enhanced README generated with {len(themes)} themes")

    def _build_readme_content(self, themes: List[ThemeInfo]) -> str:
        """Build the complete README content"""
        content = []
        
        # Header
        content.extend(self._build_header())
        
        # Statistics
        content.extend(self._build_statistics())
        
        # Navigation
        content.extend(self._build_navigation(themes))
        
        # Quick filters
        content.extend(self._build_quick_filters(themes))
        
        # Main table
        content.extend(self._build_main_table(themes))
        
        # Category sections
        content.extend(self._build_category_sections(themes))
        
        # Footer
        content.extend(self._build_footer())
        
        return ''.join(content)

    def _build_header(self) -> List[str]:
        """Build README header"""
        return [
            "# 🎨 Flow Launcher Themes Collection\n\n",
            "[![Themes Count](https://img.shields.io/badge/themes-{}-blue.svg)]() ".format(self.stats["total_themes"]),
            "[![Last Update](https://img.shields.io/badge/updated-{}-green.svg)]()\n\n".format(datetime.now().strftime("%Y-%m-%d")),
            "Welcome to the comprehensive collection of Flow Launcher themes! This repository automatically aggregates and organizes theme submissions from the [Flow Launcher Theme Gallery discussion](https://github.com/Flow-Launcher/Flow.Launcher/discussions/1438).\n\n",
            "## 🚀 Quick Start\n\n",
            "1. **Browse** themes by category or use the search filters\n",
            "2. **Download** your favorite theme\n",
            "3. **Install** by placing the `.xaml` file in your Flow Launcher themes directory\n",
            "4. **Activate** in Flow Launcher settings\n\n",
        ]

    def _build_statistics(self) -> List[str]:
        """Build statistics section"""
        content = [
            "## 📊 Statistics\n\n",
            f"- 📦 **Total Themes:** {self.stats['total_themes']}\n",
            f"- 👥 **Contributors:** {len(self.stats['authors'])}\n",
            f"- 🏷️ **Categories:** {len(self.stats['categories'])}\n",
            f"- 🔖 **Tags:** {len(self.stats['tags'])}\n",
            f"- 🕒 **Last Updated:** {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n\n",
        ]
        
        # Top contributors
        if self.stats["authors"]:
            top_authors = sorted(
                self.stats["authors"].items(), 
                key=lambda x: x[1], 
                reverse=True
            )[:5]
            
            content.append("### 🏆 Top Contributors\n\n")
            for i, (author, count) in enumerate(top_authors, 1):
                content.append(f"{i}. **{author}** - {count} themes\n")
            content.append("\n")
        
        return content

    def _build_navigation(self, themes: List[ThemeInfo]) -> List[str]:
        """Build navigation links"""
        categories = sorted(set(theme.category for theme in themes))
        
        content = [
            "## 🧭 Navigation\n\n",
            "- [📋 All Themes](#-all-themes)\n"
        ]
        
        for category in categories:
            anchor = category.lower().replace(" ", "-")
            content.append(f"- [🎨 {category}](#{anchor})\n")
        
        content.extend([
            "- [📊 Statistics](#-statistics)\n",
            "- [🔍 Search Tips](#-search-tips)\n\n"
        ])
        
        return content

    def _build_quick_filters(self, themes: List[ThemeInfo]) -> List[str]:
        """Build quick filter buttons"""
        categories = sorted(set(theme.category for theme in themes))
        
        content = [
            "## 🔍 Quick Filters\n\n",
            '<div align="center">\n\n'
        ]
        
        # Category filters
        for category in categories:
            count = len([t for t in themes if t.category == category])
            anchor = category.lower().replace(" ", "-")
            content.append(f'<a href="#{anchor}"><img src="https://img.shields.io/badge/{category}-{count}-blue?style=for-the-badge" alt="{category}"></a>\n')
        
        content.extend([
            '\n</div>\n\n',
            "### 🏷️ Popular Tags\n\n"
        ])
        
        # Tag clouds
        if self.stats["tags"]:
            popular_tags = sorted(
                self.stats["tags"].items(), 
                key=lambda x: x[1], 
                reverse=True
            )[:10]
            
            for tag, count in popular_tags:
                content.append(f'`{tag} ({count})` ')
            content.append('\n\n')
        
        return content

    def _build_main_table(self, themes: List[ThemeInfo]) -> List[str]:
        """Build main themes table"""
        content = [
            "## 📋 All Themes\n\n",
            "| # | 🎨 Theme | 📁 Category | 🗂️ Files | 📥 Download | ✍️ Author | 🖼️ Preview | ✅ Status | 🏷️ Tags |\n",
            "|---|----------|-------------|----------|-------------|-----------|----------|--------|------|\n"
        ]
        
        for idx, theme in enumerate(themes, 1):
            # Format fields
            name = self._escape_markdown(theme.name)
            category = theme.category
            files = self._format_xaml_files(theme.xaml_files)
            download = f"[⬇️]({theme.download_link})" if theme.download_link else "❌"
            author = theme.author
            preview = "🖼️" if theme.has_image else ""
            status = "✅" if theme.xaml_valid else "⚠️"
            tags = " ".join([f"`{tag}`" for tag in theme.tags[:3]])  # Limit to 3 tags
            
            row = f"| {idx} | **{name}** | {category} | {files} | {download} | {author} | {preview} | {status} | {tags} |\n"
            content.append(row)
        
        content.append("\n")
        return content

    def _build_category_sections(self, themes: List[ThemeInfo]) -> List[str]:
        """Build detailed category sections"""
        content = []
        categories = {}
        
        # Group by category
        for theme in themes:
            if theme.category not in categories:
                categories[theme.category] = []
            categories[theme.category].append(theme)
        
        # Build sections
        for category, category_themes in sorted(categories.items()):
            anchor = category.lower().replace(" ", "-")
            content.extend([
                f"## {category}\n\n",
                f"<details>\n",
                f"<summary>View {len(category_themes)} {category} themes</summary>\n\n"
            ])
            
            for theme in category_themes:
                content.extend(self._build_theme_card(theme))
            
            content.append("</details>\n\n")
        
        return content

    def _build_theme_card(self, theme: ThemeInfo) -> List[str]:
        """Build individual theme card"""
        content = [
            f"### {theme.name}\n\n",
            f"**Author:** {theme.author} | ",
            f"**Version:** {theme.version} | ",
            f"**Status:** {'✅ Valid' if theme.xaml_valid else '⚠️ Check Required'}\n\n"
        ]
        
        if theme.description:
            content.append(f"**Description:** {theme.description}\n\n")
        
        if theme.tags:
            tags_str = " ".join([f"`{tag}`" for tag in theme.tags])
            content.append(f"**Tags:** {tags_str}\n\n")
        
        if theme.xaml_files:
            content.append("**Files:**\n")
            for file in theme.xaml_files:
                content.append(f"- `{file}`\n")
            content.append("\n")
        
        if theme.download_link:
            content.append(f"[📥 Download]({theme.download_link}) | ")
        
        if theme.comment_url:
            content.append(f"[💬 Discussion]({theme.comment_url})")
        
        content.append("\n\n---\n\n")
        
        return content

    def _build_footer(self) -> List[str]:
        """Build README footer"""
        return [
            "## 🔍 Search Tips\n\n",
            "- Use **Ctrl+F** to search for specific themes\n",
            "- Filter by **category** using the quick links above\n",
            "- Look for **tags** to find themes with specific features\n",
            "- Check the **status** column for theme validation\n\n",
            "## 📖 Installation Guide\n\n",
            "1. Download the `.xaml` file from your chosen theme\n",
            "2. Navigate to your Flow Launcher installation directory\n",
            "3. Place the file in the `Themes` folder\n",
            "4. Restart Flow Launcher\n",
            "5. Go to Settings > Theme and select your new theme\n\n",
            "## 🤝 Contributing\n\n",
            "Want to share your theme? Join the discussion in the [Flow Launcher Theme Gallery](https://github.com/Flow-Launcher/Flow.Launcher/discussions/1438)\n\n",
            "## 🔧 Automation\n\n",
            f"This README is automatically generated using the Enhanced Theme Collector script.\n",
            f"- **Last automated update:** {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n",
            f"- **Themes processed:** {self.stats['total_themes']}\n",
            f"- **Comments analyzed:** From GitHub Discussion #1438\n\n",
            "---\n\n",
            "*Generated with ❤️ by the Enhanced Flow Launcher Theme Collector*\n"
        ]

    def _escape_markdown(self, text: str) -> str:
        """Escape markdown special characters"""
        chars_to_escape = ['|', '*', '_', '`', '#', '+', '-', '.', '!', '[', ']', '(', ')']
        for char in chars_to_escape:
            text = text.replace(char, f'\\{char}')
        return text

    def _format_xaml_files(self, files: List[str]) -> str:
        """Format XAML files for table display"""
        if not files:
            return "❓"
        
        if len(files) == 1:
            return f"`{files[0]}`"
        else:
            return f"`{files[0]}` +{len(files)-1} more"

    def save_cache(self, themes: List[ThemeInfo]) -> None:
        """Save themes to cache"""
        cache_data = {
            "timestamp": datetime.now().isoformat(),
            "themes": [asdict(theme) for theme in themes],
            "stats": self.stats
        }
        
        with open(self.cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Cache saved with {len(themes)} themes")

    def load_cache(self) -> Optional[List[ThemeInfo]]:
        """Load themes from cache if valid"""
        if not os.path.exists(self.cache_file):
            return None
        
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            # Check cache age
            cache_time = datetime.fromisoformat(cache_data["timestamp"])
            if datetime.now() - cache_time > timedelta(seconds=CONFIG["cache_duration"]):
                logger.info("Cache expired, will fetch fresh data")
                return None
            
            # Load themes
            themes = [ThemeInfo(**theme_data) for theme_data in cache_data["themes"]]
            self.stats = cache_data.get("stats", self.stats)
            
            logger.info(f"Loaded {len(themes)} themes from cache")
            return themes
            
        except Exception as e:
            logger.error(f"Error loading cache: {e}")
            return None

    def generate_ci_workflow(self) -> None:
        """Generate GitHub Actions workflow for automated updates"""
        workflow_content = """name: Update Theme Collection

on:
  schedule:
    - cron: '0 */6 * * *'  # Every 6 hours
  workflow_dispatch:  # Manual trigger
  push:
    paths:
      - 'theme_collector.py'

jobs:
  update-themes:
    runs-on: ubuntu-latest
    
    steps:
    - name: Checkout repository
      uses: actions/checkout@v4
      
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.9'
        
    - name: Install dependencies
      run: |
        pip install requests
        
    - name: Update theme collection
      env:
        PAT_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      run: python theme_collector.py
      
    - name: Commit changes
      run: |
        git config --local user.email "action@github.com"
        git config --local user.name "GitHub Action"
        git add README.md theme_stats.json themes_cache.json
        git diff --staged --quiet || git commit -m "🤖 Auto-update theme collection - $(date)"
        
    - name: Push changes
      uses: ad-m/github-push-action@master
      with:
        github_token: ${{ secrets.GITHUB_TOKEN }}
"""
        
        os.makedirs('.github/workflows', exist_ok=True)
        with open('.github/workflows/update-themes.yml', 'w') as f:
            f.write(workflow_content)
        
        logger.info("Generated CI/CD workflow")

    def generate_web_interface(self) -> None:
        """Generate interactive HTML interface"""
        html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Flow Launcher Themes</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
        }
        .container { 
            max-width: 1200px; 
            margin: 0 auto; 
            padding: 20px;
            background: rgba(255,255,255,0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            margin-top: 20px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
        }
        .header { 
            text-align: center; 
            margin-bottom: 30px;
            padding: 30px 0;
            background: linear-gradient(45deg, #FF6B6B, #4ECDC4);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .search-bar { 
            width: 100%; 
            padding: 15px 20px; 
            font-size: 16px; 
            border: 2px solid #ddd;
            border-radius: 50px;
            margin-bottom: 20px;
            transition: all 0.3s ease;
        }
        .search-bar:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 20px rgba(102, 126, 234, 0.3);
        }
        .filters { 
            display: flex; 
            flex-wrap: wrap; 
            gap: 10px; 
            margin-bottom: 20px; 
            justify-content: center;
        }
        .filter-btn { 
            padding: 8px 16px; 
            border: 2px solid #667eea;
            background: white;
            color: #667eea;
            border-radius: 25px;
            cursor: pointer;
            transition: all 0.3s ease;
            font-weight: 500;
        }
        .filter-btn:hover, .filter-btn.active { 
            background: #667eea;
            color: white;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.3);
        }
        .theme-grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); 
            gap: 20px; 
        }
        .theme-card { 
            background: white;
            border-radius: 15px;
            padding: 20px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            transition: all 0.3s ease;
            border: 1px solid #eee;
        }
        .theme-card:hover { 
            transform: translateY(-5px);
            box-shadow: 0 20px 40px rgba(0,0,0,0.15);
        }
        .theme-name { 
            font-size: 18px; 
            font-weight: 600; 
            margin-bottom: 8px;
            color: #333;
        }
        .theme-author { 
            color: #666; 
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 5px;
        }
        .theme-category {
            display: inline-block;
            background: linear-gradient(45deg, #FF6B6B, #4ECDC4);
            color: white;
            padding: 4px 12px;
            border-radius: 15px;
            font-size: 12px;
            font-weight: 500;
            margin-bottom: 10px;
        }
        .theme-tags { 
            margin-bottom: 15px; 
        }
        .tag { 
            background: #f0f0f0;
            color: #666;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 11px;
            margin-right: 5px;
            display: inline-block;
        }
        .download-btn { 
            background: linear-gradient(45deg, #667eea, #764ba2);
            color: white;
            padding: 10px 20px;
            border: none;
            border-radius: 25px;
            cursor: pointer;
            transition: all 0.3s ease;
            text-decoration: none;
            display: inline-block;
            font-weight: 500;
        }
        .download-btn:hover { 
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }
        .stats { 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); 
            gap: 15px; 
            margin-bottom: 30px; 
        }
        .stat-card { 
            background: white;
            padding: 20px;
            border-radius: 15px;
            text-align: center;
            box-shadow: 0 5px 20px rgba(0,0,0,0.08);
        }
        .stat-number { 
            font-size: 24px; 
            font-weight: 700;
            color: #667eea;
        }
        .hidden { display: none; }
        @media (max-width: 768px) {
            .container { margin: 10px; padding: 15px; }
            .theme-grid { grid-template-columns: 1fr; }
            .filters { justify-content: flex-start; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎨 Flow Launcher Themes Collection</h1>
            <p>Interactive Theme Browser</p>
        </div>
        
        <div class="stats" id="stats">
            <!-- Stats will be populated by JavaScript -->
        </div>
        
        <input type="text" class="search-bar" id="searchBar" placeholder="🔍 Search themes...">
        
        <div class="filters" id="filters">
            <button class="filter-btn active" data-category="all">All</button>
            <!-- Filters will be populated by JavaScript -->
        </div>
        
        <div class="theme-grid" id="themeGrid">
            <!-- Themes will be populated by JavaScript -->
        </div>
    </div>

    <script>
        // This would be populated with actual theme data from theme_stats.json
        let themes = [];
        let currentFilter = 'all';
        
        // Initialize the interface
        function init() {
            loadThemeData();
            renderStats();
            renderFilters();
            renderThemes();
            setupEventListeners();
        }
        
        function loadThemeData() {
            // In a real implementation, this would fetch from theme_stats.json
            // For now, using placeholder data
            themes = [
                {
                    name: "Dark Professional",
                    author: "developer1",
                    category: "Dark",
                    tags: ["professional", "minimal"],
                    download_link: "#",
                    has_image: true
                }
                // More themes would be loaded here
            ];
        }
        
        function renderStats() {
            const statsContainer = document.getElementById('stats');
            const totalThemes = themes.length;
            const categories = [...new Set(themes.map(t => t.category))].length;
            const authors = [...new Set(themes.map(t => t.author))].length;
            
            statsContainer.innerHTML = `
                <div class="stat-card">
                    <div class="stat-number">${totalThemes}</div>
                    <div>Total Themes</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number">${categories}</div>
                    <div>Categories</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number">${authors}</div>
                    <div>Contributors</div>
                </div>
            `;
        }
        
        function renderFilters() {
            const filtersContainer = document.getElementById('filters');
            const categories = [...new Set(themes.map(t => t.category))].sort();
            
            categories.forEach(category => {
                const btn = document.createElement('button');
                btn.className = 'filter-btn';
                btn.dataset.category = category.toLowerCase();
                btn.textContent = category;
                filtersContainer.appendChild(btn);
            });
        }
        
        function renderThemes() {
            const grid = document.getElementById('themeGrid');
            const filteredThemes = filterThemes();
            
            grid.innerHTML = filteredThemes.map(theme => `
                <div class="theme-card">
                    <div class="theme-category">${theme.category}</div>
                    <div class="theme-name">${theme.name}</div>
                    <div class="theme-author">👤 ${theme.author}</div>
                    <div class="theme-tags">
                        ${theme.tags.map(tag => `<span class="tag">${tag}</span>`).join('')}
                    </div>
                    <a href="${theme.download_link}" class="download-btn">📥 Download</a>
                </div>
            `).join('');
        }
        
        function filterThemes() {
            const searchTerm = document.getElementById('searchBar').value.toLowerCase();
            
            return themes.filter(theme => {
                const matchesSearch = !searchTerm || 
                    theme.name.toLowerCase().includes(searchTerm) ||
                    theme.author.toLowerCase().includes(searchTerm) ||
                    theme.category.toLowerCase().includes(searchTerm) ||
                    theme.tags.some(tag => tag.toLowerCase().includes(searchTerm));
                
                const matchesCategory = currentFilter === 'all' || 
                    theme.category.toLowerCase() === currentFilter;
                
                return matchesSearch && matchesCategory;
            });
        }
        
        function setupEventListeners() {
            // Search functionality
            document.getElementById('searchBar').addEventListener('input', renderThemes);
            
            // Filter functionality
            document.getElementById('filters').addEventListener('click', (e) => {
                if (e.target.classList.contains('filter-btn')) {
                    document.querySelectorAll('.filter-btn').forEach(btn => 
                        btn.classList.remove('active'));
                    e.target.classList.add('active');
                    currentFilter = e.target.dataset.category;
                    renderThemes();
                }
            });
        }
        
        // Initialize when page loads
        document.addEventListener('DOMContentLoaded', init);
    </script>
</body>
</html>"""
        
        with open('theme_browser.html', 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        logger.info("Generated interactive web interface")

    def run(self) -> None:
        """Main execution function"""
        logger.info("🚀 Starting Enhanced Theme Collector")
        
        try:
            # Try loading from cache first
            themes = self.load_cache()
            
            if themes is None:
                # Fetch fresh data
                logger.info("Fetching fresh data from GitHub...")
                comments = self.fetch_discussion_comments()
                themes = self.extract_theme_info(comments)
                
                # Save to cache
                self.save_cache(themes)
            else:
                logger.info("Using cached data")
            
            # Generate outputs
            self.generate_enhanced_readme(themes)
            
            # Generate additional files
            if CONFIG["generate_stats"]:
                self.generate_ci_workflow()
                self.generate_web_interface()
            
            logger.info("✅ Theme collection updated successfully!")
            logger.info(f"📊 Processed {len(themes)} themes")
            logger.info(f"👥 From {len(self.stats['authors'])} contributors")
            logger.info(f"🏷️ Across {len(self.stats['categories'])} categories")
            
        except Exception as e:
            logger.error(f"❌ Error during execution: {e}")
            raise


# Additional utility functions
def validate_xaml_content(content: str) -> tuple:
    """Validate XAML content and extract metadata"""
    try:
        # Basic XML validation
        root = ET.fromstring(content)
        
        # Check if it's a ResourceDictionary
        is_valid = root.tag.endswith('ResourceDictionary')
        
        # Extract colors and styles count
        colors = len(root.findall('.//*[@Color]'))
        styles = len(root.findall('.//Style'))
        
        metadata = {
            'colors': colors,
            'styles': styles,
            'valid': is_valid
        }
        
        return is_valid, metadata
        
    except ET.ParseError:
        return False, {'error': 'Invalid XML'}


def generate_theme_preview(xaml_file: str) -> str:
    """Generate theme preview (placeholder for future implementation)"""
    # This would integrate with Flow Launcher to generate actual previews
    return "preview_placeholder.png"


def check_theme_compatibility(xaml_content: str) -> Dict:
    """Check theme compatibility with Flow Launcher versions"""
    compatibility = {
        'v1.8+': True,  # Default assumption
        'issues': []
    }
    
    # Add compatibility checks here
    # This would analyze XAML for version-specific features
    
    return compatibility


if __name__ == "__main__":
    # Create and run collector
    collector = ThemeCollector()
    collector.run()
