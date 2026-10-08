"""User-authorized Telegram notifications for new prospective paper trades only."""
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from swing_telegram import send_telegram, trade_levels, fmt_price
from telegram_rewards import reward_snapshot

STATE = Path('paper_learning/telegram.json')


def eligible(trade, config, now, price):
    if trade['predicted_ms'] < config['enabled_after_ms']:
        return False, 'predates activation'
    if trade['status'] != 'OPEN':
        return False, 'not an open paper trade'
    if not 0 <= now-trade['entry_ms'] <= config['max_entry_age_seconds']*1000:
        return False, 'entry too old'
    if not math.isfinite(price) or price <= 0:
        return False, 'invalid current price'
    if abs(price/trade['entry_price']-1)*100 > config['max_entry_drift_pct']:
        return False, 'entry moved too far'
    return True, 'fresh paper observation'


def message(trade, price):
    entry=trade['entry_price']
    tp,sl=trade_levels(entry,trade['side'],trade['target_pct'],trade['stop_pct'])
    stamp=datetime.fromtimestamp(trade['entry_ms']/1000,timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return (f"EXPERIMENTAL PAPER SIGNAL — {trade['side']} {trade['symbol']}\n"
            f"Paper entry (spot): {fmt_price(entry)}\nCurrent spot: {fmt_price(price)}\n"
            f"Paper target (+5%): {fmt_price(tp)}\nPaper stop (-6%): {fmt_price(sl)}\n"
            f"Entry time: {stamp}\nID: {trade['id']}\n\n"
            "Forward win rate unproven. This is a rule-based research setup, not a validated AI call. "
            "Shorts are simulated on spot prices; futures fills, funding and liquidation can differ. "
            "No orders placed. Do not chase if price differs by more than 0.5% from entry. "
            "6% price risk can exceed initial margin at 20x leverage.")


def save(state):
    temp=STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state,indent=2,sort_keys=True)+'\n')
    temp.replace(STATE)


def deliver(text):
    try:
        if not send_telegram(text):
            raise RuntimeError('delivery not confirmed')
    except Exception as exc:
        # Do not print request URLs, which may contain the Telegram token.
        raise RuntimeError('Telegram delivery failed: '+type(exc).__name__) from None


def main():
    config=json.loads(Path('paper_learning_telegram_config.json').read_text())
    if not config.get('enabled'):
        print('Paper Telegram disabled'); return
    if os.environ.get('GITHUB_REF') != 'refs/heads/main':
        print('Paper Telegram restricted to main'); return
    learning=json.loads(Path('paper_learning/state.json').read_text())
    state=json.loads(STATE.read_text()) if STATE.exists() else {'sent':{},'outcomes':[], 'skipped':{}}
    if not state.get('activated'):
        deliver('Paper-learning alerts enabled. New research setups and their eventual outcomes will appear here. '
                '5% target / 6% stop; no guaranteed win rate or automated trades. '
                'Earlier WLD, ENA and BTC observations will not be resent. This is an activation notice, not a trade.')
        state['activated']=True;save(state)
    state.setdefault('reward_predictions', {})
    reward_config=json.loads(Path('telegram_reward_config.json').read_text())
    reward_path=Path('paper_learning/telegram_reward_report.json')
    reward_report=json.loads(reward_path.read_text()) if reward_path.exists() else {}
    if reward_config['enabled'] and not state.get('reward_activation_sent'):
        deliver('Telegram reward tracking enabled for new calls from now. 1R = the planned 6% price risk. A 5% target earns +0.83R gross; a stop loses -1R gross, with costs deducted and gap losses retained. Open calls stay pending. Learning runs in shadow mode; no claimed improvement or automatic rule changes.')
        state['reward_activation_sent']=True;save(state)
    now=int(time.time()*1000)
    for tid,t in sorted(learning['trades'].items(),key=lambda item:item[1]['predicted_ms']):
        if tid in state['sent']:
            if t['status'] in ('TARGET','STOP','AMBIGUOUS_LOSS') and tid not in state['outcomes']:
                reward_note=''
                if state['sent'][tid]>=reward_config['enabled_after_ms']:
                    if t['resolved_bar_ms'] < state['sent'][tid]:
                        reward_note='Reward excluded: exit candle overlaps or precedes alert delivery.\n'
                    else:
                        reward_note=(f"Reward: {t['net_return_pct']/t['stop_pct']:+.3f}R after modeled costs\n"
                                     f"Ledger total: {reward_report.get('total_reward_r',0):+.3f}R (paper observations, not account return)\n")
                deliver(f"PAPER OUTCOME — {t['side']} {t['symbol']}\nID: {tid}\n"
                        f"Result: {t['status']}\nModeled net return: {t['net_return_pct']:.2f}%\n"
                        f"Holding time: {t['holding_hours']:.2f} hours\n"
                        f"{reward_note}"
                        "Spot simulation with assumed costs, not your actual futures P&L. "
                        "Ambiguous same-candle target/stop hits count as losses.")
                state['outcomes'].append(tid);save(state)
            continue
        if tid in state['skipped'] or t['status']=='PENDING':
            continue
        ok,reason=eligible(t,config,now,t.get('entry_price',0))
        if ok:
            try:
                query=urlencode({'symbol':t['symbol']})
                with urlopen('https://data-api.binance.vision/api/v3/ticker/price?'+query,timeout=15) as r:
                    price=float(json.load(r)['price'])
            except Exception as exc:
                raise RuntimeError('Spot quote unavailable: '+type(exc).__name__) from None
            ok,reason=eligible(t,config,int(time.time()*1000),price)
            if ok:
                prediction=reward_snapshot(t['side'],t['features'])
                deliver(message(t,price))
                state['sent'][tid]=int(time.time()*1000)
                state['reward_predictions'][tid]=prediction
        if not ok:state['skipped'][tid]=reason
        save(state)
    print('Paper Telegram completed; total setups sent:',len(state['sent']))


if __name__=='__main__':
    main()
