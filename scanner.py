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
        "sl": 55.0,
        "trail_step": 75.0,
        "min_oi_diff": 250000
    },
    "SENSEX": {
        "name": "SENSEX",
        "exchange": "BSE",
        "opt_exchange": "BFO",
        "symbol": "SENSEX",
        "token": "99919000",
        "step": 100,
        "target": 240.0,
        "sl": 120.0,
        "trail_step": 175.0,
        "min_oi_diff": 50000
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
            if isinstance(data, dict) and data.get("date") == today_str:
                if not isinstance(data.get("active_trades"), dict):
                    data["active_trades"] = {}
                else:
                    data["active_trades"] = {
                        k: v for k, v in data["active_trades"].items() if isinstance(v, dict)
                    }
                if not isinstance(data.get("cooldown_until"), dict):
                    data["cooldown_until"] = {}
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
        return {"spot": spot, "signal": None, "oi_bias": "NEUTRAL"}

    # Focus on ATM +/- 2 strikes (highest liquidity & sharpest OI reaction)
    strikes = [atm + i * step for i in range(-2, 3)]
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
        return {"spot": spot, "signal": None, "oi_bias": "NEUTRAL"}

    tokens_list = list(target_tokens.keys())
    try:
        mkt = obj.getMarketData("FULL", {cfg["opt_exchange"]: tokens_list})
        fetched = mkt.get("data", {}).get("fetched", [])
    except Exception as e:
        print(f"MarketData error {key}:", e)
        return {"spot": spot, "signal": None, "oi_bias": "NEUTRAL"}

    ce_oi_chg = 0.0
    pe_oi_chg = 0.0
    ce_oi_tot = 0.0
    pe_oi_tot = 0.0
    atm_ce_gain = False
    atm_pe_gain = False

    for item in fetched:
        tok = str(item.get("symbolToken"))
        info = target_tokens.get(tok)
        if not info:
            continue
        oi = float(item.get("opnInterest", 0) or 0)
        chg = float(item.get("netChangeOpnInterest", 0) or 0)
        ltp = float(item.get("ltp", 0) or 0)
        opn = float(item.get("open", 0) or 0)

        if info["type"] == "CE":
            ce_oi_tot += oi
            ce_oi_chg += chg
            if info["strike"] == atm and opn > 0 and ltp > opn:
                atm_ce_gain = True
        else:
            pe_oi_tot += oi
            pe_oi_chg += chg
            if info["strike"] == atm and opn > 0 and ltp > opn:
                atm_pe_gain = True

    pcr = (pe_oi_tot / ce_oi_tot) if ce_oi_tot > 0 else 1.0
    days_left = max((exp_dt - ist_now().date()).days, 0) + 0.5
    t_years = days_left / 365.0
    iv = max(vix / 100.0, 0.12)

    # Pure OI Pressure = Writing on one side + Unwinding on the opposite side
    bullish_oi_net = pe_oi_chg - ce_oi_chg
    bearish_oi_net = ce_oi_chg - pe_oi_chg
    min_diff = cfg["min_oi_diff"]

    signal = None
    oi_bias = "NEUTRAL"
    reason = ""
    strike_1 = atm
    strike_2 = atm

    # 1. Pure Bullish OI (Put Writing + Call Unwinding) + Strong CE Delta
    if bullish_oi_net >= min_diff and (pe_oi_chg > 0 or ce_oi_chg < 0):
        oi_bias = "CALL"
        ce_delta = abs(calc_delta(spot, atm, t_years, iv, "CE"))
        if ce_delta >= 0.48 and atm_ce_gain:
            signal = "CALL"
            strike_1 = atm
            strike_2 = atm - step
            reason = f"Put writing + Call unwinding (Net +{int(bullish_oi_net/1000)}K OI) & Delta {round(ce_delta, 2)} favor fast CALL growth."

    # 2. Pure Bearish OI (Call Writing + Put Unwinding) + Strong PE Delta
    elif bearish_oi_net >= min_diff and (ce_oi_chg > 0 or pe_oi_chg < 0):
        oi_bias = "PUT"
        pe_delta = abs(calc_delta(spot, atm, t_years, iv, "PE"))
        if pe_delta >= 0.48 and atm_pe_gain:
            signal = "PUT"
            strike_1 = atm
            strike_2 = atm + step
            reason = f"Call writing + Put unwinding (Net +{int(bearish_oi_net/1000)}K OI) & Delta {round(pe_delta, 2)} favor fast PUT growth."

    return {
        "spot": spot,
        "signal": signal,
        "oi_bias": oi_bias,
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

    # 2. Check active trades: Instant OI Reversal Exit & Dynamic Trailing Target
    for key in list(active.keys()):
        tr = active.get(key)
        if not isinstance(tr, dict):
            del active[key]
            continue

        cfg = INDICES[key]
        res = results[key]
        spot = res["spot"]
        side = tr["side"]
        entry = tr["entry_spot"]
        tgt = tr["target_spot"]
        sl = tr["sl_spot"]
        level = tr.get("trail_level", 0)
        pts = round(spot - entry if side == "CALL" else entry - spot, 1)

        closed = False

        # A. Check if SL or Trailing SL is hit
        if (side == "CALL" and spot <= sl) or (side == "PUT" and spot >= sl):
            if level == 0:
                send_tg(
                    f"🛑 SL HIT — {cfg['name']} {side}\n"
                    f"Strike: {tr['strike']} | Spot: {round(spot, 2)} ({pts} pts)"
                )
            else:
                send_tg(
                    f"🏁 FINAL EXIT (Trailing SL Hit) — {cfg['name']} {side}\n"
                    f"Strike: {tr['strike']} | Spot: {round(spot, 2)}\n"
                    f"💰 Locked Profit: {'+' if pts >= 0 else ''}{pts} pts"
                )
            closed = True

        # B. Instant Exit if OI reverses direction before SL is hit
        elif res["oi_bias"] != "NEUTRAL" and res["oi_bias"] != side:
            send_tg(
                f"⚠️ OI REVERSAL EXIT — {cfg['name']} {side}\n"
                f"Strike: {tr['strike']} | Spot: {round(spot, 2)} ({'+' if pts >= 0 else ''}{pts} pts)\n"
                f"📉 OI reversed before SL — exit immediately!"
            )
            closed = True

        # C. Check if Target Milestone is reached -> Extend Target & Trail SL!
        elif (side == "CALL" and spot >= tgt) or (side == "PUT" and spot <= tgt):
            if res["oi_bias"] != "NEUTRAL" and res["oi_bias"] != side:
                send_tg(
                    f"🎯 TARGET HIT & OI EXHAUSTED — Exit {cfg['name']} {side}\n"
                    f"Strike: {tr['strike']} | Spot: {round(spot, 2)} (+{abs(pts)} pts)\n"
                    f"💰 Book full profit now!"
                )
                closed = True
            else:
                new_level = level + 1
                step_pts = cfg["trail_step"]
                if side == "CALL":
                    new_tgt = round(spot + step_pts, 1)
                    new_sl = round(entry, 1) if new_level == 1 else round(spot - step_pts, 1)
                else:
                    new_tgt = round(spot - step_pts, 1)
                    new_sl = round(entry, 1) if new_level == 1 else round(spot + step_pts, 1)

                tr["trail_level"] = new_level
                tr["target_spot"] = new_tgt
                tr["sl_spot"] = new_sl

                sl_note = "Entry/Cost (Risk-Free!)" if new_level == 1 else f"{new_sl} (Profit Locked!)"
                send_tg(
                    f"🔥 TARGET {new_level} HIT — {cfg['name']} {side} (+{abs(pts)} pts)\n"
                    f"Spot: {round(spot, 2)} | OI still {side} supportive!\n"
                    f"🚀 Extended Target {new_level + 1}: {new_tgt}\n"
                    f"🛡️ Trailing SL moved to: {sl_note}"
                )

        if closed:
            del active[key]
            cooldown[key] = now_ts + 360

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
            f"🎯 Target 1: {tgt_spot} (+{cfg['target']} pts)\n"
            f"🛑 Initial SL: {sl_spot} (-{cfg['sl']} pts)\n"
            f"💡 {res['reason']}"
        )
        send_tg(msg)

        active[key] = {
            "side": sig,
            "strike": strike_str,
            "entry_spot": spot,
            "target_spot": tgt_spot,
            "sl_spot": sl_spot,
            "trail_level": 0,
            "opened_at": now_ts
        }

    save_state(state)

if __name__ == "__main__":
    main()
