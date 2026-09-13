import os,json,urllib.request,urllib.parse,time
BOT_TOKEN=os.getenv('TELEGRAM_BOT_TOKEN'); CHAT_ID=os.getenv('TELEGRAM_CHAT_ID')
COINS={'BTC-USD':'BTC','ETH-USD':'ETH','SOL-USD':'SOL','XRP-USD':'XRP','ADA-USD':'ADA','DOGE-USD':'DOGE','AVAX-USD':'AVAX','LINK-USD':'LINK','UNI-USD':'UNI','SUSHI-USD':'SUSHI','AAVE-USD':'AAVE','DOT-USD':'DOT','ATOM-USD':'ATOM','LTC-USD':'LTC','BCH-USD':'BCH','ETC-USD':'ETC','NEAR-USD':'NEAR','ALGO-USD':'ALGO','FIL-USD':'FIL','APT-USD':'APT','ARB-USD':'ARB','OP-USD':'OP','INJ-USD':'INJ','PEPE-USD':'PEPE','SHIB-USD':'SHIB'}
WATCH=68; TRADE=80

def get(u):
    r=urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'Crypto-Early-Alert/3.1'}),timeout=20)
    return json.loads(r.read().decode())

def candles(p,g,h):
    # Coinbase max is 350 buckets per request.
    n=int(time.time())
    q=urllib.parse.urlencode({'start':n-h*3600,'end':n,'granularity':g})
    a=get(f'https://api.coinbase.com/api/v3/brokerage/market/products/{p}/candles?{q}').get('candles',[])
    a.sort(key=lambda x:int(x['start']))
    return a[:-1]

def avg(a): return sum(a)/len(a) if a else 0
def pct(a,b): return (a-b)/b*100 if b else 0

def scan(p):
    c5=candles(p,'FIVE_MINUTE',24)       # 288 buckets
    c15=candles(p,'FIFTEEN_MINUTE',72)   # 288 buckets
    c1=candles(p,'ONE_HOUR',180)         # 180 buckets
    if len(c5)<25 or len(c15)<10 or len(c1)<4:return None
    a=c5[-1]; close=float(a['close']); high=float(a['high']); low=float(a['low']); vol=float(a['volume'])
    x5=pct(close,float(c5[-2]['close'])); x15=pct(close,float(c15[-2]['close'])); x1=pct(close,float(c1[-2]['close'])); x30=pct(close,float(c5[-7]['close']))
    vr=vol/avg([float(x['volume']) for x in c5[-13:-1]])
    va=avg([float(x['volume']) for x in c5[-4:]])/avg([float(x['volume']) for x in c5[-12:-4]])
    pressure=(close-low)/(high-low) if high>low else .5
    res=max(float(x['high']) for x in c5[-37:-1]); dist=(close-res)/res*100
    s=0;r=[]
    if .10<=x5<.80:s+=16;r+=['شتاب تازه 5m']
    elif .80<=x5<1.50:s+=9;r+=['حرکت 5m']
    if .25<=x15<2:s+=13;r+=['روند 15m مثبت']
    elif x15>=2:s+=6
    if .30<=x1<3.5:s+=8;r+=['روند 1H مثبت']
    elif x1>=3.5:s-=8;r+=['رشد 1H زیاد']
    if x5>0 and x15>0 and x1>0:s+=9;r+=['هم‌جهتی تایم‌فریم‌ها']
    if vr>=1.5:s+=10;r+=['حجم غیرعادی']
    elif vr>=1.25:s+=6;r+=['افزایش حجم']
    if va>=1.35:s+=9;r+=['شتاب حجم']
    elif va>=1.15:s+=4
    if pressure>=.70:s+=7;r+=['فشار خرید']
    if -.40<=dist<=.25:s+=12;r+=['فشار روی مقاومت']
    elif .25<dist<=1:s+=6;r+=['شکست تازه مقاومت']
    if x30>4.5:s-=12;r+=['حرکت 30m زیاد؛ دیر شده']
    if x1>6:s-=15
    return max(0,min(100,s)),x5,x15,x1,x30,vr,va,dist,pressure,r

def send(t):
    if not BOT_TOKEN or not CHAT_ID:return False
    u=f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    d=urllib.parse.urlencode({'chat_id':CHAT_ID,'text':t}).encode()
    return json.loads(urllib.request.urlopen(urllib.request.Request(u,data=d),timeout=20).read().decode()).get('ok',False)

def main():
    b=scan('BTC-USD'); regime=(not b) or (b[2]>-.8 and b[3]>-1.5); out=[]
    for p,n in COINS.items():
        if n=='BTC':continue
        try:
            x=scan(p)
            if x: out.append((n,x)); print(n,x[0],f'5m={x[1]:+.2f}%',f'15m={x[2]:+.2f}%',f'1h={x[3]:+.2f}%',f'vol={x[5]:.2f}x')
        except Exception as e:print('ERROR',n,e)
    for n,x in sorted(out,key=lambda z:z[1][0],reverse=True)[:5]:
        if x[0]<WATCH or x[1]<.10 or x[2]<=0 or x[4]>=4.5:continue
        if x[0]<TRADE and not regime:continue
        tag='🚨 هشدار معامله' if x[0]>=TRADE and regime else '👀 هشدار دیده‌بانی'
        reasons='، '.join(x[9])
        msg=(f'{tag}\n{n}\nامتیاز: {x[0]}/100\n5m {x[1]:+.2f}% | 15m {x[2]:+.2f}% | 1H {x[3]:+.2f}%\n'
             f'30m {x[4]:+.2f}% | حجم {x[5]:.2f}x | شتاب حجم {x[6]:.2f}x\n'
             f'فاصله مقاومت {x[7]:+.2f}% | فشار خرید {x[8]:.0%}\nدلایل: {reasons}\n\n'
             'هشدار برای فشار قبل/آغاز حرکت است؛ تضمین رشد نیست.')
        send(msg)

if __name__=='__main__':main()
