
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "ovpn" / "vpnbook"
JSON_FILE = ROOT / "data" / "vpnbook.json"
PAGE_URL = "https://www.vpnbook.com/freevpn/openvpn"
RAW_BASE = "https://raw.githubusercontent.com/pvKaLeed/GVPN/main"

SERVERS = [
    {"id": "us16", "name": "US Server 1", "country": "United States"},
    {"id": "us178", "name": "US Server 2", "country": "United States"},
    {"id": "ca149", "name": "Canada Server 1", "country": "Canada"},
    {"id": "ca196", "name": "Canada Server 2", "country": "Canada"},
    {"id": "uk205", "name": "UK Server 1", "country": "United Kingdom"},
    {"id": "uk68", "name": "UK Server 2", "country": "United Kingdom"},
    {"id": "de20", "name": "Germany Server 1", "country": "Germany"},
    {"id": "de220", "name": "Germany Server 2", "country": "Germany"},
    {"id": "fr200", "name": "France Server 1", "country": "France"},
    {"id": "fr231", "name": "France Server 2", "country": "France"},
]


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_json_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def extract_credentials(page):
    """Extract current public credentials from VPNBook's official page."""
    text = page.locator("body").inner_text(timeout=15000)

    username_match = re.search(
        r"Username\s*:?\s*([A-Za-z0-9_.-]+)",
        text,
        re.IGNORECASE,
    )
    password_match = re.search(
        r"Password\s*:?\s*([A-Za-z0-9!@#$%^&*()_+=.,?-]+)",
        text,
        re.IGNORECASE,
    )

    if not username_match or not password_match:
        raise RuntimeError(
            "Could not read VPNBook username/password from the official "
            "page. The page may have changed; refusing to publish "
            "incomplete credentials."
        )

    username = username_match.group(1).strip()
    password = password_match.group(1).strip()

    if not username or not password:
        raise RuntimeError("VPNBook credentials are empty")

    return {"username": username, "password": password}


def validate_ovpn(data, expected_host):
    text = data.decode("utf-8", errors="strict")

    if len(text) < 100:
        raise ValueError("Downloaded config is too small")

    if not re.search(r"(?m)^\s*client\s*$", text):
        raise ValueError("Missing client directive")

    remotes = re.findall(r"(?m)^\s*remote\s+(\S+)", text)
    if not remotes:
        raise ValueError("Missing remote directive")

    if not any(
        remote.lower() == expected_host.lower()
        for remote in remotes
    ):
        raise ValueError(
            f"Remote does not match {expected_host}: {remotes}"
        )

    forbidden = re.compile(
        r"(?im)^\s*(up|down|route-up|ipchange|plugin|"
        r"client-connect|client-disconnect)\s+"
    )
    if forbidden.search(text):
        raise ValueError("Profile contains a disallowed command")

    return text.encode("utf-8")


def click_visible_text(page, text):
    locator = page.get_by_text(text, exact=False)

    for index in range(locator.count()):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible() and candidate.is_enabled():
                candidate.click(timeout=5000)
                return
        except Exception:
            continue

    raise RuntimeError(f"Could not click VPNBook element: {text!r}")


def click_download(page):
    for selector in (
        'a[download]',
        'button:has-text("Download")',
        'a:has-text("Download")',
        '[role="button"]:has-text("Download")',
        'button:has-text("Configuration")',
        'a:has-text("Configuration")',
    ):
        locator = page.locator(selector)

        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click(timeout=5000)
                    return
            except Exception:
                continue

    raise RuntimeError(
        "VPNBook download control not found; official page may have changed"
    )


def extract_ovpn(download_path, expected_host):
    raw = download_path.read_bytes()

    if b"client" in raw and b"remote " in raw:
        return validate_ovpn(raw, expected_host)

    if not zipfile.is_zipfile(download_path):
        raise ValueError("Download is neither an .ovpn profile nor a ZIP")

    with zipfile.ZipFile(download_path) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".ovpn"):
                continue
            if name.startswith("__MACOSX/"):
                continue

            try:
                return validate_ovpn(
                    archive.read(name),
                    expected_host,
                )
            except (ValueError, UnicodeDecodeError):
                continue

    raise ValueError(f"No valid profile found for {expected_host}")


def download_server(page, server, temp_dir):
    page.goto(
        PAGE_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )
    page.locator("body").wait_for(state="visible")

    click_visible_text(page, server["name"])
    page.wait_for_timeout(500)

    click_visible_text(page, "TCP 443")
    page.wait_for_timeout(300)

    download_path = temp_dir / f"{server['id']}-download"

    try:
        with page.expect_download(timeout=30000) as event:
            click_download(page)

        event.value.save_as(str(download_path))

    except PlaywrightTimeoutError as exc:
        screenshot = temp_dir / f"{server['id']}-error.png"
        page.screenshot(path=str(screenshot), full_page=True)
        raise RuntimeError(
            f"No download started for {server['id']}; "
            f"diagnostic screenshot: {screenshot}"
        ) from exc

    return extract_ovpn(
        download_path,
        f"{server['id']}.vpnbook.com",
    )


def main():
    JSON_FILE.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="vpnbook-") as temp:
        temp_dir = Path(temp)
        staged_profiles = {}
        records = []

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                accept_downloads=True,
                locale="en-US",
            )
            page = context.new_page()
            page.set_default_timeout(10000)

            try:
                # Read the credentials once from the official page.
                page.goto(
                    PAGE_URL,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                credentials = extract_credentials(page)
                print("VPNBook credentials retrieved successfully.")

                for server in SERVERS:
                    print(f"Downloading {server['id']} (TCP 443)...")

                    profile = download_server(page, server, temp_dir)
                    profile = validate_ovpn(
                        profile,
                        f"{server['id']}.vpnbook.com",
                    )

                    filename = f"{server['id']}-tcp443.ovpn"
                    relative_path = f"ovpn/vpnbook/{filename}"
                    staged_profiles[filename] = profile

                    records.append({
                        "id": server["id"],
                        "source": "VPNBook",
                        "name": server["name"],
                        "country": server["country"],
                        "host": f"{server['id']}.vpnbook.com",
                        "protocol": "TCP",
                        "port": 443,
                        "config_path": relative_path,
                        "config_url": f"{RAW_BASE}/{relative_path}",
                        "sha256": sha256(profile),
                        "auth_required": True,
                        "auth": credentials,
                        "validated_at": now_utc(),
                        "validation": "syntax_checked",
                    })

                    print(f"Validated {filename}")

            finally:
                context.close()
                browser.close()

        if len(records) != len(SERVERS):
            raise RuntimeError(
                f"Expected {len(SERVERS)} profiles; got {len(records)}. "
                "Existing JSON was not replaced."
            )

        # Write the new profiles first.
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        for filename, profile in staged_profiles.items():
            (OUTPUT_DIR / filename).write_bytes(profile)

        write_json_atomic(JSON_FILE, {
            "schema_version": 2,
            "source": PAGE_URL,
            "updated_at": now_utc(),
            "server_count": len(records),
            "servers": records,
        })

        # Remove obsolete files only after the JSON has been written.
        expected_files = set(staged_profiles)
        for old_file in OUTPUT_DIR.glob("*.ovpn"):
            if old_file.name not in expected_files:
                old_file.unlink()

        print(f"VPNBook: saved {len(records)} profiles")
        print(f"JSON: {JSON_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
