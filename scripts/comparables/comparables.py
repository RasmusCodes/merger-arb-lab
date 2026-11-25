"""Historical merger-spread comparables from LICENSED CRSP daily panels (not in this repository).

Set MERGERARB_CRSP_DIR to a folder with securities.parquet / panel_close.parquet / panel_tr.parquet (split-adjusted
CRSP panels; acquirer splits after each deal window are undone below). Writes derived statistics only.
Deal terms were validated by requiring the final-day spread of completed deals to be within +/-1.5%.
"""
import os
import pathlib

import pandas as pd, numpy as np

d = os.environ.get("MERGERARB_CRSP_DIR")
if not d:
    raise SystemExit("set MERGERARB_CRSP_DIR to the licensed CRSP panel folder")
out = str(pathlib.Path(__file__).resolve().parents[2] / "data" / "derived" / "comparables")
s=pd.read_parquet(d+r'\securities.parquet').set_index('permno')
C=pd.read_parquet(d+r'\panel_close.parquet'); TR=pd.read_parquet(d+r'\panel_tr.parquet')
sid=lambda p: s.loc[p,'sid']
# (n, acquirer, a_permno, target, t_permno, type, ratio, cash, ann, end, outcome, note)
D=[
(1,'AMD',61241,'XLNX',76201,'stock',1.7234,0,'2020-10-27','2022-02-14','closed',''),
(2,'ADI',60871,'MXIM',11896,'stock',0.630,0,'2020-07-13','2021-08-26','closed',''),
(3,'DOW',20626,'DD',11703,'stock',1.282,0,'2015-12-11','2017-08-31','closed','MOE; 1.282 DOW per DD'),
(4,'CMCSA',89525,'TWC',91883,'stock',2.875,0,'2014-02-13','2015-04-24','failed','CMCSA 2:1 split Feb-2015 handled'),
(5,'XOM',11850,'PXD',75241,'stock',2.3234,0,'2023-10-11','2024-05-03','closed',''),
(6,'CVX',14541,'HES',28484,'stock',1.0250,0,'2023-10-23','2025-07-18','partial','data ends 2024-12-31'),
(7,'COP',13928,'MRO',15069,'stock',0.255,0,'2024-05-29','2024-11-22','closed',''),
(8,'COF',81055,'DFS',92121,'stock',1.0192,0,'2024-02-20','2025-05-18','partial','data ends 2024-12-31; day0 2024-02-20'),
(9,'BBT',71563,'STI',68144,'stock',1.295,0,'2019-02-07','2019-12-06','closed','BBT = permno 71563 (now TFC)'),
(10,'GPN',88873,'TSS',76639,'stock',0.8101,0,'2019-05-28','2019-09-18','closed',''),
(11,'SPGI',17478,'INFO',14704,'stock',0.2838,0,'2020-11-30','2022-02-28','closed','INFO last data 2022-02-25'),
(12,'PFE',21936,'AGN',78916,'stock',11.3,0,'2015-11-23','2016-04-06','failed','capped cash election ignored'),
(13,'LRCX',48486,'KLAC',46886,'cash+stock',0.50,32.0,'2015-10-21','2016-10-05','failed',''),
(14,'AVGO',93002,'BRCM',85963,'cash+stock',0.4509,17.0,'2015-05-28','2016-02-01','closed','terms as given (0.4509)'),
(141,'AVGO',93002,'BRCM',85963,'cash+stock',0.2434,17.0,'2015-05-28','2016-02-01','closed','ALT TERMS 0.2434 (analyst recollection, not user-given); variant of deal 14'),
(142,'AVGO',93002,'BRCM',85963,'cash+stock',0.2817,17.0,'2015-05-28','2016-02-01','closed','DATA-IMPLIED ratio (back-solved from final day; circular validation, indicative only)'),
(15,'ADI',60871,'LLTC',10299,'cash+stock',0.2321,46.0,'2016-07-26','2017-03-10','closed',''),
(16,'AVGO',93002,'VMW',None,'cash+stock',0.126,71.25,'2022-05-26','2023-11-22','closed','VMW not in data'),
(17,'SNPS',77357,'ANSS',83621,'cash+stock',0.345,197.0,'2024-01-16','2025-07-17','partial','data ends 2024-12-31'),
(18,'HAL',23819,'BHI',75034,'cash+stock',1.12,19.0,'2014-11-17','2016-05-01','failed',''),
(19,'ANTM',89179,'CI',64186,'cash+stock',0.5152,103.40,'2015-07-24','2017-02-14','failed','ANTM = permno 89179 (ELV)'),
(20,'AET',88845,'HUM',48653,'cash+stock',0.8375,125.0,'2015-07-03','2017-02-14','failed',''),
(21,'MSFT',None,'ATVI',79678,'cash',0,95.0,'2022-01-18','2023-10-13','closed','cash'),
]
# CRSP panel is SPLIT-ADJUSTED to 2024-12-31; restore announcement-share basis for acquirers with later splits
SPLITF={'CMCSA':2.0,'LRCX':10.0,'AVGO':10.0}
def adj(p, start, end):
    # price on announcement-share basis: undo splits detected via close vs tr
    c=C[sid(p)].loc[start:end]; t=TR[sid(p)].loc[start:end]
    f=(c/c.shift(1))/(1+t); f=f.fillna(1)
    ev=f[(np.log(f).abs()>0.25)]
    f2=pd.Series(1.0,index=c.index)
    for dt,v in ev.items():
        r=1/v; r2=round(r*2)/2 if r>1 else 1/(round(1/r*2)/2)
        f2[dt]=1/r2; print('   split event',s.loc[p,'name'],dt.date(),'raw factor',round(float(v),3),'->',r2)
    return c/np.cumprod(f2.values)
rows=[];daily=[];fails=[]
for n,acq,ap,tg,tp,typ,ratio,cash,ann,end,out_,note in D:
    print(n,acq,tg)
    if tp is None or tp not in s.index or sid(tp) not in C.columns:
        rows.append(dict(deal=n,acquirer=acq,target=tg,consideration=typ,outcome=out_,validation='NOT_IN_DATA',notes=note)); continue
    tgt=C[sid(tp)].dropna(); i0=tgt.index.searchsorted(pd.Timestamp(ann)); d0=tgt.index[i0]
    endts=pd.Timestamp(end); last=tgt.index[tgt.index<=endts][-1]
    if out_=='failed': last=tgt.index[tgt.index>=endts][0]
    tpx=adj(tp,d0,last).reindex(tgt.loc[d0:last].index)
    if ap is None: apx=pd.Series(0.0,index=tpx.index)
    else: apx=adj(ap,d0,last).reindex(tpx.index)*SPLITF.get(acq,1.0)
    sp=((ratio*apx+cash)/tpx-1)*100
    sp=sp.dropna(); sp.index.name='date'
    for k,(dt,v) in enumerate(sp.items()): daily.append((n,dt.date(),k,round(v,3)))
    g=lambda k: sp.iloc[k] if k<len(sp) else np.nan
    sp16=g(16); final=sp.iloc[-1]; cal=(last-d0).days
    if out_=='closed': flag='OK' if abs(final)<=1.5 else 'TERMS_SUSPECT'
    elif out_=='partial': flag='PARTIAL_NO_VALIDATION'
    else: flag='FAILED_NA'
    ann_ret=((1+sp16/100)**(365/(endts-sp.index[16]).days)-1)*100 if out_=='closed' and flag=='OK' else np.nan
    if n==142: flag='IMPLIED_TERMS_CIRCULAR'
    rows.append(dict(deal=n,acquirer=acq,target=tg,consideration=typ,outcome=out_,day0=d0.date(),end_date=last.date(),
      trading_days=len(sp)-1,calendar_days=cal,spread_d0=g(0),spread_d5=g(5),spread_d16=sp16,spread_d60=g(60),
      max_date=sp.idxmax().date(),median_spread=sp.median(),max_spread=sp.max(),final_spread=final,annualized_d16_to_close_pct=ann_ret,validation=flag,notes=note))
    if out_=='failed':
        aft=tgt.index[tgt.index>=endts]; aft=aft[0] if len(aft) else last
        tl=list(tgt.index); ia=tl.index(aft)
        r={}
        for lag in (1,3):
            b=tl[ia-lag]
            rt=C[sid(tp)].loc[aft]/C[sid(tp)].loc[b]-1; ra=C[sid(ap)].loc[aft]/C[sid(ap)].loc[b]-1
            r['tgt_ret_T-%d_to_T'%lag]=round(rt*100,2); r['acq_ret_T-%d_to_T'%lag]=round(ra*100,2)
        # note: raw returns; check splits separately
        pre=sp.loc[:aft]
        dsp=sp.diff().loc[sp.index>=sp.index[-1]-pd.Timedelta(days=60)]
        big=dsp.abs().idxmax()
        fails.append(dict(deal=n,target=tg,acquirer=acq,after_date=aft.date(),
           spread_day_before=pre.iloc[-2] if aft in sp.index else np.nan,
           spread_T_minus3=pre.iloc[-4] if aft in sp.index else np.nan,**r,
           largest_move_date_last60d=big.date(),largest_move_pp=round(dsp.loc[big],2),
           spread_before_that=round(sp.shift(1).loc[big],2),spread_after_that=round(sp.loc[big],2)))
R=pd.DataFrame(rows)
for c_ in R.columns:
    if R[c_].dtype==float: R[c_]=R[c_].round(2)
R.to_csv(out+r'\comparables_summary.csv',index=False)
pd.DataFrame(daily,columns=['deal','date','trading_day_index','spread_pct']).to_csv(out+r'\comparables_daily.csv',index=False)
F=pd.DataFrame(fails); F.to_csv(out+r'\failed_deals_termination.csv',index=False)
pd.set_option('display.width',250); pd.set_option('display.max_columns',30)
print(R.drop(columns=['notes']).to_string())
print(R[['deal','target','max_date','max_spread']]); print(F.to_string())
print('median d16 closed-valid only',R[(R.validation=='OK')&(R.outcome=='closed')].spread_d16.median())
print('median d16 closed+partial valid',R[R.validation.isin(['OK','PARTIAL_NO_VALIDATION'])].spread_d16.median())
print('median d16 failed',R[R.outcome=='failed'].spread_d16.median())
print('>=15% at d16:');print(R[R.spread_d16>=15][['deal','acquirer','target','spread_d16','outcome','validation']].to_string())
V=R.set_index('deal')
cl=R[(R.validation=='OK')&(R.outcome=='closed')]; pa=R[R.validation=='PARTIAL_NO_VALIDATION']; fa=R[R.outcome=='failed']
print('MEDIANS d16: closed-valid n=%d %.2f | closed+partial n=%d %.2f | failed n=%d %.2f'%(len(cl),cl.spread_d16.median(),len(cl)+len(pa),pd.concat([cl,pa]).spread_d16.median(),len(fa),fa.spread_d16.median()))
print('medians d0 closed',cl.spread_d0.median(),'d60',cl.spread_d60.median(),'max',cl.max_spread.median())
print('closed-valid d16 values', cl[['target','spread_d16']].values.tolist())
dd=pd.DataFrame(daily,columns=['deal','date','i','s'])
x=dd[(dd.deal==1)&(dd.date>=pd.Timestamp('2021-07-28').date())&(dd.date<=pd.Timestamp('2021-08-12').date())]; print(x.to_string())
for k in (13,19,20,12,18,4): 
    q=dd[dd.deal==k].s; print(k,'d16',q.iloc[16].round(1),'pct of days >=15%:',round((q>=15).mean()*100))
