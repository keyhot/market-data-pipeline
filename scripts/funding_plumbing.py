"""E9 plumbing statistics, printed before any AUC existed (vault
`Docs/funding-experiment.md`, *Plumbing statistics*): funding spacing and the
baseline-pinned share, kline integrity and coverage, and the lag checks.

    poetry run python scripts/funding_plumbing.py
"""
import sys; from pathlib import Path; R=Path(__file__).resolve().parent; sys.path.insert(0,str(R)); sys.path.insert(0,str(R.parent))
import numpy as np, pandas as pd
from cross_asset_report import _bars
from model.features import _normalize
A=str(R.parent/'model'/'artifacts')
for s in ['BTCUSDT','ETHUSDT']:
    print('==',s)
    f=pd.read_csv(f'{A}/funding_{s}_15m.csv',header=None,names=['t','r'])
    t=pd.to_datetime(f.t,unit='ms',utc=True); d=t.diff().dt.total_seconds()/3600
    print(' funding increasing',t.is_monotonic_increasing,'spacing counts',d.round(2).value_counts().head(5).to_dict())
    print(' ms offset from hour: max',(f.t%3600000).max(), ' frac==0.0001',(f.r==0.0001).mean().round(3), 'frac 0.0001 by year', f.assign(y=t.dt.year).groupby('y').r.apply(lambda x:(x==0.0001).mean()).round(2).to_dict())
    k=lambda n: pd.read_csv(f'{A}/{n}_{s}_15m.csv',header=None)
    p,i=k('premium'),k('index')
    for nm,df in (('premium',p),('index',i)):
        print(f' {nm}: closeTime ok',((df[6]-df[0])==899999).all(),'dups',df[0].duplicated().sum(),'gaps',(np.diff(df[0])!=900000).sum())
    bars=_normalize(_bars(s)); close=bars.close.astype(float)
    pi=pd.Series(p[4].values,index=pd.to_datetime(p[0],unit='ms',utc=True))
    ii=pd.Series(i[4].values,index=pd.to_datetime(i[0],unit='ms',utc=True))
    print(' bars',len(close),'missing premium',len(close.index.difference(pi.index)),'missing index',len(close.index.difference(ii.index)))
    for lag in (-1,0,1):
        dev=(ii.shift(lag).reindex(close.index)/close-1).abs()
        print(f'  lag {lag:+d}: median |index/spot-1| = {dev.median()*1e4:.2f} bps')
    r=np.log(close/close.shift(1))
    pr=pi.reindex(close.index); dp=pr.diff()
    for lag in (-1,0,1):
        print(f'  lag {lag:+d}: corr(|dprem_t+lag|,|r_t|)={dp.shift(-lag).abs().corr(r.abs()):.3f}  corr(dprem,r)={dp.shift(-lag).corr(r):+.3f} corr(prem,r)={pr.shift(-lag).corr(r):+.3f}')
