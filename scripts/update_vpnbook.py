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
    "User-Agent": "Mozilla/5.0 (compatible; GVPN/1.0)",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

COUNTRIES = {
    "us": ("US", "United States"),
    "ca": ("CA", "Canada"),
    "uk": ("GB", "United Kingdom"),
    "de": ("DE", "Germany"),
    "fr": ("FR", "France"),
}

TIMEOUT = 45


def fetch_page():
    response = requests.get(
        PAGE,
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    if "html" not in response.headers.get(
        "Content-Type", ""
    ).lower():
        raise RuntimeError("VPNBook did not return HTML.")

    return response


def extract_servers(soup):
    """Extract hostnames actually present on the official page."""
    text = soup.get_text(" ", strip=True)

    pattern = re.compile(
        r"(?<![a-z0-9])"
        r"([a-z]{2}\d+)\.vpnbook\.com"
        r"(?![a-z0-9])",
        re.IGNORECASE,
    )

    codes = sorted({
        match.group(1).lower()
        for match in pattern.finditer(text)
    })

    servers = []

    for code in codes:
        prefix = code[:2]

        if prefix not in COUNTRIES:
            continue

        country_code, country = COUNTRIES[prefix]

        servers.append({
            "id": "vpnbook-" + code,
            "source": "VPNBook",
            "hostname": code + ".vpnbook.com",
            "country": country,
            "country_code": country_code,
            "config_download_urls": [],
            "config_path": None,
            "ping_ms_source": None,
            "speed_bps_source": None,
            "status": "server_listed_config_not_downloaded",
        })

    return servers


def extract_direct_config_links(soup, page_url):
    """Record only explicit ZIP/OVPN links from the page."""
    links = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        url = urljoin(page_url, anchor["href"].strip())
        parsed = urlparse(url)
        path = parsed.path.lower()

        if parsed.scheme not in ("http", "https"):
            continue

        if parsed.netloc.lower() not in {
            "vpnbook.com",
            "www.vpnbook.com",
            "static.vpnbook.com",
        }:
            continue

        if not path.endswith((".zip", ".ovpn")):
            continue

        if url in seen:
            continue

        seen.add(url)
        links.append({
            "url": url,
            "filename": Path(parsed.path).name,
        })

    return links


def attach_links(servers, links):
    """Attach links only when a hostname is explicitly identifiable."""
    for link in links:
        filename = link["filename"].lower()

        for server in servers:
            code = server["hostname"].split(".")[0]

            if re.search(
                r"(?<![a-z0-9])" + re.escape(code) +
                r"(?![a-z0-9])",
                filename,
            ):
                if link["url"] not in server["config_download_urls"]:
                    server["config_download_urls"].append(
                        link["url"]
                    )
                server["status"] = "config_link_found"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def main():
    print("Fetching VPNBook official page...")
    response = fetch_page()

    print("HTTP status:", response.status_code)
    print("Final URL:", response.url)

    soup = BeautifulSoup(response.text, "html.parser")

    servers = extract_servers(soup)
    links = extract_direct_config_links(soup, response.url)

    print("Server hostnames found:", len(servers))
    print("Direct config links found:", len(links))

    if not servers:
        title = (
            soup.title.get_text(" ", strip=True)
            if soup.title else "Missing"
        )
        raise RuntimeError(
            "No VPNBook server hostnames found. "
            f"Page title: {title}. Existing data preserved."
        )

    attach_links(servers, links)

    output = {
        "source": "VPNBook",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "official_page": response.url,
        "servers": servers,
    }

    output_path = DATA / "vpnbook.json"
    write_json(output_path, output)

    print(f"VPNBook: recorded {len(servers)} servers")
    print("Output:", output_path)

    without_configs = sum(
        not server["config_download_urls"]
        for server in servers
    )

    if without_configs:
        print(
            f"Notice: {without_configs} servers have no direct "
            "config download URL. They are listed, not ready "
            "to connect using a downloaded .ovpn file."
        )


if __name__ == "__main__":
    main()
