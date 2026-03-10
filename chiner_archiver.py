#!/usr/bin/env python3
"""
CHINER-ARCHIVER v1.0
Archive entire threads from Chinertown.com (SMF forum).

Downloads all post text into a single .txt file and saves all images
(inline [img] tags and forum attachments) into a dedicated folder.
"""

from __future__ import annotations

import getpass
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

# ─── Constants ────────────────────────────────────────────────────────────────

VERSION = "1.0"
BANNER = rf"""
  ╔═══════════════════════════════════════════╗
  ║       CHINER-ARCHIVER  v{VERSION}              ║
  ║  Archive Chinertown.com forum threads     ║
  ╚═══════════════════════════════════════════╝
"""

DEFAULT_BASE_URL = "https://www.chinertown.com/forum"
THREADS_DIR = "threads"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# SMF uses 'windowbg' for alternating post containers in 2.0,
# and 'windowbg' uniformly in 2.1+.
POST_CONTAINER_CLASSES = ["windowbg", "windowbg2"]


# ─── Helpers ──────────────────────────────────────────────────────────────────


def sanitize_filename(name: str) -> str:
    """Remove characters that are invalid in file/directory names."""
    # Replace sequences of invalid chars with underscore
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Collapse multiple underscores / strip leading/trailing
    sanitized = re.sub(r"_+", "_", sanitized).strip("_. ")
    # Truncate to reasonable length
    return sanitized[:200] if sanitized else "untitled_thread"


def print_status(msg: str) -> None:
    """Print a status message with a prefix arrow."""
    print(f"  → {msg}")


def print_error(msg: str) -> None:
    """Print an error message."""
    print(f"  ✗ ERROR: {msg}", file=sys.stderr)


def print_success(msg: str) -> None:
    """Print a success message."""
    print(f"  ✓ {msg}")


def derive_base_url(thread_url: str) -> str:
    """
    Derive the forum base URL from a thread URL.

    Handles both URL formats:
      - SEF:   https://chinertown.com/index.php/topic,5970.0.html
      - Query: https://www.chinertown.com/forum/index.php?topic=123.0

    Returns the URL up to and including the directory that contains
    index.php (with a trailing slash), so that ``base_url + "index.php"``
    reconstructs the script path.
    """
    parsed = urlparse(thread_url)
    path = parsed.path

    idx = path.find("index.php")
    if idx != -1:
        # Keep everything before "index.php"
        path = path[:idx]
    elif path.endswith("/"):
        pass
    else:
        path = path.rsplit("/", 1)[0] + "/"

    # Ensure trailing slash
    if not path.endswith("/"):
        path += "/"

    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


# ─── Session & Authentication ─────────────────────────────────────────────────


def create_session() -> requests.Session:
    """Create a requests session with appropriate headers."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
    )
    return session


def login(session: requests.Session, base_url: str) -> bool:
    """
    Authenticate to the SMF forum.

    1. Visit the login page to establish a session cookie.
    2. Prompt for credentials.
    3. POST to the login2 action.

    Returns True on success, False otherwise.
    """
    login_page_url = f"{base_url}index.php?action=login"
    login_post_url = f"{base_url}index.php?action=login2"

    # Step 1: Visit login page to get session cookies
    print_status("Visiting login page to establish session...")
    try:
        resp = session.get(login_page_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print_error(f"Failed to load login page: {exc}")
        return False

    # Step 2: Prompt for credentials
    print()
    username = input("  Username: ").strip()
    if not username:
        print_error("Username cannot be empty.")
        return False
    password = getpass.getpass("  Password: ")
    if not password:
        print_error("Password cannot be empty.")
        return False

    # Step 3: Extract hidden form fields (session token, etc.)
    soup = BeautifulSoup(resp.text, "html.parser")
    login_form = soup.find("form", attrs={"id": "frmLogin"})
    if not login_form:
        # Fallback: look for any form posting to login2
        login_form = soup.find("form", action=lambda a: a and "login2" in a)

    form_data = {
        "user": username,
        "passwrd": password,
        "cookielength": "-1",  # Stay logged in
    }

    # Capture any hidden fields (e.g., session hash, sc token)
    if login_form:
        for hidden in login_form.find_all("input", attrs={"type": "hidden"}):
            name = hidden.get("name")
            value = hidden.get("value", "")
            if name and name not in form_data:
                form_data[name] = value

    # Step 4: Submit login
    print_status("Logging in...")
    try:
        resp = session.post(
            login_post_url,
            data=form_data,
            timeout=30,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print_error(f"Login request failed: {exc}")
        return False

    # Verify login succeeded by checking for typical SMF logout link
    if "action=logout" in resp.text:
        print_success("Login successful!")
        return True

    # Check for error messages
    error_soup = BeautifulSoup(resp.text, "html.parser")
    error_div = error_soup.find("div", class_="error") or error_soup.find(
        "div", class_="errorbox"
    )
    if error_div:
        print_error(f"Login failed: {error_div.get_text(strip=True)}")
    else:
        print_error("Login may have failed — could not confirm. Continuing anyway...")

    return "action=logout" in resp.text


# ─── Page Fetching & Pagination ───────────────────────────────────────────────


def fetch_page(session: requests.Session, url: str) -> BeautifulSoup | None:
    """Fetch a page and return parsed soup, or None on error."""
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        print_error(f"Failed to fetch {url}: {exc}")
        return None


def get_thread_title(soup: BeautifulSoup) -> str:
    """Extract the thread title from the page."""
    # SMF 2.1: <span id="top_subject">Title</span>
    title_el = soup.find("span", id="top_subject")
    if title_el:
        return title_el.get_text(strip=True)

    # SMF 2.0: <h5> or similar inside linktree / subject area
    display_title = soup.find("h2", class_="display_title")
    if display_title:
        return display_title.get_text(strip=True)

    # Fallback: <title> tag
    title_tag = soup.find("title")
    if title_tag:
        text = title_tag.get_text(strip=True)
        # Strip " - Forum Name" suffix
        if " - " in text:
            text = text.rsplit(" - ", 1)[0]
        return text

    return "untitled_thread"


def _parse_topic_start(url: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Extract (topic_id, start) from either URL format:
      - SEF:   index.php/topic,5970.30.html  -> ("5970", 30)
      - Query: index.php?topic=5970.30       -> ("5970", 30)
    Returns (None, None) if not parseable.
    """
    # SEF format: /topic,{id}.{start}.html
    m = re.search(r"topic,(\d+)\.(\d+)(?:\.html)?", url)
    if m:
        return m.group(1), int(m.group(2))
    # Query-string format: topic={id}.{start}
    m = re.search(r"topic=(\d+)\.(\d+)", url)
    if m:
        return m.group(1), int(m.group(2))
    return None, None


def _build_page_url(thread_url: str, topic_id: str, start: int) -> str:
    """
    Build a page URL for a given start offset, matching the URL format
    of the original thread_url (SEF or query-string).
    """
    # If the original URL uses SEF format, produce SEF URLs
    if re.search(r"topic,\d+\.\d+", thread_url):
        # Replace topic,ID.OLDSTART.html with topic,ID.NEWSTART.html
        return re.sub(
            r"topic,\d+\.\d+(\.html)?",
            f"topic,{topic_id}.{start}.html",
            thread_url,
        )
    else:
        # Query-string format
        parsed = urlparse(thread_url)
        new_qs = urlencode({"topic": f"{topic_id}.{start}"})
        return urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_qs, "")
        )


def discover_page_urls(soup: BeautifulSoup, thread_url: str) -> list[str]:
    """
    Discover all page URLs for a thread.

    Supports both URL formats:
      - SEF:   index.php/topic,5970.0.html
      - Query: index.php?topic=5970.0

    Strategy:
      1. Collect start offsets from visible navPages links.
      2. Parse the expandPages() JS call in the "..." span to get
         max_start and per_page.
      3. Generate all intermediate page URLs.
    """
    topic_id, _ = _parse_topic_start(thread_url)
    if not topic_id:
        return [thread_url]

    starts: set[int] = {0}  # Always include start=0
    per_page: Optional[int] = None
    max_start: Optional[int] = None

    # Find the page navigation area
    pagelinks = soup.find_all("div", class_="pagelinks")
    if not pagelinks:
        pagelinks = soup.find_all("div", class_="pagesection")

    for nav in pagelinks:
        # Collect all navPages links
        for link in nav.find_all("a", class_="navPages"):
            href = link.get("href", "")
            _, s = _parse_topic_start(href)
            if s is not None:
                starts.add(s)

        # Also collect any other <a> that contains a topic reference
        for link in nav.find_all("a"):
            href = link.get("href", "")
            _, s = _parse_topic_start(href)
            if s is not None:
                starts.add(s)

        # Parse expandPages() from the "..." span onclick.
        # Pattern: expandPages(this, 'URL_TEMPLATE', startFrom, maxStart, perPage)
        for span in nav.find_all("span"):
            onclick = span.get("onclick", "")
            m = re.search(
                r"expandPages\(\s*this\s*,\s*'[^']+'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
                onclick,
            )
            if m:
                max_start = int(m.group(2))
                per_page = int(m.group(3))
                starts.add(max_start)

    # Determine per_page from consecutive starts if not found via expandPages
    if per_page is None:
        sorted_starts = sorted(starts)
        if len(sorted_starts) >= 2:
            # Use the smallest gap between consecutive starts
            gaps = [
                sorted_starts[i + 1] - sorted_starts[i]
                for i in range(len(sorted_starts) - 1)
                if sorted_starts[i + 1] - sorted_starts[i] > 0
            ]
            per_page = min(gaps) if gaps else 15
        else:
            per_page = 15  # SMF default

    # Determine max_start from what we collected
    if max_start is None:
        max_start = max(starts)

    # Generate all page URLs
    all_urls = []
    for start in range(0, max_start + 1, per_page):
        all_urls.append(_build_page_url(thread_url, topic_id, start))

    return all_urls


# ─── Content Extraction ──────────────────────────────────────────────────────


def extract_posts(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """
    Extract all posts from a thread page.

    Returns a list of dicts with keys:
        - author: poster's display name
        - date: post date/time string
        - body: post text content
        - images: list of image URLs found in the post
    """
    posts = []

    # Primary: Find post containers by class.  SMF 2.0 uses alternating
    # "windowbg" / "windowbg2" classes on each post wrapper div.
    # We filter to only those that contain a <div class="post_wrapper">
    # child to avoid matching non-post elements that share the same class.
    post_divs = []
    for cls in POST_CONTAINER_CLASSES:
        for div in soup.find_all("div", class_=cls):
            if div.find("div", class_="post_wrapper"):
                post_divs.append(div)

    if not post_divs:
        # Fallback: look for divs with id="msg{N}" (some SMF themes)
        post_divs = soup.find_all("div", id=re.compile(r"^msg\d+$"))

    for post_div in post_divs:
        post_images: List[str] = []
        post_data: Dict[str, Any] = {
            "author": "",
            "date": "",
            "body": "",
            "images": post_images,
        }

        # ── Author ──
        # SMF 2.1: <div class="poster"> -> <h4><a>Username</a></h4>
        poster_div = post_div.find("div", class_="poster")
        if poster_div:
            h4 = poster_div.find("h4")
            if h4:
                post_data["author"] = h4.get_text(strip=True)
        else:
            # SMF 2.0 fallback: look for poster_info
            poster_info = post_div.find("div", class_="poster_info")
            if poster_info:
                bold = poster_info.find("b") or poster_info.find("strong")
                if bold:
                    post_data["author"] = bold.get_text(strip=True)

        # ── Date ──
        # SMF 2.1: <div class="postarea"> -> <div class="keyinfo"> has date
        keyinfo = post_div.find("div", class_="keyinfo")
        if keyinfo:
            # The smalltext div or span usually contains the date
            smalltext = keyinfo.find("div", class_="smalltext") or keyinfo.find(
                "span", class_="smalltext"
            )
            if smalltext:
                post_data["date"] = smalltext.get_text(strip=True)
            else:
                # Sometimes date is just text in keyinfo after the subject link
                # Look for text containing date-like patterns
                for child in keyinfo.children:
                    text = getattr(child, "get_text", lambda **_: str(child))(
                        strip=True
                    )
                    if re.search(r"\d{4}|\d{1,2}:\d{2}", text):
                        post_data["date"] = text
                        break

        # ── Body ──
        # SMF 2.1: <div class="post"> -> <div class="inner">
        inner_div = post_div.find("div", class_="inner")
        if inner_div:
            post_data["body"] = inner_div.get_text("\n", strip=True)
        else:
            # SMF 2.0 fallback
            post_div_body = post_div.find("div", class_="post")
            if post_div_body:
                post_data["body"] = post_div_body.get_text("\n", strip=True)

        # ── Images ──
        # Search within the post body area for images
        search_area = inner_div or post_div.find("div", class_="post") or post_div
        if search_area:
            for img in search_area.find_all("img"):
                src = img.get("src", "")
                if not src:
                    continue
                # Skip smileys, icons, and UI elements
                if any(
                    skip in src.lower()
                    for skip in [
                        "smileys/",
                        "smiley",
                        "/icons/",
                        "icon_",
                        "post_icon",
                    ]
                ):
                    continue
                post_data["images"].append(src)

            # Also find forum attachment links (dlattach)
            for link in search_area.find_all("a"):
                href = link.get("href", "")
                if "dlattach" in href or "action=dlattach" in href:
                    # Check if it links to an image
                    link_img = link.find("img")
                    if link_img:
                        # The <a> wraps a thumbnail <img>; the href is the
                        # full-size image
                        post_data["images"].append(href)
                    elif any(
                        ext in href.lower()
                        for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"]
                    ):
                        post_data["images"].append(href)
                    else:
                        # Could be an image attachment without extension in URL
                        # Include it and let the downloader figure it out
                        if (
                            "image" in link.get("title", "").lower()
                            or "image" in link.get_text(strip=True).lower()
                        ):
                            post_data["images"].append(href)
                        elif "dlattach" in href:
                            # Likely an attachment — include it
                            post_data["images"].append(href)

            # SMF attachment containers (div.attached / div.attachments_top)
            for attach_div in search_area.find_all(
                "div",
                class_=lambda c: (
                    c and ("attached" in c.split() or "attachments_top" in c.split())
                ),
            ):
                for link in attach_div.find_all("a", href=True):
                    href = link["href"]
                    if href not in post_data["images"]:
                        post_data["images"].append(href)

        if post_data["body"]:  # Only include posts that have content
            posts.append(post_data)

    return posts


# ─── Image Downloading ───────────────────────────────────────────────────────


def download_image(
    session: requests.Session,
    url: str,
    save_dir: str,
    image_counter: int,
    base_url: str,
) -> Optional[str]:
    """
    Download an image and save it to save_dir.

    Returns the filename on success, None on failure.
    """
    # Resolve relative URLs
    full_url = urljoin(base_url, url)

    try:
        resp = session.get(full_url, timeout=30, stream=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print_error(f"Failed to download image: {exc}")
        return None

    # Determine filename
    content_type = resp.headers.get("Content-Type", "").lower()
    content_disp = resp.headers.get("Content-Disposition", "")

    # Try to get filename from Content-Disposition header
    filename = None
    if content_disp:
        match = re.search(r'filename[*]?=["\']?([^"\';\n]+)', content_disp)
        if match:
            filename = match.group(1).strip()

    if not filename:
        # Try from URL path
        parsed = urlparse(full_url)
        path = parsed.path
        if path and "/" in path:
            candidate = path.rsplit("/", 1)[-1]
            if "." in candidate and len(candidate) < 200:
                filename = candidate

    if not filename:
        # Generate from counter and content type
        ext_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
            "image/svg+xml": ".svg",
        }
        ext = ".jpg"  # default
        for ct, e in ext_map.items():
            if ct in content_type:
                ext = e
                break
        filename = f"image_{image_counter:04d}{ext}"

    # Sanitize filename
    filename = sanitize_filename(filename)
    if not filename:
        filename = f"image_{image_counter:04d}.jpg"

    filepath = os.path.join(save_dir, filename)

    # Avoid overwriting — append counter if file exists
    if os.path.exists(filepath):
        name, ext = os.path.splitext(filename)
        filepath = os.path.join(save_dir, f"{name}_{image_counter:04d}{ext}")

    try:
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
    except OSError as exc:
        print_error(f"Failed to save image {filename}: {exc}")
        return None

    return os.path.basename(filepath)


# ─── Main Archiver ────────────────────────────────────────────────────────────


def archive_thread(thread_url: str, session: requests.Session) -> None:
    """
    Archive a complete thread: all pages, all posts, all images.
    """
    base_url = derive_base_url(thread_url)

    # Fetch the first page
    print_status(f"Fetching thread: {thread_url}")
    first_page = fetch_page(session, thread_url)
    if first_page is None:
        print_error("Could not load the thread. Aborting.")
        return

    # Get thread title
    thread_title = get_thread_title(first_page)
    safe_title = sanitize_filename(thread_title)
    print_success(f'Thread: "{thread_title}"')

    # Discover all pages
    page_urls = discover_page_urls(first_page, thread_url)
    total_pages = len(page_urls)
    print_status(f"Found {total_pages} page(s) to archive.")

    # Prepare output inside threads/ directory
    os.makedirs(THREADS_DIR, exist_ok=True)
    txt_filename = os.path.join(THREADS_DIR, f"{safe_title}.txt")
    img_dirname = os.path.join(THREADS_DIR, f"{safe_title}_images")

    # Collect all posts and images
    all_posts: List[Dict[str, Any]] = []
    all_image_urls: List[Tuple[str, int]] = []  # (url, post_index)
    global_image_counter = 0

    for page_num, page_url in enumerate(page_urls, start=1):
        print_status(f"Downloading page {page_num}/{total_pages}...")

        if page_num == 1:
            soup = first_page  # Already fetched
        else:
            soup = fetch_page(session, page_url)
            if soup is None:
                print_error(f"Skipping page {page_num} — failed to load.")
                continue
            # Be polite — small delay between requests
            time.sleep(0.5)

        posts = extract_posts(soup)
        print_status(f"  Page {page_num}: {len(posts)} post(s) extracted.")

        for post in posts:
            post_index = len(all_posts)
            all_posts.append(post)
            for img_url in post["images"]:
                all_image_urls.append((img_url, post_index))
                global_image_counter += 1

    if not all_posts:
        print_error(
            "No posts were extracted. The thread may require login "
            "or the HTML structure is different than expected."
        )
        return

    print_success(f"Total: {len(all_posts)} posts, {len(all_image_urls)} images found.")

    # ── Save text ──
    print_status(f"Saving post text to {txt_filename}...")
    try:
        with open(txt_filename, "w", encoding="utf-8") as f:
            f.write(f"{thread_title}\n")

            for i, post in enumerate(all_posts):
                f.write("---\n")
                author = post["author"] or "?"
                date = post["date"] or "?"
                body = post["body"].replace("\n", "\\n")
                f.write(f"{author}|{date}|{body}\n")

        print_success(f"Saved {txt_filename}")
    except OSError as exc:
        print_error(f"Failed to write {txt_filename}: {exc}")
        return

    # ── Download images ──
    if all_image_urls:
        os.makedirs(img_dirname, exist_ok=True)
        print_status(f"Downloading {len(all_image_urls)} image(s) to {img_dirname}/...")
        downloaded = 0
        failed = 0

        for idx, (img_url, _post_idx) in enumerate(all_image_urls, start=1):
            saved_name = download_image(session, img_url, img_dirname, idx, base_url)
            if saved_name:
                downloaded += 1
                print_status(f"  Saved image {idx}/{len(all_image_urls)}: {saved_name}")
            else:
                failed += 1

            # Be polite
            if idx < len(all_image_urls):
                time.sleep(0.3)

        print_success(f"Images: {downloaded} downloaded, {failed} failed.")
    else:
        print_status("No images to download.")

    # ── Done ──
    print()
    print_success("Archive complete!")
    print(f"  Text file : {txt_filename}")
    if all_image_urls:
        print(f"  Images dir: {img_dirname}/")


# ─── Entry Point ──────────────────────────────────────────────────────────────


def main() -> None:
    print(BANNER)

    # Get thread URL
    if len(sys.argv) > 1:
        thread_url = sys.argv[1].strip()
    else:
        thread_url = input("  Enter thread URL: ").strip()

    if not thread_url:
        print_error("No URL provided. Exiting.")
        sys.exit(1)

    # Basic URL validation
    if not thread_url.startswith("http"):
        print_error("URL must start with http:// or https://")
        sys.exit(1)

    # Create session
    session = create_session()

    # Ask if login is needed
    print()
    needs_login = input("  Do you need to log in? (y/N): ").strip().lower()
    if needs_login in ("y", "yes"):
        base_url = derive_base_url(thread_url)
        if not login(session, base_url):
            print_error("Login failed. Attempting to continue without auth...")

    print()

    # Archive the thread
    archive_thread(thread_url, session)


if __name__ == "__main__":
    main()
