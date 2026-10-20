# scripts/update_vpnbook.py

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


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_ovpn(data: bytes, expected_host: str) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(text) < 100:
        raise ValueError("Downloaded config is too small.")

    if not re.search(r"(?m)^\s*client\s*$", text):
        raise ValueError("Config is missing the OpenVPN client directive.")

    remotes = re.findall(r"(?m)^\s*remote\s+(\S+)", text)
    if not remotes:
        raise ValueError("Config has no remote server.")

    if not any(expected_host.lower() in host.lower() for host in remotes):
        raise ValueError(
            f"Config remote does not match {expected_host}: {remotes}"
        )

    return text


def click_visible_text(page, text: str) -> None:
    """Click a visible element matching text; fail clearly if the UI changed."""
    locator = page.get_by_text(text, exact=False)
    for index in range(locator.count()):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible() and candidate.is_enabled():
                candidate.click(timeout=5000)
                return
        except Exception:
            continue

    raise RuntimeError(f"Could not click visible VPNBook element: {text!r}")


def click_download(page) -> None:
    # Prefer a real button with a download-related label.
    buttons = page.locator("button")
    for index in range(buttons.count()):
        button = buttons.nth(index)
        try:
            if not button.is_visible() or not button.is_enabled():
                continue
            label = (button.inner_text(timeout=1000) or "").strip().lower()
            if "download" in label or "configuration" in label:
                button.click(timeout=5000)
                return
        except Exception:
            continue

    # Fallback for links or other accessible controls.
    for selector in (
        'a[download]',
        '[role="button"]:has-text("Download")',
        'button:has-text("Download")',
        'a:has-text("Download")',
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
        "VPNBook download control was not found or is disabled. "
        "The official page UI may have changed."
    )


def extract_ovpn(download_path: Path, expected_host: str) -> bytes:
    raw = download_path.read_bytes()

    # Some download flows return one .ovpn directly.
    if b"client" in raw and b"remote " in raw:
        validate_ovpn(raw, expected_host)
        return raw

    # Other flows may return a ZIP containing multiple profiles.
    if not zipfile.is_zipfile(download_path):
        raise ValueError(
            f"Download is neither an OpenVPN profile nor a ZIP: "
            f"{download_path.name}"
        )

    with zipfile.ZipFile(download_path) as archive:
        candidates = [
            name for name in archive.namelist()
            if name.lower().endswith(".ovpn") and not name.startswith("__MACOSX/")
        ]

        for name in candidates:
            content = archive.read(name)
            try:
                validate_ovpn(content, expected_host)
                return content
            except (ValueError, UnicodeDecodeError):
                continue

    raise ValueError(
        f"No valid .ovpn profile for {expected_host} in {download_path.name}"
    )


def download_server(page, server: dict, temp_dir: Path) -> bytes:
    page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
    page.locator("body").wait_for(state="visible")

    # The current official page displays server names such as
    # "US Server 1 us16.vpnbook.com".
    click_visible_text(page, server["name"])
    page.wait_for_timeout(500)

    # Use TCP 443 as the default profile.
    click_visible_text(page, "TCP 443")
    page.wait_for_timeout(300)

    download_path = temp_dir / f"{server['id']}-download"
    try:
        with page.expect_download(timeout=30000) as event:
            click_download(page)

        download = event.value
        download.save_as(str(download_path))
    except PlaywrightTimeoutError as exc:
        # Save a screenshot for diagnosis in the GitHub Actions logs/artifacts.
        screenshot = temp_dir / f"{server['id']}-download-error.png"
        page.screenshot(path=str(screenshot), full_page=True)
        raise RuntimeError(
            f"VPNBook did not start a download for {server['id']}. "
            f"Screenshot: {screenshot.name}"
        ) from exc

    return extract_ovpn(download_path, f"{server['id']}.vpnbook.com")


def main() -> None:
    JSON_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Stage everything separately so a failed run never deletes working configs.
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
                for server in SERVERS:
                    print(f"Downloading VPNBook {server['id']} (TCP 443)...")
                    profile = download_server(page, server, temp_dir)
                    validate_ovpn(profile, f"{server['id']}.vpnbook.com")

                    filename = f"{server['id']}-tcp443.ovpn"
                    staged_profiles[filename] = profile

                    records.append({
                        "id": server["id"],
                        "name": server["name"],
                        "country": server["country"],
                        "host": f"{server['id']}.vpnbook.com",
                        "protocol": "TCP",
                        "port": 443,
                        "config_path": f"ovpn/vpnbook/{filename}",
                        "sha256": sha256(profile),
                    })
                    print(f"Validated {filename}")
            finally:
                context.close()
                browser.close()

        if len(records) != len(SERVERS):
            raise RuntimeError(
                f"Expected {len(SERVERS)} profiles, got {len(records)}. "
                "Existing VPNBook data was preserved."
            )

        # Save staged files before switching the JSON index.
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        for filename, profile in staged_profiles.items():
            (OUTPUT_DIR / filename).write_bytes(profile)

        payload = {
            "source": PAGE_URL,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "servers": records,
        }

        new_json = JSON_FILE.with_suffix(".json.tmp")
        new_json.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        new_json.replace(JSON_FILE)

        # Remove obsolete .ovpn profiles only after the new JSON is ready.
        expected_files = set(staged_profiles)
        for old_file in OUTPUT_DIR.glob("*.ovpn"):
            if old_file.name not in expected_files:
                old_file.unlink()

        print(f"VPNBook: saved {len(records)} validated profiles.")
        print(f"JSON: {JSON_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
