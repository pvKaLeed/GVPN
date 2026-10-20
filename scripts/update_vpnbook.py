import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OVPN = ROOT / "ovpn" / "vpnbook"

PAGE = "https://www.vpnbook.com/freevpn/openvpn"
BUNDLE_BASE = "https://www.vpnbook.com/free-openvpn-account/"

HEADERS = {"User-Agent": "Mozilla/5.0 GVPN/1.0"}
TIMEOUT = 45

COUNTRIES = {
    "us": ("US", "United States"),
    "ca": ("CA", "Canada"),
    "uk": ("GB", "United Kingdom"),
    "de": ("DE", "Germany"),
    "fr": ("FR", "France"),
}

MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def safe_name(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("._")[:120] or "config"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def valid_ovpn(content):
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False

    has_client = bool(re.search(r"(?m)^\s*client\s*$", text))
    has_remote = bool(re.search(r"(?m)^\s*remote\s+\S+", text))
    return has_client and has_remote


def discover_bundle_urls(session):
    response = session.get(
        PAGE, headers=HEADERS, timeout=TIMEOUT
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    found = {country: [] for country in COUNTRIES}

    # First use links explicitly published by the official page.
    for anchor in soup.find_all("a", href=True):
        url = urljoin(response.url, anchor["href"].strip())
        parsed = urlparse(url)

        if parsed.scheme != "https":
            continue
        if parsed.hostname not in {
            "vpnbook.com", "www.vpnbook.com"
        }:
            continue
        if not parsed.path.lower().endswith(".zip"):
            continue

        label = (
            anchor.get_text(" ", strip=True) + " " +
            parsed.path
        ).lower()

        for country in COUNTRIES:
            if re.search(
                r"(?<![a-z0-9])" + country + r"(?![a-z0-9])",
                label,
            ):
                found[country].append(url)

    # Known official legacy bundle naming convention.
    # These are candidates, not guaranteed to exist.
    for country in COUNTRIES:
        fallback = (
            BUNDLE_BASE
            + f"vpnbook-openvpn-{country}.zip"
        )
        if fallback not in found[country]:
            found[country].append(fallback)

    return found


def download_zip(session, url):
    response = session.get(
        url,
        headers=HEADERS,
        timeout=TIMEOUT,
        stream=True,
    )
    response.raise_for_status()

    content = bytearray()

    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue

        content.extend(chunk)

        if len(content) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("ZIP download exceeded size limit.")

    data = bytes(content)

    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise RuntimeError(
            f"Response is not a ZIP archive: {url}"
        )

    return data


def extract_profiles(zip_bytes):
    profiles = {}

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue

            # Never extract arbitrary archive paths.
            name = Path(info.filename).name

            if not name.lower().endswith(".ovpn"):
                continue

            if info.file_size > 2 * 1024 * 1024:
                continue

            content = archive.read(info)

            if not valid_ovpn(content):
                continue

            filename = safe_name(name)
            profiles[filename] = content

    return profiles


def fetch_country_profiles(session, country, urls):
    errors = []

    for url in urls:
        try:
            print(f"VPNBook [{country.upper()}]: downloading {url}")
            data = download_zip(session, url)
            profiles = extract_profiles(data)

            if profiles:
                print(
                    f"VPNBook [{country.upper()}]: "
                    f"validated {len(profiles)} profiles"
                )
                return profiles

            errors.append(f"{url}: no valid .ovpn profiles")

        except (requests.RequestException, RuntimeError, zipfile.BadZipFile) as exc:
            errors.append(f"{url}: {exc}")

    print(
        f"VPNBook [{country.upper()}]: no usable bundle found"
    )
    for error in errors:
        print("  -", error)

    return {}


def update_vpnbook():
    print("Fetching VPNBook official page...")

    with requests.Session() as session:
        bundle_urls = discover_bundle_urls(session)

        profiles = {}
        profile_countries = {}

        for country in COUNTRIES:
            country_profiles = fetch_country_profiles(
                session,
                country,
                bundle_urls[country],
            )

            for filename, content in country_profiles.items():
                # Avoid collisions between country bundles.
                output_name = safe_name(
                    f"{country}_{filename}"
                )

                profiles[output_name] = content
                profile_countries[output_name] = country

    if not profiles:
        raise RuntimeError(
            "VPNBook returned no validated .ovpn profiles. "
            "Existing config files and vpnbook.json were preserved. "
            "Inspect the official download flow before updating URLs."
        )

    # Build a complete staging directory before replacing old files.
    OVPN.parent.mkdir(parents=True, exist_ok=True)
    stage = OVPN.with_name("vpnbook_staging")
    stage.mkdir(parents=True, exist_ok=True)

    servers = []

    try:
        for filename, content in profiles.items():
            (stage / filename).write_bytes(content)

            country = profile_countries[filename]
            country_code, country_name = COUNTRIES[country]

            text = content.decode("utf-8-sig")
            remote_match = re.search(
                r"(?m)^\s*remote\s+(\S+)(?:\s+(\d+))?",
                text,
            )
            hostname = (
                remote_match.group(1)
                if remote_match else None
            )
            port = (
                int(remote_match.group(2))
                if remote_match and remote_match.group(2)
                else None
            )

            protocol = "unknown"
            name_lower = filename.lower()

            for candidate in (
                "tcp443", "tcp80", "udp53", "udp25000"
            ):
                if candidate in name_lower:
                    protocol = candidate
                    break

            relative_path = f"ovpn/vpnbook/{filename}"

            servers.append({
                "id": "vpnbook-" + hashlib.sha256(
                    filename.encode()
                ).hexdigest()[:16],
                "source": "VPNBook",
                "hostname": hostname,
                "port": port,
                "country": country_name,
                "country_code": country_code,
                "protocol_profile": protocol,
                "config_path": relative_path,
                "config_sha256": hashlib.sha256(
                    content
                ).hexdigest(),
                "status": "config_downloaded",
            })

        # Replace the old collection only after every new file
        # has been validated and staged.
        OVPN.mkdir(parents=True, exist_ok=True)

        for old in OVPN.glob("*.ovpn"):
            old.unlink()

        for staged_file in stage.glob("*.ovpn"):
            staged_file.replace(OVPN / staged_file.name)

    finally:
        for leftover in stage.glob("*"):
            if leftover.is_file():
                leftover.unlink()
        stage.rmdir()

    write_json(DATA / "vpnbook.json", {
        "source": "VPNBook",
        "updated_at": now_utc(),
        "official_page": PAGE,
        "servers": servers,
    })

    print(
        f"VPNBook: saved {len(servers)} validated configs to {OVPN}"
    )


if __name__ == "__main__":
    update_vpnbook()
