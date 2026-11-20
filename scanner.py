import os
import json
import time
import math
import datetime
import requests
import pyotp
from SmartApi import SmartConnect

API_KEY = os.environ["ANGEL_API_KEY"]
CLIENT_ID = os.environ["ANGEL_CLIENT_ID"]
PIN = os.environ["ANGEL_PIN"]
TOTP_SECRET = os.environ["ANGEL_TOTP_SECRET"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = "bot_state.json"

INDICES = {
    "NIFTY": {
        "name": "NIFTY 50",
        "exchange": "NSE",
        "opt_exchange": "NFO",
        "symbol": "Nifty 50",
        "token": "99926000",
        "step": 50,
        "target": 110.0,
        "sl": 55.0
    },
    "SENSEX": {
        "name": "SENSEX",
        "exchange": "BSE",
        "opt_exchange": "BFO",
        "symbol": "SENSEX",
        "token": "99919000",
        "step": 100,
        "target": 240.0,
        "sl": 120.0
    }
}

def ist_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)

def send_tg(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("TG Error:", e)

def load_state():
    today_str = ist_now().strftime("%Y-%m-%d")
    default = {
        "date": today_str,
        "live_sent": False,
        "active_trades": {},
        "cooldown_until": {}
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            if data.get("date") == today_str:
                return data
        except Exception:
            pass
    return default

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def login_angel():
    obj = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    session = obj.generateSession(CLIENT_ID, PIN, totp)
    if not session or not session.get("status"):
        raise RuntimeError(f"Angel Login Failed: {session}")
    return obj

def get_spot(obj, idx_cfg):
    res = obj.ltpData(idx_cfg["exchange"], idx_cfg["symbol"], idx_cfg["token"])
    return float(res["data"]["ltp"])

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def calc_delta(spot, strike, t_years, iv, opt_type):
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.5 if opt_type == "CE" else -0.5
    r = 0.07
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    return norm_cdf(d1) if opt_type == "CE" else norm_cdf(d1) - 1.0

def get_scrip_master():
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    r = requests.get(url, timeout=25)
    return r.json()

def get_nearest_expiry(scrips, name):
    today = ist_now().date()
    expiries = set()
    for s in scrips:
        if s.get("name") == name and s.get("instrumenttype") == "OPTIDX":
            exp_str = s.get("expiry", "")
            try:
                exp_dt = datetime.datetime.strptime(exp_str, "%d%b%Y").date()
                if exp_dt >= today:
                    expiries.add((exp_dt, exp_str))
            except Exception:
                pass
    if not expiries:
        return None, None
    return sorted(expiries, key=lambda x: x[0])[0]

def analyze_index(obj, scrips, key, cfg, vix):
    spot = get_spot(obj, cfg)
    step = cfg["step"]
    atm = round(spot / step) * step
    exp_dt, exp_str = get_nearest_expiry(scrips, key)
    if not exp_dt:
        return {"spot": spot, "signal": None}

    strikes = [atm + i * step for i in range(-3, 4)]
    target_tokens = {}
    for s in scrips:
        if s.get("name") == key and s.get("expiry") == exp_str and s.get("instrumenttype") == "OPTIDX":
            try:
                st = int(float(s.get("strike", 0)) / 100)
            except Exception:
                continue
            if st in strikes:
                sym = s.get("symbol", "")
                otype = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else None)
                if otype:
                    target_tokens[s["token"]] = {"strike": st, "type": otype, "symbol": sym}

    if not target_tokens:
        return {"spot": spot, "signal": None}

    tokens_list = list(target_tokens.keys())
    try:
        mkt = obj.getMarketData("FULL", {cfg["opt_exchange"]: tokens_list})
        fetched = mkt.get("data", {}).get("fetched", [])
    except Exception as e:
        print(f"MarketData error {key}:", e)
        return {"spot": spot, "signal": None}

    ce_oi_chg = 0
    pe_oi_chg = 0
    ce_oi_tot = 0
    pe_oi_tot = 0

    for item in fetched:
        tok = str(item.get("symbolToken"))
        info = target_tokens.get(tok)
        if not info:
            continue
        oi = float(item.get("opnInterest", 0) or 0)
        chg = float(item.get("netChangeOpnInterest", 0) or 0)
        if info["type"] == "CE":
            ce_oi_tot += oi
            ce_oi_chg += chg
        else:
            pe_oi_tot += oi
            pe_oi_chg += chg

    pcr = (pe_oi_tot / ce_oi_tot) if ce_oi_tot > 0 else 1.0
    days_left = max((exp_dt - ist_now().date()).days, 0) + 0.5
    t_years = days_left / 365.0
    iv = max(vix / 100.0, 0.12)

    signal = None
    reason = ""
    strike_1 = atm
    strike_2 = atm

    # Bullish: Put writers active (PE OI rising faster than CE OI) + PCR support
    if (pe_oi_chg > ce_oi_chg * 1.15 and pe_oi_chg > 0) or (pcr >= 1.05 and pe_oi_chg >= ce_oi_chg):
        delta_val = abs(calc_delta(spot, atm, t_years, iv, "CE"))
        if delta_val >= 0.45:
            signal = "CALL"
            strike_1 = atm
            strike_2 = atm - step
            reason = "Bullish OI support & Greeks favor fast CALL premium growth."

    # Bearish: Call writers active (CE OI rising faster than PE OI) + PCR weak
    elif (ce_oi_chg > pe_oi_chg * 1.15 and ce_oi_chg > 0) or (pcr <= 0.85 and ce_oi_chg >= pe_oi_chg):
        delta_val = abs(calc_delta(spot, atm, t_years, iv, "PE"))
        if delta_val >= 0.45:
            signal = "PUT"
            strike_1 = atm
            strike_2 = atm + step
            reason = "Bearish OI resistance & Greeks favor fast PUT premium growth."

    return {
        "spot": spot,
        "signal": signal,
        "strike_1": strike_1,
        "strike_2": strike_2,
        "reason": reason,
        "pcr": pcr
    }

def main():
    now = ist_now()
    mins = now.hour * 60 + now.minute

    # Market hours check: 9:14 AM to 3:25 PM IST
    if mins < (9 * 60 + 14) or mins > (15 * 60 + 25):
        print("Outside market hours:", now)
        return

    state = load_state()
    obj = login_angel()

    # Fetch India VIX
    try:
        vix_res = obj.ltpData("NSE", "INDIA VIX", "99926017")
        vix = float(vix_res["data"]["ltp"])
    except Exception:
        vix = 14.0

    scrips = get_scrip_master()
    results = {}
    for key, cfg in INDICES.items():
        results[key] = analyze_index(obj, scrips, key, cfg, vix)

    # 1. Morning BOT LIVE message (once per day)
    if not state.get("live_sent"):
        n_spot = round(results["NIFTY"]["spot"], 2)
        s_spot = round(results["SENSEX"]["spot"], 2)
        send_tg(f"🟢 BOT LIVE | NIFTY: {n_spot} | SENSEX: {s_spot}")
        state["live_sent"] = True

    now_ts = time.time()
    active = state.setdefault("active_trades", {})
    cooldown = state.setdefault("cooldown_until", {})

    # 2. Check active trades for Target / SL / Momentum Shift
    for key in list(active.keys()):
        tr = active[key]
        res = results[key]
        spot = res["spot"]
        side = tr["side"]
        entry = tr["entry_spot"]
        tgt = tr["target_spot"]
        sl = tr["sl_spot"]
        opened_at = tr.get("opened_at", now_ts)
        pts = round(spot - entry if side == "CALL" else entry - spot, 1)

        closed = False
        if (side == "CALL" and spot >= tgt) or (side == "PUT" and spot <= tgt):
            send_tg(
                f"🎯 TARGET HIT — {INDICES[key]['name']} {side}\n"
                f"Strike: {tr['strike']} | Spot: {round(spot, 2)} (+{abs(pts)} pts)"
            )
            closed = True
        elif (side == "CALL" and spot <= sl) or (side == "PUT" and spot >= sl):
            send_tg(
                f"🛑 SL HIT — {INDICES[key]['name']} {side}\n"
                f"Strike: {tr['strike']} | Spot: {round(spot, 2)} (-{abs(pts)} pts)"
            )
            closed = True
        elif (now_ts - opened_at) >= 900 and res["signal"] and res["signal"] != side:
            send_tg(
                f"⚠️ MOMENTUM SHIFT — Exit {INDICES[key]['name']} {side}\n"
                f"Strike: {tr['strike']} | Spot: {round(spot, 2)} ({'+' if pts >= 0 else ''}{pts} pts)\n"
                f"OI & Greeks reversed direction."
            )
            closed = True

        if closed:
            del active[key]
            cooldown[key] = now_ts + 180  # 3-minute cooldown

    # 3. Stop taking fresh entries after 3:05 PM IST
    if mins >= (15 * 60 + 5):
        save_state(state)
        return

    # 4. Generate fresh signals independently for NIFTY & SENSEX
    for key, cfg in INDICES.items():
        if key in active:
            continue
        if now_ts < cooldown.get(key, 0):
            continue

        res = results[key]
        sig = res["signal"]
        if not sig:
            continue

        spot = res["spot"]
        if sig == "CALL":
            tgt_spot = round(spot + cfg["target"], 1)
            sl_spot = round(spot - cfg["sl"], 1)
            strike_str = f"{res['strike_1']} CE"
            alt_str = f"{res['strike_2']} CE"
        else:
            tgt_spot = round(spot - cfg["target"], 1)
            sl_spot = round(spot + cfg["sl"], 1)
            strike_str = f"{res['strike_1']} PE"
            alt_str = f"{res['strike_2']} PE"

        msg = (
            f"🟢 {cfg['name']} — BUY {sig}\n"
            f"Spot: {round(spot, 2)}\n"
            f"👉 Strike: {strike_str} (or {alt_str})\n"
            f"🎯 Spot Target: {tgt_spot} (+{cfg['target']} pts)\n"
            f"🛑 Spot SL: {sl_spot} (-{cfg['sl']} pts)\n"
            f"💡 {res['reason']}"
        )
        send_tg(msg)

        active[key] = {
            "side": sig,
            "strike": strike_str,
            "entry_spot": spot,
            "target_spot": tgt_spot,
            "sl_spot": sl_spot,
            "opened_at": now_ts
        }

    save_state(state)

if __name__ == "__main__":
    main()
