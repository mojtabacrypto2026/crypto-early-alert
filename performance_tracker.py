# -*- coding: utf-8 -*-
"""Nobitex signal performance tracker V5.
Measures MICRO/FAST/CONFIRMED/NEWS independently at 15m/30m/1h/2h/4h,
tracks MFE/MAE and time-to +2/+5, and keeps a 100 completed-4h reliability gate.
"""
import os, json, time, urllib.parse, urllib.request
BASE_URL='https://apiv2.nobitex.ir'
W=os.environ.get('GITHUB_WORKSPACE','.')
ALERT=os.path.join(W,'nobitex_telegram_alert_state.json')
STATE=os.path.join(W,'nobitex_signal_performance_state.json')
START=os.path.join(W,'nobitex_performance_tracking_start.json')
SCHEMA=5; TRACK_SCHEMA=1; MIN4H=100; MAX_EVENTS=1500
TIMEOUT=20; RETRIES=3
TOKEN=os.environ.get('TELEGRAM_BOT_TOKEN','').strip(); CHAT=os.environ.get('TELEGRAM_CHAT_ID','').strip()
CHECK={'15m':900,'30m':1800,'1h':3600,'2h':7200,'4h':14400}

def sf(v,d=0.0):
    try:
        x=float(v); return x if x==x else d
    except: return d

def si(v,d=0):
    try: return int(float(v))
    except: return d

def load(p,d):
    try:
        with open(p,encoding='utf-8') as f: return json.load(f)
    except: return d

def save(p,d):
    t=p+'.tmp'
    with open(t,'w',encoding='utf-8') as f: json.dump(d,f,ensure_ascii=False,indent=2)
    os.replace(t,p)

def get(url,params=None):
    if params: url+='?'+urllib.parse.urlencode(params)
    err=None
    for i in range(RETRIES):
        try:
            r=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Nobitex-Performance-Tracker/5.0','Accept':'application/json'}),timeout=TIMEOUT)
            return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            err=e
            if i<RETRIES-1: time.sleep(i+1)
    raise err

def post(url,data):
    raw=json.dumps(data).encode(); err=None
    for i in range(RETRIES):
        try:
            r=urllib.request.urlopen(urllib.request.Request(url,data=raw,headers={'User-Agent':'Nobitex-Performance-Tracker/5.0','Content-Type':'application/json'},method='POST'),timeout=TIMEOUT)
            return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            err=e
            if i<RETRIES-1: time.sleep(i+1)
    raise err

def tg(msg):
    if not TOKEN or not CHAT: return False
    try: return bool(post('https://api.telegram.org/bot'+TOKEN+'/sendMessage',{'chat_id':CHAT,'text':msg,'disable_web_page_preview':True}).get('ok'))
    except Exception as e: print('TELEGRAM ERROR:',e); return False

def start(now):
    m=load(START,{})
    if isinstance(m,dict) and m.get('schema_version')==TRACK_SCHEMA and si(m.get('started_at'))>0: return si(m['started_at'])
    save(START,{'schema_version':TRACK_SCHEMA,'started_at':now,'created_at':now}); return now

def prices():
    d=get(BASE_URL+'/v3/orderbook/all'); out={}
    if not isinstance(d,dict): return out
    for rs,b in d.items():
        s=str(rs).upper()
        if not s.endswith('USDT') or not isinstance(b,dict): continue
        try: bid=sf((b.get('bids') or [[0]])[0][0]); ask=sf((b.get('asks') or [[0]])[0][0])
        except: continue
        if bid>0 and ask>0: out[s]=(bid+ask)/2
        elif bid>0: out[s]=bid
        elif ask>0: out[s]=ask
    return out

def ret(a,b): return ((b-a)/a*100) if a>0 and b>0 else 0.0

def blank_stats(): return {'count':0,'positive':0,'positive_rate':0.0,'average_return':0.0,'median_return':0.0,'best_return':0.0,'worst_return':0.0,'hit_2pct':0,'hit_5pct':0,'hit_minus_2pct':0}

def stats(vals):
    if not vals: return blank_stats()
    v=sorted(vals); n=len(v); med=v[n//2] if n%2 else (v[n//2-1]+v[n//2])/2
    return {'count':n,'positive':sum(x>0 for x in v),'positive_rate':sum(x>0 for x in v)/n*100,'average_return':sum(v)/n,'median_return':med,'best_return':max(v),'worst_return':min(v),'hit_2pct':sum(x>=2 for x in v),'hit_5pct':sum(x>=5 for x in v),'hit_minus_2pct':sum(x<=-2 for x in v)}

def fresh(): return {'schema_version':SCHEMA,'events':{},'milestones':{},'statistics':{},'reliability':{'minimum_required_4h':MIN4H,'completed_4h':0,'reliable':False},'updated_at':0}

def candidates(a,st):
    if not isinstance(a,dict): return []
    out=[]
    for typ,key in [('MICRO','micro_timestamp'),('FAST','fast_timestamp'),('CONFIRMED','timestamp'),('NEWS','news_timestamp')]:
        t=si(a.get(key));
        if t>=st: out.append((typ,t))
    return out

def alert_price(a,typ,p,s):
    keys={'MICRO':['micro_price','price'],'FAST':['fast_price','price'],'NEWS':['news_price','price'],'CONFIRMED':['price','confirmed_price']}[typ]
    for k in keys:
        x=sf(a.get(k));
        if x>0: return x
    return sf(p.get(s))

def register(P,s,a,typ,t,p,now):
    eid=f'{str(s).upper()}_{typ}_{t}'
    if eid in P['events']: return False
    ap=alert_price(a,typ,p,str(s).upper())
    if ap<=0: print('SKIP EVENT - NO PRICE:',eid); return False
    scorekey={'MICRO':'micro_score','FAST':'fast_score','NEWS':'news_score','CONFIRMED':'score'}[typ]
    P['events'][eid]={'symbol':str(s).upper(),'type':typ,'alert_time':t,'alert_price':ap,'score':a.get(scorekey),'checkpoints':{},'max_return':0.0,'max_price':ap,'min_return':0.0,'min_price':ap,'thresholds':{'2pct':None,'5pct':None,'minus_2pct':None},'metadata':{k:a.get(k) for k in ['micro_flow','micro_flow_delta','micro_spread_bps','fast_volume_ratio','fast_volume_acceleration','rsi','structure','resistance']},'registered_at':now}
    print('NEW EVENT:',eid,'| price:',ap); return True

def update(e,cp,now):
    a=sf(e.get('alert_price')); t=si(e.get('alert_time')); r=ret(a,cp)
    if t<=0 or a<=0:return
    if r>sf(e.get('max_return')): e['max_return']=r;e['max_price']=cp
    if r<sf(e.get('min_return')): e['min_return']=r;e['min_price']=cp
    th=e.get('thresholds') if isinstance(e.get('thresholds'),dict) else {}
    for k,target in [('2pct',2.0),('5pct',5.0),('minus_2pct',-2.0)]:
        if th.get(k) is None and ((r>=target) if target>0 else (r<=target)):
            th[k]={'timestamp':now,'minutes_to_hit':round((now-t)/60,1),'price':cp,'return_percent':r}
    e['thresholds']=th
    c=e.get('checkpoints') if isinstance(e.get('checkpoints'),dict) else {}; elapsed=now-t
    for name,sec in CHECK.items():
        if elapsed>=sec and name not in c: c[name]={'timestamp':now,'price':cp,'return_percent':r}
    e['checkpoints']=c;e['last_update']=now

def per_type(events):
    out={}
    for typ in ['MICRO','FAST','CONFIRMED','NEWS','ALL']:
        es=[e for e in events if typ=='ALL' or e.get('type')==typ]; d={'events':len(es),'checkpoints':{},'MFE':blank_stats(),'MAE':blank_stats()}
        for n in CHECK:
            d['checkpoints'][n]=stats([sf(e.get('checkpoints',{}).get(n,{}).get('return_percent')) for e in es if isinstance(e.get('checkpoints',{}).get(n),dict)])
        complete=[e for e in es if isinstance(e.get('checkpoints',{}).get('4h'),dict)]
        d['MFE']=stats([sf(e.get('max_return')) for e in complete]); d['MAE']=stats([sf(e.get('min_return')) for e in complete])
        for k in ['2pct','5pct']:
            hits=[sf(e.get('thresholds',{}).get(k,{}).get('minutes_to_hit')) for e in es if isinstance(e.get('thresholds',{}).get(k),dict)]
            d['time_to_'+k]={'count':len(hits),'average_minutes':sum(hits)/len(hits) if hits else 0.0,'best_minutes':min(hits) if hits else 0.0,'worst_minutes':max(hits) if hits else 0.0}
        out[typ]=d
    return out

def main():
    now=int(time.time()); st=start(now); print('='*72);print('NOBITEX SIGNAL PERFORMANCE TRACKER - V5');print('UTC:',time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(now)));print('Tracking started:',st);print('='*72)
    alerts=load(ALERT,{}); P=load(STATE,{})
    if not isinstance(P,dict) or P.get('schema_version')!=SCHEMA: P=fresh();print('Performance schema V5 initialized.')
    if not isinstance(P.get('events'),dict):P['events']={}
    if not isinstance(P.get('milestones'),dict):P['milestones']={}
    try: ps=prices()
    except Exception as e: print('CURRENT PRICE FETCH FAILED:',e);return
    print('Current prices:',len(ps)); reg=0
    if isinstance(alerts,dict):
        for s,a in alerts.items():
            for typ,t in candidates(a,st): reg+=register(P,s,a,typ,t,ps,now)
    print('New events registered:',reg); upd=0
    for eid,e in list(P['events'].items()):
        if not isinstance(e,dict):continue
        cp=sf(ps.get(str(e.get('symbol','')).upper()))
        if cp<=0:continue
        update(e,cp,now);P['events'][eid]=e;upd+=1
    print('Events updated:',upd)
    if len(P['events'])>MAX_EVENTS:
        P['events']=dict(sorted(P['events'].items(),key=lambda z:si(z[1].get('alert_time')),reverse=True)[:MAX_EVENTS])
    ev=[e for e in P['events'].values() if isinstance(e,dict)]; P['statistics']=per_type(ev)
    complete=sum(isinstance(e.get('checkpoints',{}).get('4h'),dict) for e in ev)
    P['reliability']={'minimum_required_4h':MIN4H,'completed_4h':complete,'reliable':complete>=MIN4H}
    if complete>=MIN4H and not P['milestones'].get('100_4h'):
        P['milestones']['100_4h']=True
        msg='📊 ۱۰۰ نمونه کامل ۴ساعته برای رادار Nobitex ثبت شد.\n\nعملکرد هر نوع هشدار اکنون قابل بررسی جداگانه است.'
        print('MILESTONE Telegram sent.' if tg(msg) else 'MILESTONE reached; Telegram not sent.')
    P['updated_at']=now;save(STATE,P)
    print('\nPERFORMANCE SUMMARY')
    for typ in ['MICRO','FAST','CONFIRMED','NEWS']:
        d=P['statistics'][typ];print('\n',typ,'| events:',d['events'])
        for n in ['15m','1h','4h']:
            q=d['checkpoints'][n];print(' ',n,'| n=',q['count'],'| avg=',f"{q['average_return']:+.2f}%",'| +2=',q['hit_2pct'],'| +5=',q['hit_5pct'],'| <=-2=',q['hit_minus_2pct'])
        print(' MFE avg/best:',f"{d['MFE']['average_return']:+.2f}%",f"/{d['MFE']['best_return']:+.2f}%",'| MAE worst:',f"{d['MAE']['worst_return']:+.2f}%")
        for k in ['2pct','5pct']:
            q=d['time_to_'+k];print(' time_to_'+k,'hits=',q['count'],'avg_min=',f"{q['average_minutes']:.1f}")
    print('\nCompleted 4H:',complete,'/',MIN4H,'| Reliable:',P['reliability']['reliable']);print('PERFORMANCE TRACKER FINISHED')

if __name__=='__main__':main()
