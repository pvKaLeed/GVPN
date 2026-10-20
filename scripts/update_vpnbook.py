import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CONFIGS = ROOT / "configs" / "vpnbook"

PAGE = "https://www.vpnbook.com/freevpn/openvpn"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 15) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
}

# Server identifiers are candidates, not proof that the
# corresponding server is currently online.
SERVERS = {
    "us16": ("US", "United States"),
    "us178": ("US", "United States"),
    "ca149": ("CA", "Canada"),
    "ca196": ("CA", "Canada"),
    "uk205": ("GB", "United Kingdom"),
    "uk68": ("GB", "United Kingdom"),
    "de20": ("DE", "Germany"),
    "de220": ("DE", "Germany"),
    "fr200": ("FR", "France"),
    "fr2311": ("FR", "France"),
}

TIMEOUT = 45


def fetch_page(url):
    """Fetch the official page and return the response."""
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        raise RuntimeError(
            f"Expected HTML but received: {content_type}"
        )

    return response


def extract_config_links(html, page_url):
    """Extract explicit ZIP/OVPN links from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        raw_href = anchor["href"].strip()

        if not raw_href:
            continue

        absolute_url = urljoin(page_url, raw_href)
        parsed = urlparse(absolute_url)

        if parsed.scheme not in ("http", "https"):
            continue

        if parsed.netloc.lower() != "www.vpnbook.com":
            continue

        path = parsed.path.lower()

        if not (
            path.endswith(".zip")
            or path.endswith(".ovpn")
        ):
            continue

        if absolute_url in seen:
            continue

        seen.add(absolute_url)

        label = anchor.get_text(" ", strip=True)

        links.append({
            "url": absolute_url,
            "label": label,
            "filename": Path(parsed.path).name,
        })

    return soup, links


def identify_server(text):
    """Match a known server identifier using word boundaries."""
    normalized = text.lower()

    for code in sorted(SERVERS, key=len, reverse=True):
        pattern = (
            r"(?<![a-z0-9])"
            + re.escape(code.lower())
            + r"(?![a-z0-9])"
        )

        if re.search(pattern, normalized):
            return code

    return None


def build_server_records(links):
    """Build records only for server IDs explicitly found in links."""
    grouped = {}

    for item in links:
        code = identify_server(
            item["url"] + " " +
            item["label"] + " " +
            item["filename"]
        )

        if not code:
            continue

        country_code, country = SERVERS[code]

        record = grouped.setdefault(
            code,
            {
                "id": "vpnbook-" + code,
                "source": "VPNBook",
                "hostname": code + ".vpnbook.com",
                "country": country,
                "country_code": country_code,
                "config_download_urls": [],
                "config_path": None,
                "ping_ms_source": None,
                "speed_bps_source": None,
                "status": "config_link_found",
            },
        )

        if item["url"] not in record["config_download_urls"]:
            record["config_download_urls"].append(item["url"])

    return list(grouped.values())


def write_json_atomically(path, data):
    """Avoid leaving a partially written JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_suffix(path.suffix + ".tmp")

    temporary_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    temporary_path.replace(path)


def main():
    print("Fetching VPNBook official page...")
    print("URL:", PAGE)

    try:
        response = fetch_page(PAGE)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Failed to fetch VPNBook page: {exc}"
        ) from exc

    print("HTTP status:", response.status_code)
    print("Final URL:", response.url)

    soup, links = extract_config_links(
        response.text,
        response.url,
    )

    if not links:
        title = (
            soup.title.get_text(" ", strip=True)
            if soup.title
            else "Missing"
        )

        print("Page title:", title)
        print(
            "Content type:",
            response.headers.get("Content-Type"),
        )
        print(
            "Anchor count:",
            len(soup.find_all("a", href=True)),
        )

        raise RuntimeError(
            "No explicit .zip or .ovpn download links found. "
            "The page may have changed or returned a challenge page. "
            "Inspect the official HTML before changing the parser."
        )

    print("Config download links found:", len(links))

    for item in links:
        print("-", item["filename"], "=>", item["url"])

    servers = build_server_records(links)

    if not servers:
        raise RuntimeError(
            "Config links were found, but none matched a configured "
            "server identifier. Existing vpnbook.json was preserved."
        )

    DATA.mkdir(parents=True, exist_ok=True)
    CONFIGS.mkdir(parents=True, exist_ok=True)

    output = {
        "source": "VPNBook",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "official_page": response.url,
        "servers": servers,
    }

    output_path = DATA / "vpnbook.json"
    write_json_atomically(output_path, output)

    print(f"VPNBook: recorded {len(servers)} servers")
    print("Output:", output_path)
    print(
        "Note: links are recorded only. "
        "Configurations have not been downloaded or validated, "
        "and server availability has not been tested."
    )


if __name__ == "__main__":
    main()
