
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
        "https://www.vpngate.net/api/iphone/",
        headers={"User-Agent": "Mozilla/5.0 GVPN/1.0"},
        timeout=60
    )
    response.raise_for_status()
    response.encoding = "utf-8"

    lines = response.text.splitlines()

    # Find the real CSV header instead of assuming a fixed line number.
    header_index = next(
        (
            i for i, line in enumerate(lines)
            if line.lstrip("#").startswith("HostName,")
            or line.lstrip("#").startswith("HostName,IP,")
        ),
        None
    )

    if header_index is None:
        raise RuntimeError(
            "VPN Gate CSV header not found. "
            f"Response preview: {response.text[:300]!r}"
        )

    header = lines[header_index].lstrip("#")
    csv_text = "\n".join([header] + lines[header_index + 1:])
    reader = csv.DictReader(io.StringIO(csv_text))

    servers = []
    CONFIGS.mkdir(parents=True, exist_ok=True)

    for row in reader:
        # Ignore comments and non-server rows.
        host = (row.get("HostName") or "").strip()
        ip = (row.get("IP") or "").strip()
        encoded = (
            row.get("OpenVPN_ConfigData_Base64")
            or row.get("#OpenVPN_ConfigData_Base64")
            or ""
        ).strip()

        if not host or host.startswith("*") or not ip or not encoded:
            continue

        try:
            config_bytes = base64.b64decode(encoded, validate=True)
            config_text = config_bytes.decode("utf-8", errors="replace")
        except Exception:
            continue

        if not re.search(r"(?m)^\s*client\s*$", config_text):
            continue

        filename = safe_name(f"{host}_{ip}") + ".ovpn"
        (CONFIGS / filename).write_text(
            config_text, encoding="utf-8"
        )

        def parse_int(value):
            try:
                return int(value)
            except (ValueError, TypeError):
                return None

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
            "config_path": f"configs/vpngate/{filename}",
            "config_sha256": hashlib.sha256(
                config_bytes
            ).hexdigest()
        })

    if not servers:
        raise RuntimeError(
            "No valid VPN Gate configs found. "
            "Check the API response and CSV column names in Actions logs."
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

