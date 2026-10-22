
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
        number = int(value)
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def validate_profile(config, hostname, ip):
    if len(config) < 100:
        raise ValueError("Profile is too small")

    if not re.search(r"(?m)^\s*client\s*$", config):
        raise ValueError("Missing client directive")

    remotes = re.findall(
        r"(?m)^\s*remote\s+(\S+)",
        config,
    )
    if not remotes:
        raise ValueError("Missing remote directive")

    expected = {hostname.lower(), ip.lower()}
    if not any(remote.lower() in expected for remote in remotes):
        raise ValueError(f"Unexpected remote: {remotes}")

    # Do not accept profiles containing external command hooks.
    forbidden = re.compile(
        r"(?im)^\s*(up|down|route-up|ipchange|plugin|"
        r"client-connect|client-disconnect)\s+"
    )
    if forbidden.search(config):
        raise ValueError("Profile contains a disallowed command")

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
        raise RuntimeError("VPN Gate CSV header not found")

    csv_text = "\n".join(
        [lines[header_index].lstrip("#")]
        + lines[header_index + 1:]
    )
    reader = csv.DictReader(io.StringIO(csv_text))

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
            config = validate_profile(
                raw.decode("utf-8"),
                hostname,
                ip,
            )
        except (ValueError, UnicodeDecodeError) as exc:
            print(f"Skipping {hostname}: {exc}")
            continue

        filename = safe_name(f"{hostname}_{ip}") + ".ovpn"
        relative_path = f"ovpn/vpngate/{filename}"
        config_bytes = config.encode("utf-8")

        auth_required = bool(
            re.search(
                r"(?im)^\s*auth-user-pass(?:\s|$)",
                config,
            )
        )

        server_id = hashlib.sha256(
            f"{hostname}|{ip}".encode("utf-8")
        ).hexdigest()[:16]

        servers.append({
            "id": server_id,
            "source": "VPN Gate",
            "hostname": hostname,
            "ip": ip,
            "country": (row.get("CountryLong") or "").strip(),
            "country_code": (row.get("CountryShort") or "").strip(),
            "ping_ms_source": parse_int(row.get("Ping")),
            "speed_bps_source": parse_int(row.get("Speed")),
            "sessions_reported": parse_int(
                row.get("NumVpnSessions")
            ),
            "config_path": relative_path,
            "config_url": f"{RAW_BASE}/{relative_path}",
            "config_sha256": hashlib.sha256(
                config_bytes
            ).hexdigest(),
            "auth_required": auth_required,
            "auth": (
                {"username": "vpn", "password": "vpn"}
                if auth_required else None
            ),
            "validated_at": now_utc(),
            "validation": "syntax_checked",
        })
        staged_profiles[filename] = config_bytes

    if not servers:
        raise RuntimeError(
            "No valid VPN Gate profiles found; existing data preserved"
        )

    # Stage all profiles first. Do not clear the current profiles
    # until the new set has been prepared successfully.
    stage_dir = OVPN_DIR.with_name("vpngate_staging")
    import shutil
    shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True)

    try:
        for filename, content in staged_profiles.items():
            (stage_dir / filename).write_bytes(content)

        OVPN_DIR.mkdir(parents=True, exist_ok=True)

        for staged_file in stage_dir.glob("*.ovpn"):
            staged_file.replace(OVPN_DIR / staged_file.name)

        expected_files = set(staged_profiles)
        for old_file in OVPN_DIR.glob("*.ovpn"):
            if old_file.name not in expected_files:
                old_file.unlink()

        write_json_atomic(DATA_DIR / "vpngate.json", {
            "schema_version": 2,
            "source": "VPN Gate",
            "source_url": API_URL,
            "updated_at": now_utc(),
            "server_count": len(servers),
            "servers": servers,
        })
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    print(f"VPN Gate: saved {len(servers)} profiles")


def main():
    update_vpngate()

    # Update VPNBook in the same GitHub Actions run.
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "update_vpnbook.py"),
        ],
        check=True,
        cwd=ROOT,
    )


if __name__ == "__main__":
    main()
