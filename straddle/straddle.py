import math
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import find_dotenv, load_dotenv
from pymongo import MongoClient
from slack_sdk import WebClient
from tamingnifty import connect_dhan as dhan
from tamingnifty import ta
from tamingnifty import utils as util

VERSION = "premium-renko-levels-1pct-20-gain10-low5-2026-11-05"
SIGNAL_ID = "nifty_options_renko"


def notify(client, message):
    print(message, flush=True)
    try:
        util.notify(message=message, slack_client=client, slack_channel="niftyswing")
    except Exception as error:
        print("Slack notification failed: " + util.exception_detail(error), flush=True)


def locked_contracts(master, index, day):
    frame = index.copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    spot = frame.loc[frame["datetime"] == pd.Timestamp(day + " 09:29")]
    if len(spot) != 1:
        raise ValueError("Need the exact completed Nifty 09:29 minute close to lock ATM")
    price = float(spot.iloc[0]["close"])
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Invalid Nifty 09:29 close")
    strike = int(util.round_to_nearest(price, base=50))
    options = master.loc[(master["UNDERLYING_SYMBOL"] == "NIFTY")
                         & (master["INSTRUMENT"] == "OPTIDX")
                         & (master["EXCH_ID"] == "NSE")
                         & (master["STRIKE_PRICE"] == strike)].copy()
    options["expiry_date"] = pd.to_datetime(options["SM_EXPIRY_DATE"]).dt.normalize()
    options = options.loc[options["expiry_date"] > pd.Timestamp(day) + pd.Timedelta(days=6)]
    if options.empty:
        raise ValueError("No ATM contracts strictly beyond six calendar days")
    expiry = options["expiry_date"].min()
    contracts = {}
    for kind in ("CE", "PE"):
        matches = options.loc[(options["expiry_date"] == expiry) & (options["OPTION_TYPE"] == kind)]
        if len(matches) != 1:
            raise ValueError("Expected one exact dated ATM " + kind + " contract")
        row = matches.iloc[0]
        contracts[kind] = dict(security_id=str(int(row["SECURITY_ID"])),
                               symbol=str(row["DISPLAY_NAME"]), lot_size=int(row["LOT_SIZE"]))
    if contracts["CE"]["lot_size"] != contracts["PE"]["lot_size"]:
        raise ValueError("CE/PE lot sizes disagree")
    return dict(strike=strike, spot_0929_close=price, expiry=str(expiry.date()),
                dte=(expiry.date() - pd.Timestamp(day).date()).days, contracts=contracts)


def session_candles(candles, day, now):
    frame = candles.copy()
    required = ["datetime", "open", "high", "low", "close", "volume"]
    if not set(required).issubset(frame.columns) or frame.empty:
        raise ValueError("Missing contract OHLCV history")
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    frame = frame.loc[(frame["datetime"] >= pd.Timestamp(day) - pd.Timedelta(days=14))
                      & (frame["datetime"] < pd.Timestamp(now).floor("min"))
                      & (frame["datetime"].dt.time >= pd.Timestamp("09:15").time())
                      & (frame["datetime"].dt.time <= pd.Timestamp("15:29").time())]
    frame = frame.sort_values("datetime").reset_index(drop=True)
    if frame.empty or frame["datetime"].duplicated().any():
        raise ValueError("Empty or duplicate completed normal-session candles")
    if frame[required].isna().any().any():
        raise ValueError("Missing OHLCV values")
    for field in required[1:]:
        if not frame[field].map(math.isfinite).all():
            raise ValueError("Non-finite " + field)
    if (frame[["open", "high", "low", "close"]] <= 0).any().any() or (frame["volume"] < 0).any():
        raise ValueError("Nonpositive OHLC or negative volume")
    if ((frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any()):
        raise ValueError("Invalid OHLC ordering")
    # A rounded zero step would hang the shared percentage-Renko builder.
    if (frame[["open", "close"]] < 1).any().any():
        raise ValueError("Option premium too small for safe percentage-Renko rounding")
    return frame


def option_levels(candles, day, now):
    bars = session_candles(candles, day, now)
    today = bars.loc[bars["datetime"].dt.strftime("%Y-%m-%d") == day]
    if today.empty:
        raise ValueError("No completed option candles today")
    data_asof = today.iloc[-1]["datetime"]
    if pd.Timestamp(now).floor("min") - data_asof > pd.Timedelta(minutes=2):
        raise ValueError("Stale option candles; no fresh levels can be published")
    bricks = ta.convert_to_renko(brick_size=1, df=bars.copy())
    if bricks.empty:
        raise ValueError("Not enough history to form Renko")
    bricks["high20"] = bricks["high"].shift(1).rolling(20).max()
    bricks["low20"] = bricks["low"].shift(1).rolling(20).min()
    bricks["low5"] = bricks["low"].shift(1).rolling(5).min()
    latest = bricks.iloc[-1]
    return dict(brick_time=str(latest["datetime"]), close=float(latest["close"]),
                last_20_high=float(latest["high20"]) if pd.notna(latest["high20"]) else None,
                last_20_low=float(latest["low20"]) if pd.notna(latest["low20"]) else None,
                last_5_low=float(latest["low5"]) if pd.notna(latest["low5"]) else None,
                data_asof=str(data_asof))


def main():
    load_dotenv(find_dotenv())
    for key in ("CONNECTION_STRING", "user_name", "slack_token", "DHAN_CLIENT_ID",
                "DHAN_PIN", "DHAN_TOTP"):
        if not os.environ.get(key, "").strip():
            raise ValueError("Missing environment variable: " + key)
    if os.environ.get("instrument_name", "NIFTY") != "NIFTY":
        raise ValueError("This strategy supports NIFTY only")
    slack = WebClient(token=os.environ["slack_token"])
    client = MongoClient(os.environ["CONNECTION_STRING"])
    collection = client["Bots"]["supertrend"]
    strategies = client["Bots"][f"nifty_swing_{os.environ['user_name']}"]
    master = None
    master_day = None
    last_minute = None
    last_error = None
    heartbeat = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    last_alert = heartbeat
    latest_state = "Waiting for completed09:29 candles"
    published_day = None
    try:
        notify(slack, f"Nifty signal started | 1% entry20 / exit20->5 after gain>10% | IP {util.get_public_ip()}")
        while True:
            now = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
            try:
                if (strategies.count_documents(
                        {"strategy_state": {"$in": ["pending", "active", "partial", "closing"]}}) == 0
                        and strategies.find_one({"entry_date": str(now.date()),
                                                 "strategy_state": "closed"}) is not None):
                    notify(slack, "Trade complete. Signal job stopped.")
                    return
                if now.strftime("%H:%M") >= "15:16":
                    break
                minute = now.replace(second=0, microsecond=0)
                if now.weekday() < 5 and now.strftime("%H:%M") >= "09:30" and minute != last_minute:
                    conn = dhan.login_to_dhan(slack_channel="niftyswing")
                    if master_day != now.date():
                        master = dhan.load_instruments()
                        master_day = now.date()
                    day = str(now.date())
                    doc = collection.find_one({"_id": SIGNAL_ID})
                    new_day = doc is None or pd.Timestamp(doc["updated_at"]).date() != now.date()
                    if doc is not None and pd.Timestamp(doc["updated_at"]) > pd.Timestamp(now):
                        raise ValueError("Cannot overwrite a newer signal day")
                    if new_day:
                        index = dhan.fetch_candles(
                            conn, 13, "IDX_I", "INDEX",
                            now.replace(hour=0, minute=0, second=0, microsecond=0), now)
                        locked = locked_contracts(master, index, day)
                        values = dict(strategy_version=VERSION, ATM=locked["spot_0929_close"],
                                      STRIKE=locked["strike"], expiry=locked["expiry"], dte=locked["dte"])
                        for kind in ("CE", "PE"):
                            contract = locked["contracts"][kind]
                            values[kind + "_symbol"] = contract["symbol"]
                            values[kind + "_security_id"] = contract["security_id"]
                            values[kind + "_lot_size"] = contract["lot_size"]
                    else:
                        values = dict(doc)
                        values.pop("_id")
                    if values["strategy_version"] != VERSION:
                        raise ValueError("Incompatible daily signal; do not overwrite its contract lock")
                    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=14)
                    for kind in ("CE", "PE"):
                        bars = dhan.fetch_candles(
                            conn, values[kind + "_security_id"], "NSE_FNO", "OPTIDX", start, now)
                        for field, value in option_levels(bars, day, now).items():
                            values[kind + "_" + field] = value
                    values.update(updated_at=str(now), broker="DHAN")
                    for field in ("day", "CE_last_40_high", "PE_last_40_high", "skip_day"):
                        values.pop(field, None)
                    query = {"_id": SIGNAL_ID}
                    if doc is not None:
                        query["updated_at"] = doc["updated_at"]
                    changes = {"$set": values, "$unset": {
                        "day": "", "CE_last_40_high": "", "PE_last_40_high": ""}}
                    if new_day:
                        changes["$unset"]["skip_day"] = ""
                    # Both charts must succeed; preserve the executor-owned same-day skip marker.
                    result = collection.update_one(query, changes, upsert=doc is None)
                    if doc is not None and result.matched_count != 1:
                        raise ValueError("Signal day changed during publication; retry with the current lock")
                    doc = collection.find_one({"_id": SIGNAL_ID})
                    latest_state = f"ATM {doc['STRIKE']} | Expiry {doc['expiry']} | Updated {doc['updated_at'][11:19]}"
                    details = latest_state
                    for kind in ("CE", "PE"):
                        details += (f" | {kind} close={doc[kind + '_close']} "
                                         f"high20={doc[kind + '_last_20_high']} "
                                         f"low20={doc[kind + '_last_20_low']} "
                                         f"asof={doc[kind + '_data_asof']}")
                    print(details, flush=True)
                    if published_day != now.date():
                        notify(slack, "Nifty charts ready | " + latest_state)
                        published_day = now.date()
                    last_minute = minute
                if last_error is not None:
                    notify(slack, "Nifty premium signal recovered")
                    last_error = None
            except Exception as error:
                message = util.exception_detail(error)
                print(message, flush=True)
                if message != last_error or now - last_alert >= timedelta(minutes=15):
                    notify(slack, "Nifty premium signal error: " + message)
                    last_error, last_alert = message, now
            if now - heartbeat >= timedelta(hours=1):
                state = latest_state
                if last_error is not None:
                    state = "ERROR: " + last_error
                notify(slack, "Nifty signal status | " + state)
                heartbeat = now
            time.sleep(10)
        notify(slack, "Nifty premium signal closing bell | " + latest_state)
    finally:
        client.close()


if __name__ == "__main__":
    main()
