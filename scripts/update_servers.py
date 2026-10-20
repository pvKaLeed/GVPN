import base64
import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

from update_vpnbook import update_vpnbook

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OVPN = ROOT / "ovpn" / "vpngate"

VPNGATE_URL = "https://www.vpngate.net/api/iphone/"
HEADERS = {"User-Agent": "Mozilla/5.0 GVPN/1.0"}
TIMEOUT = 60


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def safe_name(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))
    return value.strip("._")[:100] or "server"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def update_vpngate():
    print("Fetching VPN Gate API...")

    response = requests.get(
        VPNGATE_URL,
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    response.encoding = "utf-8"

    lines = response.text.splitlines()

    header_index = next(
        (
            i for i, line in enumerate(lines)
            if line.lstrip("#").startswith("HostName,")
        ),
        None,
    )

    if header_index is None:
        raise RuntimeError("VPN Gate CSV header not found.")

    header = lines[header_index].lstrip("#")
    csv_text = "\n".join([header] + lines[header_index + 1:])
    reader = csv.DictReader(io.StringIO(csv_text))

    OVPN.mkdir(parents=True, exist_ok=True)
    staged = {}
    servers = []

    for row in reader:
        host = (row.get("HostName") or "").strip()
        ip = (row.get("IP") or "").strip()
        encoded = (
            row.get("OpenVPN_ConfigData_Base64")
            or row.get("#OpenVPN_ConfigData_Base64")
            or ""
        ).strip()

        if not host or not ip or not encoded:
            continue

        try:
            raw = base64.b64decode(encoded, validate=True)
            config = raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue

        if not re.search(r"(?m)^\s*client\s*$", config):
            continue

        filename = safe_name(f"{host}_{ip}") + ".ovpn"
        relative_path = f"ovpn/vpngate/{filename}"

        staged[filename] = config

        servers.append({
            "id": hashlib.sha256(
                f"{host}|{ip}".encode()
            ).hexdigest()[:16],
            "source": "VPN Gate",
            "hostname": host,
            "ip": ip,
            "country": (row.get("CountryLong") or "").strip(),
            "country_code": (row.get("CountryShort") or "").strip(),
            "ping_ms_source": parse_int(row.get("Ping")),
            "speed_bps_source": parse_int(row.get("Speed")),
            "sessions_reported": parse_int(row.get("NumVpnSessions")),
            "config_path": relative_path,
            "config_sha256": hashlib.sha256(raw).hexdigest(),
        })

    if not servers:
        raise RuntimeError(
            "No valid VPN Gate configs found; existing data preserved."
        )

    # Stage every config before replacing the previous collection.
    stage_dir = OVPN.with_name("vpngate_staging")
    stage_dir.mkdir(parents=True, exist_ok=True)

    try:
        for filename, config in staged.items():
            (stage_dir / filename).write_text(
                config, encoding="utf-8"
            )

        for old in OVPN.glob("*.ovpn"):
            old.unlink()

        for staged_file in stage_dir.glob("*.ovpn"):
            staged_file.replace(OVPN / staged_file.name)

    finally:
        for leftover in stage_dir.glob("*"):
            if leftover.is_file():
                leftover.unlink()
        stage_dir.rmdir()

    write_json(DATA / "vpngate.json", {
        "source": "VPN Gate",
        "updated_at": now_utc(),
        "servers": servers,
    })

    print(f"VPN Gate: saved {len(servers)} configs to {OVPN}")


def main():
    DATA.mkdir(parents=True, exist_ok=True)

    update_vpngate()

    # Do not silently hide VPNBook failures.
    update_vpnbook()

    write_json(DATA / "manifest.json", {
        "app": "GVPN",
        "schema_version": 1,
        "updated_at": now_utc(),
        "sources": {
            "vpngate": "data/vpngate.json",
            "vpnbook": "data/vpnbook.json",
        },
        "config_directories": {
            "vpngate": "ovpn/vpngate/",
            "vpnbook": "ovpn/vpnbook/",
        },
    })

    print("GVPN server and config update completed.")


if __name__ == "__main__":
    main()
