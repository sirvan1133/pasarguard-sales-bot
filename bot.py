#!/usr/bin/env python3
import html
import json
import logging
import mimetypes
import os
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sales-bot")

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
ORDERS_PATH = BASE_DIR / "orders.json"
USERS_PATH = BASE_DIR / "users.json"
SETTINGS_PATH = BASE_DIR / "settings.json"
COUPONS_PATH = BASE_DIR / "coupons.json"
INVOICES_PATH = BASE_DIR / "invoices.json"
REMINDERS_PATH = BASE_DIR / "reminders.json"
META_PATH = BASE_DIR / "meta.json"

DEFAULT_GB_OPTIONS = [20, 30, 50, 100, 200, 400, 1000]
DEFAULT_MONTH_OPTIONS = [1, 2, 3, 6, 12]
BSC_USDT_CONTRACT = "0x55d398326f99059ff775485246999027b3197955"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BSC_RPC_URLS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed1.ninicoin.io",
    "https://bsc-rpc.publicnode.com",
]

orders_lock = threading.Lock()
settings_lock = threading.Lock()
users_lock = threading.Lock()
coupons_lock = threading.Lock()
rate_lock = threading.Lock()
rate_cache: dict = {"ts": 0.0, "rate": 0}
panel_lock = threading.Lock()
panel_token_cache: dict = {"token": "", "ts": 0.0}
user_states: dict[int, dict] = {}
bot_info_cache: dict = {"username": ""}

BTN_BUY = "🛒 خرید اشتراک"
BTN_RENEW = "🔄 تمدید اشتراک"
BTN_TRIAL = "🎁 تست رایگان"
BTN_MY_SUBS = "📦 اشتراک‌های من"
BTN_WALLET = "👛 کیف پول"
BTN_PRICES = "💰 تعرفه و قیمت روز"
BTN_CHECK = "📊 استعلام اشتراک"
BTN_REFERRAL = "🤝 دعوت دوستان"
BTN_SUPPORT = "👨‍💻 پشتیبانی"
BTN_HELP = "❓ راهنما"
BTN_ADMIN = "👑 پنل ادمین"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def get_admin_ids(env: dict[str, str]) -> set[int]:
    return {int(x.strip()) for x in env.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


def main_keyboard(user_id: int, env: dict[str, str]) -> dict:
    rows = [
        [{"text": BTN_BUY}, {"text": BTN_RENEW}],
        [{"text": BTN_TRIAL}, {"text": BTN_MY_SUBS}],
        [{"text": BTN_WALLET}, {"text": BTN_CHECK}],
        [{"text": BTN_PRICES}, {"text": BTN_REFERRAL}],
        [{"text": BTN_SUPPORT}, {"text": BTN_HELP}],
    ]
    if user_id in get_admin_ids(env):
        rows.append([{"text": BTN_ADMIN}])
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)


def track_user(user: dict, referred_by: int | None = None) -> None:
    if not user or "id" not in user:
        return
    uid = str(user["id"])
    now = int(time.time())
    with users_lock:
        users = load_json(USERS_PATH)
        if uid not in users:
            users[uid] = {
                "first_seen": now,
                "last_seen": now,
                "username": user.get("username") or "",
                "trial_used": False,
                "referred_by": referred_by if (referred_by and str(referred_by) != uid) else None,
                "referral_rewarded": False,
                "referrals_count": 0,
            }
        else:
            users[uid]["last_seen"] = now
            if user.get("username"):
                users[uid]["username"] = user["username"]
        save_json(USERS_PATH, users)


def get_usdt_toman_rate() -> int:
    now = time.time()
    with rate_lock:
        if rate_cache["rate"] > 0 and now - rate_cache["ts"] < 60:
            return int(rate_cache["rate"])

    rate = 0
    # 1. Nobitex (returns Rials -> divide by 10 for Tomans)
    try:
        req = urllib.request.Request("https://apiv2.nobitex.ir/v3/orderbook/USDTIRT", headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))
        irr = float(data.get("lastTradePrice") or 0)
        if irr > 100000:
            rate = int(round(irr / 10.0))
    except Exception as e:
        log.warning("Nobitex rate error: %s", e)

    # 2. Wallex fallback (returns Tomans)
    if rate <= 0:
        try:
            req = urllib.request.Request("https://api.wallex.ir/v1/markets", headers={"User-Agent": "Mozilla/5.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))
            tmn = float(data.get("result", {}).get("symbols", {}).get("USDTTMN", {}).get("stats", {}).get("lastPrice") or 0)
            if tmn > 10000:
                rate = int(round(tmn))
        except Exception as e:
            log.warning("Wallex rate error: %s", e)

    with rate_lock:
        if rate > 0:
            rate_cache["rate"] = rate
            rate_cache["ts"] = now
            return rate
        if rate_cache["rate"] > 0:
            return int(rate_cache["rate"])
    raise RuntimeError("دریافت نرخ لحظه‌ای تتر ناموفق بود")


def get_settings(env: dict[str, str]) -> dict:
    defaults = {
        "base_price_per_gb": 15000,
        "pct_under_threshold": 2.0,
        "pct_over_threshold": 1.0,
        "bulk_threshold_gb": 100,
        "renew_discount_pct": 10.0,
        "global_discount_pct": 0.0,
        "trial_enabled": True,
        "trial_gb": 1,
        "trial_hours": 24,
        "trial_cooldown_days": 7,
        "referral_reward_pct": 20.0,
        "reminder_days": 3,
        "reminder_pct": 10,
        "force_join_enabled": False,
        "force_join_channel": "",
        "max_pending_orders": 2,
        "card_instant_mode": "all",
        "card_number": env.get("CARD_NUMBER", "0000000000000000"),
        "card_owner": env.get("CARD_OWNER", "نام صاحب کارت"),
        "usdt_bep20": env.get("USDT_BEP20", "0x0000000000000000000000000000000000000000"),
        "panel_url": env.get("PANEL_URL", ""),
        "panel_user": env.get("PANEL_USER", ""),
        "panel_pass": env.get("PANEL_PASS", ""),
        "group_id": int(env.get("GROUP_ID", "1") or 1),
    }
    with settings_lock:
        saved = load_json(SETTINGS_PATH)
        merged = {**defaults, **{k: v for k, v in saved.items() if v is not None}}
        return merged


def update_setting(env: dict[str, str], key: str, value) -> dict:
    with settings_lock:
        cur = load_json(SETTINGS_PATH)
        cur[key] = value
        save_json(SETTINGS_PATH, cur)
    if key in ("panel_url", "panel_user", "panel_pass"):
        with panel_lock:
            panel_token_cache["token"] = ""
            panel_token_cache["ts"] = 0.0
    return get_settings(env)


def calc_price(
    gb: int,
    months: int,
    env: dict[str, str] | None = None,
    is_renew: bool = False,
    coupon_pct: float = 0.0,
) -> dict:
    cfg = get_settings(env or load_env())
    base_price = int(cfg["base_price_per_gb"])
    threshold = int(cfg["bulk_threshold_gb"])
    pct_under = float(cfg["pct_under_threshold"])
    pct_over = float(cfg["pct_over_threshold"])
    renew_pct = float(cfg["renew_discount_pct"]) if is_renew else 0.0
    global_pct = float(cfg.get("global_discount_pct", 0.0) or 0.0)
    total_discount_pct = min(90.0, renew_pct + global_pct + float(coupon_pct or 0.0))
    if months <= 1:
        surcharge_pct = 0.0
    elif gb >= threshold:
        surcharge_pct = (months - 1) * pct_over
    else:
        surcharge_pct = (months - 1) * pct_under
    per_gb = round(base_price * (1.0 + surcharge_pct / 100.0))
    raw_total = gb * per_gb
    total_toman = max(1000, round(raw_total * (1.0 - total_discount_pct / 100.0)))
    usdt_rate = get_usdt_toman_rate()
    usdt_amount = round(total_toman / usdt_rate, 2)
    return {
        "gb": gb,
        "months": months,
        "surcharge_pct": int(surcharge_pct) if surcharge_pct == int(surcharge_pct) else surcharge_pct,
        "discount_pct": int(renew_pct) if renew_pct == int(renew_pct) else renew_pct,
        "global_discount_pct": int(global_pct) if global_pct == int(global_pct) else global_pct,
        "coupon_pct": int(coupon_pct) if float(coupon_pct) == int(coupon_pct) else float(coupon_pct),
        "total_discount_pct": int(total_discount_pct) if total_discount_pct == int(total_discount_pct) else total_discount_pct,
        "per_gb": per_gb,
        "raw_total_toman": raw_total,
        "total_toman": total_toman,
        "usdt_rate": usdt_rate,
        "usdt_amount": usdt_amount,
    }


def generate_unique_card_amount(base_toman: int) -> int:
    """Add a 3-digit random suffix (101..999) unique across the last 48 hours."""
    cutoff = int(time.time()) - 48 * 3600
    with orders_lock:
        orders = load_json(ORDERS_PATH)
        invs = load_json(INVOICES_PATH)
        used = {
            int(o.get("total_toman") or 0)
            for o in orders.values()
            if int(o.get("created_at") or 0) >= cutoff and o.get("pay_method") == "card"
        }
        used.update(
            int(v.get("card_toman") or 0)
            for v in invs.values()
            if int(v.get("ts") or 0) >= cutoff and v.get("card_toman")
        )
    for _ in range(900):
        candidate = base_toman + (101 + secrets.randbelow(899))
        if candidate not in used:
            return candidate
    return base_toman + (101 + secrets.randbelow(899))


def save_invoice(data: dict) -> str:
    inv_id = secrets.token_hex(4).upper()
    with orders_lock:
        invs = load_json(INVOICES_PATH)
        if len(invs) > 500:
            oldest = sorted(invs.items(), key=lambda kv: kv[1].get("ts", 0))[:200]
            for k, _ in oldest:
                invs.pop(k, None)
        invs[inv_id] = {**data, "ts": int(time.time())}
        save_json(INVOICES_PATH, invs)
    return inv_id


def get_invoice(inv_id: str) -> dict | None:
    with orders_lock:
        return load_json(INVOICES_PATH).get(inv_id)


def update_invoice(inv_id: str, patch: dict) -> dict | None:
    with orders_lock:
        invs = load_json(INVOICES_PATH)
        if inv_id not in invs:
            return None
        invs[inv_id].update(patch)
        save_json(INVOICES_PATH, invs)
        return invs[inv_id]


def create_coupon(code: str, pct: float, max_uses: int = 1, owner_uid: int | None = None) -> dict:
    c = code.strip().upper()
    with coupons_lock:
        coupons = load_json(COUPONS_PATH)
        coupons[c] = {
            "code": c,
            "pct": float(pct),
            "max_uses": int(max_uses),
            "used_by": [],
            "owner_uid": owner_uid,
            "created_at": int(time.time()),
        }
        save_json(COUPONS_PATH, coupons)
        return coupons[c]


def validate_coupon(code: str, user_id: int) -> tuple[bool, str, float]:
    c = code.strip().upper()
    with coupons_lock:
        coupons = load_json(COUPONS_PATH)
        item = coupons.get(c)
        if not item:
            return False, "❌ کد تخفیف نامعتبر است.", 0.0
        used_by = item.get("used_by", [])
        if len(used_by) >= int(item.get("max_uses", 1)):
            return False, "❌ ظرفیت استفاده از این کد تخفیف تکمیل شده است.", 0.0
        if user_id in used_by:
            return False, "❌ شما قبلاً از این کد تخفیف استفاده کرده‌اید.", 0.0
        owner_uid = item.get("owner_uid")
        if owner_uid is not None and int(owner_uid) != user_id:
            return False, "❌ این کد تخفیف اختصاصی است و متعلق به حساب شما نیست.", 0.0
        return True, "", float(item["pct"])


def consume_coupon(code: str, user_id: int) -> None:
    if not code:
        return
    c = code.strip().upper()
    with coupons_lock:
        coupons = load_json(COUPONS_PATH)
        item = coupons.get(c)
        if item and user_id not in item.get("used_by", []):
            item.setdefault("used_by", []).append(user_id)
            save_json(COUPONS_PATH, coupons)


def verify_bsc_usdt_tx(tx_hash: str, expected_to: str, min_usdt: float) -> tuple[bool, str, float]:
    """Verify a BSC BEP20 USDT transfer via public BSC JSON-RPC nodes."""
    to_norm = expected_to.strip().lower().removeprefix("0x")
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionReceipt",
        "params": [tx_hash],
    }).encode("utf-8")
    receipt = None
    rpc_ok = False
    last_err = ""
    for rpc in BSC_RPC_URLS:
        try:
            req = urllib.request.Request(
                rpc,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "curl/8.5.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                res = json.loads(resp.read().decode("utf-8"))
            if "result" in res:
                rpc_ok = True
                receipt = res.get("result")
                break
        except Exception as e:
            last_err = str(e)
    if not receipt:
        reason = "pending" if rpc_ok else (last_err or "rpc_error")
        return False, f"تراکنش هنوز در شبکه یافت نشد یا در حال تایید است ({reason})", 0.0
    if receipt.get("status") != "0x1":
        return False, "تراکنش در بلاک‌چین ناموفق (Failed) بوده است", 0.0

    matched_amount = 0.0
    for lg in receipt.get("logs", []):
        addr = (lg.get("address") or "").lower()
        topics = lg.get("topics") or []
        if addr != BSC_USDT_CONTRACT or len(topics) < 3:
            continue
        if (topics[0] or "").lower() != TRANSFER_TOPIC:
            continue
        to_topic = (topics[2] or "").lower().removeprefix("0x")[-40:]
        if to_topic == to_norm:
            raw_val = int(lg.get("data") or "0x0", 16)
            matched_amount += raw_val / 1e18

    if matched_amount <= 0:
        return False, "انتقال USDT به آدرس ولت شما در این تراکنش پیدا نشد", 0.0
    if matched_amount + 0.02 < min_usdt:
        return False, f"مبلغ واریزی ({matched_amount:.2f} USDT) کمتر از مبلغ فاکتور ({min_usdt} USDT) است", matched_amount
    return True, "تایید شده روی شبکه BSC", round(matched_amount, 4)


def panel_get_token(env: dict[str, str], force_refresh: bool = False) -> str:
    now = time.time()
    cfg = get_settings(env)
    with panel_lock:
        if not force_refresh and panel_token_cache["token"] and now - panel_token_cache["ts"] < 1800:
            return panel_token_cache["token"]
        base = str(cfg["panel_url"]).rstrip("/")
        if not base or "your-panel-domain" in base:
            raise RuntimeError("آدرس پنل هنوز در پنل ادمین تنظیم نشده است")
        form = urllib.parse.urlencode({"username": str(cfg["panel_user"]), "password": str(cfg["panel_pass"])}).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/admin/token",
            data=form,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SalesBot/1.0)"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tok = data["access_token"]
        panel_token_cache["token"] = tok
        panel_token_cache["ts"] = now
        return tok


def panel_request(env: dict[str, str], path: str, method: str = "GET", body: dict | None = None) -> dict:
    base = str(get_settings(env)["panel_url"]).rstrip("/")
    for attempt in range(2):
        tok = panel_get_token(env, force_refresh=(attempt == 1))
        headers = {"Authorization": f"Bearer {tok}", "User-Agent": "Mozilla/5.0 (compatible; SalesBot/1.0)"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue
            raise


def create_panel_user(
    env: dict[str, str],
    order_id: str,
    buyer_id: int,
    gb: int,
    months: int,
    pay_method: str,
    hours: int | None = None,
    custom_prefix: str | None = None,
) -> dict:
    group_id = int(get_settings(env).get("group_id", 1))
    data_limit = gb * 1024 * 1024 * 1024
    duration_sec = (hours * 3600) if hours is not None else (months * 30 * 86400)
    expire = int(time.time()) + duration_sec
    base_uname = custom_prefix or f"u_{order_id.lower()}"
    for attempt in range(3):
        uname = base_uname if attempt == 0 else f"{base_uname}_{secrets.token_hex(2)}"
        payload = {
            "username": uname,
            "status": "active",
            "data_limit": data_limit,
            "expire": expire,
            "group_ids": [group_id],
            "note": f"tg:{buyer_id} order:{order_id} pay:{pay_method}",
        }
        try:
            return panel_request(env, "/api/user", method="POST", body=payload)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code in (400, 409) and "exist" in err_body.lower():
                continue
            raise RuntimeError(f"Panel API {e.code}: {err_body[:200]}")
    raise RuntimeError("ساخت نام کاربری یکتا در پنل ناموفق بود")


def resolve_user_for_renew(env: dict[str, str], text: str) -> dict:
    t = text.strip()
    m = re.search(r"/sub/([A-Za-z0-9._\-]+)", t)
    if m:
        sub_tok = m.group(1)
        base = str(get_settings(env)["panel_url"]).rstrip("/")
        req = urllib.request.Request(
            f"{base}/sub/{sub_tok}/info",
            headers={"User-Agent": "Mozilla/5.0 (compatible; SalesBot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        uname = info.get("username")
        if not uname:
            raise RuntimeError("نام کاربری از لینک ساب استخراج نشد")
        return panel_request(env, f"/api/user/{urllib.parse.quote(uname)}")
    return panel_request(env, f"/api/user/{urllib.parse.quote(t)}")


def renew_panel_user(env: dict[str, str], target_username: str, gb: int, months: int) -> dict:
    u = panel_request(env, f"/api/user/{urllib.parse.quote(target_username)}")
    cur_limit = int(u.get("data_limit") or 0)
    cur_used = int(u.get("used_traffic") or 0)
    remaining = max(0, cur_limit - cur_used) if cur_limit > 0 else 0
    new_limit = remaining + gb * 1024 * 1024 * 1024

    now = int(time.time())
    cur_exp_raw = u.get("expire")
    cur_exp_ts = now
    if isinstance(cur_exp_raw, (int, float)):
        cur_exp_ts = int(cur_exp_raw)
    elif isinstance(cur_exp_raw, str) and cur_exp_raw:
        try:
            cur_exp_ts = int(datetime.fromisoformat(cur_exp_raw.replace("Z", "+00:00")).timestamp())
        except Exception:
            cur_exp_ts = now
    new_expire = max(now, cur_exp_ts) + months * 30 * 86400

    panel_request(env, f"/api/user/{urllib.parse.quote(target_username)}/reset", method="POST")
    return panel_request(
        env,
        f"/api/user/{urllib.parse.quote(target_username)}",
        method="PUT",
        body={"data_limit": new_limit, "expire": new_expire, "status": "active"},
    )


def tg_api(token: str, method: str, payload: dict | None = None, timeout: int = 35) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tg_send_photo(token: str, chat_id: int, photo_path: Path, caption: str = "", reply_markup: dict | None = None) -> dict | None:
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []

    def add_field(name: str, val: str) -> None:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode("utf-8"))

    add_field("chat_id", str(chat_id))
    if caption:
        add_field("caption", caption)
        add_field("parse_mode", "HTML")
    if reply_markup is not None:
        add_field("reply_markup", json.dumps(reply_markup))

    mime = mimetypes.guess_type(photo_path.name)[0] or "image/png"
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"{photo_path.name}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n".encode("utf-8")
        + photo_path.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8")).get("result")


def send_message(token: str, chat_id: int, text: str, reply_markup: dict | None = None, env: dict[str, str] | None = None, user_id: int | None = None) -> dict | None:
    markup = reply_markup
    if markup is None and env is not None and user_id is not None:
        markup = main_keyboard(user_id, env)
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if markup is not None:
        payload["reply_markup"] = markup
    return tg_api(token, "sendMessage", payload, timeout=15).get("result")


def edit_message(token: str, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> None:
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        tg_api(token, "editMessageText", payload, timeout=15)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise


def tg_send_document(token: str, chat_id: int, doc_path: Path, caption: str = "") -> dict | None:
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []

    def add_field(name: str, val: str) -> None:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode("utf-8"))

    add_field("chat_id", str(chat_id))
    if caption:
        add_field("caption", caption)
        add_field("parse_mode", "HTML")

    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{doc_path.name}\"\r\n"
        f"Content-Type: application/zip\r\n\r\n".encode("utf-8")
        + doc_path.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8")).get("result")


def get_wallet_balance(user_id: int) -> int:
    with users_lock:
        return int(load_json(USERS_PATH).get(str(user_id), {}).get("balance_toman", 0))


def adjust_wallet_balance(user_id: int, delta: int) -> tuple[bool, int]:
    uid = str(user_id)
    with users_lock:
        users = load_json(USERS_PATH)
        u = users.setdefault(uid, {"first_seen": int(time.time()), "last_seen": int(time.time()), "username": ""})
        cur = int(u.get("balance_toman", 0))
        new_bal = cur + delta
        if new_bal < 0:
            return False, cur
        u["balance_toman"] = new_bal
        save_json(USERS_PATH, users)
        return True, new_bal


def send_connection_guide(token: str, chat_id: int, env: dict[str, str], user_id: int) -> None:
    txt = (
        "📱 <b>راهنمای سریع اتصال</b>\n\n"
        "۱) ابتدا یکی از برنامه‌های زیر را نصب کنید:\n"
        "• <b>اندروید:</b> <a href=\"https://github.com/2dust/v2rayNG/releases/latest\">v2rayNG</a> یا <a href=\"https://github.com/hiddify/hiddify-app/releases/latest\">Hiddify</a>\n"
        "• <b>آیفون (iOS):</b> <a href=\"https://apps.apple.com/app/streisand/id6450534064\">Streisand</a> یا <a href=\"https://apps.apple.com/app/happ-proxy-utility/id6504287215\">Happ</a>\n"
        "• <b>ویندوز:</b> <a href=\"https://github.com/hiddify/hiddify-app/releases/latest\">Hiddify</a> یا <a href=\"https://github.com/2dust/v2rayN/releases/latest\">v2rayN</a>\n\n"
        "۲) لینک سابسکریپشن ارسال‌شده را کپی کنید (یا QR کد را داخل برنامه اسکن کنید).\n"
        "۳) در برنامه گزینهٔ <b>Import from Clipboard / افزودن از کلیپ‌بورد</b> را بزنید، سپس <b>Update Subscription</b> کنید و دکمهٔ اتصال را بزنید."
    )
    send_message(token, chat_id, txt, env=env, user_id=user_id)


def check_force_join(token: str, env: dict[str, str], chat_id: int, user_id: int) -> bool:
    cfg = get_settings(env)
    if not cfg.get("force_join_enabled") or user_id in get_admin_ids(env):
        return True
    ch = str(cfg.get("force_join_channel") or "").strip()
    if not ch:
        return True
    if not ch.startswith("@") and not ch.lstrip("-").isdigit():
        ch = f"@{ch}"
    try:
        res = tg_api(token, "getChatMember", {"chat_id": ch, "user_id": user_id}, timeout=8).get("result", {})
        if res.get("status") in ("member", "administrator", "creator"):
            return True
    except Exception as e:
        log.warning("Force join check failed for %s: %s", ch, e)
        return True
    ch_url = f"https://t.me/{ch.lstrip('@')}" if ch.startswith("@") else "https://t.me"
    kb = {
        "inline_keyboard": [
            [{"text": "📢 عضویت در کانال", "url": ch_url}],
            [{"text": "✅ عضو شدم", "callback_data": "check_join"}],
        ]
    }
    send_message(
        token,
        chat_id,
        f"📢 <b>برای استفاده از این بخش ابتدا در کانال ما عضو شوید:</b>\n{html.escape(ch)}\n\nپس از عضویت روی <b>✅ عضو شدم</b> بزنید.",
        reply_markup=kb,
    )
    return False


def send_backup_zip(token: str, env: dict[str, str], targets: set[int] | None = None) -> int:
    files = [ORDERS_PATH, USERS_PATH, SETTINGS_PATH, COUPONS_PATH, REMINDERS_PATH, META_PATH]
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        zip_path = Path(tmp.name)
    sent = 0
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in files:
                if p.exists():
                    zf.write(p, arcname=p.name)
        now_tehran = (datetime.now(timezone.utc) + timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d %H:%M")
        caption = f"🗄 <b>بکاپ خودکار دیتابیس ربات</b>\n🕒 {now_tehran}"
        for adm_id in (targets or get_admin_ids(env)):
            try:
                tg_send_document(token, adm_id, zip_path, caption=caption)
                sent += 1
            except Exception as e:
                log.warning("Backup send failed for %s: %s", adm_id, e)
    finally:
        zip_path.unlink(missing_ok=True)
    return sent


def build_sales_report(start_ts: int, end_ts: int, title: str) -> str:
    orders = load_json(ORDERS_PATH)
    users = load_json(USERS_PATH)
    buys, renews, topups = 0, 0, 0
    total_tmn, total_usdt = 0, 0.0
    for o in orders.values():
        if o.get("status") != "approved":
            continue
        ts = int(o.get("created_at") or 0)
        if not (start_ts <= ts < end_ts):
            continue
        otype = o.get("type", "buy")
        if otype == "topup":
            topups += 1
        elif otype == "renew":
            renews += 1
        else:
            buys += 1
        total_tmn += int(o.get("total_toman") or 0)
        if o.get("pay_method") == "usdt":
            total_usdt += float(o.get("usdt_amount") or 0)
    trials = sum(1 for u in users.values() if start_ts <= int(u.get("last_trial_at") or 0) < end_ts)
    new_users = sum(1 for u in users.values() if start_ts <= int(u.get("first_seen") or 0) < end_ts)
    return (
        f"📊 <b>{title}</b>\n\n"
        f"👥 کاربران جدید: <b>{new_users}</b> | 🎁 تست رایگان: <b>{trials}</b>\n"
        f"🛒 خرید جدید: <b>{buys}</b> | 🔄 تمدید: <b>{renews}</b> | 👛 شارژ کیف‌پول: <b>{topups}</b>\n"
        f"💰 جمع فروش ثبت‌شده: <b>{total_tmn:,} تومان</b>\n"
        f"🪙 سهم پرداخت تتری: <code>{round(total_usdt, 2)} USDT</code>"
    )


def send_qr_with_sub(token: str, chat_id: int, sub_url: str, caption: str, env: dict[str, str], user_id: int) -> None:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(["qrencode", "-o", str(tmp_path), "-s", "10", "-m", "2", "--", sub_url], check=True, timeout=10)
        kb = {"inline_keyboard": [[{"text": "📱 آموزش اتصال", "callback_data": "conn_guide"}]]}
        tg_send_photo(token, chat_id, tmp_path, caption=caption, reply_markup=kb)
    finally:
        tmp_path.unlink(missing_ok=True)


def build_gb_keyboard(prefix: str = "buy") -> dict:
    rows = [
        [{"text": "20 گیگ", "callback_data": f"{prefix}_gb:20"}, {"text": "30 گیگ", "callback_data": f"{prefix}_gb:30"}, {"text": "50 گیگ", "callback_data": f"{prefix}_gb:50"}],
        [{"text": "100 گیگ", "callback_data": f"{prefix}_gb:100"}, {"text": "200 گیگ", "callback_data": f"{prefix}_gb:200"}, {"text": "400 گیگ", "callback_data": f"{prefix}_gb:400"}],
        [{"text": "1000 گیگ", "callback_data": f"{prefix}_gb:1000"}, {"text": "✏️ حجم دلخواه", "callback_data": f"{prefix}_gb:custom"}],
    ]
    return {"inline_keyboard": rows}


def build_month_keyboard(gb: int, env: dict[str, str] | None = None, prefix: str = "buy") -> dict:
    cfg = get_settings(env or load_env())
    threshold = int(cfg["bulk_threshold_gb"])
    pct = float(cfg["pct_over_threshold"]) if gb >= threshold else float(cfg["pct_under_threshold"])
    pct_s = int(pct) if pct == int(pct) else pct
    def fmt(m: int) -> str:
        val = max(0, m - 1) * pct
        return str(int(val) if val == int(val) else val)
    rows = [
        [{"text": "۱ ماهه (۰٪)", "callback_data": f"{prefix}_m:{gb}:1"}, {"text": f"۲ ماهه (+{fmt(2)}٪)", "callback_data": f"{prefix}_m:{gb}:2"}],
        [{"text": f"۳ ماهه (+{fmt(3)}٪)", "callback_data": f"{prefix}_m:{gb}:3"}, {"text": f"۶ ماهه (+{fmt(6)}٪)", "callback_data": f"{prefix}_m:{gb}:6"}],
        [{"text": f"۱۲ ماهه / ۱ سال (+{fmt(12)}٪)", "callback_data": f"{prefix}_m:{gb}:12"}],
        [{"text": f"✏️ مدت دلخواه ({pct_s}٪ هر ماه)", "callback_data": f"{prefix}_m:{gb}:custom"}, {"text": "🔙 تغییر حجم", "callback_data": f"{prefix}_restart"}],
    ]
    return {"inline_keyboard": rows}


def show_admin_panel(token: str, chat_id: int, env: dict[str, str], message_id: int | None = None) -> None:
    orders = load_json(ORDERS_PATH)
    users = load_json(USERS_PATH)
    coupons = load_json(COUPONS_PATH)
    pending = sum(1 for o in orders.values() if o.get("status") == "pending")
    approved = [o for o in orders.values() if o.get("status") == "approved"]
    total_tmn = sum(int(o.get("total_toman", 0)) for o in approved)
    cfg = get_settings(env)
    try:
        rate = get_usdt_toman_rate()
        rate_s = f"{rate:,} تومان"
    except Exception:
        rate_s = "خطا"
    trial_s = f"فعال ({cfg['trial_gb']}GB / {cfg['trial_hours']}h / هر {cfg['trial_cooldown_days']} روز)" if cfg.get("trial_enabled", True) else "غیرفعال"
    fj_s = f"فعال ({cfg['force_join_channel']})" if cfg.get("force_join_enabled") else "غیرفعال"
    cm_map = {"all": "فوری برای همه + بررسی بعدی", "trusted": "فوری فقط مشتریان قدیمی", "off": "تایید دستی"}
    cm_s = cm_map.get(str(cfg.get("card_instant_mode", "all")), "فوری برای همه + بررسی بعدی")
    txt = (
        "👑 <b>پنل مدیریت ربات فروش</b>\n\n"
        f"👥 کل کاربران: <b>{len(users)}</b> | 🎟 کدهای تخفیف: <b>{len(coupons)}</b>\n"
        f"⏳ سفارش‌های در انتظار: <b>{pending}</b> | ✅ تاییدشده: <b>{len(approved)}</b>\n"
        f"💰 فروش کل: <b>{total_tmn:,} تومان</b>\n"
        f"💵 نرخ لحظه‌ای تتر: <code>{rate_s}</code>\n\n"
        "⚙️ <b>تنظیمات فعلی:</b>\n"
        f"• قیمت پایه هر گیگ: <code>{int(cfg['base_price_per_gb']):,}</code> تومان\n"
        f"• سود ماهانه زیر {cfg['bulk_threshold_gb']}GB: <code>{cfg['pct_under_threshold']}٪</code> | {cfg['bulk_threshold_gb']}GB+: <code>{cfg['pct_over_threshold']}٪</code>\n"
        f"• تخفیف تمدید: <code>{cfg['renew_discount_pct']}٪</code> | کمپین همگانی: <code>{cfg['global_discount_pct']}٪</code> | رفرال: <code>{cfg['referral_reward_pct']}٪</code>\n"
        f"• تست رایگان: <code>{trial_s}</code>\n"
        f"• قفل کانال: <code>{html.escape(fj_s)}</code> | سقف سفارش در انتظار: <code>{cfg['max_pending_orders']}</code>\n"
        f"• حالت کارت‌به‌کارت: <code>{cm_s}</code>\n"
        f"• یادآور تمدید: <code>{cfg['reminder_days']} روز</code> مانده یا زیر <code>{cfg['reminder_pct']}٪</code> حجم\n"
        f"• کارت: <code>{html.escape(str(cfg['card_number']))}</code> ({html.escape(str(cfg['card_owner']))})\n"
        f"• تتر BEP20: <code>{html.escape(str(cfg['usdt_bep20']))}</code>\n"
        f"• پنل: <code>{html.escape(str(cfg['panel_url']))}</code> (یوزر: <code>{html.escape(str(cfg['panel_user']))}</code> | گروه پنل: <code>{cfg['group_id']}</code>)\n"
        "💡 <i>آیدی گروه پنل مشخص می‌کند کاربران جدید عضو کدام گروه PasarGuard شوند (در پنل‌های تک‌گروهه همان 1 است).</i>\n\n"
        "برای تغییر هر مورد روی دکمهٔ مربوطه بزنید:"
    )
    kb = {
        "inline_keyboard": [
            [{"text": "💵 قیمت هر گیگ", "callback_data": "adm_set:base_price_per_gb"}, {"text": "🎯 آستانه عمده", "callback_data": "adm_set:bulk_threshold_gb"}],
            [{"text": "📉 سود زیر آستانه (٪)", "callback_data": "adm_set:pct_under_threshold"}, {"text": "📈 سود بالای آستانه (٪)", "callback_data": "adm_set:pct_over_threshold"}],
            [{"text": "🎁 تخفیف تمدید (٪)", "callback_data": "adm_set:renew_discount_pct"}, {"text": "🏷 تخفیف همگانی (٪)", "callback_data": "adm_set:global_discount_pct"}],
            [{"text": "🤝 پاداش رفرال (٪)", "callback_data": "adm_set:referral_reward_pct"}, {"text": "🛡 سقف سفارش در انتظار", "callback_data": "adm_set:max_pending_orders"}],
            [{"text": "🎁 روشن/خاموش تست", "callback_data": "adm_toggle_trial"}, {"text": "📦 حجم تست (GB)", "callback_data": "adm_set:trial_gb"}],
            [{"text": "⏱ مدت تست (ساعت)", "callback_data": "adm_set:trial_hours"}, {"text": "📆 فاصله تست (روز)", "callback_data": "adm_set:trial_cooldown_days"}],
            [{"text": "⏳ روز یادآور انقضا", "callback_data": "adm_set:reminder_days"}, {"text": "📉 درصد حجم یادآور", "callback_data": "adm_set:reminder_pct"}],
            [{"text": "📢 روشن/خاموش قفل کانال", "callback_data": "adm_toggle_force_join"}, {"text": "📢 آیدی کانال قفل", "callback_data": "adm_set:force_join_channel"}],
            [{"text": "💳 تغییر شماره کارت", "callback_data": "adm_set:card_number"}, {"text": "👤 تغییر صاحب کارت", "callback_data": "adm_set:card_owner"}],
            [{"text": "🪙 آدرس تتر BEP20", "callback_data": "adm_set:usdt_bep20"}, {"text": "🎟 مدیریت کد تخفیف", "callback_data": "adm_coupons"}],
            [{"text": "🌐 آدرس پنل", "callback_data": "adm_set:panel_url"}, {"text": "👤 یوزر پنل", "callback_data": "adm_set:panel_user"}],
            [{"text": "🔑 رمز پنل", "callback_data": "adm_set:panel_pass"}, {"text": "👥 آیدی گروه پنل", "callback_data": "adm_set:group_id"}],
            [{"text": "🔍 مدیریت کاربر", "callback_data": "adm_find_user"}, {"text": "👛 شارژ دستی کیف‌پول", "callback_data": "adm_wallet"}],
            [{"text": "📢 پیام همگانی", "callback_data": "adm_broadcast"}, {"text": "📊 گزارش امروز", "callback_data": "adm_report_today"}],
            [{"text": "🗄 بکاپ فوری", "callback_data": "adm_backup"}, {"text": "🔄 آپدیت ربات از گیت‌هاب", "callback_data": "adm_update_bot"}],
            [{"text": "⚡ حالت کارت‌به‌کارت", "callback_data": "adm_cycle_card_mode"}, {"text": "🔄 بروزرسانی پنل", "callback_data": "adm_refresh"}],
        ]
    }
    if message_id is not None:
        edit_message(token, chat_id, message_id, txt, reply_markup=kb)
    else:
        send_message(token, chat_id, txt, reply_markup=kb)


def show_invoice(
    token: str,
    chat_id: int,
    gb: int,
    months: int,
    message_id: int | None = None,
    env: dict[str, str] | None = None,
    target_username: str = "",
    coupon_code: str = "",
    coupon_pct: float = 0.0,
    inv_id: str | None = None,
) -> None:
    is_renew = bool(target_username)
    p = calc_price(gb, months, env=env, is_renew=is_renew, coupon_pct=coupon_pct)
    if not inv_id:
        inv_id = save_invoice({
            "gb": gb,
            "months": months,
            "target_username": target_username,
            "coupon_code": coupon_code,
            "coupon_pct": coupon_pct,
        })
    title = f"🔄 <b>پیش‌فاکتور تمدید ({html.escape(target_username)})</b>" if is_renew else "🧾 <b>پیش‌فاکتور اشتراک جدید</b>"
    disc_lines = []
    if is_renew and p["discount_pct"] > 0:
        disc_lines.append(f"🎁 تخفیف تمدید: <b>{p['discount_pct']}٪</b>")
    if p["global_discount_pct"] > 0:
        disc_lines.append(f"🏷 تخفیف ویژه کمپین: <b>{p['global_discount_pct']}٪</b>")
    if coupon_code and p["coupon_pct"] > 0:
        disc_lines.append(f"🎟 کد تخفیف <code>{html.escape(coupon_code)}</code>: <b>{p['coupon_pct']}٪</b>")
    disc_block = ""
    if disc_lines:
        disc_block = "\n".join(disc_lines) + f"\n💸 جمع قبل از تخفیف: <s>{p['raw_total_toman']:,}</s> تومان\n"
    prefix = "ren" if is_renew else "buy"
    txt = (
        f"{title}\n\n"
        f"📦 حجم: <b>{p['gb']} گیگابایت</b>\n"
        f"⏳ مدت اعتبار: <b>{p['months']} ماه</b> ({p['months'] * 30} روز)\n"
        f"📈 افزایش مدت: <b>{p['surcharge_pct']}٪</b> (هر گیگ: <code>{p['per_gb']:,}</code> تومان)\n"
        f"{disc_block}"
        f"💵 نرخ لحظه‌ای تتر: <code>{p['usdt_rate']:,}</code> تومان\n\n"
        f"💳 <b>مبلغ قابل پرداخت (تومان):</b> <code>{p['total_toman']:,}</code> تومان\n"
        f"🪙 <b>مبلغ قابل پرداخت (تتر BEP20):</b> <code>{p['usdt_amount']}</code> USDT\n\n"
        "روش پرداخت را انتخاب کنید:"
    )
    bal = get_wallet_balance(chat_id)
    pay_rows = []
    if bal >= p["total_toman"]:
        pay_rows.append([{"text": f"👛 پرداخت آنی از کیف پول (موجودی: {bal:,} ت)", "callback_data": f"pay:wallet:{inv_id}"}])
    pay_rows.extend([
        [{"text": "💳 پرداخت کارت‌به‌کارت", "callback_data": f"pay:card:{inv_id}"}],
        [{"text": "🪙 پرداخت تتر (BEP20 - تایید خودکار)", "callback_data": f"pay:usdt:{inv_id}"}],
        [{"text": "🎟 اعمال کد تخفیف", "callback_data": f"coupon:{inv_id}"}],
        [{"text": "🔙 تغییر مدت", "callback_data": f"{prefix}_gb:{gb}"}],
    ])
    kb = {"inline_keyboard": pay_rows}
    if message_id is not None:
        edit_message(token, chat_id, message_id, txt, reply_markup=kb)
    else:
        send_message(token, chat_id, txt, reply_markup=kb)


def extract_tx_key(m: dict) -> str:
    txt = (m.get("text") or m.get("caption") or "").strip()
    if not txt:
        return ""
    match = re.search(r"0x[a-fA-F0-9]{64}", txt)
    if match:
        return match.group(0).lower()
    cleaned = re.sub(r"\s+", " ", txt)
    if len(cleaned) >= 8 and re.search(r"[0-9a-zA-Z]{6,}", cleaned):
        return cleaned.lower()
    return ""


def grant_referral_reward(token: str, env: dict[str, str], buyer_id: int) -> None:
    uid = str(buyer_id)
    with users_lock:
        users = load_json(USERS_PATH)
        u = users.get(uid)
        if not u or u.get("referral_rewarded") or not u.get("referred_by"):
            return
        ref_id = int(u["referred_by"])
        u["referral_rewarded"] = True
        ref_key = str(ref_id)
        if ref_key in users:
            users[ref_key]["referrals_count"] = int(users[ref_key].get("referrals_count", 0)) + 1
        save_json(USERS_PATH, users)

    cfg = get_settings(env)
    pct = float(cfg.get("referral_reward_pct", 20.0))
    if pct <= 0:
        return
    code = f"REF{secrets.token_hex(3).upper()}"
    create_coupon(code, pct, max_uses=1, owner_uid=ref_id)
    try:
        send_message(
            token,
            ref_id,
            f"🎉 <b>پاداش دعوت دوستان!</b>\n\n"
            f"یکی از دوستان شما با لینک دعوتتان خرید کرد.\n"
            f"🎁 کد تخفیف اختصاصی <b>{int(pct) if pct == int(pct) else pct}٪</b> برای شما:\n"
            f"<code>{code}</code>\n\n"
            "می‌توانید این کد را در پیش‌فاکتور خرید یا تمدید بعدی اعمال کنید.",
            env=env,
            user_id=ref_id,
        )
    except Exception as e:
        log.warning("Failed to notify referrer %s: %s", ref_id, e)


def fulfill_approved_order(token: str, env: dict[str, str], order_id: str, order: dict) -> str:
    if order.get("type") == "topup":
        amt = int(order["total_toman"])
        _, new_bal = adjust_wallet_balance(order["user_id"], amt)
        with orders_lock:
            orders = load_json(ORDERS_PATH)
            orders[order_id]["status"] = "approved"
            save_json(ORDERS_PATH, orders)
        send_message(
            token,
            order["user_id"],
            f"✅ <b>کیف پول شما شارژ شد!</b>\n\n"
            f"💵 مبلغ اضافه‌شده: <b>{amt:,} تومان</b>\n"
            f"👛 موجودی فعلی: <b>{new_bal:,} تومان</b>",
            env=env,
            user_id=order["user_id"],
        )
        return f"wallet+{amt}"

    panel_base = str(get_settings(env)["panel_url"]).rstrip("/")
    if order.get("type") == "renew" and order.get("target_username"):
        u = renew_panel_user(env, order["target_username"], order["gb"], order["months"])
        sub_path = u["subscription_url"]
        full_sub = f"{panel_base}{sub_path}"
        uname = u["username"]
        new_gb = round((u.get("data_limit") or 0) / (1024**3), 2)
        caption = (
            f"✅ <b>اشتراک شما تمدید شد!</b>\n\n"
            f"🆔 نام کاربری: <code>{html.escape(uname)}</code>\n"
            f"📦 حجم جدید کل: <b>{new_gb} گیگابایت</b>\n"
            f"⏳ مدت اضافه شده: <b>+{order['months']} ماه</b>\n\n"
            f"🔗 <b>لینک سابسکریپشن:</b>\n<code>{html.escape(full_sub)}</code>"
        )
    else:
        u = create_panel_user(env, order_id, order["user_id"], order["gb"], order["months"], order["pay_method"])
        sub_path = u["subscription_url"]
        full_sub = f"{panel_base}{sub_path}"
        uname = u["username"]
        caption = (
            f"🎉 <b>اشتراک شما آماده است!</b>\n\n"
            f"🆔 نام کاربری: <code>{html.escape(uname)}</code>\n"
            f"📦 حجم: <b>{order['gb']} گیگابایت</b>\n"
            f"⏳ مدت: <b>{order['months']} ماه</b>\n\n"
            f"🔗 <b>لینک سابسکریپشن (برای کپی لمس کنید):</b>\n<code>{html.escape(full_sub)}</code>"
        )

    with orders_lock:
        orders = load_json(ORDERS_PATH)
        orders[order_id]["status"] = "approved"
        orders[order_id]["panel_username"] = uname
        orders[order_id]["sub_url"] = full_sub
        save_json(ORDERS_PATH, orders)

    if order.get("coupon_code"):
        consume_coupon(order["coupon_code"], order["user_id"])

    send_qr_with_sub(token, order["user_id"], full_sub, caption, env, order["user_id"])
    grant_referral_reward(token, env, order["user_id"])
    return uname


def handle_receipt_submission(token: str, env: dict[str, str], m: dict, st: dict) -> None:
    user_id = m["from"]["id"]
    username = m["from"].get("username") or "-"
    is_topup = bool(st.get("is_topup"))
    gb = st.get("gb", 0)
    months = st.get("months", 0)
    pay_method = st["pay_method"]
    target_username = st.get("target_username", "")
    coupon_code = st.get("coupon_code", "")
    coupon_pct = float(st.get("coupon_pct", 0.0) or 0.0)
    is_renew = bool(target_username)
    tx_key = extract_tx_key(m)
    if is_topup:
        rate = get_usdt_toman_rate()
        amt_tmn = int(st.get("card_toman") or st["topup_toman"])
        p = {"total_toman": amt_tmn, "usdt_rate": rate, "usdt_amount": round(amt_tmn / rate, 2)}
    else:
        p = calc_price(gb, months, env=env, is_renew=is_renew, coupon_pct=coupon_pct)
        if pay_method == "card" and st.get("card_toman"):
            p["total_toman"] = int(st["card_toman"])
    order_id = secrets.token_hex(4).upper()

    with orders_lock:
        orders = load_json(ORDERS_PATH)
        max_pend = int(get_settings(env).get("max_pending_orders", 2))
        if user_id not in get_admin_ids(env):
            pend_cnt = sum(1 for o in orders.values() if o.get("user_id") == user_id and o.get("status") == "pending")
            if pend_cnt >= max_pend:
                send_message(
                    token,
                    m["chat"]["id"],
                    f"⏳ شما هم‌اکنون <b>{pend_cnt}</b> سفارش در انتظار تایید دارید. لطفاً تا بررسی مدیریت صبر کنید.",
                    env=env,
                    user_id=user_id,
                )
                return
        if tx_key:
            for existing_id, existing in orders.items():
                if existing.get("status") in ("pending", "processing", "approved") and existing.get("tx_key") == tx_key:
                    send_message(
                        token,
                        m["chat"]["id"],
                        f"❌ این هش تراکنش / کد پیگیری قبلاً در سفارش <code>#{existing_id}</code> ثبت شده و قابل استفادهٔ مجدد نیست.",
                        env=env,
                        user_id=user_id,
                    )
                    return

        order = {
            "id": order_id,
            "type": "topup" if is_topup else ("renew" if is_renew else "buy"),
            "target_username": target_username,
            "user_id": user_id,
            "tg_username": username,
            "gb": gb,
            "months": months,
            "pay_method": pay_method,
            "coupon_code": coupon_code,
            "coupon_pct": coupon_pct,
            "tx_key": tx_key,
            "total_toman": p["total_toman"],
            "usdt_amount": p["usdt_amount"],
            "usdt_rate": p["usdt_rate"],
            "status": "pending",
            "created_at": int(time.time()),
            "panel_username": target_username if is_renew else "",
            "sub_url": "",
        }
        orders[order_id] = order
        save_json(ORDERS_PATH, orders)

    # Instant card-to-card activation with post-audit when enabled
    cfg = get_settings(env)
    card_mode = str(cfg.get("card_instant_mode", "all"))
    if pay_method == "card" and card_mode in ("all", "trusted"):
        is_trusted = True
        if card_mode == "trusted":
            with orders_lock:
                is_trusted = any(
                    o.get("user_id") == user_id and o.get("status") == "approved" and o.get("id") != order_id
                    for o in load_json(ORDERS_PATH).values()
                )
        if is_trusted:
            with orders_lock:
                orders = load_json(ORDERS_PATH)
                orders[order_id]["status"] = "processing"
                orders[order_id]["audit_status"] = "pending_review"
                save_json(ORDERS_PATH, orders)
            try:
                uname = fulfill_approved_order(token, env, order_id, order)
                type_label = "👛 شارژ کیف پول" if is_topup else (f"🔄 تمدید (<code>{html.escape(target_username)}</code>)" if is_renew else "🛒 خرید جدید")
                plan_line = "📦 پلن: <b>شارژ کیف پول</b>\n" if is_topup else f"📦 پلن: <b>{gb} گیگ / {months} ماه</b>\n"
                audit_cap = (
                    f"⚡ <b>فعال‌سازی فوری کارت‌به‌کارت #{order_id} (نیازمند بررسی واریز)</b>\n\n"
                    f"📌 نوع: {type_label}\n"
                    f"👤 کاربر: <code>{user_id}</code> (@{html.escape(username)})\n"
                    f"🆔 اکانت: <code>{html.escape(uname)}</code>\n"
                    f"{plan_line}"
                    f"💵 مبلغ یکتای واریزی: <b>{p['total_toman']:,} تومان</b>"
                )
                audit_kb = {
                    "inline_keyboard": [[
                        {"text": "✅ واریز تایید شد", "callback_data": f"adm_audit_ok:{order_id}"},
                        {"text": "🚫 فیش جعلی (قطع اکانت)", "callback_data": f"adm_audit_fake:{order_id}"},
                    ]]
                }
                for adm_id in get_admin_ids(env):
                    try:
                        if "photo" in m and m["photo"]:
                            tg_api(token, "sendPhoto", {"chat_id": adm_id, "photo": m["photo"][-1]["file_id"], "caption": audit_cap, "parse_mode": "HTML", "reply_markup": audit_kb})
                        elif "document" in m:
                            tg_api(token, "sendDocument", {"chat_id": adm_id, "document": m["document"]["file_id"], "caption": audit_cap, "parse_mode": "HTML", "reply_markup": audit_kb})
                        else:
                            send_message(token, adm_id, f"{audit_cap}\n\n📝 کد پیگیری:\n<code>{html.escape(m.get('text') or '')}</code>", reply_markup=audit_kb)
                    except Exception:
                        pass
                return
            except Exception as e:
                with orders_lock:
                    orders = load_json(ORDERS_PATH)
                    if order_id in orders:
                        orders[order_id]["status"] = "pending"
                        save_json(ORDERS_PATH, orders)
                auto_note = f"\n⚠️ خطا در فعال‌سازی فوری: {html.escape(str(e))}"

    # Automatic BSC BEP20 USDT verification when user submits a 0x... TxHash
    auto_note = ""
    if pay_method == "usdt" and re.fullmatch(r"0x[a-fA-F0-9]{64}", tx_key or ""):
        cfg = get_settings(env)
        ok, verify_msg, paid_usdt = verify_bsc_usdt_tx(tx_key, cfg["usdt_bep20"], p["usdt_amount"])
        if ok:
            with orders_lock:
                orders = load_json(ORDERS_PATH)
                orders[order_id]["status"] = "processing"
                save_json(ORDERS_PATH, orders)
            try:
                uname = fulfill_approved_order(token, env, order_id, order)
                for adm_id in get_admin_ids(env):
                    try:
                        send_message(
                            token,
                            adm_id,
                            f"⚡ <b>تایید خودکار تتر (BEP20) — #{order_id}</b>\n\n"
                            f"👤 کاربر: <code>{user_id}</code> (@{html.escape(username)})\n"
                            f"📦 پلن: <b>{gb} گیگ / {months} ماه</b>\n"
                            f"🪙 واریزی تاییدشده: <code>{paid_usdt} USDT</code>\n"
                            f"🆔 اکانت: <code>{html.escape(uname)}</code>",
                        )
                    except Exception:
                        pass
                return
            except Exception as e:
                with orders_lock:
                    orders = load_json(ORDERS_PATH)
                    if order_id in orders:
                        orders[order_id]["status"] = "pending"
                        save_json(ORDERS_PATH, orders)
                auto_note = f"\n⚠️ خطا در ساخت خودکار پس از تایید BSC: {html.escape(str(e))}"
        else:
            auto_note = f"\n🔍 بررسی خودکار BSC: {html.escape(verify_msg)}"

    send_message(
        token,
        m["chat"]["id"],
        f"✅ رسید سفارش <code>#{order_id}</code> دریافت شد و برای تایید مدیریت ارسال گردید.\n"
        "بلافاصله پس از تایید، نتیجه و لینک اشتراک برای شما ارسال می‌شود.",
        env=env,
        user_id=user_id,
    )

    method_label = "💳 کارت‌به‌کارت" if pay_method == "card" else "🪙 تتر (BEP20)"
    if is_topup:
        type_label = "👛 شارژ کیف پول"
    elif is_renew:
        type_label = f"🔄 تمدید (<code>{html.escape(target_username)}</code>)"
    else:
        type_label = "🛒 خرید جدید"
    coupon_info = f"\n🎟 کد تخفیف: <code>{html.escape(coupon_code)}</code> ({coupon_pct}٪)" if coupon_code else ""
    plan_line = f"📦 پلن: <b>شارژ کیف پول</b>\n" if is_topup else f"📦 پلن: <b>{gb} گیگ / {months} ماه</b>\n"
    admin_caption = (
        f"🆕 <b>سفارش جدید #{order_id}</b>\n\n"
        f"📌 نوع: {type_label}\n"
        f"👤 کاربر: <code>{user_id}</code> (@{html.escape(username)})\n"
        f"{plan_line}"
        f"💰 روش: {method_label}\n"
        f"💵 مبلغ: <b>{p['total_toman']:,} تومان</b> (<code>{p['usdt_amount']} USDT</code>)"
        f"{coupon_info}{auto_note}"
    )
    kb = {
        "inline_keyboard": [
            [
                {"text": "✅ تایید و ساخت خودکار", "callback_data": f"adm_ok:{order_id}"},
                {"text": "❌ رد سفارش", "callback_data": f"adm_no:{order_id}"},
            ]
        ]
    }
    for adm_id in get_admin_ids(env):
        try:
            if "photo" in m and m["photo"]:
                tg_api(
                    token,
                    "sendPhoto",
                    {
                        "chat_id": adm_id,
                        "photo": m["photo"][-1]["file_id"],
                        "caption": admin_caption,
                        "parse_mode": "HTML",
                        "reply_markup": kb,
                    },
                )
            elif "document" in m:
                tg_api(
                    token,
                    "sendDocument",
                    {
                        "chat_id": adm_id,
                        "document": m["document"]["file_id"],
                        "caption": admin_caption,
                        "parse_mode": "HTML",
                        "reply_markup": kb,
                    },
                )
            else:
                txt_receipt = html.escape(m.get("text") or "")
                send_message(token, adm_id, f"{admin_caption}\n\n📝 پیام/کد پیگیری:\n<code>{txt_receipt}</code>", reply_markup=kb)
        except Exception as e:
            log.warning("Failed to notify admin %s: %s", adm_id, e)


def handle_text(token: str, env: dict[str, str], m: dict) -> None:
    chat_id = m["chat"]["id"]
    user_id = m["from"]["id"]
    t = m["text"].strip()

    if user_id not in get_admin_ids(env):
        with users_lock:
            if load_json(USERS_PATH).get(str(user_id), {}).get("blocked"):
                send_message(token, chat_id, "⛔ حساب شما به دلیل ارسال رسید نامعتبر مسدود شده است.")
                return

    if t in ("/help", BTN_HELP, "/guide"):
        user_states.pop(user_id, None)
        send_connection_guide(token, chat_id, env, user_id)
        return

    if t == BTN_WALLET or t == "/wallet":
        user_states.pop(user_id, None)
        bal = get_wallet_balance(user_id)
        kb = {"inline_keyboard": [[{"text": "➕ شارژ کیف پول", "callback_data": "wallet_topup"}]]}
        send_message(
            token,
            chat_id,
            f"👛 <b>کیف پول شما</b>\n\n"
            f"💵 موجودی فعلی: <b>{bal:,} تومان</b>\n\n"
            "با شارژ کیف پول می‌توانید بدون نیاز به تایید دوباره، خرید و تمدید را به‌صورت <b>آنی</b> انجام دهید.",
            reply_markup=kb,
        )
        return

    if t == BTN_SUPPORT or t == "/support":
        user_states[user_id] = {"step": "await_support"}
        send_message(
            token,
            chat_id,
            "👨‍💻 <b>پشتیبانی مستقیم</b>\n\nپیام، سوال یا اسکرین‌شات خود را همینجا بفرستید تا مستقیماً برای مدیریت ارسال شود:",
            env=env,
            user_id=user_id,
        )
        return

    if t == "/start" or t.startswith("/start "):
        user_states.pop(user_id, None)
        send_message(
            token,
            chat_id,
            "👋 <b>به ربات فروش اشتراک خوش آمدید</b>\n\n"
            "• برای خرید از دکمهٔ <b>🛒 خرید اشتراک</b> استفاده کنید.\n"
            "• پرداخت با <b>کارت‌به‌کارت</b> و <b>تتر (BEP20 - تایید خودکار)</b> با نرخ لحظه‌ای.\n"
            "• هر هفته یک‌بار امکان دریافت <b>🎁 تست رایگان</b> دارید.\n"
            "• با <b>🤝 دعوت دوستان</b> از هر خرید موفق دوستانتان کد تخفیف هدیه بگیرید!",
            env=env,
            user_id=user_id,
        )
        return

    if t == BTN_TRIAL or t == "/trial":
        user_states.pop(user_id, None)
        if not check_force_join(token, env, chat_id, user_id):
            return
        cfg = get_settings(env)
        if not cfg.get("trial_enabled", True):
            send_message(token, chat_id, "⛔ بخش تست رایگان در حال حاضر غیرفعال است.", env=env, user_id=user_id)
            return
        cooldown_days = int(cfg.get("trial_cooldown_days", 7))
        cooldown_sec = cooldown_days * 86400
        now = int(time.time())
        with users_lock:
            users = load_json(USERS_PATH)
            u_rec = users.get(str(user_id), {})
            prev_trial_at = int(u_rec.get("last_trial_at") or (u_rec.get("last_seen", 0) if u_rec.get("trial_used") else 0))
            if prev_trial_at > 0 and (now - prev_trial_at) < cooldown_sec:
                rem_sec = cooldown_sec - (now - prev_trial_at)
                rem_days = rem_sec // 86400
                rem_hours = (rem_sec % 86400) // 3600
                wait_s = f"{rem_days} روز و {rem_hours} ساعت" if rem_days > 0 else f"{max(1, rem_hours)} ساعت"
                send_message(
                    token,
                    chat_id,
                    f"⚠️ شما در {cooldown_days} روز گذشته تست رایگان گرفته‌اید.\n⏳ تا تست بعدی: <b>{wait_s}</b> باقی مانده است.",
                    env=env,
                    user_id=user_id,
                )
                return
            u_rec["trial_used"] = True
            u_rec["last_trial_at"] = now
            users[str(user_id)] = u_rec
            save_json(USERS_PATH, users)
        trial_gb = int(cfg.get("trial_gb", 1))
        trial_hours = int(cfg.get("trial_hours", 24))
        try:
            u = create_panel_user(
                env,
                f"TRIAL_{user_id}",
                user_id,
                trial_gb,
                1,
                "trial",
                hours=trial_hours,
                custom_prefix=f"trial_{user_id}",
            )
            full_sub = f"{str(cfg['panel_url']).rstrip('/')}{u['subscription_url']}"
            uname = u["username"]
            caption = (
                f"🎁 <b>اشتراک تست رایگان شما آماده است!</b>\n\n"
                f"🆔 نام کاربری: <code>{html.escape(uname)}</code>\n"
                f"📦 حجم: <b>{trial_gb} گیگابایت</b>\n"
                f"⏳ اعتبار: <b>{trial_hours} ساعت</b>\n\n"
                f"🔗 <b>لینک سابسکریپشن:</b>\n<code>{html.escape(full_sub)}</code>"
            )
            send_qr_with_sub(token, chat_id, full_sub, caption, env, user_id)
        except Exception as e:
            with users_lock:
                users = load_json(USERS_PATH)
                if str(user_id) in users:
                    users[str(user_id)]["last_trial_at"] = prev_trial_at
                    if prev_trial_at == 0:
                        users[str(user_id)]["trial_used"] = False
                    save_json(USERS_PATH, users)
            send_message(token, chat_id, f"❌ خطا در ساخت تست رایگان: {html.escape(str(e))}", env=env, user_id=user_id)
        return

    if t == BTN_REFERRAL or t == "/ref":
        user_states.pop(user_id, None)
        cfg = get_settings(env)
        pct = cfg.get("referral_reward_pct", 20.0)
        bot_u = bot_info_cache.get("username")
        if not bot_u:
            try:
                bot_u = tg_api(token, "getMe", timeout=8).get("result", {}).get("username", "")
                if bot_u:
                    bot_info_cache["username"] = bot_u
            except Exception:
                bot_u = ""
        ref_link = f"https://t.me/{bot_u}?start=ref_{user_id}" if bot_u else "نام کاربری ربات در دسترس نیست"
        with users_lock:
            cnt = int(load_json(USERS_PATH).get(str(user_id), {}).get("referrals_count", 0))
        send_message(
            token,
            chat_id,
            f"🤝 <b>دعوت از دوستان و دریافت تخفیف</b>\n\n"
            f"لینک دعوت اختصاصی شما را برای دوستانتان بفرستید.\n"
            f"به ازای اولین خرید موفق هر دوست، یک <b>کد تخفیف {int(pct) if float(pct) == int(pct) else pct}٪</b> اختصاصی دریافت می‌کنید!\n\n"
            f"👥 تعداد دعوت‌های موفق شما: <b>{cnt} نفر</b>\n\n"
            f"🔗 <b>لینک دعوت شما:</b>\n<code>{ref_link}</code>",
            env=env,
            user_id=user_id,
        )
        return

    if t == BTN_BUY or t == "/buy":
        user_states.pop(user_id, None)
        if not check_force_join(token, env, chat_id, user_id):
            return
        send_message(token, chat_id, "📦 <b>مرحله ۱:</b> حجم مورد نظر (به گیگابایت) را انتخاب کنید:", reply_markup=build_gb_keyboard("buy"))
        return

    if t == BTN_RENEW or t == "/renew":
        user_states.pop(user_id, None)
        if not check_force_join(token, env, chat_id, user_id):
            return
        cfg = get_settings(env)
        disc = cfg["renew_discount_pct"]
        orders = load_json(ORDERS_PATH)
        my_unames = []
        seen_u = set()
        for o in sorted(orders.values(), key=lambda x: x.get("created_at", 0), reverse=True):
            un = o.get("panel_username")
            if o.get("user_id") == user_id and o.get("status") == "approved" and un and un not in seen_u:
                seen_u.add(un)
                my_unames.append(un)
        buttons = [[{"text": f"🔄 {un}", "callback_data": f"ren_pick:{un}"}] for un in my_unames[:8]]
        buttons.append([{"text": "✏️ وارد کردن نام کاربری یا لینک ساب", "callback_data": "ren_custom"}])
        send_message(
            token,
            chat_id,
            f"🔄 <b>تمدید اشتراک (با {disc}٪ تخفیف ویژه)</b>\n\nاشتراک مورد نظر را انتخاب کنید یا نام کاربری/لینک ساب خود را وارد کنید:",
            reply_markup={"inline_keyboard": buttons},
        )
        return

    if t == BTN_PRICES or t == "/prices":
        user_states.pop(user_id, None)
        try:
            cfg = get_settings(env)
            rate = get_usdt_toman_rate()
            lines = [f"💰 <b>تعرفه و نرخ لحظه‌ای</b>\n💵 نرخ فعلی تتر: <code>{rate:,}</code> تومان\n"]
            for gb in DEFAULT_GB_OPTIONS:
                p1 = calc_price(gb, 1, env)
                p12 = calc_price(gb, 12, env)
                lines.append(
                    f"• <b>{gb} گیگ</b>:\n"
                    f"  ۱ ماهه: <code>{p1['total_toman']:,}</code> ت (<code>{p1['usdt_amount']}</code> USDT)\n"
                    f"  ۱۲ ماهه (+{p12['surcharge_pct']}٪): <code>{p12['total_toman']:,}</code> ت (<code>{p12['usdt_amount']}</code> USDT)"
                )
            lines.append(f"\n📌 زیر {cfg['bulk_threshold_gb']} گیگ: هر ماه {cfg['pct_under_threshold']}٪ | {cfg['bulk_threshold_gb']} گیگ و بالاتر: هر ماه {cfg['pct_over_threshold']}٪")
            send_message(token, chat_id, "\n".join(lines), env=env, user_id=user_id)
        except Exception as e:
            send_message(token, chat_id, f"❌ خطا در دریافت نرخ: {html.escape(str(e))}", env=env, user_id=user_id)
        return

    if t == BTN_MY_SUBS:
        user_states.pop(user_id, None)
        orders = load_json(ORDERS_PATH)
        mine = [o for o in orders.values() if o.get("user_id") == user_id and o.get("status") == "approved" and o.get("panel_username")]
        if not mine:
            send_message(token, chat_id, "📭 هنوز اشتراک تاییدشده‌ای ندارید.", env=env, user_id=user_id)
            return
        seen_u = set()
        unique_subs = []
        for o in sorted(mine, key=lambda x: x.get("created_at", 0), reverse=True):
            un = o["panel_username"]
            if un not in seen_u:
                seen_u.add(un)
                unique_subs.append(o)
        send_message(token, chat_id, "📦 <b>اشتراک‌های شما:</b>\nبرای مشاهدهٔ وضعیت لحظه‌ای، دریافت QR یا تمدید هر اشتراک روی دکمه‌های زیرش بزنید:", env=env, user_id=user_id)
        for o in unique_subs[:8]:
            uname = o["panel_username"]
            sub = o.get("sub_url", "")
            kb = {
                "inline_keyboard": [
                    [
                        {"text": "📊 وضعیت لحظه‌ای", "callback_data": f"sub_status:{uname}"},
                        {"text": "📱 QR و لینک", "callback_data": f"sub_qr:{uname}"},
                    ],
                    [
                        {"text": "🔄 تمدید", "callback_data": f"ren_pick:{uname}"},
                        {"text": "🔑 تغییر لینک ساب", "callback_data": f"sub_revoke:{uname}"},
                    ],
                ]
            }
            send_message(
                token,
                chat_id,
                f"🆔 <code>{html.escape(uname)}</code> ({o['gb']}GB / {o['months']} ماه)\n<code>{html.escape(sub)}</code>",
                reply_markup=kb,
            )
        return

    if t == BTN_CHECK:
        user_states[user_id] = {"step": "await_check"}
        send_message(token, chat_id, "📊 نام کاربری اشتراک (مثلاً <code>u_a1b2c3d4</code>) را بفرستید:", env=env, user_id=user_id)
        return

    if t == BTN_ADMIN and user_id in get_admin_ids(env):
        user_states.pop(user_id, None)
        show_admin_panel(token, chat_id, env)
        return

    st = user_states.get(user_id)
    if st:
        step = st.get("step")
        if step == "await_renew_user":
            user_states.pop(user_id, None)
            try:
                u = resolve_user_for_renew(env, t)
                uname = u["username"]
                user_states[user_id] = {"step": "renew_selected", "target_username": uname}
                send_message(
                    token,
                    chat_id,
                    f"✅ اشتراک <code>{html.escape(uname)}</code> پیدا شد.\nحجم تمدید را انتخاب کنید:",
                    reply_markup=build_gb_keyboard("ren"),
                )
            except Exception:
                send_message(token, chat_id, "❌ اشتراکی با این نام کاربری یا لینک ساب پیدا نشد.", env=env, user_id=user_id)
            return

        if step == "await_custom_gb":
            if not t.isdigit() or int(t) < 1 or int(t) > 10000:
                send_message(token, chat_id, "❌ لطفاً یک عدد معتبر بین ۱ تا ۱۰۰۰۰ بفرستید:", env=env, user_id=user_id)
                return
            gb = int(t)
            target_username = st.get("target_username", "")
            prefix = "ren" if target_username else "buy"
            user_states[user_id] = {"step": "renew_selected", "target_username": target_username} if target_username else {}
            if not target_username:
                user_states.pop(user_id, None)
            send_message(token, chat_id, f"⏳ حجم انتخابی: <b>{gb} گیگ</b>\nمدت زمان را انتخاب کنید:", reply_markup=build_month_keyboard(gb, env=env, prefix=prefix))
            return

        if step == "await_custom_month":
            if not t.isdigit() or int(t) < 1 or int(t) > 36:
                send_message(token, chat_id, "❌ لطفاً تعداد ماه معتبر بین ۱ تا ۳۶ بفرستید:", env=env, user_id=user_id)
                return
            months = int(t)
            gb = st["gb"]
            target_username = st.get("target_username", "")
            user_states.pop(user_id, None)
            show_invoice(token, chat_id, gb, months, env=env, target_username=target_username)
            return

        if step == "await_receipt":
            user_states.pop(user_id, None)
            handle_receipt_submission(token, env, m, st)
            return

        if step == "await_topup_amount":
            user_states.pop(user_id, None)
            clean = t.replace(",", "").strip()
            if not clean.isdigit() or int(clean) < 10000 or int(clean) > 50000000:
                send_message(token, chat_id, "❌ مبلغ باید بین ۱۰,۰۰۰ تا ۵۰,۰۰۰,۰۰۰ تومان باشد (مثلاً 200000):", env=env, user_id=user_id)
                return
            amt = int(clean)
            rate = get_usdt_toman_rate()
            usdt_amt = round(amt / rate, 2)
            kb = {
                "inline_keyboard": [
                    [{"text": "💳 شارژ کارت‌به‌کارت", "callback_data": f"topup_pay:card:{amt}"}],
                    [{"text": "🪙 شارژ تتر (BEP20 - تایید خودکار)", "callback_data": f"topup_pay:usdt:{amt}"}],
                ]
            }
            send_message(
                token,
                chat_id,
                f"👛 <b>فاکتور شارژ کیف پول</b>\n\n💵 مبلغ: <b>{amt:,} تومان</b> (<code>{usdt_amt} USDT</code>)\nروش پرداخت را انتخاب کنید:",
                reply_markup=kb,
            )
            return

        if step == "await_support":
            user_states.pop(user_id, None)
            uname_tg = m["from"].get("username") or "-"
            kb = {"inline_keyboard": [[{"text": "↩️ پاسخ به کاربر", "callback_data": f"adm_reply:{user_id}"}]]}
            for adm_id in get_admin_ids(env):
                try:
                    send_message(
                        token,
                        adm_id,
                        f"📩 <b>پیام پشتیبانی جدید</b>\n👤 کاربر: <code>{user_id}</code> (@{html.escape(uname_tg)})\n\n💬 متن:\n{html.escape(t)}",
                        reply_markup=kb,
                    )
                except Exception:
                    pass
            send_message(token, chat_id, "✅ پیام شما برای پشتیبانی ارسال شد. به‌زودی پاسخ داده می‌شود.", env=env, user_id=user_id)
            return

        if step == "await_admin_reply" and user_id in get_admin_ids(env):
            user_states.pop(user_id, None)
            target_uid = st["target_uid"]
            try:
                send_message(token, target_uid, f"📩 <b>پاسخ پشتیبانی:</b>\n\n{html.escape(t)}", env=env, user_id=target_uid)
                send_message(token, chat_id, f"✅ پاسخ شما به کاربر <code>{target_uid}</code> ارسال شد.", env=env, user_id=user_id)
            except Exception as e:
                send_message(token, chat_id, f"❌ خطا در ارسال پاسخ: {html.escape(str(e))}", env=env, user_id=user_id)
            return

        if step == "await_admin_wallet" and user_id in get_admin_ids(env):
            user_states.pop(user_id, None)
            parts = t.split()
            if len(parts) != 2 or not parts[0].isdigit():
                send_message(token, chat_id, "❌ فرمت صحیح: <code>USER_ID AMOUNT</code>\nمثال: <code>123456789 100000</code> (یا عدد منفی برای کسر)", env=env, user_id=user_id)
                return
            try:
                t_uid = int(parts[0])
                delta = int(parts[1].replace(",", ""))
                ok, new_bal = adjust_wallet_balance(t_uid, delta)
                if not ok:
                    send_message(token, chat_id, "❌ موجودی کاربر کافی نیست تا این مقدار کسر شود.", env=env, user_id=user_id)
                    return
                send_message(token, chat_id, f"✅ موجودی کاربر <code>{t_uid}</code> به <b>{new_bal:,} تومان</b> تغییر یافت.", env=env, user_id=user_id)
                if delta > 0:
                    try:
                        send_message(token, t_uid, f"🎁 کیف پول شما به مبلغ <b>{delta:,} تومان</b> توسط مدیریت شارژ شد.\n👛 موجودی فعلی: <b>{new_bal:,} تومان</b>", env=env, user_id=t_uid)
                    except Exception:
                        pass
            except Exception as e:
                send_message(token, chat_id, f"❌ خطا: {html.escape(str(e))}", env=env, user_id=user_id)
            return

        if step == "await_coupon":
            user_states.pop(user_id, None)
            inv_id = st["inv_id"]
            inv = get_invoice(inv_id)
            if not inv:
                send_message(token, chat_id, "❌ پیش‌فاکتور منقضی شده است، لطفاً دوباره انتخاب کنید.", env=env, user_id=user_id)
                return
            ok, err_msg, pct = validate_coupon(t, user_id)
            if not ok:
                send_message(token, chat_id, err_msg, env=env, user_id=user_id)
                return
            code = t.strip().upper()
            inv = update_invoice(inv_id, {"coupon_code": code, "coupon_pct": pct})
            send_message(token, chat_id, f"✅ کد تخفیف <code>{html.escape(code)}</code> ({pct}٪) اعمال شد!", env=env, user_id=user_id)
            show_invoice(
                token,
                chat_id,
                inv["gb"],
                inv["months"],
                env=env,
                target_username=inv.get("target_username", ""),
                coupon_code=code,
                coupon_pct=pct,
                inv_id=inv_id,
            )
            return

        if step == "await_admin_coupon" and user_id in get_admin_ids(env):
            user_states.pop(user_id, None)
            parts = t.split()
            if len(parts) < 2:
                send_message(token, chat_id, "❌ فرمت صحیح: <code>CODE PERCENT [MAX_USES]</code>\nمثلاً: <code>OFF20 20 50</code>", env=env, user_id=user_id)
                return
            try:
                code = parts[0].upper()
                pct = float(parts[1].replace("%", ""))
                max_uses = int(parts[2]) if len(parts) >= 3 else 1
                if pct <= 0 or pct > 90 or max_uses < 1:
                    raise ValueError("درصد باید بین ۱ تا ۹۰ و تعداد حداقل ۱ باشد")
                create_coupon(code, pct, max_uses=max_uses)
                send_message(token, chat_id, f"✅ کد تخفیف <code>{html.escape(code)}</code> ({pct}٪، ظرفیت {max_uses}) ساخته شد.", env=env, user_id=user_id)
                show_admin_panel(token, chat_id, env)
            except Exception as e:
                send_message(token, chat_id, f"❌ خطا: {html.escape(str(e))}", env=env, user_id=user_id)
            return

        if step == "await_broadcast" and user_id in get_admin_ids(env):
            user_states[user_id] = {"step": "confirm_broadcast", "text": m.get("text") or ""}
            users_cnt = len(load_json(USERS_PATH))
            kb = {
                "inline_keyboard": [
                    [
                        {"text": f"✅ ارسال به {users_cnt} کاربر", "callback_data": "adm_broadcast_go"},
                        {"text": "❌ لغو", "callback_data": "adm_broadcast_cancel"},
                    ]
                ]
            }
            send_message(token, chat_id, f"📢 <b>پیش‌نمایش پیام همگانی:</b>\n\n{html.escape(t)}\n\nآیا ارسال شود؟", reply_markup=kb)
            return

        if step == "await_find_user" and user_id in get_admin_ids(env):
            user_states.pop(user_id, None)
            q = t.strip()
            orders = load_json(ORDERS_PATH)
            users = load_json(USERS_PATH)
            target_uid = int(q) if q.isdigit() else None
            target_uname = "" if q.isdigit() else q
            if target_uid is None:
                for o in orders.values():
                    if o.get("panel_username") == target_uname:
                        target_uid = o.get("user_id")
                        break
            else:
                for o in sorted(orders.values(), key=lambda x: x.get("created_at", 0), reverse=True):
                    if o.get("user_id") == target_uid and o.get("panel_username"):
                        target_uname = o["panel_username"]
                        break

            lines = [f"🔍 <b>نتیجه جستجو:</b> <code>{html.escape(q)}</code>\n"]
            kb_rows = []
            if target_uid is not None:
                u_rec = users.get(str(target_uid), {})
                u_orders = [o for o in orders.values() if o.get("user_id") == target_uid]
                app_cnt = sum(1 for o in u_orders if o.get("status") == "approved")
                lines.append(
                    f"👤 تلگرام: <code>{target_uid}</code> (@{html.escape(str(u_rec.get('username') or '-'))})\n"
                    f"👛 کیف پول: <b>{int(u_rec.get('balance_toman', 0)):,} تومان</b> | سفارش موفق: <b>{app_cnt}</b>"
                )
                kb_rows.append([{"text": "👛 تغییر کیف پول", "callback_data": f"adm_u_wallet:{target_uid}"}])
            if target_uname:
                try:
                    pu = panel_request(env, f"/api/user/{urllib.parse.quote(target_uname)}")
                    used_gb = round((pu.get("used_traffic") or 0) / (1024**3), 2)
                    lim_gb = round((pu.get("data_limit") or 0) / (1024**3), 2)
                    st_p = pu.get("status", "?")
                    lines.append(
                        f"\n🆔 اکانت پنل: <code>{html.escape(target_uname)}</code>\n"
                        f"• وضعیت: <b>{html.escape(str(st_p))}</b> | مصرف: <b>{used_gb}/{lim_gb} GB</b>"
                    )
                    tog_txt = "⏸ غیرفعال‌سازی" if st_p == "active" else "▶️ فعال‌سازی"
                    kb_rows.append([
                        {"text": "➕ ۵ گیگ هدیه", "callback_data": f"adm_u_addgb:{target_uname}:5"},
                        {"text": "⏳ ۳۰ روز هدیه", "callback_data": f"adm_u_adddays:{target_uname}:30"},
                    ])
                    kb_rows.append([{"text": tog_txt, "callback_data": f"adm_u_toggle:{target_uname}"}])
                except Exception as e:
                    lines.append(f"\n⚠️ اکانت پنل <code>{html.escape(target_uname)}</code> یافت نشد: {html.escape(str(e))}")
            if not kb_rows:
                lines.append("❌ موردی پیدا نشد.")
            send_message(token, chat_id, "\n".join(lines), reply_markup={"inline_keyboard": kb_rows} if kb_rows else None, env=env, user_id=user_id)
            return

        if step == "await_admin_setting" and user_id in get_admin_ids(env):
            user_states.pop(user_id, None)
            key = st["key"]
            try:
                if key in ("base_price_per_gb", "bulk_threshold_gb", "trial_gb", "trial_hours", "trial_cooldown_days", "reminder_days", "group_id", "max_pending_orders"):
                    val = int(t.replace(",", "").strip())
                    if val <= 0:
                        raise ValueError("باید مثبت باشد")
                elif key == "reminder_pct":
                    val = int(t.replace("%", "").strip())
                    if val < 1 or val > 90:
                        raise ValueError("باید بین ۱ تا ۹۰ باشد")
                elif key in ("pct_under_threshold", "pct_over_threshold", "renew_discount_pct", "global_discount_pct", "referral_reward_pct"):
                    val = float(t.replace("%", "").strip())
                    if val < 0 or val > 100:
                        raise ValueError("باید بین ۰ تا ۱۰۰ باشد")
                else:
                    val = t.strip()
                    if not val:
                        raise ValueError("نباید خالی باشد")
                update_setting(env, key, val)
                send_message(token, chat_id, f"✅ پارامتر <code>{key}</code> با موفقیت به <code>{html.escape(str(val))}</code> تغییر یافت.", env=env, user_id=user_id)
                show_admin_panel(token, chat_id, env)
            except Exception as e:
                send_message(token, chat_id, f"❌ مقدار نامعتبر: {html.escape(str(e))}", env=env, user_id=user_id)
            return

        if step == "await_check":
            user_states.pop(user_id, None)
            uname = t.strip()
            orders = load_json(ORDERS_PATH)
            owns = any(o.get("user_id") == user_id and o.get("panel_username") == uname for o in orders.values())
            if not owns and user_id not in get_admin_ids(env):
                send_message(token, chat_id, "❌ این نام کاربری متعلق به شما نیست یا پیدا نشد.", env=env, user_id=user_id)
                return
            try:
                u = panel_request(env, f"/api/user/{urllib.parse.quote(uname)}")
                used_gb = round((u.get("used_traffic") or 0) / (1024**3), 2)
                limit_gb = round((u.get("data_limit") or 0) / (1024**3), 2)
                exp = u.get("expire") or "نامحدود"
                send_message(
                    token,
                    chat_id,
                    f"📊 <b>وضعیت اشتراک {html.escape(uname)}</b>\n\n"
                    f"• وضعیت: <b>{html.escape(str(u.get('status')))}</b>\n"
                    f"• مصرف‌شده: <b>{used_gb} GB</b> از <b>{limit_gb} GB</b>\n"
                    f"• انقضا: <code>{html.escape(str(exp))}</code>",
                    env=env,
                    user_id=user_id,
                )
            except Exception as e:
                send_message(token, chat_id, f"❌ خطا در استعلام: {html.escape(str(e))}", env=env, user_id=user_id)
            return

    send_message(token, chat_id, "برای شروع خرید یا مشاهدهٔ قیمت‌ها از دکمه‌های زیر استفاده کنید:", env=env, user_id=user_id)


def handle_callback(token: str, env: dict[str, str], cb: dict) -> None:
    cb_id = cb["id"]
    user_id = cb["from"]["id"]
    data = cb.get("data", "")
    msg = cb.get("message")
    if not msg:
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        return
    chat_id = msg["chat"]["id"]
    msg_id = msg["message_id"]

    if user_id not in get_admin_ids(env):
        with users_lock:
            if load_json(USERS_PATH).get(str(user_id), {}).get("blocked"):
                tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "حساب شما مسدود است", "show_alert": True})
                return

    if data == "buy_restart":
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        edit_message(token, chat_id, msg_id, "📦 <b>مرحله ۱:</b> حجم مورد نظر (به گیگابایت) را انتخاب کنید:", reply_markup=build_gb_keyboard("buy"))
        return

    if data == "ren_restart":
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        edit_message(token, chat_id, msg_id, "🔄 حجم تمدید را انتخاب کنید:", reply_markup=build_gb_keyboard("ren"))
        return

    if data == "ren_custom":
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_renew_user"}
        send_message(token, chat_id, "✏️ نام کاربری یا لینک سابسکریپشن خود را بفرستید:", env=env, user_id=user_id)
        return

    if data.startswith("ren_pick:"):
        uname = data.split(":", 1)[1]
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "renew_selected", "target_username": uname}
        edit_message(token, chat_id, msg_id, f"🔄 تمدید اشتراک <code>{html.escape(uname)}</code>\nحجم تمدید را انتخاب کنید:", reply_markup=build_gb_keyboard("ren"))
        return

    if data.startswith("buy_gb:") or data.startswith("ren_gb:"):
        prefix = "ren" if data.startswith("ren_gb:") else "buy"
        val = data.split(":", 1)[1]
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        target_username = (user_states.get(user_id) or {}).get("target_username", "") if prefix == "ren" else ""
        if val == "custom":
            user_states[user_id] = {"step": "await_custom_gb", "target_username": target_username}
            send_message(token, chat_id, "✏️ حجم دلخواه خود را به عدد (گیگابایت) بفرستید:", env=env, user_id=user_id)
            return
        gb = int(val)
        edit_message(token, chat_id, msg_id, f"⏳ حجم انتخابی: <b>{gb} گیگابایت</b>\nمدت زمان را انتخاب کنید:", reply_markup=build_month_keyboard(gb, env=env, prefix=prefix))
        return

    if data.startswith("buy_m:") or data.startswith("ren_m:"):
        prefix = "ren" if data.startswith("ren_m:") else "buy"
        _, gb_s, m_s = data.split(":", 2)
        gb = int(gb_s)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        target_username = (user_states.get(user_id) or {}).get("target_username", "") if prefix == "ren" else ""
        if m_s == "custom":
            user_states[user_id] = {"step": "await_custom_month", "gb": gb, "target_username": target_username}
            send_message(token, chat_id, f"✏️ مدت دلخواه برای <b>{gb} گیگ</b> را به عدد (تعداد ماه) بفرستید:", env=env, user_id=user_id)
            return
        show_invoice(token, chat_id, gb, int(m_s), message_id=msg_id, env=env, target_username=target_username)
        return

    if data == "check_join":
        if check_force_join(token, env, chat_id, user_id):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ عضویت تایید شد!"})
            edit_message(token, chat_id, msg_id, "✅ عضویت شما تایید شد! حالا می‌توانید از دکمه‌های منو استفاده کنید.")
        else:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "❌ هنوز عضو کانال نشده‌اید", "show_alert": True})
        return

    if data == "conn_guide":
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        send_connection_guide(token, chat_id, env, user_id)
        return

    if data == "wallet_topup":
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_topup_amount"}
        send_message(token, chat_id, "💵 مبلغ شارژ کیف پول را به تومان وارد کنید (مثلاً <code>200000</code>):", env=env, user_id=user_id)
        return

    if data.startswith("topup_pay:"):
        _, method, amt_s = data.split(":", 2)
        amt = int(amt_s)
        rate = get_usdt_toman_rate()
        usdt_amt = round(amt / rate, 2)
        cfg = get_settings(env)
        if method == "card" and str(cfg["card_number"]).strip("0") == "":
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "شماره کارت هنوز توسط مدیریت تنظیم نشده است", "show_alert": True})
            return
        if method == "usdt" and str(cfg["usdt_bep20"]).lower().startswith("0x000000000"):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "آدرس تتر هنوز توسط مدیریت تنظیم نشده است", "show_alert": True})
            return
        card_amt = generate_unique_card_amount(amt) if method == "card" else amt
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_receipt", "is_topup": True, "topup_toman": amt, "card_toman": card_amt, "pay_method": method}
        if method == "card":
            txt = (
                "💳 <b>شارژ کیف پول (کارت‌به‌کارت)</b>\n\n"
                f"شماره کارت: <code>{html.escape(str(cfg['card_number']))}</code>\n"
                f"به نام: <b>{html.escape(str(cfg['card_owner']))}</b>\n"
                f"مبلغ دقیق و یکتا: <code>{card_amt:,}</code> تومان\n"
                "<i>(عدد رندوم برای شناسایی خودکار واریز شما اضافه شده است)</i>\n\n"
                "📸 پس از واریز، <b>عکس فیش یا کد پیگیری</b> را همینجا بفرستید:"
            )
        else:
            txt = (
                "🪙 <b>شارژ کیف پول (تتر BEP20 — تایید خودکار)</b>\n\n"
                f"آدرس ولت BEP20:\n<code>{html.escape(str(cfg['usdt_bep20']))}</code>\n\n"
                f"مبلغ دقیق: <code>{usdt_amt}</code> USDT\n\n"
                "⚡ پس از انتقال، <b>هش تراکنش (0x...)</b> را بفرستید تا بلافاصله کیف پولتان شارژ شود:"
            )
        edit_message(token, chat_id, msg_id, txt)
        return

    if data.startswith("sub_status:") or data.startswith("sub_qr:") or data.startswith("sub_revoke:"):
        action, uname = data.split(":", 1)
        orders = load_json(ORDERS_PATH)
        owns = any(o.get("user_id") == user_id and o.get("panel_username") == uname for o in orders.values())
        if not owns and user_id not in get_admin_ids(env):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "دسترسی ندارید", "show_alert": True})
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        cfg = get_settings(env)
        try:
            if action == "sub_revoke":
                u = panel_request(env, f"/api/user/{urllib.parse.quote(uname)}/revoke_sub", method="POST")
                full_sub = f"{str(cfg['panel_url']).rstrip('/')}{u['subscription_url']}"
                with orders_lock:
                    orders = load_json(ORDERS_PATH)
                    for o in orders.values():
                        if o.get("panel_username") == uname:
                            o["sub_url"] = full_sub
                    save_json(ORDERS_PATH, orders)
                send_qr_with_sub(
                    token,
                    chat_id,
                    full_sub,
                    f"🔑 <b>لینک اشتراک {html.escape(uname)} تغییر یافت!</b>\nلینک قبلی باطل شد.\n\n🔗 <b>لینک جدید:</b>\n<code>{html.escape(full_sub)}</code>",
                    env,
                    user_id,
                )
                return
            u = panel_request(env, f"/api/user/{urllib.parse.quote(uname)}")
            if action == "sub_status":
                used_gb = round((u.get("used_traffic") or 0) / (1024**3), 2)
                limit_gb = round((u.get("data_limit") or 0) / (1024**3), 2)
                rem_gb = round(max(0.0, limit_gb - used_gb), 2)
                exp = u.get("expire") or "نامحدود"
                send_message(
                    token,
                    chat_id,
                    f"📊 <b>وضعیت اشتراک {html.escape(uname)}</b>\n\n"
                    f"• وضعیت: <b>{html.escape(str(u.get('status')))}</b>\n"
                    f"• مصرف‌شده: <b>{used_gb} GB</b> از <b>{limit_gb} GB</b>\n"
                    f"• باقیمانده: <b>{rem_gb} GB</b>\n"
                    f"• انقضا: <code>{html.escape(str(exp))}</code>",
                    env=env,
                    user_id=user_id,
                )
            else:
                full_sub = f"{str(cfg['panel_url']).rstrip('/')}{u['subscription_url']}"
                send_qr_with_sub(
                    token,
                    chat_id,
                    full_sub,
                    f"📱 <b>QR و لینک اشتراک {html.escape(uname)}</b>\n\n<code>{html.escape(full_sub)}</code>",
                    env,
                    user_id,
                )
        except Exception as e:
            send_message(token, chat_id, f"❌ خطا در دریافت اطلاعات: {html.escape(str(e))}", env=env, user_id=user_id)
        return

    if data.startswith("coupon:"):
        inv_id = data.split(":", 1)[1]
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_coupon", "inv_id": inv_id}
        send_message(token, chat_id, "🎟 کد تخفیف خود را وارد کنید:", env=env, user_id=user_id)
        return

    if data.startswith("pay:"):
        _, method, inv_id = data.split(":", 2)
        inv = get_invoice(inv_id)
        if not inv:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "پیش‌فاکتور منقضی شده، دوباره انتخاب کنید", "show_alert": True})
            return
        gb = int(inv["gb"])
        months = int(inv["months"])
        target_username = inv.get("target_username", "")
        coupon_code = inv.get("coupon_code", "")
        coupon_pct = float(inv.get("coupon_pct", 0.0) or 0.0)
        p = calc_price(gb, months, env=env, is_renew=bool(target_username), coupon_pct=coupon_pct)
        if method == "wallet":
            ok_bal, new_bal = adjust_wallet_balance(user_id, -p["total_toman"])
            if not ok_bal:
                tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "موجودی کیف پول کافی نیست", "show_alert": True})
                return
            order_id = secrets.token_hex(4).upper()
            order = {
                "id": order_id,
                "type": "renew" if target_username else "buy",
                "target_username": target_username,
                "user_id": user_id,
                "tg_username": cb["from"].get("username") or "-",
                "gb": gb,
                "months": months,
                "pay_method": "wallet",
                "coupon_code": coupon_code,
                "coupon_pct": coupon_pct,
                "tx_key": f"wallet:{order_id}",
                "total_toman": p["total_toman"],
                "usdt_amount": p["usdt_amount"],
                "usdt_rate": p["usdt_rate"],
                "status": "processing",
                "created_at": int(time.time()),
                "panel_username": target_username,
                "sub_url": "",
            }
            with orders_lock:
                orders = load_json(ORDERS_PATH)
                orders[order_id] = order
                save_json(ORDERS_PATH, orders)
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ از کیف پول پرداخت شد"})
            try:
                uname = fulfill_approved_order(token, env, order_id, order)
                edit_message(token, chat_id, msg_id, f"✅ پرداخت از کیف پول انجام شد (موجودی باقیمانده: <b>{new_bal:,} تومان</b>). اشتراک <code>{html.escape(uname)}</code> ارسال شد.")
            except Exception as e:
                adjust_wallet_balance(user_id, p["total_toman"])
                with orders_lock:
                    orders = load_json(ORDERS_PATH)
                    orders.pop(order_id, None)
                    save_json(ORDERS_PATH, orders)
                send_message(token, chat_id, f"❌ خطا در ساخت اشتراک (مبلغ به کیف پول برگشت خورد): {html.escape(str(e))}", env=env, user_id=user_id)
            return
        cfg = get_settings(env)
        if method == "card" and str(cfg["card_number"]).strip("0") == "":
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "شماره کارت هنوز توسط مدیریت تنظیم نشده است", "show_alert": True})
            return
        if method == "usdt" and str(cfg["usdt_bep20"]).lower().startswith("0x000000000"):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "آدرس تتر هنوز توسط مدیریت تنظیم نشده است", "show_alert": True})
            return
        card_amt = inv.get("card_toman") or (generate_unique_card_amount(p["total_toman"]) if method == "card" else p["total_toman"])
        if method == "card" and not inv.get("card_toman"):
            update_invoice(inv_id, {"card_toman": card_amt})
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {
            "step": "await_receipt",
            "gb": gb,
            "months": months,
            "pay_method": method,
            "target_username": target_username,
            "coupon_code": coupon_code,
            "coupon_pct": coupon_pct,
            "card_toman": card_amt,
        }
        if method == "card":
            txt = (
                "💳 <b>پرداخت کارت‌به‌کارت</b>\n\n"
                f"شماره کارت: <code>{html.escape(str(cfg['card_number']))}</code>\n"
                f"به نام: <b>{html.escape(str(cfg['card_owner']))}</b>\n"
                f"مبلغ دقیق و یکتا: <code>{card_amt:,}</code> تومان\n"
                "<i>(عدد رندوم برای شناسایی خودکار واریز شما اضافه شده است)</i>\n\n"
                "📸 پس از واریز، <b>عکس فیش یا کد پیگیری</b> را همینجا بفرستید:"
            )
        else:
            txt = (
                "🪙 <b>پرداخت تتر (شبکه BEP20 / BSC — تایید خودکار)</b>\n\n"
                f"آدرس ولت BEP20:\n<code>{html.escape(str(cfg['usdt_bep20']))}</code>\n\n"
                f"مبلغ دقیق: <code>{p['usdt_amount']}</code> USDT\n"
                f"(معادل {p['total_toman']:,} تومان با نرخ {p['usdt_rate']:,})\n\n"
                "⚡ پس از انتقال، <b>هش تراکنش (0x...)</b> را بفرستید تا بلافاصله به‌صورت خودکار تایید شود (یا اسکرین‌شات بفرستید):"
            )
        edit_message(token, chat_id, msg_id, txt)
        return

    if data == "adm_refresh":
        if user_id not in get_admin_ids(env):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "🔄 بروزرسانی شد"})
        show_admin_panel(token, chat_id, env, message_id=msg_id)
        return

    if data.startswith("adm_reply:"):
        if user_id not in get_admin_ids(env):
            return
        target_uid = int(data.split(":", 1)[1])
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_admin_reply", "target_uid": target_uid}
        send_message(token, chat_id, f"↩️ پاسخ خود برای کاربر <code>{target_uid}</code> را بنویسید:", env=env, user_id=user_id)
        return

    if data == "adm_wallet":
        if user_id not in get_admin_ids(env):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_admin_wallet"}
        send_message(
            token,
            chat_id,
            "👛 آیدی عددی کاربر و مبلغ (به تومان) را بفرستید:\n<code>USER_ID AMOUNT</code>\nمثال: <code>123456789 100000</code>",
            env=env,
            user_id=user_id,
        )
        return

    if data == "adm_backup":
        if user_id not in get_admin_ids(env):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "⏳ در حال ساخت بکاپ..."})
        send_backup_zip(token, env, targets={user_id})
        return

    if data == "adm_report_today":
        if user_id not in get_admin_ids(env):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        now_utc = datetime.now(timezone.utc)
        tehran_now = now_utc + timedelta(hours=3, minutes=30)
        tehran_midnight = tehran_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ts = int((tehran_midnight - timedelta(hours=3, minutes=30)).timestamp())
        rep = build_sales_report(start_ts, int(time.time()) + 1, f"گزارش فروش امروز ({tehran_now.strftime('%Y-%m-%d')})")
        send_message(token, chat_id, rep, env=env, user_id=user_id)
        return

    if data == "adm_cycle_card_mode":
        if user_id not in get_admin_ids(env):
            return
        cur = str(get_settings(env).get("card_instant_mode", "all"))
        nxt = {"all": "trusted", "trusted": "off", "off": "all"}.get(cur, "all")
        update_setting(env, "card_instant_mode", nxt)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": f"✅ حالت کارت‌به‌کارت: {nxt}"})
        show_admin_panel(token, chat_id, env, message_id=msg_id)
        return

    if data.startswith("adm_audit_ok:") or data.startswith("adm_audit_fake:"):
        if user_id not in get_admin_ids(env):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "فقط ادمین مجاز است", "show_alert": True})
            return
        act, order_id = data.split(":", 1)
        with orders_lock:
            orders = load_json(ORDERS_PATH)
            order = orders.get(order_id)
            if not order:
                tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "سفارش پیدا نشد", "show_alert": True})
                return
            if act == "adm_audit_ok":
                order["audit_status"] = "verified"
                save_json(ORDERS_PATH, orders)
                tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ واریز تایید نهایی شد"})
                try:
                    tg_api(token, "editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}})
                except Exception:
                    pass
                send_message(token, chat_id, f"✅ واریز سفارش <code>#{order_id}</code> (<code>{order['total_toman']:,}</code> ت) تایید نهایی شد.")
                return
            order["status"] = "rejected"
            order["audit_status"] = "fake"
            save_json(ORDERS_PATH, orders)

        target_uid = order["user_id"]
        with users_lock:
            users = load_json(USERS_PATH)
            u_rec = users.setdefault(str(target_uid), {"first_seen": int(time.time()), "last_seen": int(time.time())})
            u_rec["blocked"] = True
            save_json(USERS_PATH, users)

        action_note = ""
        if order.get("type") == "topup":
            adjust_wallet_balance(target_uid, -int(order["total_toman"]))
            action_note = "مبلغ شارژ از کیف پول کسر شد"
        elif order.get("panel_username"):
            try:
                panel_request(env, f"/api/user/{urllib.parse.quote(order['panel_username'])}", method="PUT", body={"status": "disabled"})
                action_note = f"اکانت <code>{html.escape(order['panel_username'])}</code> غیرفعال شد"
            except Exception as e:
                action_note = f"خطا در غیرفعال‌سازی اکانت: {html.escape(str(e))}"

        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "🚫 مسدود و قطع شد"})
        try:
            tg_api(token, "editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}})
        except Exception:
            pass
        send_message(token, chat_id, f"🚫 سفارش <code>#{order_id}</code> به عنوان فیش جعلی ثبت شد.\n• {action_note}\n• کاربر <code>{target_uid}</code> مسدود گردید.")
        try:
            send_message(token, target_uid, f"🚫 سفارش <code>#{order_id}</code> به دلیل عدم تایید واریز لغو و اشتراک شما مسدود شد.")
        except Exception:
            pass
        return

    if data == "adm_toggle_force_join":
        if user_id not in get_admin_ids(env):
            return
        cur = bool(get_settings(env).get("force_join_enabled", False))
        update_setting(env, "force_join_enabled", not cur)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ وضعیت قفل کانال تغییر یافت"})
        show_admin_panel(token, chat_id, env, message_id=msg_id)
        return

    if data == "adm_find_user":
        if user_id not in get_admin_ids(env):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_find_user"}
        send_message(token, chat_id, "🔍 آیدی عددی تلگرام کاربر یا نام کاربری اکانت پنل (مثلاً <code>u_a1b2c3d4</code>) را بفرستید:", env=env, user_id=user_id)
        return

    if data.startswith("adm_u_wallet:"):
        if user_id not in get_admin_ids(env):
            return
        t_uid = data.split(":", 1)[1]
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_admin_wallet"}
        send_message(token, chat_id, f"👛 مبلغ را با آیدی بفرستید:\n<code>{t_uid} 100000</code>", env=env, user_id=user_id)
        return

    if data.startswith("adm_u_addgb:") or data.startswith("adm_u_adddays:") or data.startswith("adm_u_toggle:"):
        if user_id not in get_admin_ids(env):
            return
        parts = data.split(":")
        act, uname = parts[0], parts[1]
        try:
            pu = panel_request(env, f"/api/user/{urllib.parse.quote(uname)}")
            if act == "adm_u_addgb":
                add_b = int(parts[2]) * 1024 * 1024 * 1024
                new_lim = int(pu.get("data_limit") or 0) + add_b
                panel_request(env, f"/api/user/{urllib.parse.quote(uname)}", method="PUT", body={"data_limit": new_lim})
                msg_ok = f"✅ +{parts[2]} گیگ به <code>{html.escape(uname)}</code> اضافه شد."
            elif act == "adm_u_adddays":
                now_ts = int(time.time())
                cur_exp = pu.get("expire")
                base_ts = cur_exp if isinstance(cur_exp, (int, float)) and cur_exp > now_ts else now_ts
                new_exp = int(base_ts) + int(parts[2]) * 86400
                panel_request(env, f"/api/user/{urllib.parse.quote(uname)}", method="PUT", body={"expire": new_exp, "status": "active"})
                msg_ok = f"✅ +{parts[2]} روز به <code>{html.escape(uname)}</code> اضافه شد."
            else:
                new_st = "disabled" if pu.get("status") == "active" else "active"
                panel_request(env, f"/api/user/{urllib.parse.quote(uname)}", method="PUT", body={"status": new_st})
                msg_ok = f"✅ وضعیت <code>{html.escape(uname)}</code> به <b>{new_st}</b> تغییر یافت."
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ انجام شد"})
            send_message(token, chat_id, msg_ok, env=env, user_id=user_id)
        except Exception as e:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "❌ خطا", "show_alert": True})
            send_message(token, chat_id, f"❌ خطا: {html.escape(str(e))}", env=env, user_id=user_id)
        return

    if data == "adm_update_bot":
        if user_id not in get_admin_ids(env):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "⏳ در حال دانلود آپدیت..."})
        try:
            url = "https://raw.githubusercontent.com/sirvan1133/pasarguard-sales-bot/main/bot.py"
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"})
            new_code = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
            cur_path = Path(__file__).resolve()
            tmp_path = cur_path.with_suffix(".py.new")
            tmp_path.write_text(new_code, encoding="utf-8")
            subprocess.run([sys.executable, "-m", "py_compile", str(tmp_path)], check=True, timeout=10)
            os.replace(tmp_path, cur_path)
            os.chmod(cur_path, 0o700)
            send_message(token, chat_id, "✅ آخرین نسخه از گیت‌هاب دریافت و کامپایل شد. در حال ری‌استارت ربات...", env=env, user_id=user_id)
            os.execv(sys.executable, [sys.executable, str(cur_path)])
        except Exception as e:
            send_message(token, chat_id, f"❌ خطا در آپدیت از گیت‌هاب: {html.escape(str(e))}", env=env, user_id=user_id)
        return

    if data == "adm_toggle_trial":
        if user_id not in get_admin_ids(env):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
            return
        cur = get_settings(env).get("trial_enabled", True)
        update_setting(env, "trial_enabled", not cur)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ وضعیت تست تغییر یافت"})
        show_admin_panel(token, chat_id, env, message_id=msg_id)
        return

    if data == "adm_coupons":
        if user_id not in get_admin_ids(env):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        with coupons_lock:
            coupons = load_json(COUPONS_PATH)
        lines = ["🎟 <b>مدیریت کدهای تخفیف:</b>\n"]
        kb_rows = [[{"text": "➕ ساخت کد تخفیف جدید", "callback_data": "adm_coupon_add"}]]
        if not coupons:
            lines.append("هنوز کد تخفیفی ساخته نشده است.")
        else:
            for code, item in list(coupons.items())[-12:]:
                used_cnt = len(item.get("used_by", []))
                lines.append(f"• <code>{html.escape(code)}</code> — {item['pct']}٪ (مصرف: {used_cnt}/{item['max_uses']})")
                kb_rows.append([{"text": f"🗑 حذف {code}", "callback_data": f"adm_coupon_del:{code}"}])
        kb_rows.append([{"text": "🔙 بازگشت به پنل", "callback_data": "adm_refresh"}])
        edit_message(token, chat_id, msg_id, "\n".join(lines), reply_markup={"inline_keyboard": kb_rows})
        return

    if data == "adm_coupon_add":
        if user_id not in get_admin_ids(env):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_admin_coupon"}
        send_message(
            token,
            chat_id,
            "🎟 کد تخفیف جدید را به فرمت زیر بفرستید:\n<code>CODE PERCENT MAX_USES</code>\n\nمثال: <code>OFF20 20 50</code> (کد OFF20 با ۲۰٪ تخفیف برای ۵۰ نفر)",
            env=env,
            user_id=user_id,
        )
        return

    if data.startswith("adm_coupon_del:"):
        if user_id not in get_admin_ids(env):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
            return
        code = data.split(":", 1)[1]
        with coupons_lock:
            coupons = load_json(COUPONS_PATH)
            coupons.pop(code, None)
            save_json(COUPONS_PATH, coupons)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": f"🗑 {code} حذف شد"})
        show_admin_panel(token, chat_id, env, message_id=msg_id)
        return

    if data == "adm_broadcast":
        if user_id not in get_admin_ids(env):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_broadcast"}
        send_message(token, chat_id, "📢 متن پیام همگانی را بفرستید تا پیش‌نمایش آن نمایش داده شود:", env=env, user_id=user_id)
        return

    if data == "adm_broadcast_cancel":
        if user_id not in get_admin_ids(env):
            return
        user_states.pop(user_id, None)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "لغو شد"})
        edit_message(token, chat_id, msg_id, "❌ ارسال همگانی لغو شد.")
        return

    if data == "adm_broadcast_go":
        if user_id not in get_admin_ids(env):
            return
        st = user_states.pop(user_id, None)
        if not st or st.get("step") != "confirm_broadcast":
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "پیامی در انتظار نیست", "show_alert": True})
            return
        msg_text = st["text"]
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "⏳ در حال ارسال..."})
        edit_message(token, chat_id, msg_id, "⏳ در حال ارسال پیام همگانی...")
        with users_lock:
            all_uids = [int(k) for k in load_json(USERS_PATH).keys() if k.isdigit()]
        sent_ok, sent_fail = 0, 0
        for uid in all_uids:
            try:
                send_message(token, uid, f"📢 <b>اطلاعیه:</b>\n\n{html.escape(msg_text)}")
                sent_ok += 1
            except Exception:
                sent_fail += 1
            time.sleep(0.05)
        send_message(token, chat_id, f"✅ پیام همگانی ارسال شد.\n• موفق: <b>{sent_ok}</b>\n• ناموفق: <b>{sent_fail}</b>", env=env, user_id=user_id)
        return

    if data.startswith("adm_set:"):
        if user_id not in get_admin_ids(env):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
            return
        key = data.split(":", 1)[1]
        labels = {
            "base_price_per_gb": "قیمت پایه هر گیگ (به تومان، مثلاً 15000)",
            "bulk_threshold_gb": "آستانه حجم عمده (به گیگ، مثلاً 100)",
            "pct_under_threshold": "درصد سود ماهانه زیر آستانه (مثلاً 2)",
            "pct_over_threshold": "درصد سود ماهانه بالای آستانه (مثلاً 1)",
            "renew_discount_pct": "درصد تخفیف تمدید (مثلاً 10)",
            "global_discount_pct": "درصد تخفیف همگانی کمپین (مثلاً 15 و برای غیرفعال 0)",
            "referral_reward_pct": "درصد کد تخفیف پاداش رفرال (مثلاً 20)",
            "max_pending_orders": "حداکثر سفارش در انتظار همزمان برای هر کاربر (مثلاً 2)",
            "force_join_channel": "آیدی کانال قفل اجباری (مثلاً @MyChannel)",
            "trial_gb": "حجم تست رایگان به گیگابایت (مثلاً 1)",
            "trial_hours": "مدت تست رایگان به ساعت (مثلاً 24)",
            "trial_cooldown_days": "فاصله زمانی بین هر تست رایگان به روز (مثلاً 7)",
            "reminder_days": "چند روز مانده به انقضا یادآوری ارسال شود (مثلاً 3)",
            "reminder_pct": "حجم باقیمانده کمتر از چند درصد باشد یادآوری ارسال شود (مثلاً 10)",
            "card_number": "شماره کارت جدید",
            "card_owner": "نام صاحب کارت جدید",
            "usdt_bep20": "آدرس تتر BEP20 جدید",
            "panel_url": "آدرس پنل (مثلاً https://panel.example.com:8443)",
            "panel_user": "نام کاربری ادمین پنل",
            "panel_pass": "رمز عبور ادمین پنل",
            "group_id": "آیدی عددی گروه در پنل PasarGuard که کاربران جدید عضو آن می‌شوند (در پنل‌های تک‌گروهه همان 1 است)",
        }
        user_states[user_id] = {"step": "await_admin_setting", "key": key}
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        send_message(token, chat_id, f"✏️ مقدار جدید برای <b>{labels.get(key, key)}</b> را بفرستید:", env=env, user_id=user_id)
        return

    if data.startswith("adm_ok:") or data.startswith("adm_no:"):
        if user_id not in get_admin_ids(env):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "فقط ادمین مجاز است", "show_alert": True})
            return
        action, order_id = data.split(":", 1)
        with orders_lock:
            orders = load_json(ORDERS_PATH)
            order = orders.get(order_id)
            if not order:
                tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "سفارش پیدا نشد", "show_alert": True})
                return
            if order["status"] != "pending":
                tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": f"قبلاً {order['status']} شده است", "show_alert": True})
                return
            if action == "adm_no":
                order["status"] = "rejected"
                save_json(ORDERS_PATH, orders)
                tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "❌ رد شد"})
                try:
                    tg_api(token, "editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}})
                except Exception:
                    pass
                send_message(token, chat_id, f"❌ سفارش <code>#{order_id}</code> رد شد.")
                send_message(token, order["user_id"], f"❌ سفارش <code>#{order_id}</code> توسط مدیریت رد شد. در صورت نیاز با پشتیبانی تماس بگیرید.", env=env, user_id=order["user_id"])
                return
            order["status"] = "processing"
            save_json(ORDERS_PATH, orders)

        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "⏳ در حال اعمال در پنل..."})
        try:
            uname = fulfill_approved_order(token, env, order_id, order)
            try:
                tg_api(token, "editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}})
            except Exception:
                pass
            send_message(token, chat_id, f"✅ سفارش <code>#{order_id}</code> تایید شد و اشتراک <code>{html.escape(uname)}</code> برای مشتری ارسال گردید.")
        except Exception as e:
            with orders_lock:
                orders = load_json(ORDERS_PATH)
                if order_id in orders:
                    orders[order_id]["status"] = "pending"
                    save_json(ORDERS_PATH, orders)
            send_message(token, chat_id, f"❌ خطا در ساخت اشتراک <code>#{order_id}</code>: {html.escape(str(e))}")


def expiry_reminder_loop(token: str, env: dict[str, str]) -> None:
    """Background worker that reminds users 3 days before expiry or <10% traffic left."""
    time.sleep(60)
    while True:
        try:
            cfg = get_settings(env)
            disc = cfg.get("renew_discount_pct", 10.0)
            rem_days = int(cfg.get("reminder_days", 3))
            rem_pct = int(cfg.get("reminder_pct", 10))
            orders = load_json(ORDERS_PATH)
            reminders = load_json(REMINDERS_PATH)
            now = int(time.time())
            latest_by_uname: dict[str, dict] = {}
            for o in sorted(orders.values(), key=lambda x: x.get("created_at", 0)):
                un = o.get("panel_username")
                if o.get("status") == "approved" and un:
                    latest_by_uname[un] = o

            changed = False
            for uname, o in latest_by_uname.items():
                last_rem = int(reminders.get(uname, 0))
                if now - last_rem < 72 * 3600:
                    continue
                try:
                    u = panel_request(env, f"/api/user/{urllib.parse.quote(uname)}")
                except Exception:
                    continue
                if u.get("status") != "active":
                    continue
                reasons = []
                exp_raw = u.get("expire")
                if isinstance(exp_raw, (int, float)) and exp_raw > now:
                    days_left = (exp_raw - now) / 86400.0
                    if days_left <= rem_days:
                        reasons.append(f"⏳ کمتر از <b>{max(1, int(round(days_left)))} روز</b> تا پایان اعتبار")
                elif isinstance(exp_raw, str) and exp_raw:
                    try:
                        exp_ts = int(datetime.fromisoformat(exp_raw.replace("Z", "+00:00")).timestamp())
                        if exp_ts > now and (exp_ts - now) <= rem_days * 86400:
                            days_left = (exp_ts - now) / 86400.0
                            reasons.append(f"⏳ کمتر از <b>{max(1, int(round(days_left)))} روز</b> تا پایان اعتبار")
                    except Exception:
                        pass
                limit = int(u.get("data_limit") or 0)
                used = int(u.get("used_traffic") or 0)
                if limit > 0 and used < limit:
                    rem_ratio = (limit - used) / limit
                    if rem_ratio <= (rem_pct / 100.0):
                        rem_gb = round((limit - used) / (1024**3), 2)
                        reasons.append(f"📉 فقط <b>{rem_gb} GB</b> حجم باقی مانده")
                if reasons:
                    kb = {"inline_keyboard": [[{"text": f"🔄 تمدید با {disc}٪ تخفیف", "callback_data": f"ren_pick:{uname}"}]]}
                    try:
                        send_message(
                            token,
                            o["user_id"],
                            f"⚠️ <b>یادآوری تمدید اشتراک <code>{html.escape(uname)}</code></b>\n\n"
                            + "\n".join(reasons)
                            + f"\n\nهمین حالا با <b>{disc}٪ تخفیف ویژه</b> تمدید کنید تا قطعی نداشته باشید:",
                            reply_markup=kb,
                        )
                        reminders[uname] = now
                        changed = True
                    except Exception as e:
                        log.warning("Reminder send failed for %s: %s", uname, e)
            if changed:
                save_json(REMINDERS_PATH, reminders)
        except Exception as e:
            log.warning("Reminder loop error: %s", e)
        time.sleep(6 * 3600)


def backup_and_report_loop(token: str, env: dict[str, str]) -> None:
    """Sends a daily backup zip and nightly sales report (23:55 Tehran time) to admins."""
    time.sleep(90)
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            tehran_now = now_utc + timedelta(hours=3, minutes=30)
            today_str = tehran_now.strftime("%Y-%m-%d")
            meta = load_json(META_PATH)
            last_rep = meta.get("last_report_date", "")
            last_bkp = int(meta.get("last_backup_ts") or 0)
            now_ts = int(time.time())

            if tehran_now.hour == 23 and tehran_now.minute >= 50 and last_rep != today_str:
                tehran_midnight = tehran_now.replace(hour=0, minute=0, second=0, microsecond=0)
                start_ts = int((tehran_midnight - timedelta(hours=3, minutes=30)).timestamp())
                rep = build_sales_report(start_ts, now_ts + 1, f"گزارش فروش شبانه ({today_str})")
                for adm_id in get_admin_ids(env):
                    try:
                        send_message(token, adm_id, rep)
                    except Exception:
                        pass
                meta["last_report_date"] = today_str
                save_json(META_PATH, meta)

            if now_ts - last_bkp >= 24 * 3600:
                if send_backup_zip(token, env) > 0:
                    meta["last_backup_ts"] = now_ts
                    save_json(META_PATH, meta)
        except Exception as e:
            log.warning("Backup/report loop error: %s", e)
        time.sleep(600)


def main() -> None:
    env = load_env()
    token = env["BOT_TOKEN"]
    try:
        me = tg_api(token, "getMe", timeout=10).get("result", {})
        bot_info_cache["username"] = me.get("username", "")
    except Exception as e:
        log.warning("getMe failed: %s", e)

    try:
        tg_api(
            token,
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "شروع و منوی اصلی"},
                    {"command": "buy", "description": "خرید اشتراک جدید"},
                    {"command": "renew", "description": "تمدید اشتراک با تخفیف"},
                    {"command": "trial", "description": "دریافت تست رایگان"},
                    {"command": "wallet", "description": "کیف پول و شارژ حساب"},
                    {"command": "prices", "description": "مشاهده تعرفه و نرخ لحظه‌ای تتر"},
                    {"command": "support", "description": "ارتباط با پشتیبانی"},
                    {"command": "guide", "description": "راهنمای اتصال"},
                ]
            },
            timeout=10,
        )
    except Exception as e:
        log.warning("setMyCommands failed: %s", e)

    threading.Thread(target=expiry_reminder_loop, args=(token, env), daemon=True).start()
    threading.Thread(target=backup_and_report_loop, args=(token, env), daemon=True).start()

    offset = 0
    log.info("Sales bot starting...")
    while True:
        try:
            resp = tg_api(token, "getUpdates", {"offset": offset, "timeout": 30}, timeout=35)
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    m = upd["message"]
                    ref_by = None
                    txt_m = (m.get("text") or "").strip()
                    if txt_m.startswith("/start ref_"):
                        ref_part = txt_m.split("ref_", 1)[1].strip()
                        if ref_part.isdigit():
                            ref_by = int(ref_part)
                    track_user(m.get("from", {}), referred_by=ref_by)
                    user_id = m["from"]["id"]
                    st = user_states.get(user_id)
                    if "text" in m:
                        handle_text(token, env, m)
                    elif ("photo" in m or "document" in m) and st and st.get("step") == "await_receipt":
                        user_states.pop(user_id, None)
                        handle_receipt_submission(token, env, m, st)
                    elif "photo" in m and m["photo"] and st and st.get("step") == "await_support":
                        user_states.pop(user_id, None)
                        uname_tg = m["from"].get("username") or "-"
                        cap = html.escape(m.get("caption") or "")
                        kb = {"inline_keyboard": [[{"text": "↩️ پاسخ به کاربر", "callback_data": f"adm_reply:{user_id}"}]]}
                        for adm_id in get_admin_ids(env):
                            try:
                                tg_api(
                                    token,
                                    "sendPhoto",
                                    {
                                        "chat_id": adm_id,
                                        "photo": m["photo"][-1]["file_id"],
                                        "caption": f"📩 <b>پیام پشتیبانی جدید</b>\n👤 کاربر: <code>{user_id}</code> (@{html.escape(uname_tg)})\n\n{cap}",
                                        "parse_mode": "HTML",
                                        "reply_markup": kb,
                                    },
                                )
                            except Exception:
                                pass
                        send_message(token, m["chat"]["id"], "✅ پیام شما برای پشتیبانی ارسال شد. به‌زودی پاسخ داده می‌شود.", env=env, user_id=user_id)
                elif "callback_query" in upd:
                    track_user(upd["callback_query"].get("from", {}))
                    handle_callback(token, env, upd["callback_query"])
        except Exception as e:
            log.warning("Polling error: %s", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
