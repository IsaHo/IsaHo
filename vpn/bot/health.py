"""Synthetic end-to-end path probes and the Telegram health dashboard."""

import asyncio
import html
import json
import logging
import os
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import db
import handlers as h
import healthdb
import links
import nodes
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message
from config import cfg

router = Router()
log = logging.getLogger(__name__)
TEST_URL = "https://www.gstatic.com/generate_204"
INTERVAL = 5 * 60
FRESH = 15 * 60
_lock = asyncio.Lock()
_relay_jobs: dict[str, float] = {}
_force_relays: set[str] = set()


@dataclass
class Spec:
    key: str
    label: str
    kind: str
    mode: str
    target: str
    outbound: dict | None = None
    url: str = ""


def _reality(address: str, port: int, uuid: str) -> dict:
    return {
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": address,
                    "port": port,
                    "users": [
                        {"id": uuid, "encryption": "none", "flow": "xtls-rprx-vision"}
                    ],
                }
            ]
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "serverName": cfg.reality_sni,
                "fingerprint": "chrome",
                "publicKey": cfg.reality_public_key,
                "shortId": cfg.reality_short_id,
            },
        },
    }


def _cdn(uuid: str) -> dict:
    return {
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": links.cdn_address(),
                    "port": links.cdn_public_port(),
                    "users": [{"id": uuid, "encryption": "none"}],
                }
            ]
        },
        "streamSettings": {
            "network": "xhttp",
            "security": "tls",
            "tlsSettings": {
                "serverName": cfg.domain,
                "fingerprint": "chrome",
                "alpn": ["h2", "http/1.1"],
            },
            "xhttpSettings": {
                "host": cfg.domain,
                "path": cfg.cdn_path,
                "mode": "packet-up",
            },
        },
    }


def build_specs(user) -> list[Spec]:
    specs = []
    if cfg.server_ip and cfg.reality_public_key:
        specs.append(
            Spec(
                "vpn:main",
                "Reality سرور اصلی",
                "vpn",
                "xray",
                f"{cfg.server_ip}:{cfg.reality_port}",
                _reality(cfg.server_ip, cfg.reality_port, user.uuid),
            )
        )
    if cfg.domain:
        specs += [
            Spec(
                "vpn:cdn",
                "مسیر پشتیبان CDN",
                "cdn",
                "xray",
                cfg.domain,
                _cdn(user.uuid),
            ),
            Spec(
                "sub:cdn",
                "سابسکریپشن Cloudflare",
                "sub",
                "http",
                cfg.domain,
                url=links.cdn_sub_url(user),
            ),
        ]
    return specs


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _client_config(port: int, outbound: dict) -> dict:
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks"}],
        "outbounds": [outbound],
    }


def _xray_check(spec: Spec) -> tuple[bool, int, str]:
    port = _free_port()
    fd, path = tempfile.mkstemp(suffix=".json")
    process = None
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(_client_config(port, spec.outbound), stream)
        process = subprocess.Popen(  # noqa: S603 -- trusted configured executable
            [cfg.xray_bin, "run", "-c", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        time.sleep(1.2)
        result = subprocess.run(  # noqa: S603 -- fixed executable and no shell
            [
                "/usr/bin/curl",
                "-sS",
                "--connect-timeout",
                "5",
                "-m",
                "12",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code} %{time_total}",
                "--socks5-hostname",
                f"127.0.0.1:{port}",
                TEST_URL,
            ],
            capture_output=True,
            text=True,
            env={
                key: value
                for key, value in os.environ.items()
                if "proxy" not in key.lower()
            },
            timeout=15,
        )
        raw = result.stdout.strip().split()
        code = raw[0] if raw else "000"
        latency = round(float(raw[1]) * 1000) if len(raw) > 1 else 0
        ok = result.returncode == 0 and code == "204"
        detail = f"HTTP {code}" if ok else (result.stderr.strip() or f"HTTP {code}")
        return ok, latency, detail[:300]
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, 0, type(exc).__name__
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        try:
            os.unlink(path)
        except OSError:
            pass


def _http_check(spec: Spec) -> tuple[bool, int, str]:
    start = time.monotonic()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if not spec.url.startswith(("http://", "https://")):
        return False, 0, "invalid URL scheme"
    request = urllib.request.Request(  # noqa: S310 -- scheme restricted above
        spec.url, headers={"User-Agent": "v2rayNG/1.9"}
    )
    try:
        with opener.open(request, timeout=12) as response:
            response.read(32)
            latency = round((time.monotonic() - start) * 1000)
            return response.status == 200, latency, f"HTTP {response.status}"
    except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
        return False, round((time.monotonic() - start) * 1000), type(exc).__name__


def _run_specs(specs: list[Spec]) -> list[healthdb.Check]:
    rows = []
    for spec in specs:
        ok, latency, detail = (
            _xray_check(spec) if spec.mode == "xray" else _http_check(spec)
        )
        rows.append(
            healthdb.add(spec.key, spec.label, spec.kind, "main", ok, latency, detail)
        )
    return rows


async def run_local_checks() -> list[healthdb.Check]:
    if _lock.locked():
        return []
    async with _lock:
        users = db.active_users()
        if not users:
            return []
        return await asyncio.to_thread(_run_specs, build_specs(users[0]))


def _relay_label(ip: str) -> str:
    for index, (host, _) in enumerate(links.relays(), 1):
        if host == ip:
            return f"IR{index}"
    return ip


def ingest_relay_result(data: dict) -> None:
    ip = str(data.get("ip") or "")[:64]
    result = data.get("probe_result")
    configured = {host for host, _ in links.relays()}
    if not ip or ip not in configured or not isinstance(result, dict):
        return
    label = _relay_label(ip)
    for name, title, kind in (
        ("vpn", f"{label} · اتصال واقعی از ایران", "relay"),
        ("sub", f"{label} · دریافت سابسکریپشن", "sub"),
    ):
        item = result.get(name)
        if not isinstance(item, dict) or not isinstance(item.get("ok"), bool):
            continue
        healthdb.add(
            f"relay:{ip}:{name}",
            title,
            kind,
            "iran",
            item["ok"],
            int(item.get("latency_ms") or 0),
            str(item.get("detail") or "")[:300],
            int(result.get("checked_at") or time.time()),
        )
    node_results = result.get("nodes")
    configured_nodes = {str(node["name"]): node for node in nodes.all_nodes()}
    if isinstance(node_results, dict):
        for node_name, item in node_results.items():
            node = configured_nodes.get(str(node_name))
            if (
                not node
                or not isinstance(item, dict)
                or not isinstance(item.get("ok"), bool)
            ):
                continue
            healthdb.add(
                f"relay:{ip}:node:{node_name}",
                f"{label} → نود {node_name}",
                "node",
                "iran",
                item["ok"],
                int(item.get("latency_ms") or 0),
                str(item.get("detail") or "")[:300],
                int(result.get("checked_at") or time.time()),
            )


def relay_job(data: dict) -> dict | None:
    """Return a private one-shot probe job in a relay's local-only report response."""
    ingest_relay_result(data)
    ip = str(data.get("ip") or "")[:64]
    now = time.time()
    configured = {host for host, _ in links.relays()}
    if ip not in configured or (
        ip not in _force_relays and now - _relay_jobs.get(ip, 0) < INTERVAL
    ):
        return None
    users = db.active_users()
    if not users:
        return None
    _relay_jobs[ip] = now
    _force_relays.discard(ip)
    user = users[0]
    return {
        "id": f"{int(now)}-{_relay_label(ip)}",
        "link": links.reality_link(user, "127.0.0.1", 443, "health-check"),
        "sub_url": f"http://127.0.0.1:2096/sub/{user.sub_token}",
        "nodes": [
            {
                "name": str(node["name"]),
                "link": links.reality_link(
                    user, str(node["ip"]), 8443, f"health-{node['name']}"
                ),
            }
            for node in nodes.all_nodes()
            if node.get("private", True)
        ],
    }


def request_relay_checks() -> int:
    hosts = {host for host, _ in links.relays()}
    _force_relays.update(hosts)
    return len(hosts)


def _status(row: healthdb.Check, now: int | None = None) -> tuple[str, str]:
    age = (now or int(time.time())) - row.checked_at
    if age > FRESH:
        return "⚪️", "قدیمی"
    if not row.ok:
        return "🔴", "قطع"
    if row.latency_ms > 1500:
        return "🟠", "کند"
    if row.latency_ms > 800:
        return "🟡", "نسبتاً کند"
    return "🟢", "سالم"


def _age(stamp: int) -> str:
    seconds = max(0, int(time.time()) - stamp)
    if seconds < 60:
        return "همین حالا"
    if seconds < 3600:
        return f"{seconds // 60} دقیقه پیش"
    if seconds < 86400:
        return f"{seconds // 3600} ساعت پیش"
    return f"{seconds // 86400} روز پیش"


def _grade(value: int | None) -> tuple[str, str]:
    if value is None:
        return "⚪️", "هنوز داده‌ای نداریم"
    if value >= 90:
        return "🟢", "عالی"
    if value >= 75:
        return "🟡", "پایدار"
    if value >= 50:
        return "🟠", "نیازمند بررسی"
    return "🔴", "بحرانی"


def _expected_paths() -> dict[str, tuple[str, str]]:
    users = db.active_users()
    paths = (
        {spec.key: (spec.label, spec.kind) for spec in build_specs(users[0])}
        if users
        else {}
    )
    for index, (host, _) in enumerate(links.relays(), 1):
        paths[f"relay:{host}:vpn"] = (f"IR{index} · اتصال واقعی از ایران", "relay")
        paths[f"relay:{host}:sub"] = (f"IR{index} · دریافت سابسکریپشن", "sub")
        for node in nodes.all_nodes():
            if node.get("private", True):
                paths[f"relay:{host}:node:{node['name']}"] = (
                    f"IR{index} → نود {node['name']}",
                    "node",
                )
    return paths


def current_checks() -> list[healthdb.Check]:
    expected = _expected_paths()
    return [row for row in healthdb.latest() if row.path_key in expected]


def overview() -> tuple[str, object]:
    rows = current_checks()
    expected = _expected_paths()
    present = {row.path_key for row in rows}
    missing = [(key, *meta) for key, meta in expected.items() if key not in present]
    value = (
        round(sum(healthdb.path_score(row) for row in rows) / len(expected))
        if expected and rows
        else None
    )
    icon, grade = _grade(value)
    if not rows:
        text = (
            "🧭 <b>مرکز سلامت مسیرها</b>\n\n"
            "هنوز آزمایشی ثبت نشده است. «آزمایش همین حالا» را بزنید؛ "
            "نتیجه سرورهای ایران پس از گزارش بعدی ایجنت کامل می‌شود."
        )
    else:
        healthy = sum(
            1 for row in rows if row.ok and time.time() - row.checked_at <= FRESH
        )
        score_line = (
            f"⚪️ امتیاز کل: <b>در حال تکمیل · {len(missing)} نتیجه باقی مانده</b>"
            if missing
            else f"{icon} امتیاز کل: <b>{value}/100 · {grade}</b>"
        )
        lines = [
            "🧭 <b>مرکز سلامت مسیرها</b>",
            "",
            score_line,
            f"مسیرهای تازه و سالم: <b>{healthy} از {len(expected)}</b>",
            "",
            "<b>آخرین وضعیت مسیرها</b>",
        ]
        for row in rows:
            state_icon, state = _status(row)
            latency = f" · {row.latency_ms}ms" if row.latency_ms else ""
            lines.append(
                f"{state_icon} {html.escape(row.label)} — {state}{latency} · {_age(row.checked_at)}"
            )
        for _, label, _ in missing:
            lines.append(f"⚪️ {html.escape(label)} — منتظر اولین تست")
        lines += [
            "",
            "ℹ️ تست‌های IR از داخل همان سرور ایران اجرا می‌شوند؛ بقیه از سرور اصلی.",
        ]
        text = "\n".join(lines)
    buttons = [[("🔬 آزمایش همین حالا", "ph:run"), ("🔄 تازه‌سازی", "ph:menu")]]
    if rows:
        buttons.append([("📈 تاریخچه مسیرها", "ph:history")])
        detail = [
            (f"{_status(row)[0]} {row.label[:24]}", f"ph:path:{row.id}") for row in rows
        ]
        buttons += [detail[index : index + 2] for index in range(0, len(detail), 2)]
    return text, h.ikb(buttons)


def support_summary(category: str) -> tuple[str, bool]:
    kinds = {"sub"} if category == "subscription" else {"vpn", "cdn", "node", "relay"}
    expected = {key for key, (_, kind) in _expected_paths().items() if kind in kinds}
    rows = [
        row
        for row in current_checks()
        if row.kind in kinds and time.time() - row.checked_at <= FRESH
    ]
    if not rows:
        return "⚪️ هنوز تست واقعی تازه‌ای برای این بخش ثبت نشده است.", False
    good = [row for row in rows if row.ok]
    slow = [row for row in good if row.latency_ms > 1500]
    missing = len(expected) - len(rows)
    if not good:
        return (
            f"🔴 تست واقعی مسیرها: <b>هر {len(rows)} مسیر در آخرین بررسی ناموفق بوده‌اند.</b>",
            True,
        )
    if len(good) < len(rows):
        return (
            f"🟠 تست واقعی مسیرها: <b>{len(good)} از {len(rows)} مسیر سالم است.</b>",
            False,
        )
    if missing:
        return (
            f"🟡 تست واقعی مسیرها: <b>{len(good)} مسیر سالم</b> و {missing} مسیر منتظر نتیجه است.",
            False,
        )
    if slow:
        return (
            f"🟡 همه مسیرها وصل‌اند، اما <b>{len(slow)} مسیر کند</b> گزارش شده است.",
            False,
        )
    return f"🟢 تست واقعی مسیرها: <b>هر {len(rows)} مسیر سالم است.</b>", False


async def _notify(bot: Bot, text: str) -> None:
    for admin_id in db.admin_ids():
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            log.warning("could not send path-health alert to %s", admin_id)


async def evaluate_alerts(bot: Bot) -> None:
    expected = _expected_paths()
    for row in healthdb.latest():
        if row.path_key not in expected:
            continue
        state_key = f"health-alert:{row.path_key}"
        state = db.get_setting(state_key)
        if not row.ok and healthdb.failures(row.path_key, 2) >= 2 and state != "down":
            db.set_setting(state_key, "down")
            await _notify(
                bot,
                f"🔴 <b>هشدار مسیر</b>\n{html.escape(row.label)} در دو تست متوالی ناموفق بود.",
            )
        elif row.ok and state == "down":
            db.set_setting(state_key, "")
            await _notify(
                bot,
                f"🟢 <b>بازیابی مسیر</b>\n{html.escape(row.label)} دوباره سالم است ({row.latency_ms}ms).",
            )


async def monitor(bot: Bot) -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await run_local_checks()
            await evaluate_alerts(bot)
            healthdb.prune()
        except Exception:
            log.exception("path-health monitor failed")
        await asyncio.sleep(INTERVAL)


@router.message(F.text == h.BTN_HEALTH, h.admin)
async def health_menu(msg: Message):
    text, keyboard = overview()
    await msg.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "ph:menu", h.admin)
async def health_menu_cb(cb: CallbackQuery):
    await cb.answer()
    text, keyboard = overview()
    await cb.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data == "ph:run", h.admin)
async def health_run(cb: CallbackQuery):
    if _lock.locked():
        await cb.answer("یک آزمایش در حال اجراست", show_alert=True)
        return
    await cb.answer("آزمایش مسیرها شروع شد")
    await cb.message.edit_text(
        "⏳ <b>در حال آزمایش واقعی مسیرها…</b>\nاین کار ممکن است حدود یک دقیقه طول بکشد."
    )
    request_relay_checks()
    rows = await run_local_checks()
    text, keyboard = overview()
    suffix = (
        "\n\n✅ تست‌های سرور اصلی ثبت شد. نتیجه سرورهای ایران حداکثر تا گزارش بعدی ایجنت "
        "به‌روز می‌شود."
        if rows
        else "\n\n⚠️ برای آزمایش حداقل یک کاربر فعال لازم است."
    )
    await cb.message.edit_text(text + suffix, reply_markup=keyboard)


@router.callback_query(F.data == "ph:history", h.admin)
async def health_history(cb: CallbackQuery):
    await cb.answer()
    rows = current_checks()
    lines = ["📈 <b>روند آخر مسیرها</b>"]
    for row in rows:
        history = healthdb.history(row.path_key, 8)
        marks = "".join("●" if item.ok else "×" for item in reversed(history))
        successful = [item.latency_ms for item in history if item.ok]
        average = (
            f"میانگین {round(sum(successful) / len(successful))}ms"
            if successful
            else "بدون تست موفق"
        )
        lines.append(f"\n{html.escape(row.label)}\n<code>{marks}</code> · {average}")
    lines.append("\nراهنما: ● موفق  × ناموفق")
    await cb.message.edit_text(
        "\n".join(lines), reply_markup=h.ikb([[("↩️ بازگشت", "ph:menu")]])
    )


@router.callback_query(F.data.startswith("ph:path:"), h.admin)
async def health_path(cb: CallbackQuery):
    await cb.answer()
    row = healthdb.get(int(cb.data.rsplit(":", 1)[1]))
    if not row:
        return
    history = healthdb.history(row.path_key, 12)
    lines = [
        f"🧪 <b>{html.escape(row.label)}</b>",
        "",
        f"نوع تست: {html.escape(row.kind)} · مبدأ: {'ایران' if row.origin == 'iran' else 'سرور اصلی'}",
    ]
    for item in history:
        icon, state = _status(item, item.checked_at)
        latency = f" · {item.latency_ms}ms" if item.latency_ms else ""
        lines.append(
            f"{icon} {_age(item.checked_at)} · {state}{latency} · {html.escape(item.detail)}"
        )
    await cb.message.edit_text(
        "\n".join(lines), reply_markup=h.ikb([[("↩️ سلامت مسیرها", "ph:menu")]])
    )
