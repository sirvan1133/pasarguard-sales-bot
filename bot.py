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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sales-bot")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", str(BASE_DIR))).resolve()
ENV_PATH = DATA_DIR / ".env"
ORDERS_PATH = DATA_DIR / "orders.json"
USERS_PATH = DATA_DIR / "users.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
COUPONS_PATH = DATA_DIR / "coupons.json"
INVOICES_PATH = DATA_DIR / "invoices.json"
REMINDERS_PATH = DATA_DIR / "reminders.json"
META_PATH = DATA_DIR / "meta.json"
RESELLERS_PATH = DATA_DIR / "resellers.json"
LEDGER_PATH = DATA_DIR / "ledger.json"
TICKETS_PATH = DATA_DIR / "tickets.json"
TENANTS_DIR = DATA_DIR / "tenants"

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
resellers_lock = threading.Lock()
rate_lock = threading.Lock()
rate_cache: dict = {"ts": 0.0, "rate": 0}
panel_lock = threading.Lock()
panel_token_cache: dict = {"token": "", "ts": 0.0}
user_states: dict[int, dict] = {}
bot_info_cache: dict = {"username": ""}

BTN_BUY = "🔐 خرید اشتراک"
BTN_RENEW = "♻️ تمدید سرویس"
BTN_TRIAL = "🎁 تست رایگان"
BTN_MY_SUBS = "🛍 سرویس های من"
BTN_WALLET = "💰 کیف پول"
BTN_CHECK = "📊 استعلام اشتراک"
BTN_REFERRAL = "👥 معرفی به دوستان"
BTN_SUPPORT = "☎️ پشتیبانی"
BTN_RESELLER = "💼 پنل نمایندگی"
BTN_HELP = "📚 آموزش"
BTN_MENU_MODE = "🎛 تغییر حالت منو"
BTN_ADMIN = "👑 پنل ادمین"


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def load_env() -> dict[str, str]:
    return load_env_file(ENV_PATH)


def is_tenant_bot(env: dict[str, str] | None = None) -> bool:
    e = env if env is not None else load_env()
    return bool(e.get("ROOT_DIR") or e.get("PARENT_DIR"))


def get_panel_config(env: dict[str, str]) -> dict:
    """Always read PasarGuard credentials from ROOT_DIR for tenant sub-bots."""
    root_s = env.get("ROOT_DIR")
    if root_s:
        root_dir = Path(root_s)
        root_env = load_env_file(root_dir / ".env")
        root_saved = load_json(root_dir / "settings.json")
        return {
            "panel_url": str(root_saved.get("panel_url") or root_env.get("PANEL_URL", "")),
            "panel_user": str(root_saved.get("panel_user") or root_env.get("PANEL_USER", "")),
            "panel_pass": str(root_saved.get("panel_pass") or root_env.get("PANEL_PASS", "")),
            "group_id": int(root_saved.get("group_id") or root_env.get("GROUP_ID", "1") or 1),
        }
    cfg = get_settings(env)
    return {
        "panel_url": str(cfg.get("panel_url", "")),
        "panel_user": str(cfg.get("panel_user", "")),
        "panel_pass": str(cfg.get("panel_pass", "")),
        "group_id": int(cfg.get("group_id", 1) or 1),
    }


def get_admin_ids(env: dict[str, str]) -> set[int]:
    return {int(x.strip()) for x in env.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


def get_user_menu_mode(user_id: int, env: dict[str, str] | None = None) -> str:
    return str(get_settings(env or load_env()).get("menu_mode") or "inline")


def set_user_menu_mode(user_id: int, mode: str) -> None:
    cfg = load_json(SETTINGS_PATH)
    cfg["menu_mode"] = mode
    save_json(SETTINGS_PATH, cfg)


def main_keyboard(user_id: int, env: dict[str, str]) -> dict:
    if get_user_menu_mode(user_id, env) == "inline":
        return {"remove_keyboard": True}
    miniapp_url = (env.get("MINIAPP_URL") or "https://drhomes.ir/vpnapp/").strip()
    rows = []
    if miniapp_url and not is_tenant_bot(env):
        rows.append([{"text": "🚀 پنل شیشه‌ای پیشرفته (مینی‌اپ)", "web_app": {"url": miniapp_url}}])
    rows.extend([
        [{"text": BTN_BUY}],
        [{"text": BTN_RENEW}, {"text": BTN_MY_SUBS}],
        [{"text": BTN_WALLET}, {"text": BTN_HELP}],
        [{"text": BTN_REFERRAL}, {"text": BTN_SUPPORT}],
        [{"text": BTN_TRIAL}, {"text": BTN_RESELLER}],
        [{"text": BTN_CHECK}],
    ])
    if user_id in get_admin_ids(env):
        rows.append([{"text": BTN_ADMIN}])
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


def build_home_inline_keyboard(user_id: int, env: dict[str, str]) -> dict:
    is_res = bool(get_reseller(user_id)) or (is_tenant_bot(env) and user_id in get_admin_ids(env))
    res_lbl = "💼 پنل نمایندگی" if is_res else "🤝 درخواست نمایندگی"
    miniapp_url = (env.get("MINIAPP_URL") or "https://drhomes.ir/vpnapp/").strip()
    rows = []
    if miniapp_url and not is_tenant_bot(env):
        rows.append([{"text": "🚀 پنل شیشه‌ای پیشرفته (مینی‌اپ)", "web_app": {"url": miniapp_url}, "style": "primary"}])
    rows.extend([
        [{"text": "🛒 خرید سرویس جدید", "callback_data": "home:buy", "style": "success"}],
        [
            {"text": "♻️ تمدید سرویس", "callback_data": "home:renew"},
            {"text": "📲 سرویس های من", "callback_data": "home:subs"},
        ],
        [
            {"text": "💰 اعتبار من", "callback_data": "home:wallet"},
            {"text": "💡 آموزش استفاده", "callback_data": "conn_guide"},
        ],
        [
            {"text": "👥 معرفی به دوستان", "callback_data": "home:ref"},
            {"text": "👨‍💻 تماس با پشتیبانی", "callback_data": "home:support"},
        ],
        [
            {"text": "🎁 تست رایگان", "callback_data": "home:trial"},
            {"text": "📊 استعلام اشتراک", "callback_data": "home:check"},
        ],
        [{"text": res_lbl, "callback_data": "res_panel", "style": "danger"}],
    ])
    if user_id in get_admin_ids(env):
        rows.append([{"text": "👑 پنل ادمین", "callback_data": "adm_open", "style": "primary"}])
    return {"inline_keyboard": rows}


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


@contextmanager
def users_file_lock(users_path: Path = USERS_PATH):
    """Cross-process + in-process lock for any users.json file."""
    lock_path = users_path.with_suffix(".lock")
    users_path.parent.mkdir(parents=True, exist_ok=True)
    with users_lock:
        lock_fp = open(lock_path, "a+")
        try:
            if fcntl is not None:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            lock_fp.close()


def track_user(user: dict, referred_by: int | None = None) -> None:
    if not user or "id" not in user:
        return
    uid = str(user["id"])
    now = int(time.time())
    with users_file_lock(USERS_PATH):
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
        "reseller_price_per_gb": 5000,
        "reseller_min_gb": 500,
        "reseller_max_gb_per_acc": 200,
        "gb_options": list(DEFAULT_GB_OPTIONS),
        "menu_mode": "inline",
        "maintenance_mode": False,
        "maintenance_msg": "🛠 ربات در حال بروزرسانی و نگهداری سرورهاست. لطفاً دقایقی دیگر دوباره تلاش کنید.",
        "pending_alert_minutes": 30,
        "backup_enabled": True,
        "backup_interval_hours": 24,
        "backup_bot_token": env.get("BACKUP_BOT_TOKEN", ""),
        "backup_chat_id": env.get("BACKUP_CHAT_ID", ""),
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


def claim_invoice(inv_id: str) -> dict | None:
    """Atomically claim an invoice once to prevent double-click double-charging."""
    with orders_lock:
        invs = load_json(INVOICES_PATH)
        inv = invs.get(inv_id)
        if not inv or inv.get("paid"):
            return None
        inv["paid"] = True
        save_json(INVOICES_PATH, invs)
        return inv


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
    pcfg = get_panel_config(env)
    with panel_lock:
        if not force_refresh and panel_token_cache["token"] and now - panel_token_cache["ts"] < 1800:
            return panel_token_cache["token"]
        base = pcfg["panel_url"].rstrip("/")
        if not base or "your-panel-domain" in base:
            raise RuntimeError("آدرس پنل هنوز در پنل ادمین تنظیم نشده است")
        form = urllib.parse.urlencode({"username": pcfg["panel_user"], "password": pcfg["panel_pass"]}).encode("utf-8")
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
    base = get_panel_config(env)["panel_url"].rstrip("/")
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
    group_id = get_panel_config(env)["group_id"]
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
        base = get_panel_config(env)["panel_url"].rstrip("/")
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


def _strip_button_styles(markup: dict) -> dict:
    if not isinstance(markup, dict):
        return markup
    out = dict(markup)
    for key in ("inline_keyboard", "keyboard"):
        if key in out and isinstance(out[key], list):
            out[key] = [
                [{k: v for k, v in btn.items() if k not in ("style", "icon_custom_emoji_id")} for btn in row]
                for row in out[key]
            ]
    return out


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
    try:
        return tg_api(token, "sendMessage", payload, timeout=15).get("result")
    except urllib.error.HTTPError as e:
        if e.code == 400 and markup and ("inline_keyboard" in markup or "keyboard" in markup):
            payload["reply_markup"] = _strip_button_styles(markup)
            return tg_api(token, "sendMessage", payload, timeout=15).get("result")
        raise


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
        if e.code == 400 and reply_markup is not None:
            try:
                payload["reply_markup"] = _strip_button_styles(reply_markup)
                tg_api(token, "editMessageText", payload, timeout=15)
                return
            except urllib.error.HTTPError as e2:
                if e2.code != 400:
                    raise
                return
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


def resolve_tg_user_id(target: str) -> int | None:
    """Resolve a numeric Telegram ID or @username from users.json."""
    t = target.strip()
    if t.isdigit():
        return int(t)
    uname = t.lstrip("@").lower()
    if not uname:
        return None
    with users_file_lock(USERS_PATH):
        users = load_json(USERS_PATH)
        for uid_s, rec in users.items():
            if str(rec.get("username") or "").lstrip("@").lower() == uname and uid_s.isdigit():
                return int(uid_s)
    return None


def get_wallet_balance(user_id: int) -> int:
    with users_lock:
        return int(load_json(USERS_PATH).get(str(user_id), {}).get("balance_toman", 0))


def adjust_wallet_balance_at(users_path: Path, user_id: int, delta: int, note: str = "", allow_grace: bool = False) -> tuple[bool, int]:
    """Cross-process file-locked wallet adjustment + ledger logging (with optional 24h reseller grace)."""
    uid = str(user_id)
    now = int(time.time())
    notify_low = False
    notify_grace_start = False
    notify_grace_expired = False
    with users_file_lock(users_path):
        users = load_json(users_path)
        u = users.setdefault(uid, {"first_seen": now, "last_seen": now, "username": ""})
        cur = int(u.get("balance_toman", 0))
        new_bal = cur + delta
        if new_bal < 0:
            if not allow_grace:
                return False, cur
            grace_start = int(u.get("grace_started_at") or 0)
            if grace_start == 0:
                u["grace_started_at"] = now
                notify_grace_start = True
            elif now - grace_start >= 86400:
                notify_grace_expired = True
                save_json(users_path, users)
                return False, cur
        else:
            if delta > 0 and int(u.get("grace_started_at") or 0) > 0:
                u["grace_started_at"] = 0

        u["balance_toman"] = new_bal
        if delta < 0 and users_path != USERS_PATH and 0 <= new_bal <= 200000:
            last_alert = int(u.get("low_bal_alert_ts") or 0)
            if now - last_alert >= 12 * 3600:
                u["low_bal_alert_ts"] = now
                notify_low = True
        save_json(users_path, users)

        ledger_path = users_path.parent / "ledger.json"
        ledger = load_json(ledger_path)
        entries = ledger.get(uid, [])
        entries.append({"ts": now, "delta": int(delta), "balance": new_bal, "note": note or ("شارژ" if delta > 0 else "برداشت")})
        ledger[uid] = entries[-50:]
        save_json(ledger_path, ledger)

    if notify_grace_start or notify_grace_expired or notify_low:
        try:
            p_env = load_env_file(users_path.parent / ".env")
            p_tok = p_env.get("BOT_TOKEN")
            if p_tok:
                if notify_grace_start:
                    send_message(
                        p_tok,
                        user_id,
                        f"🚨 <b>هشدار قطع سرویس نمایندگی (مهلت ۲۴ ساعته)!</b>\n\n"
                        f"👛 موجودی شما تمام شد (تراز فعلی: <b>{new_bal:,} تومان</b>).\n"
                        "⏳ برای جلوگیری از قطع شدن مشتریان، به شما <b>۱ روز (۲۴ ساعت) فرصت</b> داده شده است.\n"
                        "لطفاً ظرف ۲۴ ساعت آینده حساب خود را شارژ و بدهی را تسویه کنید؛ در غیر این صورت ساخت اکانت متوقف خواهد شد.",
                    )
                elif notify_grace_expired:
                    send_message(
                        p_tok,
                        user_id,
                        f"⛔ <b>مهلت ۲۴ ساعتهٔ نمایندگی به پایان رسید!</b>\n\n"
                        f"👛 بدهی فعلی: <b>{cur:,} تومان</b>\n"
                        "ساخت اکانت در ربات شما متوقف شد. برای فعال‌سازی مجدد، حساب خود را شارژ کنید.",
                    )
                elif notify_low:
                    send_message(
                        p_tok,
                        user_id,
                        f"⚠️ <b>هشدار کمبود ترافیک و موجودی نمایندگی!</b>\n\n"
                        f"👛 موجودی فعلی شما به <b>{new_bal:,} تومان</b> رسیده و رو به پایان است.\n"
                        "لطفاً پیش از اتمام موجودی، حساب خود را شارژ کنید.",
                    )
        except Exception as e:
            log.warning("Balance alert failed for %s: %s", user_id, e)
    return True, new_bal


def adjust_wallet_balance(user_id: int, delta: int, note: str = "") -> tuple[bool, int]:
    return adjust_wallet_balance_at(USERS_PATH, user_id, delta, note=note)


def get_upstream_balance(env: dict[str, str]) -> int | None:
    parent_s = env.get("PARENT_DIR")
    owner_s = env.get("TENANT_OWNER_ID")
    if not parent_s or not owner_s:
        return None
    users = load_json(Path(parent_s) / "users.json")
    return int(users.get(str(owner_s), {}).get("balance_toman", 0))


def get_sales_wallet_balance(user_id: int) -> int:
    uid = str(user_id)
    with users_file_lock(USERS_PATH):
        users = load_json(USERS_PATH)
        u = users.get(uid, {})
        if "sales_balance_toman" in u:
            return int(u["sales_balance_toman"])
        # Backfill from existing approved orders if not initialized yet
        orders = load_json(ORDERS_PATH)
        total = sum(
            int(o.get("total_toman") or 0)
            for o in orders.values()
            if o.get("status") == "approved"
            and str(o.get("user_id")) != uid
            and (o.get("type") == "topup" or o.get("pay_method") in ("card", "usdt"))
        )
        if total > 0:
            u = users.setdefault(uid, {"first_seen": int(time.time()), "last_seen": int(time.time()), "username": ""})
            u["sales_balance_toman"] = total
            save_json(USERS_PATH, users)
        return total


def add_sales_wallet_balance(owner_uid: int, delta: int, note: str = "") -> int:
    uid = str(owner_uid)
    now = int(time.time())
    with users_file_lock(USERS_PATH):
        users = load_json(USERS_PATH)
        u = users.setdefault(uid, {"first_seen": now, "last_seen": now, "username": ""})
        new_bal = int(u.get("sales_balance_toman", 0)) + delta
        u["sales_balance_toman"] = new_bal
        save_json(USERS_PATH, users)
        ledger = load_json(LEDGER_PATH)
        entries = ledger.get(f"sales_{uid}", [])
        entries.append({"ts": now, "delta": int(delta), "balance": new_bal, "note": note or "درآمد فروش"})
        ledger[f"sales_{uid}"] = entries[-50:]
        save_json(LEDGER_PATH, ledger)
    return new_bal


def _resolve_wholesale_ppg(parent_dir: Path, owner_s: str, cur_env: dict[str, str]) -> int:
    res_rec = load_json(parent_dir / "resellers.json").get(str(owner_s), {})
    if res_rec.get("price_per_gb"):
        return int(res_rec["price_per_gb"])
    p_set = load_json(parent_dir / "settings.json")
    if p_set.get("reseller_price_per_gb"):
        return int(p_set["reseller_price_per_gb"])
    return int(cur_env.get("WHOLESALE_PPG", "5000") or 5000)


def charge_upstream_chain(data_dir: Path, gb: int, note: str = "") -> tuple[bool, str]:
    """Recursively deduct wholesale GB cost up the parent tenant chain."""
    cur_env = load_env_file(data_dir / ".env")
    parent_s = cur_env.get("PARENT_DIR")
    owner_s = cur_env.get("TENANT_OWNER_ID")
    if not parent_s or not owner_s:
        return True, ""
    parent_dir = Path(parent_s)
    ppg = _resolve_wholesale_ppg(parent_dir, owner_s, cur_env)
    cost = int(gb) * ppg
    ok_up, err_up = charge_upstream_chain(parent_dir, gb, note=note)
    if not ok_up:
        return False, err_up
    ok_cur, cur_bal = adjust_wallet_balance_at(parent_dir / "users.json", int(owner_s), -cost, note=note or f"کسر عمده {gb}GB", allow_grace=True)
    if not ok_cur:
        refund_upstream_chain(parent_dir, gb, note="برگشت خطای کسر")
        return False, f"❌ موجودی نمایندگی کافی نیست و مهلت ۲۴ ساعته نیز به پایان رسیده است (تراز: {cur_bal:,} ت). لطفاً حساب نمایندگی خود را شارژ کنید."
    return True, ""


def refund_upstream_chain(data_dir: Path, gb: int, note: str = "") -> None:
    cur_env = load_env_file(data_dir / ".env")
    parent_s = cur_env.get("PARENT_DIR")
    owner_s = cur_env.get("TENANT_OWNER_ID")
    if not parent_s or not owner_s:
        return
    parent_dir = Path(parent_s)
    ppg = _resolve_wholesale_ppg(parent_dir, owner_s, cur_env)
    adjust_wallet_balance_at(parent_dir / "users.json", int(owner_s), int(gb) * ppg, note=note or f"برگشت عمده {gb}GB")
    refund_upstream_chain(parent_dir, gb, note=note)


def is_user_blocked(user_id: int) -> tuple[bool, int]:
    """Returns (blocked, remaining_seconds; 0 means permanent/manual block)."""
    uid = str(user_id)
    now = int(time.time())
    with users_lock:
        users = load_json(USERS_PATH)
        u = users.get(uid)
        if not u or not u.get("blocked"):
            return False, 0
        until = int(u.get("blocked_until") or 0)
        if until > 0:
            if now >= until:
                u["blocked"] = False
                u["blocked_until"] = 0
                save_json(USERS_PATH, users)
                return False, 0
            return True, until - now
        return True, 0


def get_reseller(user_id: int) -> dict | None:
    with resellers_lock:
        rec = load_json(RESELLERS_PATH).get(str(user_id))
        return rec if (rec and rec.get("active")) else None


def set_reseller(user_id: int, active: bool = True, price_per_gb: int | None = None) -> dict:
    uid = str(user_id)
    with resellers_lock:
        data = load_json(RESELLERS_PATH)
        rec = data.get(uid, {
            "user_id": user_id,
            "total_gb_sold": 0,
            "accounts_created": 0,
            "created_at": int(time.time()),
        })
        rec["active"] = active
        if price_per_gb is not None:
            rec["price_per_gb"] = int(price_per_gb)
        data[uid] = rec
        save_json(RESELLERS_PATH, data)
        return rec


def record_reseller_sale(user_id: int, gb: int, count: int = 1) -> None:
    uid = str(user_id)
    with resellers_lock:
        data = load_json(RESELLERS_PATH)
        if uid in data:
            data[uid]["total_gb_sold"] = int(data[uid].get("total_gb_sold", 0)) + int(gb)
            data[uid]["accounts_created"] = int(data[uid].get("accounts_created", 0)) + int(count)
            save_json(RESELLERS_PATH, data)


def get_reseller_price_per_gb(user_id: int, env: dict[str, str]) -> int:
    cfg = get_settings(env)
    rec = get_reseller(user_id)
    if rec and rec.get("price_per_gb"):
        return int(rec["price_per_gb"])
    return int(cfg.get("reseller_price_per_gb", 5000))


def show_reseller_panel(token: str, chat_id: int, env: dict[str, str], user_id: int, message_id: int | None = None) -> None:
    cfg = get_settings(env)
    if is_tenant_bot(env) and user_id in get_admin_ids(env):
        up_bal = get_upstream_balance(env) or 0
        sales_bal = get_sales_wallet_balance(user_id)
        ppg = int(env.get("WHOLESALE_PPG", "5000") or 5000)
        parent_u = env.get("PARENT_BOT_USERNAME", "")
        txt = (
            "💼 <b>پنل نمایندگی شما (ادمین این ربات)</b>\n\n"
            f"💼 <b>کیف پول بالادستی (اعتبار خرید از بالادست):</b> <b>{up_bal:,} تومان</b> (تعرفه: {ppg:,} ت/گیگ)\n"
            f"💵 <b>کیف پول فروش (دریافتی از زیردست‌ها و مشتریان):</b> <b>{sales_bal:,} تومان</b>\n\n"
            "• وقتی زیردست‌ها خرید/تمدید کنند، هزینهٔ عمده از <b>کیف پول بالادستی</b> شما کسر می‌شود.\n"
            "• برای افزایش اعتبار بالادستی، در ربات بالادست شارژ کنید."
        )
        rows = [[{"text": "💼 مدیریت زیرنمایندگان", "callback_data": "adm_resellers"}]]
        if parent_u:
            rows.append([{"text": f"➕ شارژ کیف پول بالادستی (@{parent_u})", "url": f"https://t.me/{parent_u}"}])
        rows.append([{"text": "🏠 بازگشت به منوی اصلی", "callback_data": "home_back"}])
        kb = {"inline_keyboard": rows}
        if message_id is not None:
            edit_message(token, chat_id, message_id, txt, reply_markup=kb)
        else:
            send_message(token, chat_id, txt, reply_markup=kb)
        return

    rec = get_reseller(user_id)
    if not rec:
        min_gb = int(cfg.get("reseller_min_gb", 500))
        ppg = int(cfg.get("reseller_price_per_gb", 5000))
        min_tmn = min_gb * ppg
        bal = get_wallet_balance(user_id)
        txt = (
            "💼 <b>درخواست پنل نمایندگی</b>\n\n"
            "شما هنوز نماینده نیستید. شرایط دریافت پنل نمایندگی:\n"
            f"• تعرفهٔ ثابت نماینده: <b>{ppg:,} تومان</b> به ازای هر گیگ (بدون افزایش ماهانه)\n"
            f"• حداقل شارژ/خرید اولیه: <b>{min_gb:,} گیگ</b> (معادل <b>{min_tmn:,} تومان</b>)\n"
            f"• موجودی فعلی کیف پول شما: <b>{bal:,} تومان</b>\n\n"
            "پس از شارژ کیف پول یا برای بررسی مدیریت، دکمهٔ زیر را بزنید:"
        )
        kb = {
            "inline_keyboard": [
                [{"text": f"➕ شارژ نمایندگی (حداقل {min_tmn:,} ت)", "callback_data": "reseller_topup"}],
                [{"text": "📩 ارسال درخواست نمایندگی به ادمین", "callback_data": "reseller_apply"}],
                [{"text": "🏠 بازگشت به منوی اصلی", "callback_data": "home_back"}],
            ]
        }
        if message_id is not None:
            edit_message(token, chat_id, message_id, txt, reply_markup=kb)
        else:
            send_message(token, chat_id, txt, reply_markup=kb)
        return

    ppg = get_reseller_price_per_gb(user_id, env)
    bal = get_wallet_balance(user_id)
    est_gb = bal // ppg if ppg > 0 else 0
    max_acc = int(cfg.get("reseller_max_gb_per_acc", 200))
    cap_s = f"{max_acc} گیگ" if max_acc > 0 else "نامحدود"
    txt = (
        "💼 <b>پنل نمایندگی شما</b>\n\n"
        f"💵 تعرفهٔ ثابت شما: <b>{ppg:,} تومان / گیگ</b>\n"
        f"👛 موجودی کیف پول: <b>{bal:,} تومان</b> (معادل ~<b>{est_gb:,} GB</b>)\n"
        f"📦 سقف حجم هر اکانت: <b>{cap_s}</b>\n"
        f"📊 اکانت‌های ساخته‌شده: <b>{int(rec.get('accounts_created', 0))}</b> | کل فروش: <b>{int(rec.get('total_gb_sold', 0)):,} GB</b>"
    )
    kb = {
        "inline_keyboard": [
            [{"text": "➕ ساخت اکانت جدید", "callback_data": "res_create"}, {"text": "📦 ساخت گروهی (Bulk)", "callback_data": "res_bulk"}],
            [{"text": "📋 اکانت‌های من (نمایندگی)", "callback_data": "res_list"}, {"text": "👛 شارژ کیف پول", "callback_data": "wallet_topup"}],
            [{"text": "📜 گزارش تراکنش‌ها", "callback_data": "wallet_ledger"}, {"text": "📡 وضعیت سرورها", "callback_data": "res_nodes"}],
            [{"text": "🤖 ربات اختصاصی من (White-Label)", "callback_data": "res_subbot"}],
            [{"text": "🏠 بازگشت به منوی اصلی", "callback_data": "home_back"}],
        ]
    }
    if message_id is not None:
        edit_message(token, chat_id, message_id, txt, reply_markup=kb)
    else:
        send_message(token, chat_id, txt, reply_markup=kb)


def provision_tenant_bot(env: dict[str, str], owner_uid: int, bot_token: str) -> tuple[str, str]:
    tok = bot_token.strip()
    if tok == env.get("BOT_TOKEN"):
        raise RuntimeError("نمی‌توانید توکن همین ربات مادر را وارد کنید!")
    me = tg_api(tok, "getMe", timeout=10).get("result", {})
    sub_uname = me.get("username")
    if not sub_uname:
        raise RuntimeError("توکن نامعتبر است")

    t_dir = TENANTS_DIR / str(owner_uid)
    t_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(t_dir, 0o700)
    root_dir = env.get("ROOT_DIR") or str(DATA_DIR)
    ppg = get_reseller_price_per_gb(owner_uid, env)
    parent_u = bot_info_cache.get("username", "")
    if not parent_u:
        try:
            parent_u = tg_api(env["BOT_TOKEN"], "getMe", timeout=8).get("result", {}).get("username", "")
        except Exception:
            parent_u = ""
    env_content = (
        f"BOT_TOKEN={tok}\n"
        f"ADMIN_IDS={owner_uid}\n"
        f"TENANT_OWNER_ID={owner_uid}\n"
        f"PARENT_DIR={DATA_DIR}\n"
        f"ROOT_DIR={root_dir}\n"
        f"WHOLESALE_PPG={ppg}\n"
        f"PARENT_BOT_USERNAME={parent_u}\n"
    )
    t_env = t_dir / ".env"
    t_env.write_text(env_content, encoding="utf-8")
    os.chmod(t_env, 0o600)

    svc_name = f"sales-bot-t-{owner_uid}.service"
    svc_path = Path("/etc/systemd/system") / svc_name
    svc_body = (
        "[Unit]\n"
        f"Description=Sales Bot Tenant {owner_uid} (@{sub_uname})\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={t_dir}\n"
        f"Environment=BOT_DATA_DIR={t_dir}\n"
        f"ExecStart={sys.executable} {Path(__file__).resolve()}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "User=root\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    svc_path.write_text(svc_body, encoding="utf-8")
    subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=15)
    subprocess.run(["systemctl", "enable", "--now", svc_name], check=True, timeout=20)
    subprocess.run(["systemctl", "restart", svc_name], check=True, timeout=20)

    with resellers_lock:
        data = load_json(RESELLERS_PATH)
        if str(owner_uid) in data:
            data[str(owner_uid)]["sub_bot_username"] = sub_uname
            data[str(owner_uid)]["sub_bot_service"] = svc_name
            save_json(RESELLERS_PATH, data)
    return sub_uname, svc_name


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


def check_maintenance(token: str, env: dict[str, str], chat_id: int, user_id: int) -> bool:
    cfg = get_settings(env)
    if not cfg.get("maintenance_mode") or user_id in get_admin_ids(env):
        return True
    send_message(token, chat_id, str(cfg.get("maintenance_msg") or "🛠 در حال تعمیر هستیم."), env=env, user_id=user_id)
    return False


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


def send_backup_zip(token: str, env: dict[str, str], targets: set[int] | None = None, use_backup_bot: bool = False) -> int:
    cfg = get_settings(env)
    bkp_tok = str(cfg.get("backup_bot_token") or "").strip() if use_backup_bot else ""
    send_tok = bkp_tok or token
    if targets is None:
        raw_cid = str(cfg.get("backup_chat_id") or "").strip()
        if raw_cid.lstrip("-").isdigit():
            targets = {int(raw_cid)}
        else:
            targets = get_admin_ids(env)
    files = [ENV_PATH, ORDERS_PATH, USERS_PATH, SETTINGS_PATH, COUPONS_PATH, REMINDERS_PATH, META_PATH, RESELLERS_PATH, LEDGER_PATH]
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        zip_path = Path(tmp.name)
    sent = 0
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in files:
                if p.exists():
                    zf.write(p, arcname=p.name)
            if TENANTS_DIR.exists():
                for pattern in ("*.json", ".env"):
                    for tp in TENANTS_DIR.rglob(pattern):
                        zf.write(tp, arcname=str(tp.relative_to(DATA_DIR)))
        now_tehran = (datetime.now(timezone.utc) + timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d %H:%M")
        caption = f"🗄 <b>بکاپ خودکار دیتابیس ربات</b>\n🕒 {now_tehran}"
        for adm_id in targets:
            try:
                tg_send_document(send_tok, adm_id, zip_path, caption=caption)
                sent += 1
            except Exception as e:
                log.warning("Backup send failed for %s: %s", adm_id, e)
    finally:
        zip_path.unlink(missing_ok=True)
    return sent


def restore_from_backup_zip(zip_path: Path, data_dir: Path, bot_script: Path) -> tuple[int, int]:
    """Safely extract backup zip into data_dir and recreate/restart all tenant systemd services."""
    allowed_ext = (".json", ".env")
    restored_files = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = Path(info.filename)
            if rel.is_absolute() or ".." in rel.parts or not info.filename.endswith(allowed_ext):
                continue
            dest = (data_dir / rel).resolve()
            if not str(dest).startswith(str(data_dir.resolve())):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.parent != data_dir:
                os.chmod(dest.parent, 0o700)
            with zf.open(info) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            os.chmod(dest, 0o600)
            restored_files += 1

    tenants_dir = data_dir / "tenants"
    services_started = 0
    if tenants_dir.exists():
        for env_p in tenants_dir.rglob(".env"):
            t_dir = env_p.parent
            t_env = load_env_file(env_p)
            owner_s = t_env.get("TENANT_OWNER_ID")
            if not owner_s or not owner_s.isdigit():
                continue
            # Ensure PARENT_DIR/ROOT_DIR match current server paths
            rel_parent = t_dir.parent.parent if t_dir.parent.name == "tenants" else data_dir
            t_env["PARENT_DIR"] = str(rel_parent)
            t_env["ROOT_DIR"] = str(data_dir)
            env_p.write_text("\n".join(f"{k}={v}" for k, v in t_env.items()) + "\n", encoding="utf-8")
            os.chmod(env_p, 0o600)

            svc_name = f"sales-bot-t-{owner_s}.service"
            svc_path = Path("/etc/systemd/system") / svc_name
            svc_body = (
                "[Unit]\n"
                f"Description=Sales Bot Tenant {owner_s}\n"
                "After=network-online.target\n\n"
                "[Service]\n"
                "Type=simple\n"
                f"WorkingDirectory={t_dir}\n"
                f"Environment=BOT_DATA_DIR={t_dir}\n"
                f"ExecStart={sys.executable} {bot_script}\n"
                "Restart=always\n"
                "RestartSec=5\n"
                "User=root\n\n"
                "[Install]\n"
                "WantedBy=multi-user.target\n"
            )
            svc_path.write_text(svc_body, encoding="utf-8")
            services_started += 1

        if services_started > 0:
            subprocess.run(["systemctl", "daemon-reload"], check=False, timeout=15)
            for env_p in tenants_dir.rglob(".env"):
                owner_s = load_env_file(env_p).get("TENANT_OWNER_ID")
                if owner_s and owner_s.isdigit():
                    svc_name = f"sales-bot-t-{owner_s}.service"
                    subprocess.run(["systemctl", "enable", "--now", svc_name], check=False, timeout=15)
                    subprocess.run(["systemctl", "restart", svc_name], check=False, timeout=15)

    return restored_files, services_started


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
        kb = {"inline_keyboard": [[{"text": "📱 آموزش اتصال", "callback_data": "conn_guide"}, {"text": "🏠 بازگشت به منوی اصلی", "callback_data": "home_back"}]]}
        tg_send_photo(token, chat_id, tmp_path, caption=caption, reply_markup=kb)
    finally:
        tmp_path.unlink(missing_ok=True)


def build_gb_keyboard(prefix: str = "buy", env: dict[str, str] | None = None) -> dict:
    gb_list = get_settings(env or load_env()).get("gb_options") or DEFAULT_GB_OPTIONS
    rows = [
        [{"text": f"📦 {g} گیگ", "callback_data": f"{prefix}_gb:{g}"} for g in gb_list[i:i+3]]
        for i in range(0, len(gb_list), 3)
    ]
    rows.append([{"text": "✏️ حجم دلخواه", "callback_data": f"{prefix}_gb:custom"}])
    rows.append([{"text": "🏠 بازگشت به منوی اصلی", "callback_data": "home_back"}])
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
        [{"text": "🏠 بازگشت به منوی اصلی", "callback_data": "home_back"}],
    ]
    return {"inline_keyboard": rows}


def show_admin_wallet_user(token: str, chat_id: int, env: dict[str, str], target_uid: int, message_id: int | None = None) -> None:
    bal = get_wallet_balance(target_uid)
    with users_file_lock(USERS_PATH):
        u_rec = load_json(USERS_PATH).get(str(target_uid), {})
    uname = u_rec.get("username") or "-"
    txt = (
        f"👛 <b>مدیریت کیف پول کاربر</b>\n\n"
        f"👤 کاربر: <code>{target_uid}</code> (@{html.escape(str(uname))})\n"
        f"💵 موجودی فعلی: <b>{bal:,} تومان</b>\n\n"
        "یکی از گزینه‌های زیر را انتخاب کنید:"
    )
    kb = {
        "inline_keyboard": [
            [
                {"text": "➕ ۵۰ هزار", "callback_data": f"adm_w_do:{target_uid}:50000"},
                {"text": "➕ ۱۰۰ هزار", "callback_data": f"adm_w_do:{target_uid}:100000"},
            ],
            [
                {"text": "➕ ۵۰۰ هزار", "callback_data": f"adm_w_do:{target_uid}:500000"},
                {"text": "➕ ۲.۵ میلیون", "callback_data": f"adm_w_do:{target_uid}:2500000"},
            ],
            [
                {"text": "➖ کسر ۵۰ هزار", "callback_data": f"adm_w_do:{target_uid}:-50000"},
                {"text": "🗑 صفر کردن کیف پول", "callback_data": f"adm_w_zero:{target_uid}"},
            ],
            [
                {"text": "✏️ مبلغ دلخواه (+/-)", "callback_data": f"adm_w_custom:{target_uid}"},
                {"text": "🔙 بازگشت", "callback_data": "adm_wallet"},
            ],
        ]
    }
    if message_id is not None:
        edit_message(token, chat_id, message_id, txt, reply_markup=kb)
    else:
        send_message(token, chat_id, txt, reply_markup=kb)


def show_admin_panel(token: str, chat_id: int, env: dict[str, str], message_id: int | None = None) -> None:
    orders = load_json(ORDERS_PATH)
    users = load_json(USERS_PATH)
    coupons = load_json(COUPONS_PATH)
    pending = sum(1 for o in orders.values() if o.get("status") == "pending")
    approved = [o for o in orders.values() if o.get("status") == "approved"]
    bot_subs = len({o.get("panel_username") for o in approved if o.get("panel_username")})
    total_tmn = sum(int(o.get("total_toman", 0)) for o in approved)
    cfg = get_settings(env)
    tenant = is_tenant_bot(env)
    if tenant:
        up_bal = get_upstream_balance(env)
        up_s = f"{up_bal:,} تومان" if up_bal is not None else "-"
        sales_s = f"{get_sales_wallet_balance(int(env.get('TENANT_OWNER_ID') or 0)):,} ت"
        panel_stats_s = f"📦 ساب‌های ربات: <b>{bot_subs:,}</b> | 💼 بالادستی: <b>{up_s}</b> | 💵 فروش: <b>{sales_s}</b>"
    else:
        try:
            sys_info = panel_request(env, "/api/system")
            panel_stats_s = f"📦 ساب‌های ربات: <b>{bot_subs:,}</b> | 🟢 فعال پنل: <b>{int(sys_info.get('active_users', 0)):,}</b> | 🌐 آنلاین: <b>{int(sys_info.get('online_users', 0)):,}</b>"
        except Exception:
            panel_stats_s = f"📦 ساب‌های ربات: <b>{bot_subs:,}</b>"
    try:
        rate = get_usdt_toman_rate()
        rate_s = f"{rate:,} تومان"
    except Exception:
        rate_s = "خطا"
    trial_s = f"فعال ({cfg['trial_gb']}GB / {cfg['trial_hours']}h / هر {cfg['trial_cooldown_days']} روز)" if cfg.get("trial_enabled", True) else "غیرفعال"
    fj_s = f"فعال ({cfg['force_join_channel']})" if cfg.get("force_join_enabled") else "غیرفعال"
    cm_map = {"all": "فوری برای همه + بررسی بعدی", "trusted": "فوری فقط مشتریان قدیمی", "off": "تایید دستی"}
    cm_s = cm_map.get(str(cfg.get("card_instant_mode", "all")), "فوری برای همه + بررسی بعدی")
    maint_s = "🔴 فعال (فروش متوقف)" if cfg.get("maintenance_mode") else "🟢 غیرفعال"
    bkp_s = f"فعال (هر {cfg['backup_interval_hours']}h)" if cfg.get("backup_enabled", True) else "غیرفعال"
    bkp_tok_s = "تنظیم‌شده" if str(cfg.get("backup_bot_token") or "").strip() else "ربات اصلی"
    panel_info_line = "" if tenant else (
        f"• بکاپ خودکار: <code>{bkp_s}</code> | ارسال به: <code>{bkp_tok_s}</code>\n"
        f"• پنل: <code>{html.escape(str(cfg['panel_url']))}</code> (یوزر: <code>{html.escape(str(cfg['panel_user']))}</code> | گروه پنل: <code>{cfg['group_id']}</code>)\n"
        "💡 <i>آیدی گروه پنل مشخص می‌کند کاربران جدید عضو کدام گروه PasarGuard شوند (در پنل‌های تک‌گروهه همان 1 است).</i>\n"
    )
    txt = (
        "👑 <b>پنل مدیریت ربات فروش</b>\n\n"
        f"👥 کل کاربران: <b>{len(users)}</b> | 🎟 کدهای تخفیف: <b>{len(coupons)}</b>\n"
        f"{panel_stats_s}\n"
        f"⏳ سفارش‌های در انتظار: <b>{pending}</b> | ✅ تاییدشده: <b>{len(approved)}</b>\n"
        f"💰 فروش کل: <b>{total_tmn:,} تومان</b>\n"
        f"💵 نرخ لحظه‌ای تتر: <code>{rate_s}</code>\n\n"
        "⚙️ <b>تنظیمات فعلی:</b>\n"
        f"• قیمت پایه هر گیگ: <code>{int(cfg['base_price_per_gb']):,}</code> تومان\n"
        f"• سود ماهانه زیر {cfg['bulk_threshold_gb']}GB: <code>{cfg['pct_under_threshold']}٪</code> | {cfg['bulk_threshold_gb']}GB+: <code>{cfg['pct_over_threshold']}٪</code>\n"
        f"• تخفیف تمدید: <code>{cfg['renew_discount_pct']}٪</code> | کمپین همگانی: <code>{cfg['global_discount_pct']}٪</code> | رفرال: <code>{cfg['referral_reward_pct']}٪</code>\n"
        f"• تست رایگان: <code>{trial_s}</code>\n"
        f"• قفل کانال: <code>{html.escape(fj_s)}</code> | سقف سفارش در انتظار: <code>{cfg['max_pending_orders']}</code>\n"
        f"• حالت کارت‌به‌کارت: <code>{cm_s}</code> | حالت تعمیر: <code>{maint_s}</code> | هشدار معوق: <code>{cfg['pending_alert_minutes']}m</code>\n"
        f"• نمایندگی: هر گیگ <code>{int(cfg['reseller_price_per_gb']):,}</code> ت | کف <code>{cfg['reseller_min_gb']}</code> GB | سقف تک‌اکانت <code>{cfg['reseller_max_gb_per_acc']}</code> GB\n"
        f"• بسته‌های حجم: <code>{', '.join(str(x) for x in cfg.get('gb_options', DEFAULT_GB_OPTIONS))}</code>\n"
        f"• یادآور تمدید: <code>{cfg['reminder_days']} روز</code> مانده یا زیر <code>{cfg['reminder_pct']}٪</code> حجم\n"
        f"• کارت: <code>{html.escape(str(cfg['card_number']))}</code> ({html.escape(str(cfg['card_owner']))})\n"
        f"• تتر BEP20: <code>{html.escape(str(cfg['usdt_bep20']))}</code>\n"
        f"{panel_info_line}\n"
        "برای تغییر هر مورد روی دکمهٔ مربوطه بزنید:"
    )
    kb_rows = [
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
    ]
    if not tenant:
        kb_rows.extend([
            [{"text": "🌐 آدرس پنل", "callback_data": "adm_set:panel_url"}, {"text": "👤 یوزر پنل", "callback_data": "adm_set:panel_user"}],
            [{"text": "🔑 رمز پنل", "callback_data": "adm_set:panel_pass"}, {"text": "👥 آیدی گروه پنل", "callback_data": "adm_set:group_id"}],
        ])
    kb_rows.extend([
        [{"text": "🔍 مدیریت کاربر", "callback_data": "adm_find_user"}, {"text": "👛 شارژ دستی کیف‌پول", "callback_data": "adm_wallet"}],
        [{"text": "💼 مدیریت نمایندگان", "callback_data": "adm_resellers"}, {"text": "💵 قیمت گیگ نماینده", "callback_data": "adm_set:reseller_price_per_gb"}],
        [{"text": "📦 حداقل گیگ نمایندگی", "callback_data": "adm_set:reseller_min_gb"}, {"text": "🎯 سقف گیگ اکانت نماینده", "callback_data": "adm_set:reseller_max_gb_per_acc"}],
        [{"text": "📢 پیام همگانی", "callback_data": "adm_broadcast"}, {"text": "📊 گزارش امروز", "callback_data": "adm_report_today"}],
    ])
    if not tenant:
        kb_rows.extend([
            [{"text": "🗄 بکاپ فوری", "callback_data": "adm_backup"}, {"text": "♻️ بازیابی بکاپ (Restore)", "callback_data": "adm_restore"}],
            [{"text": "🔄 آپدیت ربات از گیت‌هاب", "callback_data": "adm_update_bot"}, {"text": "🗄 روشن/خاموش بکاپ", "callback_data": "adm_toggle_backup"}],
            [{"text": "⏱ بازه بکاپ (ساعت)", "callback_data": "adm_set:backup_interval_hours"}, {"text": "🤖 توکن ربات بکاپ", "callback_data": "adm_set:backup_bot_token"}],
            [{"text": "🆔 آیدی مقصد بکاپ", "callback_data": "adm_set:backup_chat_id"}],
        ])
    kb_rows.extend([
        [{"text": "📦 بسته‌های حجم پیش‌فرض", "callback_data": "adm_set:gb_options"}, {"text": "⚡ حالت کارت‌به‌کارت", "callback_data": "adm_cycle_card_mode"}],
        [{"text": "🛠 روشن/خاموش تعمیر", "callback_data": "adm_toggle_maint"}, {"text": "📝 متن پیام تعمیر", "callback_data": "adm_set:maintenance_msg"}],
        [{"text": "⏰ هشدار سفارش معوق", "callback_data": "adm_set:pending_alert_minutes"}, {"text": "📡 وضعیت سرورها", "callback_data": "adm_nodes"}],
        [{"text": f"🎛 حالت منو: { {'both': 'کیبورد + شیشه‌ای', 'inline': 'فقط شیشه‌ای', 'keyboard': 'فقط کیبوردی'}.get(get_user_menu_mode(0, env), 'فقط شیشه‌ای') }", "callback_data": "menu_mode"}],
    ])
    if not tenant:
        kb_rows.append([{"text": "🔄 بروزرسانی پنل", "callback_data": "adm_refresh"}])
    kb_rows.append([{"text": "🏠 بازگشت به منوی اصلی", "callback_data": "home_back"}])
    kb = {"inline_keyboard": kb_rows}
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
        pay_rows.append([{"text": f"👛 پرداخت آنی از کیف پول (موجودی: {bal:,} ت)", "callback_data": f"pay:wallet:{inv_id}", "style": "success"}])
    card_lbl = "💳 پرداخت کارت‌به‌کارت (فعال‌سازی آنی)" if get_settings(env).get("card_instant_mode", "all") != "off" else "💳 پرداخت کارت‌به‌کارت"
    pay_rows.extend([
        [{"text": card_lbl, "callback_data": f"pay:card:{inv_id}"}],
        [{"text": "🪙 پرداخت تتر (BEP20 - تایید خودکار)", "callback_data": f"pay:usdt:{inv_id}"}],
        [{"text": "🎟 اعمال کد تخفیف", "callback_data": f"coupon:{inv_id}"}],
        [{"text": "🔙 تغییر مدت", "callback_data": f"{prefix}_gb:{gb}"}, {"text": "🏠 بازگشت به منوی اصلی", "callback_data": "home_back"}],
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
        _, new_bal = adjust_wallet_balance(order["user_id"], amt, note=f"شارژ کیف پول #{order_id}")
        if is_tenant_bot(env) and env.get("TENANT_OWNER_ID") and str(order["user_id"]) != str(env["TENANT_OWNER_ID"]):
            add_sales_wallet_balance(int(env["TENANT_OWNER_ID"]), amt, note=f"شارژ کاربر/زیردست {order['user_id']} (#{order_id})")
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

    panel_base = get_panel_config(env)["panel_url"].rstrip("/")
    order_gb = int(order.get("gb") or 0)
    ok_up, err_up = charge_upstream_chain(DATA_DIR, order_gb)
    if not ok_up:
        raise RuntimeError(err_up)
    try:
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
    except Exception:
        refund_upstream_chain(DATA_DIR, order_gb)
        raise

    with orders_lock:
        orders = load_json(ORDERS_PATH)
        orders[order_id]["status"] = "approved"
        orders[order_id]["panel_username"] = uname
        orders[order_id]["sub_url"] = full_sub
        save_json(ORDERS_PATH, orders)

    if order.get("coupon_code"):
        consume_coupon(order["coupon_code"], order["user_id"])

    if is_tenant_bot(env) and env.get("TENANT_OWNER_ID") and order.get("pay_method") in ("card", "usdt") and str(order["user_id"]) != str(env["TENANT_OWNER_ID"]):
        add_sales_wallet_balance(int(env["TENANT_OWNER_ID"]), int(order.get("total_toman") or 0), note=f"فروش مستقیم #{order_id} ({uname})")

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
                {"text": "✅ تایید و ساخت خودکار", "callback_data": f"adm_ok:{order_id}", "style": "success"},
                {"text": "❌ رد سفارش", "callback_data": f"adm_no:{order_id}", "style": "danger"},
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
        blk, rem = is_user_blocked(user_id)
        if blk:
            if rem > 0:
                hrs = max(1, (rem + 3599) // 3600)
                send_message(token, chat_id, f"⛔ حساب شما به دلیل ارسال رسید نامعتبر به مدت <b>{hrs} ساعت</b> دیگر مسدود است.")
            else:
                send_message(token, chat_id, "⛔ حساب شما توسط مدیریت مسدود شده است.")
            return

    if t in ("/help", BTN_HELP, "/guide", "❓ راهنما"):
        user_states.pop(user_id, None)
        send_connection_guide(token, chat_id, env, user_id)
        return

    if t in (BTN_WALLET, "/wallet", "👛 کیف پول"):
        user_states.pop(user_id, None)
        if is_tenant_bot(env) and user_id in get_admin_ids(env):
            up_bal = get_upstream_balance(env) or 0
            sales_bal = get_sales_wallet_balance(user_id)
            parent_u = env.get("PARENT_BOT_USERNAME", "")
            rows = [[{"text": "📜 گزارش تراکنش‌های فروش", "callback_data": "sales_ledger"}], [{"text": "🏠 بازگشت به منوی اصلی", "callback_data": "home_back"}]]
            if parent_u:
                rows.insert(0, [{"text": f"➕ شارژ کیف پول بالادستی (@{parent_u})", "url": f"https://t.me/{parent_u}"}])
            send_message(
                token,
                chat_id,
                f"👛 <b>کیف پول‌های شما (نماینده)</b>\n"
                f"🆔 آیدی عددی شما: <code>{user_id}</code>\n\n"
                f"💼 <b>کیف پول بالادستی (خرید از بالادست):</b> <b>{up_bal:,} تومان</b>\n"
                f"💵 <b>کیف پول فروش (واریزی زیردست‌ها و مشتریان):</b> <b>{sales_bal:,} تومان</b>\n\n"
                "• هنگام خرید/تمدید مشتریان و زیردست‌ها، هزینهٔ عمده از <b>کیف پول بالادستی</b> کسر می‌شود.\n"
                "• مبلغی که زیردست‌ها واریز می‌کنند به <b>کیف پول فروش</b> شما اضافه می‌گردد.",
                reply_markup={"inline_keyboard": rows},
            )
            return
        bal = get_wallet_balance(user_id)
        kb = {
            "inline_keyboard": [
                [{"text": "➕ شارژ کیف پول", "callback_data": "wallet_topup"}, {"text": "💸 انتقال موجودی", "callback_data": "wallet_transfer"}],
                [{"text": "📜 گزارش تراکنش‌ها", "callback_data": "wallet_ledger"}],
                [{"text": "🏠 بازگشت به منوی اصلی", "callback_data": "home_back"}],
            ]
        }
        send_message(
            token,
            chat_id,
            f"👛 <b>کیف پول شما</b>\n"
            f"🆔 آیدی عددی شما (برای کپی لمس کنید): <code>{user_id}</code>\n\n"
            f"💵 موجودی فعلی: <b>{bal:,} تومان</b>\n\n"
            "با شارژ کیف پول می‌توانید بدون نیاز به تایید دوباره، خرید و تمدید را به‌صورت <b>آنی</b> انجام دهید.",
            reply_markup=kb,
        )
        return

    if t in (BTN_RESELLER, "/reseller", "🤝 پنل نمایندگی"):
        user_states.pop(user_id, None)
        show_reseller_panel(token, chat_id, env, user_id)
        return

    if t in (BTN_SUPPORT, "/support", "👨\u200d💻 پشتیبانی"):
        user_states[user_id] = {"step": "await_support"}
        send_message(
            token,
            chat_id,
            "👨‍💻 <b>پشتیبانی مستقیم</b>\n\nپیام، سوال یا اسکرین‌شات خود را همینجا بفرستید تا مستقیماً برای مدیریت ارسال شود:",
            env=env,
            user_id=user_id,
        )
        return

    if t == BTN_MENU_MODE:
        if user_id not in get_admin_ids(env):
            return
        user_states.pop(user_id, None)
        cur = get_user_menu_mode(user_id, env)
        nxt = {"both": "inline", "inline": "keyboard", "keyboard": "both"}.get(cur, "both")
        set_user_menu_mode(user_id, nxt)
        lbl = {"both": "کیبورد + شیشه‌ای", "inline": "فقط شیشه‌ای", "keyboard": "فقط کیبوردی"}[nxt]
        send_message(token, chat_id, f"✅ حالت منوی ربات برای همه کاربران به <b>{lbl}</b> تغییر یافت.", env=env, user_id=user_id)
        if nxt in ("both", "inline"):
            send_message(token, chat_id, "📌 <b>منوی دسترسی سریع:</b>", reply_markup=build_home_inline_keyboard(user_id, env))
        return

    if t == "/start" or t.startswith("/start ") or t == "/menu":
        user_states.pop(user_id, None)
        mode = get_user_menu_mode(user_id)
        send_message(
            token,
            chat_id,
            "👋 <b>به ربات فروش اشتراک خوش آمدید!</b>\n\n"
            "• خرید و تمدید فوری با حجم و مدت دلخواه\n"
            "• پرداخت کارت‌به‌کارت آنی و تتر خودکار (BEP20)\n"
            "• تست رایگان، کیف پول، معرفی دوستان و پشتیبانی\n\n"
            f"🆔 آیدی عددی شما (برای کپی لمس کنید): <code>{user_id}</code>",
            env=env,
            user_id=user_id,
        )
        if mode in ("both", "inline"):
            send_message(
                token,
                chat_id,
                "📌 <b>منوی دسترسی سریع:</b>",
                reply_markup=build_home_inline_keyboard(user_id, env),
            )
        return

    if t == BTN_TRIAL or t == "/trial":
        user_states.pop(user_id, None)
        if not check_maintenance(token, env, chat_id, user_id):
            return
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
        ok_up, err_up = charge_upstream_chain(DATA_DIR, trial_gb)
        if not ok_up:
            with users_lock:
                users = load_json(USERS_PATH)
                if str(user_id) in users:
                    users[str(user_id)]["last_trial_at"] = prev_trial_at
                    if prev_trial_at == 0:
                        users[str(user_id)]["trial_used"] = False
                    save_json(USERS_PATH, users)
            send_message(token, chat_id, err_up, env=env, user_id=user_id)
            return
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
            full_sub = f"{get_panel_config(env)['panel_url'].rstrip('/')}{u['subscription_url']}"
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
            refund_upstream_chain(DATA_DIR, trial_gb)
            with users_lock:
                users = load_json(USERS_PATH)
                if str(user_id) in users:
                    users[str(user_id)]["last_trial_at"] = prev_trial_at
                    if prev_trial_at == 0:
                        users[str(user_id)]["trial_used"] = False
                    save_json(USERS_PATH, users)
            send_message(token, chat_id, f"❌ خطا در ساخت تست رایگان: {html.escape(str(e))}", env=env, user_id=user_id)
        return

    if t in (BTN_REFERRAL, "/ref", "🤝 دعوت دوستان"):
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

    if t in (BTN_BUY, "/buy", "🛒 خرید اشتراک"):
        user_states.pop(user_id, None)
        if not check_maintenance(token, env, chat_id, user_id):
            return
        if not check_force_join(token, env, chat_id, user_id):
            return
        send_message(token, chat_id, "📦 <b>مرحله ۱:</b> حجم مورد نظر (به گیگابایت) را انتخاب کنید:", reply_markup=build_gb_keyboard("buy"))
        return

    if t in (BTN_RENEW, "/renew", "🔄 تمدید اشتراک"):
        user_states.pop(user_id, None)
        if not check_maintenance(token, env, chat_id, user_id):
            return
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
        buttons.append([{"text": "🏠 بازگشت به منوی اصلی", "callback_data": "home_back"}])
        send_message(
            token,
            chat_id,
            f"🔄 <b>تمدید اشتراک (با {disc}٪ تخفیف ویژه)</b>\n\nاشتراک مورد نظر را انتخاب کنید یا نام کاربری/لینک ساب خود را وارد کنید:",
            reply_markup={"inline_keyboard": buttons},
        )
        return

    if t in (BTN_MY_SUBS, "📦 اشتراک‌های من"):
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
                user_states[user_id] = st
                send_message(token, chat_id, "❌ اشتراکی با این نام کاربری یا لینک ساب پیدا نشد. دوباره بفرستید:", env=env, user_id=user_id)
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

        if step in ("await_topup_amount", "await_reseller_topup"):
            clean = t.replace(",", "").strip()
            cfg = get_settings(env)
            min_amt = 10000
            if step == "await_reseller_topup":
                min_gb = int(cfg.get("reseller_min_gb", 500))
                ppg = int(cfg.get("reseller_price_per_gb", 5000))
                min_amt = min_gb * ppg
            if not clean.isdigit() or int(clean) < min_amt or int(clean) > 200000000:
                send_message(token, chat_id, f"❌ مبلغ باید حداقل <b>{min_amt:,} تومان</b> باشد (مثلاً <code>{min_amt}</code>). دوباره بفرستید:", env=env, user_id=user_id)
                return
            user_states.pop(user_id, None)
            amt = int(clean)
            rate = get_usdt_toman_rate()
            usdt_amt = round(amt / rate, 2)
            card_lbl = "💳 شارژ کارت‌به‌کارت (فعال‌سازی آنی)" if cfg.get("card_instant_mode", "all") != "off" else "💳 شارژ کارت‌به‌کارت"
            kb = {
                "inline_keyboard": [
                    [{"text": card_lbl, "callback_data": f"topup_pay:card:{amt}"}],
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

        if step == "await_wallet_transfer":
            parts = t.split()
            if len(parts) != 2 or not parts[1].replace(",", "").isdigit():
                send_message(token, chat_id, "❌ فرمت صحیح:\n<code>@username 50000</code> یا <code>123456789 50000</code>\nدوباره بفرستید:", env=env, user_id=user_id)
                return
            t_uid = resolve_tg_user_id(parts[0])
            if t_uid is None:
                send_message(token, chat_id, "❌ کاربری با این آیدی یا @یوزرنیم در ربات پیدا نشد (طرف مقابل باید حداقل یک بار ربات را استارت زده باشد). دوباره بفرستید:", env=env, user_id=user_id)
                return
            amt = int(parts[1].replace(",", ""))
            if t_uid == user_id or amt < 10000:
                send_message(token, chat_id, "❌ مقصد نباید خودتان باشد و حداقل مبلغ انتقال <b>۱۰,۰۰۰ تومان</b> است. دوباره بفرستید:", env=env, user_id=user_id)
                return
            ok_bal, new_bal = adjust_wallet_balance(user_id, -amt, note=f"انتقال به {t_uid}")
            if not ok_bal:
                send_message(token, chat_id, f"❌ موجودی کیف پول کافی نیست (موجودی: <b>{new_bal:,} تومان</b>). مبلغ دیگری بفرستید:", env=env, user_id=user_id)
                return
            user_states.pop(user_id, None)
            _, rec_bal = adjust_wallet_balance(t_uid, amt, note=f"دریافت از {user_id}")
            send_message(token, chat_id, f"✅ مبلغ <b>{amt:,} تومان</b> به کاربر <code>{t_uid}</code> منتقل شد.\n👛 موجودی فعلی شما: <b>{new_bal:,} تومان</b>", env=env, user_id=user_id)
            try:
                send_message(token, t_uid, f"💸 مبلغ <b>{amt:,} تومان</b> از طرف کاربر <code>{user_id}</code> به کیف پول شما منتقل شد.\n👛 موجودی فعلی: <b>{rec_bal:,} تومان</b>", env=env, user_id=t_uid)
            except Exception:
                pass
            return

        if step == "await_support":
            user_states.pop(user_id, None)
            uname_tg = m["from"].get("username") or "-"
            now = int(time.time())
            tid = f"T{secrets.token_hex(3).upper()}"
            tickets = load_json(TICKETS_PATH)
            tickets[tid] = {
                "id": tid, "user_id": user_id, "username": uname_tg,
                "subject": (t[:60] + "...") if len(t) > 60 else t,
                "category": "پشتیبانی ربات", "status": "waiting_admin",
                "created_at": now, "updated_at": now,
                "messages": [{"sender": "user", "text": t, "ts": now}],
            }
            save_json(TICKETS_PATH, tickets)
            kb = {
                "inline_keyboard": [[
                    {"text": f"↩️ پاسخ به #{tid}", "callback_data": f"adm_tk_reply:{tid}"},
                    {"text": "🔒 بستن تیکت", "callback_data": f"adm_tk_close:{tid}"},
                ]]
            }
            for adm_id in get_admin_ids(env):
                try:
                    send_message(
                        token,
                        adm_id,
                        f"🎫 <b>تیکت پشتیبانی جدید</b> <code>#{tid}</code>\n👤 کاربر: <code>{user_id}</code> (@{html.escape(uname_tg)})\n\n💬 پیام:\n{html.escape(t)}",
                        reply_markup=kb,
                    )
                except Exception:
                    pass
            send_message(token, chat_id, f"✅ تیکت <code>#{tid}</code> ثبت و برای پشتیبانی ارسال شد. پاسخ از طریق همین ربات و مینی‌اپ نمایش داده می‌شود.", env=env, user_id=user_id)
            return

        if step == "await_tk_reply" and user_id in get_admin_ids(env):
            user_states.pop(user_id, None)
            tid = st.get("ticket_id", "")
            tickets = load_json(TICKETS_PATH)
            tk = tickets.get(tid)
            if not tk:
                send_message(token, chat_id, "❌ تیکت یافت نشد.", env=env, user_id=user_id)
                return
            now = int(time.time())
            tk.setdefault("messages", []).append({"sender": "admin", "text": t, "ts": now})
            tk["status"] = "answered"
            tk["updated_at"] = now
            save_json(TICKETS_PATH, tickets)
            t_uid = int(tk["user_id"])
            try:
                send_message(token, t_uid, f"🎫 <b>پاسخ پشتیبانی به تیکت <code>#{tid}</code></b>\n📌 {html.escape(tk['subject'])}\n\n💬 {html.escape(t)}")
            except Exception:
                pass
            send_message(token, chat_id, f"✅ پاسخ تیکت <code>#{tid}</code> ثبت و برای کاربر ارسال شد.", env=env, user_id=user_id)
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

        if step == "await_admin_wallet_user" and user_id in get_admin_ids(env):
            t_uid = resolve_tg_user_id(t)
            if t_uid is None:
                send_message(token, chat_id, "❌ کاربر با این آیدی یا @یوزرنیم پیدا نشد. دوباره بفرستید:", env=env, user_id=user_id)
                return
            user_states.pop(user_id, None)
            show_admin_wallet_user(token, chat_id, env, t_uid)
            return

        if step == "await_admin_wallet_amt" and user_id in get_admin_ids(env):
            t_uid = int(st["target_uid"])
            clean = t.replace(",", "").strip()
            try:
                delta = int(clean)
            except Exception:
                send_message(token, chat_id, "❌ فقط یک عدد بفرستید (مثلاً <code>100000</code> یا <code>-50000</code>). دوباره بفرستید:", env=env, user_id=user_id)
                return
            ok, new_bal = adjust_wallet_balance(t_uid, delta, note="تغییر دستی مدیریت")
            if not ok:
                send_message(token, chat_id, "❌ موجودی کاربر کافی نیست تا این مقدار کسر شود. عدد دیگری بفرستید:", env=env, user_id=user_id)
                return
            user_states.pop(user_id, None)
            send_message(token, chat_id, f"✅ موجودی کاربر <code>{t_uid}</code> به <b>{new_bal:,} تومان</b> تغییر یافت.", env=env, user_id=user_id)
            if delta > 0:
                try:
                    send_message(token, t_uid, f"🎁 کیف پول شما به مبلغ <b>{delta:,} تومان</b> توسط مدیریت شارژ شد.\n👛 موجودی فعلی: <b>{new_bal:,} تومان</b>", env=env, user_id=t_uid)
                except Exception:
                    pass
            show_admin_wallet_user(token, chat_id, env, t_uid)
            return

        if step == "await_admin_wallet" and user_id in get_admin_ids(env):
            parts = t.split()
            if len(parts) != 2:
                send_message(token, chat_id, "❌ فرمت صحیح: <code>@username 100000</code> یا <code>USER_ID AMOUNT</code> (دوباره بفرستید):", env=env, user_id=user_id)
                return
            t_uid = resolve_tg_user_id(parts[0])
            if t_uid is None:
                send_message(token, chat_id, "❌ کاربر با این آیدی یا @یوزرنیم پیدا نشد. دوباره بفرستید:", env=env, user_id=user_id)
                return
            try:
                delta = int(parts[1].replace(",", ""))
                ok, new_bal = adjust_wallet_balance(t_uid, delta)
                if not ok:
                    send_message(token, chat_id, "❌ موجودی کاربر کافی نیست تا این مقدار کسر شود. دوباره بفرستید:", env=env, user_id=user_id)
                    return
                user_states.pop(user_id, None)
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
            inv_id = st["inv_id"]
            inv = get_invoice(inv_id)
            if not inv:
                user_states.pop(user_id, None)
                send_message(token, chat_id, "❌ پیش‌فاکتور منقضی شده است، لطفاً دوباره انتخاب کنید.", env=env, user_id=user_id)
                return
            ok, err_msg, pct = validate_coupon(t, user_id)
            if not ok:
                send_message(token, chat_id, f"{err_msg}\nکد دیگری بفرستید:", env=env, user_id=user_id)
                return
            user_states.pop(user_id, None)
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
            parts = t.split()
            if len(parts) < 2:
                send_message(token, chat_id, "❌ فرمت صحیح: <code>CODE PERCENT [MAX_USES]</code>\nمثلاً: <code>OFF20 20 50</code> (دوباره بفرستید):", env=env, user_id=user_id)
                return
            try:
                code = parts[0].upper()
                pct = float(parts[1].replace("%", ""))
                max_uses = int(parts[2]) if len(parts) >= 3 else 1
                if pct <= 0 or pct > 90 or max_uses < 1:
                    raise ValueError("درصد باید بین ۱ تا ۹۰ و تعداد حداقل ۱ باشد")
                user_states.pop(user_id, None)
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
            target_uid = resolve_tg_user_id(q) if (q.isdigit() or q.startswith("@")) else None
            target_uname = "" if target_uid is not None else q
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
                blk, rem = is_user_blocked(target_uid)
                blk_s = f"⛔ مسدود ({max(1, (rem + 3599)//3600)} ساعت مانده)" if (blk and rem > 0) else ("⛔ مسدود دستی" if blk else "🟢 آزاد")
                lines.append(
                    f"👤 تلگرام: <code>{target_uid}</code> (@{html.escape(str(u_rec.get('username') or '-'))})\n"
                    f"👛 کیف پول: <b>{int(u_rec.get('balance_toman', 0)):,} تومان</b> | سفارش موفق: <b>{app_cnt}</b>\n"
                    f"🛡 وضعیت ربات: <b>{blk_s}</b>"
                )
                blk_btn_txt = "🔓 رفع مسدودیت ربات" if blk else "⛔ مسدود در ربات"
                kb_rows.append([
                    {"text": "👛 تغییر کیف پول", "callback_data": f"adm_u_wallet:{target_uid}"},
                    {"text": blk_btn_txt, "callback_data": f"adm_u_botblock:{target_uid}"},
                ])
                kb_rows.append([{"text": "📜 گزارش تراکنش‌ها", "callback_data": f"adm_u_ledger:{target_uid}"}])
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
                send_message(token, chat_id, "❌ موردی پیدا نشد. آیدی یا نام کاربری دیگری بفرستید:", env=env, user_id=user_id)
                return
            send_message(token, chat_id, "\n".join(lines), reply_markup={"inline_keyboard": kb_rows}, env=env, user_id=user_id)
            return

        if step == "await_res_subbot_token":
            if not get_reseller(user_id):
                user_states.pop(user_id, None)
                return
            if ":" not in t or len(t.strip()) < 25:
                send_message(token, chat_id, "❌ فرمت توکن صحیح نیست. توکن را از @BotFather بگیرید و دوباره بفرستید:", env=env, user_id=user_id)
                return
            try:
                sub_uname, _ = provision_tenant_bot(env, user_id, t.strip())
                user_states.pop(user_id, None)
                send_message(
                    token,
                    chat_id,
                    f"🎉 <b>ربات اختصاصی شما (@{html.escape(sub_uname)}) راه‌اندازی شد!</b>\n\n"
                    f"• همین الان وارد @{html.escape(sub_uname)} شوید و <code>/start</code> بزنید.\n"
                    "• تمام امکانات <b>👑 پنل ادمین</b> (تغییر قیمت‌ها، شماره کارت، ولت، تست رایگان، کد تخفیف و حتی دادن نمایندگی) برای شما فعال است.\n"
                    "• هزینهٔ عمده هر ساخت/تمدید به‌صورت خودکار از موجودی کیف پول نمایندگی شما کسر می‌شود.",
                    env=env,
                    user_id=user_id,
                )
            except Exception as e:
                send_message(token, chat_id, f"❌ خطا در راه‌اندازی ربات: {html.escape(str(e))}\nتوکن صحیح را دوباره بفرستید:", env=env, user_id=user_id)
            return

        if step == "await_res_bulk":
            if not get_reseller(user_id):
                user_states.pop(user_id, None)
                return
            parts = t.split()
            if len(parts) != 3 or not all(p.isdigit() for p in parts):
                send_message(token, chat_id, "❌ فرمت صحیح: <code>COUNT GB MONTHS</code>\nمثال: <code>5 30 1</code> (دوباره بفرستید):", env=env, user_id=user_id)
                return
            cnt, gb, months = int(parts[0]), int(parts[1]), int(parts[2])
            cfg = get_settings(env)
            max_acc = int(cfg.get("reseller_max_gb_per_acc", 200))
            if cnt < 1 or cnt > 30 or gb < 1 or months < 1 or months > 3:
                send_message(token, chat_id, "❌ تعداد بین ۱ تا ۳۰، حجم از ۱ گیگ و مدت بین ۱ تا ۳ ماه مجاز است. دوباره بفرستید:", env=env, user_id=user_id)
                return
            if max_acc > 0 and gb > max_acc:
                send_message(token, chat_id, f"❌ حداکثر حجم هر اکانت نماینده <b>{max_acc} گیگابایت</b> است. دوباره بفرستید:", env=env, user_id=user_id)
                return
            user_states.pop(user_id, None)
            ppg = get_reseller_price_per_gb(user_id, env)
            cost = cnt * gb * ppg
            bal = get_wallet_balance(user_id)
            if bal < cost:
                send_message(token, chat_id, f"❌ موجودی کیف پول کافی نیست.\nمبلغ: <b>{cost:,} تومان</b> | موجودی: <b>{bal:,} تومان</b>", env=env, user_id=user_id)
                return
            inv_id = save_invoice({"res_bulk": True, "cnt": cnt, "gb": gb, "months": months, "cost": cost})
            kb = {"inline_keyboard": [[{"text": f"✅ تایید و ساخت {cnt} اکانت ({cost:,} ت)", "callback_data": f"res_do_bulk:{inv_id}"}]]}
            send_message(
                token,
                chat_id,
                f"📦 <b>پیش‌نمایش ساخت گروهی نماینده</b>\n\n"
                f"• تعداد: <b>{cnt} اکانت</b>\n"
                f"• هر اکانت: <b>{gb} گیگ / {months} ماه</b>\n"
                f"• هزینهٔ کل ({ppg:,} ت/گیگ): <b>{cost:,} تومان</b>\n"
                f"• موجودی فعلی: <b>{bal:,} تومان</b>",
                reply_markup=kb,
            )
            return

        if step == "await_res_username":
            user_states.pop(user_id, None)
            if not get_reseller(user_id):
                return
            gb = int(st["gb"])
            months = int(st["months"])
            custom_u = "" if t in ("-", "خودکار", "auto") else re.sub(r"[^A-Za-z0-9_]", "", t.strip())
            if t not in ("-", "خودکار", "auto") and len(custom_u) < 3:
                send_message(token, chat_id, "❌ نام کاربری باید حداقل ۳ کاراکتر انگلیسی/عدد باشد (یا <code>-</code> بفرستید):", env=env, user_id=user_id)
                user_states[user_id] = st
                return
            ppg = get_reseller_price_per_gb(user_id, env)
            cost = gb * ppg
            bal = get_wallet_balance(user_id)
            if bal < cost:
                send_message(token, chat_id, f"❌ موجودی کیف پول کافی نیست.\nمبلغ: <b>{cost:,} تومان</b> | موجودی: <b>{bal:,} تومان</b>", env=env, user_id=user_id)
                return
            inv_id = save_invoice({"res_single": True, "gb": gb, "months": months, "custom_u": custom_u, "cost": cost})
            u_label = custom_u if custom_u else "تولید خودکار"
            kb = {"inline_keyboard": [[{"text": f"✅ پرداخت از کیف پول و ساخت ({cost:,} ت)", "callback_data": f"res_do_single:{inv_id}"}]]}
            send_message(
                token,
                chat_id,
                f"🧾 <b>تایید ساخت اکانت نماینده</b>\n\n"
                f"🆔 نام کاربری: <code>{html.escape(u_label)}</code>\n"
                f"📦 حجم: <b>{gb} گیگ</b> | ⏳ مدت: <b>{months} ماه</b>\n"
                f"💵 هزینه ({ppg:,} ت/گیگ): <b>{cost:,} تومان</b>",
                reply_markup=kb,
            )
            return

        if step == "await_res_custom_gb":
            if not get_reseller(user_id):
                user_states.pop(user_id, None)
                return
            if not t.isdigit() or int(t) < 1:
                send_message(token, chat_id, "❌ عدد معتبر وارد کنید:", env=env, user_id=user_id)
                return
            gb = int(t)
            cfg = get_settings(env)
            max_acc = int(cfg.get("reseller_max_gb_per_acc", 200))
            if max_acc > 0 and gb > max_acc:
                send_message(token, chat_id, f"❌ سقف حجم هر اکانت نماینده <b>{max_acc} گیگابایت</b> است. عدد دیگری بفرستید:", env=env, user_id=user_id)
                return
            user_states.pop(user_id, None)
            renew_u = st.get("renew_uname", "")
            rows = [
                [
                    {"text": "۱ ماهه", "callback_data": f"res_m:{gb}:1:{renew_u}"},
                    {"text": "۲ ماهه", "callback_data": f"res_m:{gb}:2:{renew_u}"},
                    {"text": "۳ ماهه", "callback_data": f"res_m:{gb}:3:{renew_u}"},
                ]
            ]
            send_message(token, chat_id, f"⏳ حجم: <b>{gb} گیگ</b>\nمدت زمان را انتخاب کنید:", reply_markup={"inline_keyboard": rows})
            return

        if step == "await_admin_reseller_add" and user_id in get_admin_ids(env):
            parts = t.split()
            if not parts:
                send_message(token, chat_id, "❌ فرمت صحیح: <code>@username [PRICE]</code> یا <code>USER_ID [PRICE]</code> (دوباره بفرستید):", env=env, user_id=user_id)
                return
            t_uid = resolve_tg_user_id(parts[0])
            if t_uid is None:
                send_message(token, chat_id, "❌ کاربر با این آیدی یا @یوزرنیم پیدا نشد. دوباره بفرستید:", env=env, user_id=user_id)
                return
            user_states.pop(user_id, None)
            ppg = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            set_reseller(t_uid, active=True, price_per_gb=ppg)
            send_message(token, chat_id, f"✅ کاربر <code>{t_uid}</code> به عنوان نماینده فعال شد.", env=env, user_id=user_id)
            try:
                send_message(token, t_uid, "🎉 <b>پنل نمایندگی برای شما فعال شد!</b>\nاز دکمهٔ <b>💼 پنل نمایندگی</b> در منو استفاده کنید.", env=env, user_id=t_uid)
            except Exception:
                pass
            return

        if step == "await_admin_setting" and user_id in get_admin_ids(env):
            user_states.pop(user_id, None)
            key = st["key"]
            try:
                if key in ("base_price_per_gb", "bulk_threshold_gb", "trial_gb", "trial_hours", "trial_cooldown_days", "reminder_days", "group_id", "max_pending_orders", "reseller_price_per_gb", "reseller_min_gb", "pending_alert_minutes", "backup_interval_hours"):
                    val = int(t.replace(",", "").strip())
                    if val <= 0:
                        raise ValueError("باید مثبت باشد")
                elif key == "reseller_max_gb_per_acc":
                    val = int(t.replace(",", "").strip())
                    if val < 0:
                        raise ValueError("نباید منفی باشد (0 برای نامحدود)")
                elif key == "reminder_pct":
                    val = int(t.replace("%", "").strip())
                    if val < 1 or val > 90:
                        raise ValueError("باید بین ۱ تا ۹۰ باشد")
                elif key == "gb_options":
                    nums = sorted({int(x) for x in re.split(r"[\s,،]+", t.strip()) if x.isdigit() and 1 <= int(x) <= 10000})
                    if not nums:
                        raise ValueError("حداقل یک عدد معتبر بین ۱ تا ۱۰۰۰۰ وارد کنید")
                    val = nums
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
                user_states[user_id] = st
                send_message(token, chat_id, f"❌ مقدار نامعتبر: {html.escape(str(e))}\nدوباره بفرستید:", env=env, user_id=user_id)
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

    send_message(token, chat_id, "❌ دستور یافت نشد، از منوی زیر انتخاب کنید:", reply_markup=build_home_inline_keyboard(user_id, env))


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
        blk, rem = is_user_blocked(user_id)
        if blk:
            txt_b = f"حساب شما تا {max(1, (rem + 3599)//3600)} ساعت دیگر مسدود است" if rem > 0 else "حساب شما توسط مدیریت مسدود است"
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": txt_b, "show_alert": True})
            return

    if data == "home_back":
        user_states.pop(user_id, None)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        mode = get_user_menu_mode(user_id, env)
        if mode in ("both", "inline"):
            if cb.get("message", {}).get("text"):
                edit_message(token, chat_id, msg_id, "📌 <b>منوی دسترسی سریع:</b>", reply_markup=build_home_inline_keyboard(user_id, env))
            else:
                send_message(token, chat_id, "📌 <b>منوی دسترسی سریع:</b>", reply_markup=build_home_inline_keyboard(user_id, env))
        else:
            send_message(token, chat_id, "🏠 <b>منوی اصلی:</b>", env=env, user_id=user_id)
        return

    if data == "menu_mode":
        if user_id not in get_admin_ids(env):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "⛔ این گزینه فقط برای ادمین است.", "show_alert": True})
            return
        cur = get_user_menu_mode(user_id, env)
        nxt = {"both": "inline", "inline": "keyboard", "keyboard": "both"}.get(cur, "inline")
        set_user_menu_mode(user_id, nxt)
        lbl = {"both": "کیبورد + شیشه‌ای", "inline": "فقط شیشه‌ای", "keyboard": "فقط کیبوردی"}[nxt]
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": f"🎛 حالت منو: {lbl}"})
        show_admin_panel(token, chat_id, env, message_id=msg_id)
        send_message(token, chat_id, f"✅ حالت منوی ربات برای همه کاربران به <b>{lbl}</b> تغییر یافت.", env=env, user_id=user_id)
        return

    if data.startswith("home:") or data == "adm_open":
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        cmd_map = {
            "home:buy": BTN_BUY,
            "home:renew": BTN_RENEW,
            "home:subs": BTN_MY_SUBS,
            "home:wallet": BTN_WALLET,
            "home:ref": BTN_REFERRAL,
            "home:support": BTN_SUPPORT,
            "home:trial": BTN_TRIAL,
            "home:check": BTN_CHECK,
            "adm_open": BTN_ADMIN,
        }
        mapped = cmd_map.get(data)
        if mapped:
            handle_text(token, env, {"chat": {"id": chat_id}, "from": cb["from"], "text": mapped})
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

    if data == "res_panel":
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        show_reseller_panel(token, chat_id, env, user_id, message_id=msg_id)
        return

    if data == "res_subbot":
        rec = get_reseller(user_id)
        if not rec:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "دسترسی نمایندگی ندارید", "show_alert": True})
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        svc = rec.get("sub_bot_service")
        sub_u = rec.get("sub_bot_username")
        if svc and sub_u:
            st_out = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True).stdout.strip()
            st_lbl = "🟢 فعال" if st_out == "active" else f"🔴 متوقف ({st_out})"
            txt = (
                f"🤖 <b>ربات اختصاصی شما: @{html.escape(sub_u)}</b>\n\n"
                f"• وضعیت سرویس: <b>{st_lbl}</b>\n"
                "• می‌توانید قیمت‌ها، شماره کارت، ولت، تست رایگان و حتی زیرنماینده‌های خودتان را داخل همان ربات مدیریت کنید."
            )
            kb = {
                "inline_keyboard": [
                    [{"text": "🔄 ری‌استارت ربات من", "callback_data": "res_subbot_restart"}, {"text": "⏹ توقف ربات", "callback_data": "res_subbot_stop"}],
                    [{"text": "🔑 تغییر توکن ربات", "callback_data": "res_subbot_set"}, {"text": "🔙 بازگشت", "callback_data": "res_panel"}],
                ]
            }
        else:
            txt = (
                "🤖 <b>راه‌اندازی ربات اختصاصی (White-Label)</b>\n\n"
                "با ثبت توکن ربات تلگرام خودتان (از @BotFather):\n"
                "• ربات اختصاصی شما با برند و کارت بانکی/تتر خودتان راه‌اندازی می‌شود.\n"
                "• تمام امکانات <b>👑 پنل ادمین</b> (به جز مشخصات پنل اصلی) در اختیار شماست و حتی می‌توانید زیرنماینده بگیرید.\n"
                "• هنگام هر فروش، هزینهٔ عمدهٔ حجم به‌صورت خودکار از کیف پول نمایندگی شما کسر می‌شود."
            )
            kb = {
                "inline_keyboard": [
                    [{"text": "➕ ثبت توکن ربات و راه‌اندازی", "callback_data": "res_subbot_set"}],
                    [{"text": "🔙 بازگشت", "callback_data": "res_panel"}],
                ]
            }
        edit_message(token, chat_id, msg_id, txt, reply_markup=kb)
        return

    if data == "res_subbot_set":
        if not get_reseller(user_id):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_res_subbot_token"}
        send_message(token, chat_id, "🤖 توکن ربات جدید را از @BotFather گرفته و همینجا بفرستید:", env=env, user_id=user_id)
        return

    if data in ("res_subbot_restart", "res_subbot_stop"):
        rec = get_reseller(user_id)
        if not rec or not rec.get("sub_bot_service"):
            return
        svc = rec["sub_bot_service"]
        cmd = "restart" if data == "res_subbot_restart" else "stop"
        subprocess.run(["systemctl", cmd, svc], check=False, timeout=15)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ انجام شد"})
        edit_message(token, chat_id, msg_id, f"✅ دستور <code>{cmd}</code> روی ربات شما اعمال شد.", reply_markup={"inline_keyboard": [[{"text": "🔙 بازگشت", "callback_data": "res_subbot"}]]})
        return

    if data == "reseller_topup":
        cfg = get_settings(env)
        min_gb = int(cfg.get("reseller_min_gb", 500))
        ppg = int(cfg.get("reseller_price_per_gb", 5000))
        min_tmn = min_gb * ppg
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_reseller_topup"}
        send_message(
            token,
            chat_id,
            f"💵 مبلغ شارژ اولیه نمایندگی را به تومان وارد کنید\n"
            f"(حداقل <b>{min_tmn:,} تومان</b> معادل {min_gb:,} گیگ):",
            env=env,
            user_id=user_id,
        )
        return

    if data == "reseller_apply":
        cfg = get_settings(env)
        bal = get_wallet_balance(user_id)
        min_gb = int(cfg.get("reseller_min_gb", 500))
        min_tmn = min_gb * int(cfg.get("reseller_price_per_gb", 5000))
        if bal < min_tmn:
            tg_api(
                token,
                "answerCallbackQuery",
                {
                    "callback_query_id": cb_id,
                    "text": f"ابتدا باید حداقل {min_tmn:,} تومان ({min_gb} گیگ) کیف پول خود را شارژ کنید",
                    "show_alert": True,
                },
            )
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ درخواست ارسال شد"})
        uname_tg = cb["from"].get("username") or "-"
        kb = {
            "inline_keyboard": [[
                {"text": "✅ تایید و فعال‌سازی نماینده", "callback_data": f"adm_res_ok:{user_id}"},
                {"text": "❌ رد درخواست", "callback_data": f"adm_res_no:{user_id}"},
            ]]
        }
        for adm_id in get_admin_ids(env):
            try:
                send_message(
                    token,
                    adm_id,
                    f"💼 <b>درخواست نمایندگی جدید</b>\n\n"
                    f"👤 کاربر: <code>{user_id}</code> (@{html.escape(uname_tg)})\n"
                    f"👛 موجودی کیف پول: <b>{bal:,} تومان</b> (کف پیشنهادی: {min_tmn:,} ت)",
                    reply_markup=kb,
                )
            except Exception:
                pass
        edit_message(token, chat_id, msg_id, "✅ درخواست نمایندگی شما برای مدیریت ارسال شد. پس از بررسی اطلاع‌رسانی می‌شود.")
        return

    if data == "res_create" or data.startswith("res_renew:"):
        if not get_reseller(user_id):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "دسترسی نمایندگی ندارید", "show_alert": True})
            return
        if not check_maintenance(token, env, chat_id, user_id):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
            return
        renew_u = data.split(":", 1)[1] if data.startswith("res_renew:") else ""
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        cfg = get_settings(env)
        max_acc = int(cfg.get("reseller_max_gb_per_acc", 200))
        opts = [g for g in [20, 30, 50, 100, 200] if max_acc <= 0 or g <= max_acc]
        rows = [[{"text": f"{g} گیگ", "callback_data": f"res_gb:{g}:{renew_u}"} for g in opts[i:i+3]] for i in range(0, len(opts), 3)]
        rows.append([{"text": "✏️ حجم دلخواه", "callback_data": f"res_gb:custom:{renew_u}"}])
        title = f"🔄 تمدید اکانت نماینده (<code>{html.escape(renew_u)}</code>)" if renew_u else "➕ ساخت اکانت جدید نماینده"
        send_message(token, chat_id, f"<b>{title}</b>\nحجم را انتخاب کنید:", reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("res_gb:"):
        if not get_reseller(user_id):
            return
        _, gb_s, renew_u = data.split(":", 2)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        if gb_s == "custom":
            user_states[user_id] = {"step": "await_res_custom_gb", "renew_uname": renew_u}
            send_message(token, chat_id, "✏️ حجم دلخواه را به عدد (گیگابایت) بفرستید:", env=env, user_id=user_id)
            return
        gb = int(gb_s)
        rows = [
            [
                {"text": "۱ ماهه", "callback_data": f"res_m:{gb}:1:{renew_u}"},
                {"text": "۲ ماهه", "callback_data": f"res_m:{gb}:2:{renew_u}"},
                {"text": "۳ ماهه", "callback_data": f"res_m:{gb}:3:{renew_u}"},
            ]
        ]
        edit_message(token, chat_id, msg_id, f"⏳ حجم: <b>{gb} گیگ</b>\nمدت زمان را انتخاب کنید:", reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("res_m:"):
        if not get_reseller(user_id):
            return
        _, gb_s, m_s, renew_u = data.split(":", 3)
        gb, months = int(gb_s), int(m_s)
        if months < 1 or months > 3:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "حداکثر مدت مجاز برای نماینده ۳ ماه است", "show_alert": True})
            return
        if not renew_u:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
            user_states[user_id] = {"step": "await_res_username", "gb": gb, "months": months}
            send_message(token, chat_id, "✏️ نام کاربری دلخواه (انگلیسی) را بفرستید یا <code>-</code> بزنید تا خودکار ساخته شود:", env=env, user_id=user_id)
            return
        ppg = get_reseller_price_per_gb(user_id, env)
        cost = gb * ppg
        ok_bal, new_bal = adjust_wallet_balance(user_id, -cost)
        if not ok_bal:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": f"موجودی کافی نیست (نیاز: {cost:,} ت)", "show_alert": True})
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "⏳ در حال تمدید..."})
        ok_up, err_up = charge_upstream_chain(DATA_DIR, gb)
        if not ok_up:
            adjust_wallet_balance(user_id, cost)
            send_message(token, chat_id, err_up, env=env, user_id=user_id)
            return
        try:
            u = renew_panel_user(env, renew_u, gb, months)
            full_sub = f"{get_panel_config(env)['panel_url'].rstrip('/')}{u['subscription_url']}"
            order_id = secrets.token_hex(4).upper()
            with orders_lock:
                orders = load_json(ORDERS_PATH)
                orders[order_id] = {
                    "id": order_id, "type": "reseller_renew", "target_username": renew_u,
                    "user_id": user_id, "tg_username": cb["from"].get("username") or "-",
                    "gb": gb, "months": months, "pay_method": "reseller_wallet",
                    "tx_key": f"res:{order_id}", "total_toman": cost, "usdt_amount": 0, "usdt_rate": 0,
                    "status": "approved", "created_at": int(time.time()), "panel_username": renew_u, "sub_url": full_sub,
                }
                save_json(ORDERS_PATH, orders)
            record_reseller_sale(user_id, gb, count=0)
            send_qr_with_sub(token, chat_id, full_sub, f"✅ <b>تمدید نماینده انجام شد!</b>\n🆔 <code>{html.escape(renew_u)}</code> (+{gb}GB / +{months} ماه)\n👛 موجودی: <b>{new_bal:,} ت</b>\n<code>{html.escape(full_sub)}</code>", env, user_id)
        except Exception as e:
            refund_upstream_chain(DATA_DIR, gb)
            adjust_wallet_balance(user_id, cost)
            send_message(token, chat_id, f"❌ خطا در تمدید (مبلغ برگشت خورد): {html.escape(str(e))}", env=env, user_id=user_id)
        return

    if data.startswith("res_do_single:"):
        if not get_reseller(user_id):
            return
        inv_id = data.split(":", 1)[1]
        inv = claim_invoice(inv_id)
        if not inv or not inv.get("res_single"):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "منقضی یا قبلاً پرداخت شده", "show_alert": True})
            return
        gb, months, cost = int(inv["gb"]), int(inv["months"]), int(inv["cost"])
        custom_u = inv.get("custom_u", "")
        ok_bal, new_bal = adjust_wallet_balance(user_id, -cost, note=f"ساخت نماینده {gb}GB/{months}M")
        if not ok_bal:
            update_invoice(inv_id, {"paid": False})
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "موجودی کافی نیست", "show_alert": True})
            return
        ok_up, err_up = charge_upstream_chain(DATA_DIR, gb)
        if not ok_up:
            adjust_wallet_balance(user_id, cost)
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "موجودی بالادست کافی نیست", "show_alert": True})
            send_message(token, chat_id, err_up, env=env, user_id=user_id)
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "⏳ در حال ساخت..."})
        order_id = secrets.token_hex(4).upper()
        prefix_u = custom_u if custom_u else f"r_{str(user_id)[-4:]}_{order_id.lower()}"
        try:
            u = create_panel_user(env, order_id, user_id, gb, months, "reseller_wallet", custom_prefix=prefix_u)
            uname = u["username"]
            full_sub = f"{get_panel_config(env)['panel_url'].rstrip('/')}{u['subscription_url']}"
            with orders_lock:
                orders = load_json(ORDERS_PATH)
                orders[order_id] = {
                    "id": order_id, "type": "reseller_buy", "target_username": "",
                    "user_id": user_id, "tg_username": cb["from"].get("username") or "-",
                    "gb": gb, "months": months, "pay_method": "reseller_wallet",
                    "tx_key": f"res:{order_id}", "total_toman": cost, "usdt_amount": 0, "usdt_rate": 0,
                    "status": "approved", "created_at": int(time.time()), "panel_username": uname, "sub_url": full_sub,
                }
                save_json(ORDERS_PATH, orders)
            record_reseller_sale(user_id, gb, count=1)
            edit_message(token, chat_id, msg_id, f"✅ اکانت <code>{html.escape(uname)}</code> ساخته شد (موجودی: <b>{new_bal:,} ت</b>).")
            send_qr_with_sub(token, chat_id, full_sub, f"🎉 <b>اکانت نماینده آماده است!</b>\n🆔 <code>{html.escape(uname)}</code>\n📦 {gb}GB / {months} ماه\n\n<code>{html.escape(full_sub)}</code>", env, user_id)
        except Exception as e:
            refund_upstream_chain(DATA_DIR, gb)
            adjust_wallet_balance(user_id, cost)
            send_message(token, chat_id, f"❌ خطا در ساخت (مبلغ برگشت خورد): {html.escape(str(e))}", env=env, user_id=user_id)
        return

    if data == "res_bulk":
        if not get_reseller(user_id):
            return
        if not check_maintenance(token, env, chat_id, user_id):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_res_bulk"}
        send_message(token, chat_id, "📦 تعداد، حجم و مدت را با فاصله بفرستید:\n<code>COUNT GB MONTHS</code>\nمثال: <code>5 30 1</code> (۵ اکانت ۳۰ گیگ ۱ ماهه)", env=env, user_id=user_id)
        return

    if data.startswith("res_do_bulk:"):
        if not get_reseller(user_id):
            return
        inv_id = data.split(":", 1)[1]
        inv = claim_invoice(inv_id)
        if not inv or not inv.get("res_bulk"):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "منقضی یا قبلاً پرداخت شده", "show_alert": True})
            return
        cnt, gb, months, cost = int(inv["cnt"]), int(inv["gb"]), int(inv["months"]), int(inv["cost"])
        ok_bal, new_bal = adjust_wallet_balance(user_id, -cost, note=f"ساخت گروهی {cnt}×{gb}GB")
        if not ok_bal:
            update_invoice(inv_id, {"paid": False})
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "موجودی کافی نیست", "show_alert": True})
            return
        ok_up, err_up = charge_upstream_chain(DATA_DIR, cnt * gb)
        if not ok_up:
            adjust_wallet_balance(user_id, cost)
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "موجودی بالادست کافی نیست", "show_alert": True})
            send_message(token, chat_id, err_up, env=env, user_id=user_id)
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "⏳ در حال ساخت گروهی..."})
        edit_message(token, chat_id, msg_id, f"⏳ در حال ساخت {cnt} اکانت...")
        panel_base = get_panel_config(env)["panel_url"].rstrip("/")
        created_lines = []
        created_ok = 0
        for _ in range(cnt):
            oid = secrets.token_hex(4).upper()
            pref = f"r_{str(user_id)[-4:]}_{oid.lower()}"
            try:
                u = create_panel_user(env, oid, user_id, gb, months, "reseller_wallet", custom_prefix=pref)
                uname = u["username"]
                full_sub = f"{panel_base}{u['subscription_url']}"
                with orders_lock:
                    orders = load_json(ORDERS_PATH)
                    orders[oid] = {
                        "id": oid, "type": "reseller_buy", "target_username": "",
                        "user_id": user_id, "tg_username": cb["from"].get("username") or "-",
                        "gb": gb, "months": months, "pay_method": "reseller_wallet",
                        "tx_key": f"res:{oid}", "total_toman": gb * get_reseller_price_per_gb(user_id, env), "usdt_amount": 0, "usdt_rate": 0,
                        "status": "approved", "created_at": int(time.time()), "panel_username": uname, "sub_url": full_sub,
                    }
                    save_json(ORDERS_PATH, orders)
                created_ok += 1
                created_lines.append(f"• <code>{html.escape(uname)}</code>\n<code>{html.escape(full_sub)}</code>")
            except Exception as e:
                created_lines.append(f"• ❌ خطا: {html.escape(str(e))}")
        if created_ok < cnt:
            refund_upstream_chain(DATA_DIR, (cnt - created_ok) * gb)
            refund = (cnt - created_ok) * gb * get_reseller_price_per_gb(user_id, env)
            _, new_bal = adjust_wallet_balance(user_id, refund)
        if created_ok > 0:
            record_reseller_sale(user_id, created_ok * gb, count=created_ok)
        send_message(token, chat_id, f"✅ <b>{created_ok} از {cnt} اکانت ساخته شد</b> (موجودی: <b>{new_bal:,} ت</b>)\n\n" + "\n\n".join(created_lines), env=env, user_id=user_id)
        return

    if data == "res_list":
        if not get_reseller(user_id):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        orders = load_json(ORDERS_PATH)
        seen = set()
        mine = []
        for o in sorted(orders.values(), key=lambda x: x.get("created_at", 0), reverse=True):
            un = o.get("panel_username")
            if o.get("user_id") == user_id and o.get("type", "").startswith("reseller_") and un and un not in seen:
                seen.add(un)
                mine.append(o)
        if not mine:
            send_message(token, chat_id, "📭 هنوز اکانتی در پنل نمایندگی نساخته‌اید.", env=env, user_id=user_id)
            return
        for o in mine[:10]:
            un = o["panel_username"]
            kb = {
                "inline_keyboard": [
                    [{"text": "📊 وضعیت", "callback_data": f"sub_status:{un}"}, {"text": "🔄 تمدید", "callback_data": f"res_renew:{un}"}],
                    [{"text": "🔑 تغییر ساب", "callback_data": f"sub_revoke:{un}"}, {"text": "⏸ قطع/وصل", "callback_data": f"res_toggle:{un}"}],
                    [{"text": "🗑 حذف و برگشت مبلغ", "callback_data": f"res_del:{un}"}],
                ]
            }
            send_message(token, chat_id, f"🆔 <code>{html.escape(un)}</code> ({o['gb']}GB / {o['months']} ماه)\n<code>{html.escape(o.get('sub_url', ''))}</code>", reply_markup=kb)
        return

    if data.startswith("res_toggle:"):
        if not get_reseller(user_id):
            return
        un = data.split(":", 1)[1]
        orders = load_json(ORDERS_PATH)
        owns = any(o.get("user_id") == user_id and o.get("panel_username") == un for o in orders.values())
        if not owns:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "دسترسی ندارید", "show_alert": True})
            return
        try:
            pu = panel_request(env, f"/api/user/{urllib.parse.quote(un)}")
            new_st = "disabled" if pu.get("status") == "active" else "active"
            panel_request(env, f"/api/user/{urllib.parse.quote(un)}", method="PUT", body={"status": new_st})
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": f"وضعیت: {new_st}"})
            send_message(token, chat_id, f"✅ وضعیت اکانت <code>{html.escape(un)}</code> به <b>{new_st}</b> تغییر یافت.", env=env, user_id=user_id)
        except Exception as e:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "خطا", "show_alert": True})
            send_message(token, chat_id, f"❌ خطا: {html.escape(str(e))}", env=env, user_id=user_id)
        return

    if data.startswith("res_del:"):
        if not get_reseller(user_id):
            return
        un = data.split(":", 1)[1]
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        kb = {
            "inline_keyboard": [[
                {"text": "✅ بله، حذف و برگشت مبلغ", "callback_data": f"res_del_yes:{un}"},
                {"text": "❌ انصراف", "callback_data": "res_list"},
            ]]
        }
        send_message(
            token,
            chat_id,
            f"⚠️ آیا از حذف اکانت <code>{html.escape(un)}</code> مطمئن هستید؟\n<i>(فقط در صورتی که کمتر از ۵٪ مصرف شده باشد قابل حذف و برگشت وجه است)</i>",
            reply_markup=kb,
        )
        return

    if data.startswith("res_del_yes:"):
        if not get_reseller(user_id):
            return
        un = data.split(":", 1)[1]
        with orders_lock:
            orders = load_json(ORDERS_PATH)
            matched = [
                (oid, o) for oid, o in orders.items()
                if o.get("user_id") == user_id and o.get("panel_username") == un and o.get("status") == "approved" and o.get("type", "").startswith("reseller_")
            ]
        if not matched:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "اکانت یافت نشد یا قبلاً حذف شده", "show_alert": True})
            return
        try:
            pu = panel_request(env, f"/api/user/{urllib.parse.quote(un)}")
            used = int(pu.get("used_traffic") or 0)
            lim = int(pu.get("data_limit") or 1)
            if used > max(50 * 1024 * 1024, int(lim * 0.05)):
                tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "بیش از ۵٪ حجم مصرف شده و قابل برگشت نیست", "show_alert": True})
                return
            panel_request(env, f"/api/user/{urllib.parse.quote(un)}", method="DELETE")
        except Exception as e:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "خطا در حذف از پنل", "show_alert": True})
            send_message(token, chat_id, f"❌ خطا در حذف اکانت: {html.escape(str(e))}", env=env, user_id=user_id)
            return

        refund_amt = sum(int(o.get("total_toman") or 0) for _, o in matched)
        refund_gb = sum(int(o.get("gb") or 0) for _, o in matched)
        with orders_lock:
            orders = load_json(ORDERS_PATH)
            for oid, _ in matched:
                if oid in orders:
                    orders[oid]["status"] = "refunded"
            save_json(ORDERS_PATH, orders)
        _, new_bal = adjust_wallet_balance(user_id, refund_amt)
        refund_upstream_chain(DATA_DIR, refund_gb)
        record_reseller_sale(user_id, -refund_gb, count=-1)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ حذف و برگشت وجه انجام شد"})
        edit_message(token, chat_id, msg_id, f"✅ اکانت <code>{html.escape(un)}</code> حذف شد و مبلغ <b>{refund_amt:,} تومان</b> به کیف پول شما برگشت.\n👛 موجودی فعلی: <b>{new_bal:,} تومان</b>")
        return

    if data == "wallet_ledger" or data == "sales_ledger" or data.startswith("adm_u_ledger:"):
        target_uid = int(data.split(":", 1)[1]) if data.startswith("adm_u_ledger:") else user_id
        if data.startswith("adm_u_ledger:") and user_id not in get_admin_ids(env):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        ledger_key = f"sales_{target_uid}" if data == "sales_ledger" else str(target_uid)
        with users_file_lock(USERS_PATH):
            entries = load_json(LEDGER_PATH).get(ledger_key, [])
        if not entries:
            send_message(token, chat_id, "📭 هنوز تراکنشی برای این حساب ثبت نشده است.", env=env, user_id=user_id)
            return
        lines = [f"📜 <b>گزارش تراکنش‌های حساب <code>{target_uid}</code> (۱۵ مورد آخر):</b>\n"]
        for e in reversed(entries[-15:]):
            dt_s = (datetime.fromtimestamp(int(e["ts"]), timezone.utc) + timedelta(hours=3, minutes=30)).strftime("%m-%d %H:%M")
            d = int(e["delta"])
            sign = "+" if d > 0 else ""
            lines.append(f"• <code>{dt_s}</code> | <b>{sign}{d:,}</b> ت | مانده: <code>{int(e['balance']):,}</code> | {html.escape(str(e.get('note', '')))}")
        send_message(token, chat_id, "\n".join(lines), env=env, user_id=user_id)
        return

    if data in ("adm_nodes", "res_nodes"):
        if data == "adm_nodes" and user_id not in get_admin_ids(env):
            return
        if data == "res_nodes" and not get_reseller(user_id):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        try:
            nodes = panel_request(env, "/api/nodes?limit=100").get("nodes", [])
            mask_ip = (data == "res_nodes") or is_tenant_bot(env)
            online = sum(1 for n in nodes if n.get("status") == "connected")
            lines = [f"📡 <b>وضعیت سرورها</b> (🟢 {online} آنلاین از {len(nodes)})\n"]
            for idx, n in enumerate(nodes, 1):
                name = str(n.get("name") or f"Server {idx}")
                if mask_ip:
                    name = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "***", name).strip()
                    if not name or name == "***":
                        name = f"سرور #{idx}"
                ic = "🟢 آنلاین" if n.get("status") == "connected" else f"🔴 {n.get('status') or 'قطع'}"
                lines.append(f"• <b>{html.escape(name)}</b>: {ic}")
            send_message(token, chat_id, "\n".join(lines), env=env, user_id=user_id)
        except Exception as e:
            send_message(token, chat_id, f"❌ خطا در دریافت وضعیت سرورها: {html.escape(str(e))}", env=env, user_id=user_id)
        return

    if data == "wallet_transfer":
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_wallet_transfer"}
        send_message(
            token,
            chat_id,
            "💸 <b>@یوزرنیم</b> یا <b>آیدی عددی</b> مقصد را همراه با مبلغ (به تومان) بفرستید:\n"
            "• <code>@username 50000</code>\n"
            "• <code>123456789 50000</code>",
            env=env,
            user_id=user_id,
        )
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
            instant_note = "⚡ پس از واریز، <b>عکس فیش یا کد پیگیری</b> را همینجا بفرستید تا کیف پول شما <b>بلافاصله و خودکار</b> شارژ شود:" if cfg.get("card_instant_mode", "all") != "off" else "📸 پس از واریز، <b>عکس فیش یا کد پیگیری</b> را همینجا بفرستید:"
            txt = (
                "💳 <b>شارژ کیف پول (کارت‌به‌کارت — آنی و خودکار)</b>\n\n"
                f"شماره کارت: <code>{html.escape(str(cfg['card_number']))}</code>\n"
                f"به نام: <b>{html.escape(str(cfg['card_owner']))}</b>\n"
                f"مبلغ دقیق و یکتا: <code>{card_amt:,}</code> تومان\n"
                "<i>(عدد رندوم برای شناسایی خودکار واریز شما اضافه شده است)</i>\n\n"
                f"{instant_note}"
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
                full_sub = f"{get_panel_config(env)['panel_url'].rstrip('/')}{u['subscription_url']}"
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
                full_sub = f"{get_panel_config(env)['panel_url'].rstrip('/')}{u['subscription_url']}"
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
            claimed = claim_invoice(inv_id)
            if not claimed:
                tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "این پیش‌فاکتور قبلاً پرداخت شده یا منقضی شده است", "show_alert": True})
                return
            ok_bal, new_bal = adjust_wallet_balance(user_id, -p["total_toman"], note=f"پرداخت فاکتور {gb}GB/{months}M")
            if not ok_bal:
                update_invoice(inv_id, {"paid": False})
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
            instant_note = "⚡ پس از واریز، <b>عکس فیش یا کد پیگیری</b> را همینجا بفرستید تا اشتراک شما <b>بلافاصله و خودکار</b> فعال شود:" if cfg.get("card_instant_mode", "all") != "off" else "📸 پس از واریز، <b>عکس فیش یا کد پیگیری</b> را همینجا بفرستید:"
            txt = (
                "💳 <b>پرداخت کارت‌به‌کارت (فعال‌سازی آنی و خودکار)</b>\n\n"
                f"شماره کارت: <code>{html.escape(str(cfg['card_number']))}</code>\n"
                f"به نام: <b>{html.escape(str(cfg['card_owner']))}</b>\n"
                f"مبلغ دقیق و یکتا: <code>{card_amt:,}</code> تومان\n"
                "<i>(عدد رندوم برای شناسایی خودکار واریز شما اضافه شده است)</i>\n\n"
                f"{instant_note}"
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

    if data.startswith("adm_tk_reply:"):
        if user_id not in get_admin_ids(env):
            return
        tid = data.split(":", 1)[1]
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_tk_reply", "ticket_id": tid}
        send_message(token, chat_id, f"↩️ پاسخ خود برای تیکت <code>#{html.escape(tid)}</code> را بنویسید:", env=env, user_id=user_id)
        return

    if data.startswith("adm_tk_close:"):
        if user_id not in get_admin_ids(env):
            return
        tid = data.split(":", 1)[1]
        tickets = load_json(TICKETS_PATH)
        tk = tickets.get(tid)
        if tk:
            tk["status"] = "closed"
            tk["updated_at"] = int(time.time())
            save_json(TICKETS_PATH, tickets)
            try:
                send_message(token, int(tk["user_id"]), f"🔒 تیکت <code>#{tid}</code> توسط پشتیبانی بسته شد.")
            except Exception:
                pass
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": f"تیکت #{tid} بسته شد"})
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
        with users_file_lock(USERS_PATH):
            users = load_json(USERS_PATH)
        recent = sorted(
            [(int(k), v) for k, v in users.items() if k.isdigit()],
            key=lambda kv: int(kv[1].get("last_seen") or 0),
            reverse=True,
        )[:8]
        kb_rows = []
        for uid_i, rec in recent:
            un = rec.get("username") or str(uid_i)
            bal_i = int(rec.get("balance_toman") or 0)
            kb_rows.append([{"text": f"👤 @{un} ({bal_i:,} ت)", "callback_data": f"adm_w_pick:{uid_i}"}])
        kb_rows.append([{"text": "✏️ وارد کردن آیدی یا @یوزرنیم", "callback_data": "adm_w_search"}])
        kb_rows.append([{"text": "🔙 بازگشت به پنل", "callback_data": "adm_refresh"}])
        edit_message(token, chat_id, msg_id, "👛 <b>شارژ/تغییر دستی کیف پول</b>\n\nکاربر مورد نظر را انتخاب کنید:", reply_markup={"inline_keyboard": kb_rows})
        return

    if data == "adm_backup":
        if user_id not in get_admin_ids(env) or is_tenant_bot(env):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "⏳ در حال ساخت بکاپ..."})
        send_backup_zip(token, env, targets={user_id}, use_backup_bot=False)
        return

    if data == "adm_restore":
        if user_id not in get_admin_ids(env) or is_tenant_bot(env):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_restore_zip"}
        send_message(
            token,
            chat_id,
            "♻️ <b>بازیابی کامل از بکاپ (Restore)</b>\n\n"
            "فایل زیپ بکاپ (<code>.zip</code>) را همینجا ارسال کنید تا دیتابیس و تنظیمات ربات اصلی و <b>تمام ربات‌های نماینده</b> به‌صورت خودکار بازیابی و راه‌اندازی شوند.",
            env=env,
            user_id=user_id,
        )
        return

    if data == "adm_toggle_backup":
        if user_id not in get_admin_ids(env) or is_tenant_bot(env):
            return
        cur = bool(get_settings(env).get("backup_enabled", True))
        update_setting(env, "backup_enabled", not cur)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ وضعیت بکاپ خودکار تغییر یافت"})
        show_admin_panel(token, chat_id, env, message_id=msg_id)
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

    if data == "adm_resellers":
        if user_id not in get_admin_ids(env):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        with resellers_lock:
            res_all = load_json(RESELLERS_PATH)
        lines = ["💼 <b>مدیریت نمایندگان:</b>\n"]
        kb_rows = [[{"text": "➕ افزودن نماینده با آیدی", "callback_data": "adm_res_add"}]]
        active_list = [(k, v) for k, v in res_all.items() if v.get("active")]
        if not active_list:
            lines.append("هنوز نمایندهٔ فعالی ثبت نشده است.")
        else:
            for uid_s, r in active_list[:15]:
                bal = get_wallet_balance(int(uid_s))
                ppg = r.get("price_per_gb") or get_settings(env)["reseller_price_per_gb"]
                lines.append(f"• <code>{uid_s}</code> | تعرفه: {int(ppg):,} ت | کیف‌پول: {bal:,} ت | فروش: {r.get('total_gb_sold', 0)}GB")
                kb_rows.append([{"text": f"🚫 لغو نمایندگی {uid_s}", "callback_data": f"adm_res_off:{uid_s}"}])
        kb_rows.append([{"text": "🔙 بازگشت به پنل", "callback_data": "adm_refresh"}])
        edit_message(token, chat_id, msg_id, "\n".join(lines), reply_markup={"inline_keyboard": kb_rows})
        return

    if data == "adm_res_add":
        if user_id not in get_admin_ids(env):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_admin_reseller_add"}
        send_message(token, chat_id, "💼 آیدی عددی تلگرام نماینده را بفرستید (اختیاری: قیمت هر گیگ):\n<code>USER_ID [PRICE]</code>\nمثال: <code>123456789 5000</code>", env=env, user_id=user_id)
        return

    if data.startswith("adm_res_ok:") or data.startswith("adm_res_no:") or data.startswith("adm_res_off:"):
        if user_id not in get_admin_ids(env):
            return
        act, t_uid_s = data.split(":", 1)
        t_uid = int(t_uid_s)
        if act == "adm_res_ok":
            set_reseller(t_uid, active=True)
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ نماینده فعال شد"})
            try:
                tg_api(token, "editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}})
            except Exception:
                pass
            send_message(token, chat_id, f"✅ کاربر <code>{t_uid}</code> به عنوان نماینده فعال شد.")
            try:
                send_message(token, t_uid, "🎉 <b>درخواست نمایندگی شما تایید شد!</b>\nاز دکمهٔ <b>💼 پنل نمایندگی</b> در منو استفاده کنید.", env=env, user_id=t_uid)
            except Exception:
                pass
        elif act == "adm_res_no":
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "❌ رد شد"})
            try:
                tg_api(token, "editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}})
            except Exception:
                pass
            send_message(token, chat_id, f"❌ درخواست نمایندگی <code>{t_uid}</code> رد شد.")
            try:
                send_message(token, t_uid, "❌ درخواست نمایندگی شما تایید نشد.", env=env, user_id=t_uid)
            except Exception:
                pass
        else:
            with resellers_lock:
                svc = load_json(RESELLERS_PATH).get(str(t_uid), {}).get("sub_bot_service")
            if svc:
                subprocess.run(["systemctl", "disable", "--now", svc], check=False, timeout=15)
            set_reseller(t_uid, active=False)
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "🚫 نمایندگی لغو شد"})
            show_admin_panel(token, chat_id, env, message_id=msg_id)
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
            fake_count = int(u_rec.get("fake_count") or 0) + 1
            u_rec["fake_count"] = fake_count
            u_rec["blocked"] = True
            u_rec["blocked_until"] = (int(time.time()) + 86400) if fake_count == 1 else 0
            save_json(USERS_PATH, users)

        action_note = ""
        if order.get("type") == "topup":
            _, rem_b = adjust_wallet_balance_at(USERS_PATH, target_uid, -int(order["total_toman"]), note=f"کسر شارژ جعلی #{order_id}", allow_grace=True)
            if is_tenant_bot(env) and env.get("TENANT_OWNER_ID") and str(target_uid) != str(env["TENANT_OWNER_ID"]):
                add_sales_wallet_balance(int(env["TENANT_OWNER_ID"]), -int(order["total_toman"]), note=f"کسر فیش جعلی #{order_id}")
            disabled_cnt = 0
            with orders_lock:
                recent_subs = [
                    o.get("panel_username") for o in load_json(ORDERS_PATH).values()
                    if o.get("user_id") == target_uid and o.get("status") == "approved" and o.get("panel_username") and int(o.get("created_at") or 0) >= int(order.get("created_at") or 0)
                ]
            for un_s in set(recent_subs):
                try:
                    panel_request(env, f"/api/user/{urllib.parse.quote(un_s)}", method="PUT", body={"status": "disabled"})
                    disabled_cnt += 1
                except Exception:
                    pass
            action_note = f"مبلغ شارژ کسر شد (تراز: {rem_b:,} ت) | اکانت غیرفعال‌شده: {disabled_cnt}"
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
        if fake_count == 1:
            blk_adm = "به مدت <b>۲۴ ساعت (۱ روز)</b> مسدود گردید (تکرار اول)."
            blk_usr = "به مدت <b>۲۴ ساعت</b> مسدود شد."
        else:
            blk_adm = f"به دلیل تکرار (دفعهٔ <b>{fake_count}</b>) <b>به‌صورت دائمی</b> مسدود گردید تا زمانی که دستی رفع مسدودیت کنید."
            blk_usr = "به دلیل تکرار ارسال رسید نامعتبر، <b>به‌صورت دائمی</b> مسدود شد."
        send_message(token, chat_id, f"🚫 سفارش <code>#{order_id}</code> به عنوان فیش جعلی ثبت شد.\n• {action_note}\n• کاربر <code>{target_uid}</code> {blk_adm}")
        try:
            send_message(token, target_uid, f"🚫 سفارش <code>#{order_id}</code> به دلیل عدم تایید واریز لغو، اشتراک شما قطع و دسترسی شما {blk_usr}")
        except Exception:
            pass
        return

    if data == "adm_toggle_maint":
        if user_id not in get_admin_ids(env):
            return
        cur = bool(get_settings(env).get("maintenance_mode", False))
        update_setting(env, "maintenance_mode", not cur)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ حالت تعمیر تغییر یافت"})
        show_admin_panel(token, chat_id, env, message_id=msg_id)
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

    if data.startswith("adm_u_botblock:"):
        if user_id not in get_admin_ids(env):
            return
        t_uid = int(data.split(":", 1)[1])
        blk, _ = is_user_blocked(t_uid)
        with users_lock:
            users = load_json(USERS_PATH)
            u_rec = users.setdefault(str(t_uid), {"first_seen": int(time.time()), "last_seen": int(time.time())})
            u_rec["blocked"] = not blk
            u_rec["blocked_until"] = 0
            save_json(USERS_PATH, users)
        if blk:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "🔓 مسدودیت رفع شد"})
            send_message(token, chat_id, f"🔓 مسدودیت کاربر <code>{t_uid}</code> در ربات رفع شد.", env=env, user_id=user_id)
            try:
                send_message(token, t_uid, "✅ مسدودیت حساب شما توسط مدیریت رفع شد و می‌توانید دوباره از ربات استفاده کنید.", env=env, user_id=t_uid)
            except Exception:
                pass
        else:
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "⛔ کاربر مسدود شد"})
            send_message(token, chat_id, f"⛔ کاربر <code>{t_uid}</code> به‌صورت دستی در ربات مسدود شد.", env=env, user_id=user_id)
            try:
                send_message(token, t_uid, "⛔ دسترسی شما به ربات توسط مدیریت مسدود شد.")
            except Exception:
                pass
        return

    if data.startswith("adm_u_wallet:"):
        if user_id not in get_admin_ids(env):
            return
        t_uid = int(data.split(":", 1)[1])
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        show_admin_wallet_user(token, chat_id, env, t_uid, message_id=msg_id)
        return

    if data == "adm_w_search":
        if user_id not in get_admin_ids(env):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_admin_wallet_user"}
        send_message(token, chat_id, "✏️ آیدی عددی یا <code>@username</code> کاربر را بفرستید:", env=env, user_id=user_id)
        return

    if data.startswith("adm_w_pick:"):
        if user_id not in get_admin_ids(env):
            return
        t_uid = int(data.split(":", 1)[1])
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        show_admin_wallet_user(token, chat_id, env, t_uid, message_id=msg_id)
        return

    if data.startswith("adm_w_custom:"):
        if user_id not in get_admin_ids(env):
            return
        t_uid = int(data.split(":", 1)[1])
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        user_states[user_id] = {"step": "await_admin_wallet_amt", "target_uid": t_uid}
        send_message(token, chat_id, f"✏️ مبلغ تغییر کیف پول برای <code>{t_uid}</code> را بفرستید:\n(مثلاً <code>100000</code> برای افزایش یا <code>-50000</code> برای کسر)", env=env, user_id=user_id)
        return

    if data.startswith("adm_w_do:") or data.startswith("adm_w_zero:"):
        if user_id not in get_admin_ids(env):
            return
        if data.startswith("adm_w_zero:"):
            t_uid = int(data.split(":", 1)[1])
            cur_b = get_wallet_balance(t_uid)
            delta = -cur_b
        else:
            _, uid_s, d_s = data.split(":", 2)
            t_uid, delta = int(uid_s), int(d_s)
        if delta != 0:
            ok, new_bal = adjust_wallet_balance(t_uid, delta, note="تغییر دکمه‌ای مدیریت")
            if not ok:
                tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "موجودی کافی برای کسر نیست", "show_alert": True})
                return
            if delta > 0:
                try:
                    send_message(token, t_uid, f"🎁 کیف پول شما به مبلغ <b>{delta:,} تومان</b> توسط مدیریت شارژ شد.\n👛 موجودی فعلی: <b>{new_bal:,} تومان</b>", env=env, user_id=t_uid)
                except Exception:
                    pass
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ انجام شد"})
        show_admin_wallet_user(token, chat_id, env, t_uid, message_id=msg_id)
        return

    if data.startswith("adm_u_addgb:") or data.startswith("adm_u_adddays:") or data.startswith("adm_u_toggle:"):
        if user_id not in get_admin_ids(env):
            return
        parts = data.split(":")
        act, uname = parts[0], parts[1]
        try:
            pu = panel_request(env, f"/api/user/{urllib.parse.quote(uname)}")
            if act == "adm_u_addgb":
                add_gb = int(parts[2])
                ok_up, err_up = charge_upstream_chain(DATA_DIR, add_gb, note=f"هدیه ادمین +{add_gb}GB ({uname})")
                if not ok_up:
                    tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "موجودی بالادست کافی نیست", "show_alert": True})
                    send_message(token, chat_id, err_up, env=env, user_id=user_id)
                    return
                try:
                    add_b = add_gb * 1024 * 1024 * 1024
                    new_lim = int(pu.get("data_limit") or 0) + add_b
                    panel_request(env, f"/api/user/{urllib.parse.quote(uname)}", method="PUT", body={"data_limit": new_lim})
                except Exception:
                    refund_upstream_chain(DATA_DIR, add_gb, note=f"برگشت خطای هدیه ({uname})")
                    raise
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
        if user_id not in get_admin_ids(env) or is_tenant_bot(env):
            return
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "⏳ در حال دانلود آپدیت..."})
        try:
            url = f"https://raw.githubusercontent.com/sirvan1133/pasarguard-sales-bot/main/bot.py?t={int(time.time())}"
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0", "Cache-Control": "no-cache"})
            new_code = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
            cur_path = Path(__file__).resolve()
            tmp_path = cur_path.with_suffix(".py.new")
            tmp_path.write_text(new_code, encoding="utf-8")
            subprocess.run([sys.executable, "-m", "py_compile", str(tmp_path)], check=True, timeout=10)
            os.replace(tmp_path, cur_path)
            os.chmod(cur_path, 0o700)
            try:
                out = subprocess.run(
                    ["systemctl", "list-units", "--type=service", "--state=running", "--plain", "--no-legend", "sales-bot-t-*.service"],
                    capture_output=True, text=True, timeout=10,
                ).stdout
                for line in out.splitlines():
                    svc = line.split()[0] if line.split() else ""
                    if svc.startswith("sales-bot-t-"):
                        subprocess.run(["systemctl", "restart", svc], check=False, timeout=15)
            except Exception:
                pass
            meta = load_json(META_PATH)
            meta["pending_update_notify"] = {"chat_id": chat_id, "user_id": user_id}
            save_json(META_PATH, meta)
            send_message(token, chat_id, "⏳ آخرین نسخه از گیت‌هاب دریافت و کامپایل شد. در حال ری‌استارت...", env=env, user_id=user_id)
            try:
                ack = tg_api(token, "getUpdates", {"offset": -1, "limit": 1, "timeout": 0}, timeout=5).get("result", [])
                if ack:
                    tg_api(token, "getUpdates", {"offset": ack[-1]["update_id"] + 1, "timeout": 0}, timeout=5)
            except Exception:
                pass
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
        if is_tenant_bot(env) and key in ("panel_url", "panel_user", "panel_pass", "group_id", "backup_interval_hours", "backup_bot_token", "backup_chat_id"):
            tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "این بخش در ربات نماینده قفل است", "show_alert": True})
            return
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
            "reseller_price_per_gb": "قیمت هر گیگ برای نماینده به تومان (مثلاً 5000)",
            "reseller_min_gb": "حداقل گیگ شرط دریافت نمایندگی (مثلاً 500)",
            "reseller_max_gb_per_acc": "حداکثر حجم هر تک‌اکانت نماینده به گیگ (0 = نامحدود)",
            "gb_options": "لیست حجم‌های پیش‌فرض را با کاما یا فاصله بفرستید (مثلاً: 20,30,50,100,200,400,1000)",
            "maintenance_msg": "متن پیام نمایش‌داده‌شده در حالت تعمیر و نگهداری",
            "pending_alert_minutes": "یادآوری سفارش معوق بعد از چند دقیقه به ادمین ارسال شود (مثلاً 30)",
            "backup_interval_hours": "فاصله زمانی بکاپ خودکار به ساعت (مثلاً 24)",
            "backup_bot_token": "توکن ربات تلگرام مخصوص دریافت بکاپ خودکار",
            "backup_chat_id": "آیدی عددی چت/کانال مقصد بکاپ (یا آیدی ادمین)",
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

            cfg_b = get_settings(env)
            if not is_tenant_bot(env) and cfg_b.get("backup_enabled", True):
                bkp_sec = max(1, int(cfg_b.get("backup_interval_hours", 24))) * 3600
                if now_ts - last_bkp >= bkp_sec:
                    if send_backup_zip(token, env, use_backup_bot=True) > 0:
                        meta["last_backup_ts"] = now_ts
                        save_json(META_PATH, meta)

            # Alert admins about pending orders older than pending_alert_minutes
            alert_min = int(get_settings(env).get("pending_alert_minutes", 30))
            if alert_min > 0:
                to_alert = []
                with orders_lock:
                    orders = load_json(ORDERS_PATH)
                    changed_o = False
                    for oid, o in orders.items():
                        if o.get("status") == "pending" and not o.get("pending_alerted"):
                            age_min = (now_ts - int(o.get("created_at") or now_ts)) // 60
                            if age_min >= alert_min:
                                o["pending_alerted"] = True
                                changed_o = True
                                to_alert.append((oid, o, age_min))
                    if changed_o:
                        save_json(ORDERS_PATH, orders)
                for oid, o, age_min in to_alert:
                    kb = {
                        "inline_keyboard": [[
                            {"text": "✅ تایید و ساخت خودکار", "callback_data": f"adm_ok:{oid}"},
                            {"text": "❌ رد سفارش", "callback_data": f"adm_no:{oid}"},
                        ]]
                    }
                    for adm_id in get_admin_ids(env):
                        try:
                            send_message(
                                token,
                                adm_id,
                                f"⏰ <b>یادآوری سفارش معوق #{oid}</b>\n\n"
                                f"⏳ حدود <b>{age_min} دقیقه</b> است در انتظار بررسی مانده!\n"
                                f"👤 کاربر: <code>{o['user_id']}</code> | مبلغ: <b>{int(o.get('total_toman', 0)):,} تومان</b>",
                                reply_markup=kb,
                            )
                        except Exception:
                            pass
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
                    {"command": "menu", "description": "منوی سریع و تغییر حالت منو"},
                    {"command": "buy", "description": "خرید اشتراک جدید"},
                    {"command": "renew", "description": "تمدید اشتراک با تخفیف"},
                    {"command": "trial", "description": "دریافت تست رایگان"},
                    {"command": "wallet", "description": "کیف پول و شارژ حساب"},
                    {"command": "support", "description": "ارتباط با پشتیبانی"},
                    {"command": "guide", "description": "راهنمای اتصال"},
                ]
            },
            timeout=10,
        )
    except Exception as e:
        log.warning("setMyCommands failed: %s", e)

    miniapp_url = (env.get("MINIAPP_URL") or "https://drhomes.ir/vpnapp/").strip()
    if miniapp_url and not is_tenant_bot(env):
        try:
            tg_api(
                token,
                "setChatMenuButton",
                {"menu_button": {"type": "web_app", "text": "🚀 پنل کاربری", "web_app": {"url": miniapp_url}}},
                timeout=10,
            )
        except Exception as e:
            log.warning("setChatMenuButton failed: %s", e)

    threading.Thread(target=expiry_reminder_loop, args=(token, env), daemon=True).start()
    threading.Thread(target=backup_and_report_loop, args=(token, env), daemon=True).start()

    meta = load_json(META_PATH)
    upd_notify = meta.pop("pending_update_notify", None)
    rst_notify = meta.pop("pending_restore_notify", None)
    if upd_notify or rst_notify:
        save_json(META_PATH, meta)
    if upd_notify:
        try:
            send_message(
                token,
                upd_notify["chat_id"],
                "✅ <b>آپدیت کامل شد!</b> ربات با آخرین نسخهٔ گیت‌هاب راه‌اندازی و آماده است.",
                env=env,
                user_id=upd_notify["user_id"],
            )
        except Exception as e:
            log.warning("Failed to send post-update notice: %s", e)
    if rst_notify:
        try:
            send_message(
                token,
                rst_notify["chat_id"],
                f"✅ <b>بازیابی کامل بکاپ انجام شد!</b>\n\n"
                f"• فایل‌های بازیابی‌شده: <b>{rst_notify.get('files', 0)}</b>\n"
                f"• ربات‌های نمایندهٔ راه‌اندازی‌شده: <b>{rst_notify.get('services', 0)}</b>",
                env=env,
                user_id=rst_notify["user_id"],
            )
        except Exception as e:
            log.warning("Failed to send post-restore notice: %s", e)

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
                    elif "document" in m and st and st.get("step") == "await_restore_zip" and user_id in get_admin_ids(env) and not is_tenant_bot(env):
                        doc = m["document"]
                        fname = doc.get("file_name") or ""
                        if not fname.lower().endswith(".zip"):
                            send_message(token, m["chat"]["id"], "❌ لطفاً فایل زیپ (<code>.zip</code>) بکاپ را ارسال کنید:", env=env, user_id=user_id)
                            continue
                        user_states.pop(user_id, None)
                        send_message(token, m["chat"]["id"], "⏳ در حال دانلود و بازیابی بکاپ تمام پنل‌ها...", env=env, user_id=user_id)
                        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_z:
                            tmp_z_path = Path(tmp_z.name)
                        try:
                            f_info = tg_api(token, "getFile", {"file_id": doc["file_id"]}, timeout=15).get("result", {})
                            f_path = f_info["file_path"]
                            dl_url = f"https://api.telegram.org/file/bot{token}/{f_path}"
                            tmp_z_path.write_bytes(urllib.request.urlopen(dl_url, timeout=30).read())
                            r_files, r_svcs = restore_from_backup_zip(tmp_z_path, DATA_DIR, Path(__file__).resolve())
                            meta = load_json(META_PATH)
                            meta["pending_restore_notify"] = {"chat_id": m["chat"]["id"], "user_id": user_id, "files": r_files, "services": r_svcs}
                            save_json(META_PATH, meta)
                            try:
                                tg_api(token, "getUpdates", {"offset": offset, "timeout": 0}, timeout=5)
                            except Exception:
                                pass
                            os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])
                        except Exception as e:
                            send_message(token, m["chat"]["id"], f"❌ خطا در بازیابی بکاپ: {html.escape(str(e))}", env=env, user_id=user_id)
                        finally:
                            tmp_z_path.unlink(missing_ok=True)
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
