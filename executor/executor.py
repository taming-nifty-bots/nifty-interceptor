import os
import math
import time
import hashlib
import inspect
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import find_dotenv, load_dotenv
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from slack_sdk import WebClient
from tamingnifty import connect_dhan as dhan
from tamingnifty import utils as util
from tamingnifty.charges import money, order_charges, sum_charges
from tamingnifty.safety import check_trading_day

VERSION = "premium-renko-levels-1pct-20-gain10-low5-2026-11-05"
SIGNAL_ID = "nifty_options_renko"
ACTIVE_STATES = ["pending", "active", "partial", "closing"]
ORDER_NOTIFICATION_STATE = {}


def now_ist():
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


def settings():
    for key in ("CONNECTION_STRING", "user_name", "slack_token", "DHAN_CLIENT_ID",
                "DHAN_PIN", "DHAN_TOTP"):
        if not os.environ.get(key, "").strip():
            raise ValueError("Missing environment variable: " + key)
    if os.environ.get("instrument_name", "NIFTY") != "NIFTY":
        raise ValueError("This strategy supports NIFTY only")
    mode = os.environ.get("live_trading", "false").strip().lower()
    if mode not in ("true", "false"):
        raise ValueError("live_trading must be true or false")
    return dict(user=os.environ["user_name"], mongo=os.environ["CONNECTION_STRING"],
                slack=os.environ["slack_token"], simulated=mode == "false")


def notify(client, message):
    message = "Nifty Interceptor | " + message
    print(message, flush=True)
    try:
        util.notify(message=message, slack_client=client, slack_channel="nifty-interceptor")
    except Exception as error:
        print("Slack notification failed: " + util.exception_detail(error), flush=True)


def phase(message, report=None):
    text = now_ist().strftime("%H:%M:%S") + " | " + message
    if report is None:
        print("Nifty Interceptor | " + text, flush=True)
    else:
        report(text)
    return text


def request_ltp(conn, contract, purpose, report=None):
    phase(f"{purpose} | Requesting fresh LTP | {contract['symbol']}", report)
    try:
        price = valid_price(dhan.get_option_ltp(conn, contract["security_id"]))
    except Exception as error:
        phase(f"{purpose} | LTP failed | {util.exception_detail(error)}", report)
        raise
    phase(f"{purpose} | LTP received Rs{price:.2f} | {contract['symbol']}")
    return price


def trade_id(user, day):
    return f"interceptor:{day}"


def valid_price(price):
    value = float(price)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Expected a finite positive market price")
    return value


def validate_quantity(quantity, lot_size):
    qty = int(quantity)
    lot = int(lot_size)
    if qty != float(quantity) or lot <= 0 or qty <= 0 or qty % lot:
        raise ValueError(f"quantity must be a positive multiple of contract lot size {lot}")
    return qty


def fresh_signal(doc, user, now):
    if (doc is None or doc.get("_id") != SIGNAL_ID
            or doc.get("strategy_version") != VERSION):
        raise ValueError("Missing or incompatible daily signal")
    if pd.Timestamp(doc["updated_at"]).date() != now.date():
        raise ValueError("Signal is not from today")
    age = (now - pd.Timestamp(doc["updated_at"]).to_pydatetime()).total_seconds()
    if not 0 <= age <= 90:
        raise ValueError("Signal publisher is stale or future-dated")
    for kind in ("CE", "PE"):
        age = (pd.Timestamp(now).floor("min") - pd.Timestamp(doc[kind + "_data_asof"])).total_seconds()
        if not 60 <= age <= 120:
            raise ValueError("Signal option candles are stale or incomplete")
    return doc


def first_decision(doc):
    found = []
    for kind in ("CE", "PE"):
        brick = pd.Timestamp(doc[kind + "_brick_time"])
        high = doc[kind + "_last_20_high"]
        if (brick >= pd.Timestamp(doc["updated_at"]).normalize() + pd.Timedelta(hours=9, minutes=30)
                and high is not None and doc[kind + "_close"] > high):
            found.append((doc[kind + "_data_asof"], kind))
    if not found:
        return None
    found.sort()
    if len(found) == 2:
        return dict(status="skip", reason="both_latest_levels_above_high20", signal_time=found[0][0])
    return dict(status="entry", option_type=found[0][1], signal_time=found[0][0])


def entry_is_current(decision, now):
    if decision is None or decision["status"] != "entry":
        return False
    signal = pd.Timestamp(decision["signal_time"])
    earliest = signal + pd.Timedelta(minutes=1)
    return (signal.date() == now.date()
            and pd.Timestamp(str(now.date()) + " 09:30") <= signal
            < pd.Timestamp(str(now.date()) + " 15:14")
            and earliest <= pd.Timestamp(now) < earliest + pd.Timedelta(minutes=1)
            and pd.Timestamp(now) < pd.Timestamp(str(now.date()) + " 15:15"))


def exit_reason(trade, doc, now):
    if now.strftime("%H:%M") >= "15:15" or trade["entry_date"] < str(now.date()):
        return "15:15 EOD" if trade["entry_date"] == str(now.date()) else "Overdue intraday position"
    if doc is None:
        raise ValueError("Need a fresh contract signal before evaluating channel exit")
    kind = trade["option_type"]
    if (trade["security_id"] != doc[kind + "_security_id"]
            or trade["expiry"] != doc["expiry"]):
        raise ValueError("Active trade and signal contracts disagree")
    if (pd.Timestamp(trade["entry_timestamp"]).floor("min") <= pd.Timestamp(doc[kind + "_brick_time"])
            < pd.Timestamp(now).floor("min")):
        if trade["max_pnl_reached"] > trade["total_investment"] * 0.10:
            if (doc[kind + "_last_5_low"] is not None
                    and doc[kind + "_close"] < doc[kind + "_last_5_low"]):
                return "5 brick low breakout after gain>10%"
        elif (doc[kind + "_last_20_low"] is not None
                and doc[kind + "_close"] < doc[kind + "_last_20_low"]):
            return "20 brick low breakout"
    return None


def execute_order(conn, orders, key, contract, quantity, side, now, simulated, report=None):
    existing = orders.find_one({"_id": key})
    if not simulated:
        return live_order(conn, orders, key, contract, quantity, side, now, existing, report)
    if existing is not None:
        return existing
    price = request_ltp(conn, contract, side + " paper fill", report)
    filled_at = now_ist()
    if side == "BUY":
        deadline = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if not now <= filled_at < deadline or filled_at.strftime("%H:%M") >= "15:15":
            raise ValueError("Quote arrived outside the entry minute; no buy recorded")
    order = dict(_id=key, orderId=key, orderStatus="TRADED", transactionType=side,
                 securityId=contract["security_id"], tradingSymbol=contract["symbol"],
                 exchangeSegment="NSE_FNO", productType="INTRADAY",
                 quantity=quantity, filledQty=quantity,
                 averageTradedPrice=price, createTime=str(filled_at), simulated=True,
                 strategy_version=VERSION)
    try:
        orders.insert_one(order.copy())
    except DuplicateKeyError:
        order = orders.find_one({"_id": key})
    phase(f"{side} paper fill recorded | Qty {order['filledQty']} "
          f"@ Rs{order['averageTradedPrice']:.2f} | not a broker order", report)
    return order


def live_order(conn, orders, key, contract, quantity, side, now, existing, report=None):
    history = []
    if existing is None:
        deadline = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if side == "BUY" and not now <= now_ist() < deadline:
            raise ValueError("Order submission missed the entry minute")
        correlation = hashlib.sha256(key.encode("utf-8")).hexdigest()[:30]
        history.append(phase(f"{side} | Requesting fresh depth / preparing order | "
                             f"{contract['symbol']} | Qty {quantity} | no order submitted yet"))
        try:
            prepared = dhan.prepare_order(
                conn, contract["security_id"], side, quantity, "NSE_FNO", "INTRADAY",
                entry_deadline=deadline.replace(tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()
                if side == "BUY" else None, correlation_id=correlation,
                best_price_first=True, check_spread=False,
                on_event=lambda message: history.append(phase(f"{side} | " + message)))
        except Exception as error:
            history.append(phase(f"{side} | Quote/preparation failed | no order submitted by this attempt | "
                                 + util.exception_detail(error)))
            phase(f"{side} execution update:\n" + "\n".join(history), report)
            raise
        quote_details = f"LIMIT Rs{prepared['request']['price']:.2f}"
        if "initial_quote" in prepared:
            initial = prepared["initial_quote"]
            received = datetime.fromtimestamp(initial["received_at"], ZoneInfo("Asia/Kolkata"))
            quote_details += (f" | best {'ask' if side == 'BUY' else 'bid'} "
                              f"Rs{initial['reference']:.2f} | received {received.strftime('%H:%M:%S')}")
        history.append(phase(f"{side} | Fresh depth prepared | " + quote_details))
        intent = dict(_id=key, orderStatus="SUBMITTING", transactionType=side,
                      securityId=contract["security_id"], tradingSymbol=contract["symbol"],
                      quantity=quantity, simulated=False, strategy_version=VERSION,
                      prepared=prepared, correlationId=correlation)
        try:
            result = orders.insert_one(intent)
        except DuplicateKeyError:
            existing = orders.find_one({"_id": key})
        else:
            if not result.acknowledged:
                raise RuntimeError("Order intent and initial quote were not acknowledged; no order submitted")
            # Never resubmit after an ambiguous response or a crash after sending.
            history.append(phase(f"{side} | Intent saved; placing LIMIT order now | Qty {quantity}"))
            try:
                placed = dhan.submit_prepared_order(conn, prepared)
            except Exception as error:
                history.append(phase(f"{side} | Submission failed or unconfirmed | saved intent will be "
                                     f"reconciled; do not send another order | {util.exception_detail(error)}"))
                phase(f"{side} execution update:\n" + "\n".join(history), report)
                raise
            if not placed.get("orderId"):
                raise ValueError(f"Order response has no ID; reconcile intent {key} with Dhan")
            orders.update_one({"_id": key}, {"$set": placed})
            history.append(phase(f"{side} | Order submitted | ID {placed['orderId']} | "
                                 "fill not confirmed yet"))
            existing = orders.find_one({"_id": key})
    else:
        history.append(phase(f"{side} | Reconciling saved order | {key} | no replacement submission"))
    if not existing or existing.get("simulated") is not False:
        raise ValueError(f"Unconfirmed order submission {key}; reconcile with Dhan before retrying")
    if not existing.get("orderId"):
        if not existing.get("correlationId"):
            raise ValueError(f"Legacy unconfirmed order {key}; reconcile with Dhan")
        history.append(phase(f"{side} | Looking up submission by saved correlation | {key}"))
        recovered = dhan.order_by_correlation(conn, existing["correlationId"])
        orders.update_one({"_id": key}, {"$set": recovered})
        existing = orders.find_one({"_id": key})
    if "prepared" not in existing:
        raise ValueError(f"Legacy order {key} has no saved execution policy; reconcile with Dhan")
    confirmed_before = existing.get("orderStatus") == "TRADED" and existing.get("fill_timestamp")
    if confirmed_before:
        order = existing
    else:
        history.append(phase(f"{side} | Checking fills / monitoring same order | "
                             f"ID {existing['orderId']}"))
        label = f"{side} | {contract.get('symbol', contract['security_id'])} | "
        notice_state = ORDER_NOTIFICATION_STATE.setdefault(
            str(existing["orderId"]), dict(repricings=0, warned=False))
        try:
            order = dhan.wait_for_fill(conn, existing["orderId"],
                                      prepared=existing["prepared"],
                                      audit_collection=orders.database[
                                          orders.name.replace("orders_", "order_quotes_", 1)],
                                      audit_id=key,
                                      on_event=lambda message: history.append(phase(label + message)),
                                      on_alert=lambda message: phase(label + message, report),
                                      notification_state=notice_state)
            if str(order.get("orderId")) != str(existing["orderId"]):
                raise ValueError(f"Broker returned a different order for {existing['orderId']}; reconcile with Dhan")
            if order.get("filledQty", 0) < existing.get("filledQty", 0):
                raise ValueError(f"Order {existing['orderId']} filled quantity decreased; reconcile with Dhan")
            if order.get("filledQty", 0) > 0:
                order["fill_timestamp"] = str(now_ist())
            orders.update_one({"_id": key}, {"$set": order})
            order = orders.find_one({"_id": key})
            if order["orderStatus"] in dhan.FINAL_ORDER_STATUSES:
                ORDER_NOTIFICATION_STATE.pop(str(existing["orderId"]), None)
        except Exception as error:
            phase(f"{side} execution update:\n" + "\n".join(history + [
                f"{side} | Fill monitoring failed | ID {existing['orderId']} may still be working; "
                f"reconcile same order | {util.exception_detail(error)}"]), report)
            raise
    if (str(order.get("securityId")) != str(contract["security_id"])
            or order.get("transactionType") != side or order.get("quantity") != quantity):
        raise ValueError(f"Order {order['orderId']} contract/side/quantity mismatch; reconcile with Dhan")
    filled = order.get("filledQty")
    if (not isinstance(filled, int) or not 0 <= filled <= quantity
            or (order.get("orderStatus") == "TRADED" and filled != quantity)):
        raise ValueError(f"Order {order['orderId']} has inconsistent fills; reconcile with Dhan")
    if filled > 0:
        fill_price = valid_price(order["averageTradedPrice"])
    if not confirmed_before:
        result = f"{side} | Broker result {order['orderStatus']} | ID {order['orderId']} | filled {filled}/{quantity}"
        if filled > 0:
            result += f" @ Rs{fill_price:.2f}"
        if order["orderStatus"] == "TRADED":
            result += " | full fill confirmed"
        # Slack must not delay submission after pricing, or the broker fill/deadline checks.
        phase(f"{side} execution update:\n" + "\n".join(history + [result]), report)
    if side == "BUY" and order.get("orderStatus") in dhan.FINAL_ORDER_STATUSES:
        if order.get("filledQty", 0) > 0:
            valid_price(order["averageTradedPrice"])
        return order
    if order.get("orderStatus") != "TRADED" or order.get("filledQty") != quantity:
        raise ValueError(
            f"Order {order['orderId']} {order.get('orderStatus')}: "
            f"filled {order.get('filledQty', 0)}/{quantity}; "
            f"{order.get('omsErrorDescription', '')}. Reconcile with Dhan; no duplicate order will be sent")
    valid_price(order["averageTradedPrice"])
    return order


def claim_entry(strategies, doc, user, quantity, now, simulated=True):
    decision = first_decision(doc)
    if not entry_is_current(decision, now):
        return None
    # Include historical records: never place a second trade because its era differs.
    if strategies.find_one({"entry_date": str(now.date())}) is not None:
        return None
    kind = decision["option_type"]
    contract = dict(security_id=doc[kind + "_security_id"], symbol=doc[kind + "_symbol"],
                    lot_size=doc[kind + "_lot_size"])
    qty = validate_quantity(quantity, contract["lot_size"])
    trade = dict(_id=trade_id(user, str(now.date())), strategy_version=VERSION,
                 instrument_name="NIFTY", broker="DHAN", simulated=simulated,
                 strategy_state="pending", entry_date=str(now.date()), entry_signal=decision["signal_time"],
                 option_type=kind, trend="Bullish" if kind == "CE" else "Bearish",
                 strike=doc["STRIKE"], expiry=doc["expiry"], dte=doc["dte"],
                 security_id=contract["security_id"], long_option_symbol=contract["symbol"],
                 lot_size=contract["lot_size"], quantity=qty, signal_doc_id=doc["_id"],
                 entry_timestamp=str(now), entry_time=now.strftime("%H:%M:%S"))
    try:
        strategies.insert_one(trade.copy())
    except DuplicateKeyError:
        return None
    return trade


def open_pending(conn, strategies, orders, trade, now, report=None):
    key = trade["_id"] + ":BUY"
    existing = orders.find_one({"_id": key})
    decision = dict(status="entry", signal_time=trade["entry_signal"])
    if existing is None and not entry_is_current(decision, now):
        strategies.update_one({"_id": trade["_id"], "strategy_state": "pending"},
                              {"$set": dict(strategy_state="aborted",
                                             exit_reason="Missed next-minute entry window")})
        raise ValueError("Pending entry expired; daily slot remains consumed")
    contract = dict(security_id=trade["security_id"], symbol=trade["long_option_symbol"])
    try:
        order = execute_order(conn, orders, key, contract, trade["quantity"], "BUY", now,
                              trade["simulated"], report=report)
    except ValueError as error:
        # Only preparation failures without a saved intent prove nothing was sent.
        if not trade["simulated"] and orders.find_one({"_id": key}) is None:
            strategies.update_one({"_id": trade["_id"], "strategy_state": "pending"},
                                  {"$set": dict(strategy_state="aborted",
                                                 exit_reason="Entry preparation refused: " + str(error))})
        raise
    filled = order["filledQty"]
    if filled == 0:
        strategies.update_one({"_id": trade["_id"], "strategy_state": "pending"},
                              {"$set": dict(strategy_state="aborted",
                                             exit_reason=f"Entry {order['orderStatus']} without fills: "
                                             f"{order.get('omsErrorDescription', '')}; daily slot consumed")})
        return
    price = valid_price(order["averageTradedPrice"])
    filled_at = order.get("fill_timestamp", order.get("createTime"))
    if filled_at is None:
        raise ValueError("Filled entry is missing its confirmation timestamp")
    entry_fees = order_charges("options", "BUY", price, filled)
    marked_trade = dict(trade, long_option_cost=price, quantity=filled)
    updates = pnl_updates(marked_trade, price, pd.Timestamp(filled_at).to_pydatetime())
    updates["$set"].update(
        strategy_state="active", long_option_cost=price, entry_order_id=key,
        entry_timestamp=filled_at, entry_time=filled_at[11:19],
        quantity=filled, requested_quantity=trade["quantity"],
        total_investment=money(price * filled), charges=sum_charges(entry_fees),
        pnl=0.0)
    if filled != trade["quantity"]:
        updates["$set"].update(strategy_state="closing",
                               exit_reason="Cancelled partial entry; flatten actual filled quantity")
    strategies.update_one({"_id": trade["_id"], "strategy_state": "pending"}, updates)


def pnl_updates(trade, price, now):
    gross = money(trade["quantity"] * (price - trade["long_option_cost"]))
    return {
        "$set": dict(running_pnl=gross, last_mark=str(now)),
        "$max": dict(max_pnl_reached=gross),
        "$min": dict(min_pnl_reached=gross),
    }


def status_message(strategies, now):
    trade = strategies.find_one({"strategy_state": {"$in": ACTIVE_STATES}})
    if trade is None:
        trade = strategies.find_one({"entry_date": str(now.date())})
    if trade is None:
        return "FLAT | no trade today"
    state = trade["strategy_state"]
    text = f"{trade['long_option_symbol']} | Qty {trade['quantity']}"
    if state == "pending":
        return text + " | reconciling entry"
    if state == "aborted":
        return text + " | " + trade["exit_reason"] + " | daily slot consumed"
    if state == "closing":
        return text + " | closing; fill not confirmed | " + trade["exit_reason"]
    if state not in ("active", "closed") or "running_pnl" not in trade:
        return text + " | P&L monitoring unavailable; reconcile legacy/accounting state"
    if state == "closed":
        text += (f"\nEntry {trade['entry_time']} @ {trade['long_option_cost']:.2f}"
                 f" -> Exit {trade['exit_time']} @ {trade['long_exit_price']:.2f}"
                 f"\nNet Rs{trade['net_pnl']:.2f} | P&L Rs{trade['pnl']:.2f}"
                 f" | Charges Rs{trade['charges']['total']:.2f}"
                 f" | {trade['exit_reason']}")
    else:
        text += (f"\nEntry {trade['entry_time']} @ {trade['long_option_cost']:.2f}"
                 f" | Gross P&L Rs{trade['running_pnl']:.2f}"
                 f" | As of {trade['last_mark'][11:19]}")
    return text


def close_trade(conn, strategies, orders, trade, reason, now, report=None):
    contract = dict(security_id=trade["security_id"], symbol=trade["long_option_symbol"])
    key = trade["_id"] + ":SELL"
    strategies.update_one({"_id": trade["_id"], "strategy_state": "active"},
                          {"$set": dict(strategy_state="closing", exit_reason=reason)})
    order = execute_order(conn, orders, key, contract, trade["quantity"], "SELL", now,
                          trade["simulated"], report=report)
    price = valid_price(order["averageTradedPrice"])
    gross = money(trade["quantity"] * (price - trade["long_option_cost"]))
    fees = sum_charges(
        order_charges("options", "BUY", trade["long_option_cost"], trade["quantity"]),
        order_charges("options", "SELL", price, trade["quantity"]))
    filled_at = order.get("fill_timestamp", order.get("createTime"))
    if filled_at is None:
        raise ValueError("Filled exit is missing its confirmation timestamp")
    updates = pnl_updates(trade, price, pd.Timestamp(filled_at).to_pydatetime())
    updates["$set"].update(strategy_state="closed", long_exit_price=price,
                                        exit_date=filled_at[:10],
                                        exit_time=filled_at[11:19],
                                        exit_timestamp=filled_at, exit_order_id=key,
                                        exit_reason=reason, pnl=gross, running_pnl=gross,
                                        charges=fees, net_pnl=money(gross - fees["total"]))
    strategies.update_one({"_id": trade["_id"], "strategy_state": {"$in": ["active", "closing"]}},
                          updates)


def step(conn, strategies, orders, signals, user, quantity, now, simulated=True, report=None):
    active = list(strategies.find({"strategy_state": {"$in": ACTIVE_STATES}}))
    if len(active) > 1:
        raise ValueError("Multiple open/reserved swing positions; manual reconciliation required")
    if active:
        trade = active[0]
        if trade.get("strategy_version") != VERSION or trade.get("simulated") != simulated:
            raise ValueError("Legacy or different-mode open position: reconcile before running the strategy")
        if trade["strategy_state"] == "pending":
            phase(f"Entry recovery | {trade['long_option_symbol']} | "
                  "checking saved BUY intent before any submission", report)
            open_pending(conn, strategies, orders, trade, now, report=report)
            return "Pending entry reconciled | " + status_message(strategies, now)
        if trade["strategy_state"] == "partial":
            raise ValueError("Partial position requires reconciliation before further orders")
        reason = trade.get("exit_reason") if trade["strategy_state"] == "closing" else None
        doc = None
        if reason is None and (
                now.strftime("%H:%M") >= "15:15" or trade["entry_date"] < str(now.date())):
            reason = exit_reason(trade, None, now)
        if reason is None:
            doc = signals.find_one({"_id": SIGNAL_ID})
            doc = fresh_signal(doc, user, now)
            reason = exit_reason(trade, doc, now)
        if reason:
            message = (f"Exit {'recovery' if trade['strategy_state'] == 'closing' else 'observed'}"
                       f" | {trade['long_option_symbol']} | {reason}")
            if "max_pnl_reached" in trade:
                message += f" | peak gross Rs{trade['max_pnl_reached']:.2f}"
            if doc is not None:
                kind = trade["option_type"]
                message += (f"\nCompleted data {doc[kind + '_data_asof']} | "
                            f"Renko close {doc[kind + '_close']:.2f}"
                            f" | low5 {doc[kind + '_last_5_low']} | low20 {doc[kind + '_last_20_low']}")
            phase(message, report)
            close_trade(conn, strategies, orders, trade, reason, now, report=report)
            return "Position closed | " + status_message(strategies, now)
        contract = dict(security_id=trade["security_id"], symbol=trade["long_option_symbol"])
        price = request_ltp(conn, contract, "P&L monitoring")
        updates = pnl_updates(trade, price, now_ist())
        strategies.update_one({"_id": trade["_id"], "strategy_state": "active"},
                              updates)
        phase(f"P&L mark | LTP Rs{price:.2f} | gross Rs{updates['$set']['running_pnl']:.2f}"
              f" | peak gross Rs{max(trade['max_pnl_reached'], updates['$max']['max_pnl_reached']):.2f}"
              f" | low5 {doc[trade['option_type'] + '_last_5_low']}"
              f" | low20 {doc[trade['option_type'] + '_last_20_low']}")
        if (trade["max_pnl_reached"] <= trade["total_investment"] * 0.10
                < updates["$max"]["max_pnl_reached"]):
            phase(f"Gain >10% observed | {trade['long_option_symbol']} | LTP Rs{price:.2f}"
                  f" | gross Rs{updates['$set']['running_pnl']:.2f}"
                  f" | low5 {doc[trade['option_type'] + '_last_5_low']}"
                  f" | low20 {doc[trade['option_type'] + '_last_20_low']}"
                  " | subsequent exit checks use prior5 low; not profit booking", report)
        return None
    if now.weekday() >= 5 or now.strftime("%H:%M") < "09:31" or now.strftime("%H:%M") >= "15:15":
        return None
    if strategies.find_one({"entry_date": str(now.date())}) is not None:
        return None
    doc = signals.find_one({"_id": SIGNAL_ID})
    if (now.strftime("%H:%M") == "09:31"
            and (doc is None or (
                doc.get("_id") == SIGNAL_ID and doc.get("strategy_version") == VERSION
                and pd.Timestamp(doc["updated_at"]).date() < now.date()))):
        return "Waiting for today's signal"
    doc = fresh_signal(doc, user, now)
    if doc.get("skip_day"):
        return None
    decision = first_decision(doc)
    if decision is not None and decision["status"] == "skip":
        signals.update_one({"_id": doc["_id"], "updated_at": doc["updated_at"]},
                           {"$set": {"skip_day": True}})
        return "Both latest option levels exceed high20; skip today"
    trade = claim_entry(strategies, doc, user, quantity, now, simulated)
    if trade is not None:
        kind = trade["option_type"]
        phase(f"Entry observed | {trade['long_option_symbol']} | Qty {trade['quantity']}"
              f" | completed data {trade['entry_signal']}"
              f" | Renko close {doc[kind + '_close']:.2f} > high20 {doc[kind + '_last_20_high']:.2f}",
              report)
        open_pending(conn, strategies, orders, trade, now, report=report)
        return "Entry processed | " + status_message(strategies, now)
    return None


def main():
    load_dotenv(find_dotenv())
    config = settings()
    if not config["simulated"] and (
            not hasattr(dhan, "execution_event")
            or "on_event" not in inspect.signature(dhan.prepare_order).parameters
            or "on_alert" not in inspect.signature(dhan.wait_for_fill).parameters
            or "notification_state" not in inspect.signature(dhan.wait_for_fill).parameters):
        raise RuntimeError("Live batched notifications and 429 retry events require "
                           "tamingnifty 2.4.7; publish and rebuild first")
    quantity = os.environ.get("quantity")
    if quantity is None:
        raise ValueError("Missing quantity; configure the existing executor job explicitly")
    validate_quantity(quantity, 1)
    slack = WebClient(token=config["slack"])
    client = MongoClient(config["mongo"])
    if not check_trading_day(client, lambda message: notify(slack, message)):
        client.close()
        return
    strategies = client["Bots"][f"nifty_interceptor_{config['user']}"]
    orders = client["Bots"][f"orders_nifty_interceptor_{config['user']}"]
    signals = client["Bots"]["supertrend"]
    last_error = None
    last_alert = now_ist()
    heartbeat = now_ist()
    latest_state = "Waiting for first pass"
    try:
        mode = "paper" if config["simulated"] else "live"
        notify(slack, f"Nifty executor started | {mode} | Qty {quantity} | IP {util.get_public_ip()}")
        while True:
            now = now_ist()
            try:
                if (strategies.count_documents({"strategy_state": {"$in": ACTIVE_STATES}}) == 0
                        and strategies.find_one({"entry_date": str(now.date()),
                                                 "strategy_state": "closed"}) is not None):
                    notify(slack, "Trade complete. Executor stopped.\n" + status_message(strategies, now))
                    return
                if now.strftime("%H:%M") >= "15:16" and strategies.count_documents(
                        {"strategy_state": {"$in": ACTIVE_STATES}}) == 0:
                    break
                if (now.strftime("%H:%M") >= "09:31" or strategies.count_documents(
                        {"strategy_state": {"$in": ACTIVE_STATES}}) > 0):
                    conn = dhan.login_to_dhan(slack_channel="nifty-interceptor")
                    message = step(conn, strategies, orders, signals, config["user"], quantity,
                                   now_ist(), config["simulated"],
                                   report=lambda text: notify(slack, text))
                    if message == "Waiting for today's signal":
                        if latest_state != message:
                            print(message, flush=True)
                        latest_state = message
                    else:
                        latest_state = status_message(strategies, now_ist())
                    if message and message != "Waiting for today's signal":
                        if message.startswith("Position closed | "):
                            notify(slack, "Trade complete. Executor stopped.\n" + latest_state)
                            return
                        notify(slack, message)
                if last_error is not None:
                    notify(slack, "Nifty premium executor recovered")
                    last_error = None
            except Exception as error:
                message = util.exception_detail(error)
                print(message, flush=True)
                if message != last_error or now - last_alert >= timedelta(minutes=15):
                    notify(slack, "Nifty premium executor error: " + message)
                    last_error, last_alert = message, now
            if now - heartbeat >= timedelta(hours=1):
                state = latest_state
                if last_error is not None:
                    state = "ERROR: " + last_error
                notify(slack, "Nifty status\n" + state)
                heartbeat = now
            delay = 10
            if not config["simulated"] and strategies.count_documents(
                    {"strategy_state": {"$in": ["pending", "closing"]}}) > 0:
                delay = 2
            time.sleep(delay)
        notify(slack, "Nifty premium executor closing bell | " + status_message(strategies, now_ist()))
    finally:
        client.close()


if __name__ == "__main__":
    main()
