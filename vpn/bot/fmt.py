"""Formatting helpers (Persian UI)."""
import html
import time

import db


def size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.2f} {unit}" if unit != "B" else f"{b} B"
        b /= 1024


def remaining_days(u) -> str:
    if getattr(u, "pending_days", 0):
        return f"{u.pending_days} روز (شروع از اولین اتصال)"
    if not u.expire_at:
        return "نامحدود"
    left = u.expire_at - time.time()
    if left <= 0:
        return "منقضی شده"
    days, rem = divmod(int(left), db.DAY)
    return f"{days} روز و {rem // 3600} ساعت"


def status_icon(u) -> str:
    if getattr(u, "channel_blocked", 0):
        return "⏸"
    if not u.enabled:
        return "🔴"
    if (u.traffic_limit and u.used >= u.traffic_limit * 0.9) or (
            u.expire_at and u.expire_at - time.time() < 3 * db.DAY):
        return "🟡"
    return "🟢"


def day_chart(rows: list) -> list:
    """rows: [(YYYY-MM-DD, bytes)] newest first -> chart lines oldest first."""
    if not rows:
        return []
    peak = max(b for _, b in rows) or 1
    return [f"<code>{d[5:]}</code> {'▇' * max(1, round(b / peak * 10))} {size(b)}" for d, b in reversed(rows)]


def bar(used: int, total: int, width: int = 12) -> str:
    if not total:
        return ""
    ratio = min(used / total, 1)
    filled = round(ratio * width)
    return f"{'▰' * filled}{'▱' * (width - filled)} {ratio * 100:.0f}%"


def user_card(u) -> str:
    limit = size(u.traffic_limit) if u.traffic_limit else "نامحدود"
    lines = [
        f"{status_icon(u)} <b>{html.escape(u.name)}</b>",
        "",
        f"📦 مصرف: <b>{size(u.used)}</b> از {limit}",
    ]
    if u.traffic_limit:
        lines.append(f"   {bar(u.used, u.traffic_limit)}")
    lines += [
        f"   ⬆️ {size(u.up)}  ⬇️ {size(u.down)}",
        f"⏳ اعتبار: {remaining_days(u)}",
        f"📅 ساخته شده: {time.strftime('%Y-%m-%d', time.localtime(u.created_at))}",
    ]
    if not u.enabled and u.disabled_reason:
        reason = {"expired": "تاریخ انقضا رسید", "traffic": "حجم تمام شد", "manual": "دستی"}.get(
            u.disabled_reason, "استفاده روی دستگاه‌های زیاد (موقت)" if u.disabled_reason.startswith("iplimit:")
            else u.disabled_reason)
        lines.append(f"⛔ علت غیرفعال: {html.escape(reason)}")
    if getattr(u, "channel_blocked", 0):
        lines.append("⏸ دسترسی متوقف است؛ عضو کانال اطلاع‌رسانی شوید و «بررسی عضویت» را بزنید.")
    if u.tg_id:
        lines.append(f"👤 تلگرام: <code>{u.tg_id}</code>")
    if u.note:
        lines.append(f"📝 {html.escape(u.note)}")
    return "\n".join(lines)
