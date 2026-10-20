
import base64
import csv
import io
import json
import re
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CONFIGS = ROOT / "configs" / "vpngate"

VPNGATE_URL = "https://www.vpngate.net/api/iphone/"

HEADERS = {
    "User-Agent": "GVPN-ServerUpdater/1.0"
}


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def safe_name(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))
    return value.strip("._")[:100] or "server"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )


def update_vpngate():
    response = requests.get(
        VPNGATE_URL,
        headers=HEADERS,
        timeout=60
    )
    response.raise_for_status()
    response.encoding = "utf-8"

    lines = [
        line for line in response.text.splitlines()
        if line and not line.startswith("*")
    ]

    if len(lines) < 2:
        raise RuntimeError("VPN Gate returned no usable server data")

    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    servers = []
    CONFIGS.mkdir(parents=True, exist_ok=True)

    for row in reader:
        host = row.get("HostName", "").strip()
        ip = row.get("IP", "").strip()
        encoded = row.get("OpenVPN_ConfigData_Base64", "").strip()

        if not host or not ip or not encoded:
            continue

        try:
            config_bytes = base64.b64decode(
                encoded,
                validate=True
            )
            config_text = config_bytes.decode("utf-8", errors="replace")
        except Exception:
            continue

        if "client" not in config_text:
            continue

        filename = safe_name(f"{host}_{ip}") + ".ovpn"
        config_path = CONFIGS / filename
        config_path.write_text(config_text, encoding="utf-8")

        try:
            ping_ms = int(row.get("Ping", "0"))
        except (ValueError, TypeError):
            ping_ms = None

        try:
            speed_bps = int(row.get("Speed", "0"))
        except (ValueError, TypeError):
            speed_bps = None

        try:
            sessions = int(row.get("NumVpnSessions", "0"))
        except (ValueError, TypeError):
            sessions = None

        servers.append({
            "id": hashlib.sha256(
                f"{host}|{ip}".encode()
            ).hexdigest()[:16],
            "source": "VPN Gate",
            "hostname": host,
            "ip": ip,
            "country": row.get("CountryLong", ""),
            "country_code": row.get("CountryShort", ""),
            "ping_ms_source": ping_ms,
            "speed_bps_source": speed_bps,
            "sessions_reported": sessions,
            "config_path": f"configs/vpngate/{filename}",
            "config_sha256": hashlib.sha256(
                config_bytes
            ).hexdigest()
        })

    if not servers:
        raise RuntimeError(
            "No valid VPN Gate configs found; refusing to publish empty data"
        )

    write_json(DATA / "vpngate.json", {
        "source": "VPN Gate",
        "updated_at": now_utc(),
        "servers": servers
    })

    print(f"VPN Gate: saved {len(servers)} servers")


def main():
    DATA.mkdir(parents=True, exist_ok=True)

    # Fail safely if VPN Gate cannot be updated.
    update_vpngate()

    # VPNBook is intentionally not overwritten until its current
    # download links and config format have been validated.
    vpnbook_path = DATA / "vpnbook.json"
    if not vpnbook_path.exists():
        write_json(vpnbook_path, {
            "source": "VPNBook",
            "updated_at": None,
            "servers": []
        })

    manifest = {
        "app": "GVPN",
        "schema_version": 1,
        "updated_at": now_utc(),
        "sources": {
            "vpngate": "data/vpngate.json",
            "vpnbook": "data/vpnbook.json"
        }
    }
    write_json(DATA / "manifest.json", manifest)


if __name__ == "__main__":
    main()

