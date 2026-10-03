"""User-authorized experimental V3.4 short notifications; no order execution."""
import json
import math
import os
import time
from pathlib import Path
from datetime import datetime,timezone
from swing_telegram import load_json,late_entry_gate,fmt_price,trade_levels,send_telegram

CONFIG=Path('experimental_telegram_config.json')
STATE=Path('experimental_telegram_state.json')
EVENTS=Path('swing_events.jsonl')
SCAN=Path('new_swing.json')


def eligible(event,scan,cfg,state,now):
    if event.get('event')!='SWING_CONTINUATION_TRIGGER':return False,'not a continuation trigger'
    if event.get('bias')!='SHORT':return False,'short-only experiment'
    try:
        ts=int(event['timestamp_ms']);age=now-ts
        if ts<int(cfg['enabled_after_ms']):return False,'predates activation'
        if not 0<=age<=int(cfg['max_signal_age_seconds'])*1000:return False,'stale or future trigger'
        if not 0<=now-int(scan['generated_at_ms'])<=int(cfg['max_signal_age_seconds'])*1000:return False,'stale scan'
        price=float(event['trigger_price'])
        if not math.isfinite(price) or price<=0:return False,'invalid entry'
        row=next(x for x in scan.get('analysis_pool',[]) if x.get('symbol')==event['symbol'])
        if row.get('bias')!='SHORT' or row.get('setup_state')!='CONTINUATION_TRIGGER':return False,'setup no longer active'
        current=float(row['price']);volume=float(row['quote_volume_24h'])
        if not math.isfinite(current) or current<=0 or not math.isfinite(volume):return False,'invalid live data'
        if abs(current/price-1)*100>cfg['max_entry_drift_pct']:return False,'entry moved too far'
        if volume<10_000_000:return False,'insufficient spot turnover'
        btc=float(scan['market_context']['btc_24h_change_pct'])
        if not math.isfinite(btc) or btc>4:return False,'adverse or invalid BTC context'
        last=state.get('last_alert_at_ms')
        if last is not None and now-int(last)<24*3600000:return False,'one-alert-per-24h limit'
    except (KeyError,ValueError,TypeError,StopIteration):return False,'required live context unavailable'
    return True,'fresh experimental short'


def format_message(event):
    entry=float(event['trigger_price']);tp,sl=trade_levels(entry,'SHORT',5,6)
    stamp=datetime.fromtimestamp(event['timestamp_ms']/1000,timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return (f"[EXPERIMENTAL V3.4 SHORT]\n\nPair: {event['symbol']}\n"
            f"Reference entry (spot): {fmt_price(entry)}\nTP (5% below entry): {fmt_price(tp)}\n"
            f"SL (6% above entry): {fmt_price(sl)}\nSignal time: {stamp}\n"
            "Entry validity: 10 minutes from signal, within 0.5% of reference.\n\n"
            "Experimental spot-derived setup; forward win rate unproven. Check your futures price and liquidation level. "
            "No fixed holding deadline or guaranteed recovery. Manual decision and stop required. "
            "No orders are placed by this bot.")


def save(state):
    temp=STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state,indent=2,sort_keys=True)+'\n');temp.replace(STATE)


def main():
    cfg=load_json(CONFIG,{})
    if cfg.get('enabled') is not True:
        print('Experimental Telegram disabled.');return
    if not os.getenv('TELEGRAM_BOT_TOKEN') or not os.getenv('TELEGRAM_CHAT_ID'):
        raise RuntimeError('Telegram credentials unavailable; no notification sent')
    state=load_json(STATE,{'processed_ids':[],'sent_ids':[]})
    if not state.get('activation_message_sent'):
        if not send_telegram('Experimental V3.4 SHORT alerts enabled.\n5% target / 6% stop; at most one new setup per 24h.\nFresh signals only. Live win rate unproven; no automated trades.\nThis is an activation notice, not a trade call.'):
            raise RuntimeError('Telegram activation not delivered')
        state['activation_message_sent']=True;save(state)
        print('Experimental Telegram activation delivered.')
    scan=load_json(SCAN,{})
    processed=set(state.get('processed_ids',[]));sent=list(state.get('sent_ids',[]));decisions=[]
    records=[]
    if EVENTS.exists():
        for line in EVENTS.read_text().splitlines():
            try:records.append(json.loads(line))
            except (ValueError,TypeError):continue
    records.sort(key=lambda x:int(x.get('timestamp_ms') or 0),reverse=True)
    now=int(time.time()*1000)
    for e in records:
        if e.get('event')!='SWING_CONTINUATION_TRIGGER':continue
        ident=e.get('trigger_id') or f"{e.get('symbol')}-{e.get('timestamp_ms')}"
        if ident in processed:continue
        allowed,reason=eligible(e,scan,cfg,state,now)
        if allowed:
            allowed,reason,*_=late_entry_gate(e['symbol'],'SHORT')
        if allowed:
            if not send_telegram(format_message(e)):raise RuntimeError('Experimental alert not delivered')
            state['last_alert_at_ms']=now;sent.append(ident)
            print('Experimental setup delivered:',ident)
        processed.add(ident)
        decisions.append({'trigger_id':ident,'allowed':bool(allowed),'reason':reason})
        # Persist each successful delivery before proceeding to another event.
        state.update(processed_ids=sorted(processed),sent_ids=sent,last_decisions=decisions[-20:])
        save(state)
    state['last_checked_at_ms']=now;state['mode']='experimental_short_only';save(state)
    print('Experimental check complete; new setups:',sum(x['allowed'] for x in decisions))

if __name__=='__main__':main()
