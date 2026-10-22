# scripts/update_servers.py

import base64
import csv
import hashlib
import io
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OVPN_DIR = ROOT / "ovpn" / "vpngate"

API_URL = "https://www.vpngate.net/api/iphone/"
RAW_BASE = "https://raw.githubusercontent.com/pvKaLeed/GVPN/main"
TIMEOUT = 45
HEADERS = {"User-Agent": "GVPN-ServerUpdater/2.0"}


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def safe_name(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))
    return value.strip("._")[:100] or "server"


def write_json_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def parse_int(value):
    try:
        result = int(value)
        return result if result >= 0 else None
    except (TypeError, ValueError):
        return None


def validate_profile(config, expected_host, expected_ip):
    if len(config) < 100:
        raise ValueError("Profile is unexpectedly small")

    if not re.search(r"(?m)^\s*client\s*$", config):
        raise ValueError("Missing client directive")

    remotes = re.findall(
        r"(?m)^\s*remote\s+(\S+)(?:\s+\d+)?",
        config,
    )
    if not remotes:
        raise ValueError("Missing remote directive")

    if not any(
        host.lower() in {expected_host.lower(), expected_ip.lower()}
        for host in remotes
    ):
        raise ValueError(
            f"Profile remote does not match server: {remotes}"
        )

    # Reject directives that could execute external commands.
    forbidden = re.compile(
        r"(?im)^\s*(up|down|route-up|ipchange|plugin|"
        r"tls-verify|client-connect|client-disconnect)\s+"
    )
    if forbidden.search(config):
        raise ValueError("Profile contains a disallowed command directive")

    return config.rstrip() + "\n"


def update_vpngate():
    print("Fetching VPN Gate API...")

    response = requests.get(
        API_URL,
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
        raise RuntimeError("VPN Gate API CSV header not found")

    reader = csv.DictReader(
        io.StringIO("\n".join(
            [lines[header_index].lstrip("#")]
            + lines[header_index + 1:]
        ))
    )

    staged_profiles = {}
    servers = []

    for row in reader:
        hostname = (row.get("HostName") or "").strip()
        ip = (row.get("IP") or "").strip()
        encoded = (
            row.get("OpenVPN_ConfigData_Base64") or ""
        ).strip()

        if not hostname or not ip or not encoded:
            continue

        try:
            raw = base64.b64decode(encoded, validate=True)
            config = raw.decode("utf-8")
            config = validate_profile(config, hostname, ip)
        except (ValueError, UnicodeDecodeError) as exc:
            print(f"Skipping invalid profile {hostname}: {exc}")
            continue

        server_id = hashlib.sha256(
            f"{hostname}|{ip}".encode()
        ).hexdigest()[:16]

        filename = safe_name(f"{hostname}_{ip}") + ".ovpn"
        relative_path = f"ovpn/vpngate/{filename}"

        # VPN Gate's public OpenVPN instructions use vpn/vpn
        # when a server asks for credentials.
        auth_required = bool(
            re.search(r"(?im)^\s*auth-user-pass(?:\s|$)", config)
        )

        record = {
            "id": server_id,
            "source": "VPN Gate",
            "hostname": hostname,
            "ip": ip,
            "country": (row.get("CountryLong") or "").strip(),
            "country_code": (row.get("CountryShort") or "").strip(),
            "ping_ms_source": parse_int(row.get("Ping")),
            "speed_bps_source": parse_int(row.get("Speed")),
            "sessions_reported": parse_int(row.get("NumVpnSessions")),
            "uptime_seconds_source": parse_int(row.get("Uptime")),
            "config_path": relative_path,
            "config_url": f"{RAW_BASE}/{relative_path}",
            "config_sha256": hashlib.sha256(
                config.encode("utf-8")
            ).hexdigest(),
            "auth_required": auth_required,
            "auth": (
                {"username": "vpn", "password": "vpn"}
                if auth_required else None
            ),
            "validated_at": now_utc(),
            "validation": "syntax_checked",
        }

        staged_profiles[filename] = config
        servers.append(record)

    if not servers:
        raise RuntimeError(
            "No valid VPN Gate profiles; existing data preserved"
        )

    # Stage profiles before replacing the existing set.
    stage_dir = OVPN_DIR.with_name("vpngate_staging")
    if stage_dir.exists():
        import shutil
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)

    try:
        for filename, config in staged_profiles.items():
            (stage_dir / filename).write_text(
                config, encoding="utf-8"
            )

        OVPN_DIR.mkdir(parents=True, exist_ok=True)

        for staged_file in stage_dir.glob("*.ovpn"):
            staged_file.replace(OVPN_DIR / staged_file.name)

        # Remove old profiles only after all new profiles were staged.
        expected = set(staged_profiles)
        for old_file in OVPN_DIR.glob("*.ovpn"):
            if old_file.name not in expected:
                old_file.unlink()

        payload = {
            "schema_version": 2,
            "source": "VPN Gate",
            "source_url": API_URL,
            "updated_at": now_utc(),
            "server_count": len(servers),
            "servers": servers,
        }
        write_json_atomic(DATA_DIR / "vpngate.json", payload)

    finally:
        import shutil
        shutil.rmtree(stage_dir, ignore_errors=True)

    print(f"VPN Gate: saved {len(servers)} validated profiles")


def main():
    update_vpngate()

    # Keep VPNBook updating in the same scheduled workflow.
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "update_vpnbook.py")],
        check=True,
        cwd=ROOT,
    )


if __name__ == "__main__":
    main()
