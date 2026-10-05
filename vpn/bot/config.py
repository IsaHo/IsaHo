"""Settings loaded from /etc/isaho-vpn/vpn.env (written by install.sh)."""
import os
from dataclasses import dataclass

ENV_FILE = os.environ.get("ISAHO_ENV", "/etc/isaho-vpn/vpn.env")


def _load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(ENV_FILE)


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Config:
    bot_token: str = _get("BOT_TOKEN")
    admin_ids: tuple = tuple(int(x) for x in _get("ADMIN_IDS").replace(" ", "").split(",") if x)
    brand: str = _get("BRAND", "IsaHo")

    server_ip: str = _get("SERVER_IP")
    domain: str = _get("DOMAIN")

    reality_port: int = int(_get("REALITY_PORT", "443"))
    reality_sni: str = _get("REALITY_SNI", "www.speedtest.net")
    reality_dest: str = _get("REALITY_DEST", "www.speedtest.net:443")
    reality_private_key: str = _get("REALITY_PRIVATE_KEY")
    reality_public_key: str = _get("REALITY_PUBLIC_KEY")
    reality_short_id: str = _get("REALITY_SHORT_ID")

    cdn_port: int = int(_get("CDN_PORT", "2053"))
    cdn_path: str = _get("CDN_PATH", "/xh")
    sub_port: int = int(_get("SUB_PORT", "2096"))
    relay_sub_port: int = int(_get("RELAY_SUB_PORT", "2097"))
    ssh_port: int = int(_get("SSH_PORT", "22"))

    cert_file: str = _get("CERT_FILE", "/etc/isaho-vpn/cert.pem")
    key_file: str = _get("KEY_FILE", "/etc/isaho-vpn/key.pem")

    data_dir: str = _get("DATA_DIR", "/var/lib/isaho-vpn")
    xray_bin: str = _get("XRAY_BIN", "/usr/local/bin/xray")
    xray_config: str = _get("XRAY_CONFIG", "/usr/local/etc/xray/config.json")
    api_addr: str = _get("XRAY_API", "127.0.0.1:10085")

    stats_interval: int = int(_get("STATS_INTERVAL", "60"))

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "isaho.db")


cfg = Config()
