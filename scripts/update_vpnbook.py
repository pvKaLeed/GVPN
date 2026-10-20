
import json
import re
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CONFIGS = ROOT / "configs" / "vpnbook"
PAGE = "https://www.vpnbook.com/freevpn/openvpn"

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
    "fr231": ("FR", "France"),
}


def main():
    response = requests.get(
        PAGE,
        headers={"User-Agent": "Mozilla/5.0 GVPN/1.0"},
        timeout=45,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    links = []

    for a in soup.find_all("a", href=True):
        href = urljoin(PAGE, a["href"])
        label = a.get_text(" ", strip=True)
        if ".zip" in href.lower() or ".ovpn" in href.lower():
            links.append({
                "url": href,
                "label": label,
            })

    # Do not guess URLs if the official page format changes.
    if not links:
        raise RuntimeError(
            "No OpenVPN config download links found on VPNBook page. "
            "Inspect the official page and update this parser."
        )

    CONFIGS.mkdir(parents=True, exist_ok=True)
    servers = []

    for code, (country_code, country) in SERVERS.items():
        matched = [
            item for item in links
            if code in (item["url"] + " " + item["label"]).lower()
        ]

        if not matched:
            continue

        servers.append({
            "id": "vpnbook-" + code,
            "source": "VPNBook",
            "hostname": code + ".vpnbook.com",
            "country": country,
            "country_code": country_code,
            "config_download_urls": [
                item["url"] for item in matched
            ],
            "config_path": None,
            "ping_ms_source": None,
            "speed_bps_source": None,
            "status": "config_link_found",
        })

    if not servers:
        raise RuntimeError(
            "Download links were found, but no server could be matched. "
            "Do not publish empty VPNBook data."
        )

    DATA.mkdir(parents=True, exist_ok=True)
    output = {
        "source": "VPNBook",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "official_page": PAGE,
        "servers": servers,
    }

    (DATA / "vpnbook.json").write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"VPNBook: recorded {len(servers)} servers")
    print("Note: links are recorded, not downloaded or validated as .ovpn files.")


if __name__ == "__main__":
    main()
