"""Isolated scheduler + Hub + real WS parsers + real worker; fake provider only."""
import argparse,os,time,json,tempfile,threading,traceback
from pathlib import Path
from types import SimpleNamespace
import bcc.market as market
import bcc.market_clock as mc
import bcc.strategies as strategies
from bcc.ws_quotes import BybitQuoteStream
from bcc.ws_orderbooks import BybitOrderbookWS
from bcc.ws_dispatch import MarketEventQueue
from bcc.data_cache import PublicCache
from bcc.market_hub import MarketDataHub
from bcc.market import PublicData,DataUnavailable
from bcc.market_clock import book_is_fresh, observed_quote, quote_is_recent
from bcc.config import defaults,range_section
from bcc.storage import Repository,Store
from bcc.runtime import process_tick
from bcc.scheduler import MarketTick,MarketScheduler
from bcc.strategies import Veles
from bcc.trading import enter,plan
from test_core import attach
from test_margin_finance import use_mark,raw_tiers
from test_bybit_ws_position_fast_lane import _instrument


def load_universe(symbols_file, held_count):
    symbols=(json.loads(symbols_file.read_text(encoding='utf-8')) if symbols_file
             else [f'COIN{i}USDT' for i in range(824)])
    if (not isinstance(symbols,list) or any(type(s) is not str or not s.strip() for s in symbols)
            or len(set(symbols))!=len(symbols) or held_count<1
            or len(symbols)<held_count+10):
        raise ValueError('symbols must be distinct nonempty strings with room for held symbols and ten faults')
    return symbols, symbols[:held_count]


def capture_probe_inputs(reader, target, held, *, timeout=40):
    """Refresh the owner cohort and rebase depth without retimestamping."""
    deadline=time.monotonic()+timeout
    while True:
        try:
            bars=reader.candles(target,100)
            depth=reader.orderbook(target)
            marks,quotes=reader.cached_position_observations()
            if all(quote_is_recent(marks.get(s),maximum=3) and
                   quote_is_recent(quotes.get(s),maximum=3) for s in held):
                # Match the real owner's final cache handoff: only a genuine
                # newer WS depth can replace the original exchange book.
                latest=reader.cached_execution_book(target,depth)
                if latest is not None:depth=latest
                if book_is_fresh(depth['at']):return bars,depth,(marks,quotes)
        except DataUnavailable:
            pass
        if time.monotonic()>=deadline:
            raise AssertionError('fresh post-outage market observations unavailable')
        time.sleep(.1)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbols-file',type=Path,
                        help='JSON list of symbols; omit for 824 synthetic identifiers')
    parser.add_argument('--sections-file',type=Path,
                        help='Optional JSON list of isolated VELES sections')
    parser.add_argument('--held-count',type=int,default=6,
                        help='Number of initially held synthetic PAPER symbols')
    parser.add_argument('--accelerate-risk-retry',action='store_true',
                        help='Advance the test-only risk retry deadline (not real-time recovery)')
    args=parser.parse_args()
    for k in ('BCC_PUBLIC_CACHE','BCC_MARKET_HUB_MODE','BCC_BYBIT_WS_QUOTES','BCC_BYBIT_WS_BOOKS','BCC_BYBIT_WS_KLINES'):os.environ.pop(k,None)
    clock=mc.MarketClock();clock.ensure=lambda:None;clock.bounds=lambda:(time.time(),time.time());mc.clock=clock;market.clock=clock
    symbols,held=load_universe(args.symbols_file,args.held_count)
    target=symbols[-1];enabled=[False]
    sections=(json.loads(args.sections_file.read_text(encoding='utf-8')) if args.sections_file
              else [range_section(30,40)])
    if not isinstance(sections,list) or not sections:
     raise ValueError('sections file must contain a nonempty list')
    cfg=defaults('VELES');cfg['name']='isolated full scheduler hub';cfg['symbols']=symbols;cfg['sections']=sections;cfg['settings']['orders']['count']=1
    strategies.signal=lambda state,key,spec,bars,now:SimpleNamespace(matched=enabled[0] and key==target+':'+cfg['sections'][0]['id']+':entry',available=True,observations=[])
    stop=threading.Event();errors=[];queue=MarketEventQueue(capacity=16384);seq=[0]
    q=BybitQuoteStream(on_update=queue.notify);b=BybitOrderbookWS(on_update=queue.notify)
    class FakeSocket:
     def __init__(self,kind,stream):self.kind=kind;self.stream=stream
     def close(self):pass
     def send(self,text):
      msg=json.loads(text)
      if msg.get('op')=='subscribe':
       for topic in msg.get('args',[]):self.emit(topic.split('.')[-1])
     def emit(self,symbol):
      stamp=int(time.time()*1000);gen=self.stream._generation
      if self.kind=='q':
       self.stream.ingest({'topic':'tickers.'+symbol,'type':'snapshot','ts':stamp,'data':{'symbol':symbol,'bid1Price':'99.99','ask1Price':'100.01','markPrice':'100'}},generation=gen)
      else:
       seq[0]+=1;self.stream.ingest({'topic':'orderbook.50.'+symbol,'type':'snapshot','ts':stamp,'data':{'s':symbol,'u':seq[0],'seq':seq[0],'b':[['99.99','1000']],'a':[['100.01','1000']]}},generation=gen)
    qs=FakeSocket('q',q);bs=FakeSocket('b',b);q.connect(qs);b.connect(bs)
    class SyntheticProducer:
     def __init__(self,path):
      self.shared=PublicCache(path);self._thread=threading.local();self._quote_stream=q;self._orderbook_stream=b
      self.history_deadline={};self.history_failed=set();self.history_recovered=set();self.lock=threading.Lock()
     def get(self,url,params=None):
      host='api.bybit.com';key,owner,value=self.shared.begin(url,params,host,priority=bool(getattr(self._thread,'position_mark',False)))
      if owner is None:return value
      sym=params['symbol'];now=int(time.time()*1000)
      if url.endswith('/kline') and sym in symbols[len(held):len(held)+10]:
       with self.lock:
        deadline=self.history_deadline.setdefault(sym,time.monotonic()+10)
        failing=time.monotonic()<deadline
        if failing:self.history_failed.add(sym)
        else:self.history_recovered.add(sym)
       if failing:
        self.shared.finish(key,owner,host)
        raise DataUnavailable('synthetic history timeout')
      if url.endswith('/instruments-info'):value=_instrument(sym);ttl=3600
      elif url.endswith('/risk-limit'):value={'retCode':0,'time':now,'result':{'list':raw_tiers(sym)}};ttl=240
      elif url.endswith('/kline'):
       start=(now//60000)*60000;count=int(params.get('limit',100));value={'retCode':0,'time':now,'result':{'list':[[str(start-i*60000),'100','101','99','100','1000','0'] for i in range(count)]}};ttl=2
      elif url.endswith('/funding/history'):value={'retCode':0,'time':now,'result':{'list':[]}};ttl=60
      else:raise AssertionError('Unexpected synthetic HTTP route '+url)
      assert self.shared.finish(key,owner,host,value,ttl=ttl);return value
     def close(self):self.shared.close()
    with tempfile.TemporaryDirectory(prefix='bcc-pr12-whole-') as root:
     path=os.path.join(root,'market.sqlite');producer=SyntheticProducer(path);hub=MarketDataHub(path,producer,max_workers=6);hub.set_ws_dispatcher(queue);assert hub.acquire_leadership()
     os.environ['BCC_PUBLIC_CACHE']=path;os.environ['BCC_MARKET_HUB_MODE']='client';os.environ['BCC_BYBIT_WS_QUOTES']='1';os.environ['BCC_BYBIT_WS_BOOKS']='1'
     reader=PublicData();reader.session.get=lambda *a,**k:(_ for _ in ()).throw(AssertionError('unexpected network'))
     folder=Repository(os.path.join(root,'accounts')).create(cfg);store=Store(folder,10000);sec=cfg['sections'][0]['id']
     with store.transaction() as (state,events):
      for sym in held:
       attach(state,sym);use_mark(state,symbol=sym);ok,why=enter(state,events,sym+':'+sec,sym,'LONG',cfg['settings'],100,25,plan(cfg['settings']['orders'],'LONG',100),sec);assert ok,why
       state['positions'][sym+':'+sec]['exit_signal']=cfg['sections'][0]['exit'];state['marks'][sym]=observed_quote({'bid':99.99,'ask':100.01,'at':time.time()})
      state.pop('_books',None)
     scheduler=MarketScheduler(reader,cfg,lambda *_:100,workers=8,period=2)
     def hub_loop():
      try:
       while not stop.is_set():hub.poll_nonblocking();stop.wait(.02)
      except BaseException as e:errors.append(('hub',type(e).__name__,str(e)));stop.set()
     def publisher():
      try:
       while not stop.is_set():
        batch=queue.take_batch(timeout=.1)
        if batch:hub.publish_ws_events(batch)
      except BaseException as e:errors.append(('publisher',type(e).__name__,str(e)));stop.set()
     def emit_loop():
      try:
       while not stop.is_set():
        with q._lock:qa=list(q._subscriptions)
        with b._lock:ba=list(b._symbols)
        for sym in qa:qs.emit(sym)
        for sym in ba:bs.emit(sym)
        stop.wait(.4)
      except BaseException as e:errors.append(('emitter',type(e).__name__,str(e)));stop.set()
     threads=[threading.Thread(target=f,daemon=True) for f in (hub_loop,publisher,emit_loop)]
     for t in threads:t.start()
     start=time.monotonic();next_report=start+15;seen=set();failures=0;processed=0
     try:
      while time.monotonic()-start<1000 and len(seen)<len(symbols) and not stop.is_set():
       for sym,tick,error in scheduler.collect(store.read(include_charts=False),timeout=.05):
        if error:failures+=1;continue
        try:
         observations=reader.cached_position_observations()
         if process_tick(cfg,store,Veles(),tick,observations=observations):
          scheduler.retry_data_blocked(sym)
        except DataUnavailable:
         if tick and tick.book:scheduler.retry_tick(tick)
         failures+=1;continue
        processed+=1
        if tick.bars and tick.book and not tick.history_error and not tick.quote_error:seen.add(sym)
       if time.monotonic()>=next_report:
        print('elapsed',round(time.monotonic()-start,1),'symbols',len(seen),'ticks',processed,'failures',failures,'positions',len(store.read()['positions']),'queue',queue.stats()['pending'],flush=True);next_report+=15
      print('RESULT symbols',len(seen),'ticks',processed,'failures',failures,'positions',len(store.read()['positions']),'thread_errors',errors,'elapsed',round(time.monotonic()-start,1),flush=True)
      store.audit()
      if len(seen)!=len(symbols) or errors:raise AssertionError('full synthetic scheduler/Hub scan did not complete')
      with producer.lock:
       print('synthetic history faults',len(producer.history_failed),'source recovery',len(producer.history_recovered),flush=True)
       assert len(producer.history_failed)==10 and len(producer.history_recovered)==10
      # Simulate 429 only in the temporary cache: HTTP is fenced, while verified WS remains available.
      from bcc.data_cache import CacheBusy
      producer.shared.record_http_cooldown('0'*64,'synthetic-owner','api.bybit.com',cooldown=3)
      try:producer.shared.begin('https://api.bybit.com/v5/market/risk-limit',{'category':'linear','symbol':'COIN9999USDT'},'api.bybit.com')
      except CacheBusy:pass
      else:raise AssertionError('synthetic 429 did not fence HTTP')
      time.sleep(3.2)
      print('synthetic 429 HTTP fence elapsed PASS',flush=True)
      # Disconnect both real parser objects. Old generations cannot publish;
      # the test emitter behaves like an exchange after those transports reconnect.
      oldq=q._generation;oldb=b._generation;q.disconnect();b.disconnect()
      assert not q.ingest({'topic':'tickers.'+held[0],'type':'snapshot','ts':int(time.time()*1000),'data':{'symbol':held[0],'bid1Price':'99.99','ask1Price':'100.01','markPrice':'100'}},generation=oldq)
      time.sleep(3.2)
      q.connect(qs);b.connect(bs)
      assert q._generation!=oldq and b._generation!=oldb
      # A synthetic 403 in the isolated cache invalidates every Bybit row;
      # it expires on its own real monotonic deadline, without deleting the fence.
      with producer.shared.db() as db:
       db.execute('BEGIN IMMEDIATE');producer.shared._store_denial(db,'api.bybit.com',3,403);db.commit()
      try:reader.get('https://api.bybit.com/v5/market/tickers',{'category':'linear','symbol':held[0]})
      except market.SourceAccessDenied:pass
      else:raise AssertionError('synthetic 403 did not fence reads')
      before=len(store.history());time.sleep(3.2);assert len(store.history())==before
      # Choose one genuine in-flight scanner target; never restart the scheduler.
      candidates=[s for s in scheduler.jobs if s not in held]
      target=candidates[0] if candidates else symbols[-1]
      with store.transaction() as (state,events):state['margin_data'][held[0]]['risk_at']=time.time()-301
      bars,depth,observations=capture_probe_inputs(reader,target,held)
      enabled[0]=True
      if process_tick(cfg,store,Veles(),MarketTick(target,bars,depth,[],{}),observations=observations):
       scheduler.retry_data_blocked(target)
      snapshot=store.read();assert len(snapshot['positions'])==len(held)
      assert any('Устарели публичные ступени риска' in row.get('reason','') for row in snapshot['diagnostics'][target]), repr(snapshot['diagnostics'].get(target))
      # Normally retain real retry deadlines. An explicit accelerated mode
      # is available for diagnosis and must not be reported as real timing.
      if args.accelerate_risk_retry:
       with reader._lock:reader._risk_refresh_due[held[0]]=0
       scheduler.risk_due[held[0]]=0
      # The scanner's complete measured large-universe cycle takes several
      # minutes in this constrained isolated environment. Keep the signal
      # continuously valid through a full cycle; never replay the old book.
      recovery_deadline=time.monotonic()+480;entry=False
      while time.monotonic()<recovery_deadline and not stop.is_set():
       for sym,tick,error in scheduler.collect(store.read(include_charts=False),timeout=.05):
        if error:continue
        try:
         obs=reader.cached_position_observations()
         if process_tick(cfg,store,Veles(),tick,observations=obs):
          scheduler.retry_data_blocked(sym)
        except DataUnavailable:
         if tick and tick.book:scheduler.retry_tick(tick)
         continue
        if len(store.read(include_charts=False)['positions'])==len(held)+1:entry=True;break
       if entry:break
      store.audit()
      snapshot=store.read()
      print('recovery target',target,'tier_age',round(time.time()-snapshot['margin_data'][held[0]]['risk_at'],1),
            'diagnostics',snapshot['diagnostics'].get(target),flush=True)
      assert entry, 'scheduler failed to resume the signal after fresh tier and quotes'
      assert len(store.history())==len(held)+1
      print('outage recovery, source denial expiry, stale tier fence, automatically resumed entry: PASS',flush=True)
     finally:
      stop.set();reader.stop_event.set();scheduler.close()
      for t in threads:t.join(timeout=5)
      hub.close();reader.close()


if __name__ == "__main__":
    main()
