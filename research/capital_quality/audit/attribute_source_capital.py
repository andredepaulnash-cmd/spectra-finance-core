"""Saved-output P-source attribution; descriptive, never a selection rule."""
from collections import defaultdict
from decimal import Decimal as D,getcontext
from pathlib import Path
import argparse,json
import numpy as np
getcontext().prec=60
Z=D(0);TOL=D('1e-18')
def load(p):return json.loads(Path(p).read_text())
def close(a,b,label):
 if abs(D(a)-D(b))>TOL:raise ArithmeticError(label)
def attribute(directory,control,prepared,protocol):
 directory=Path(directory);result=load(directory/'result.json');baseline=load(Path(control)/'result.json')
 if result['status']!='PASS' or baseline['status']!='PASS':raise ArithmeticError('Incomplete comparison')
 final=load(directory/'terminal_release.json');native={lot['lot_id']:lot for lot in load(directory/'terminal_active_lots.json')}
 metadata=load(Path(prepared)/'metadata.json');tape=np.load(Path(prepared)/'ohlcv.npy',mmap_mode='r')
 end_timestamp=int(__import__('datetime').datetime.fromisoformat(protocol['end_exclusive_utc']).timestamp())
 end_frame=(end_timestamp-metadata['start'])//metadata['interval_seconds']
 start_timestamp=int(__import__('datetime').datetime.fromisoformat(protocol['start_utc']).timestamp())
 start_frame=(start_timestamp-metadata['start'])//metadata['interval_seconds']
 pairs={pair:i for i,pair in enumerate(metadata['pairs'])}
 records=defaultdict(lambda:dict(parked_basis_retired=Z,parked_sale_net=Z,parked_realized_pnl=Z,
  remaining_parked_basis=Z,remaining_parked_quantity=Z,remaining_parked_market_value=Z,remaining_parked_open_pnl=Z))
 # Source dispositions expose exact source basis and actual source net receipt.
 for event in load(directory/'release_events.json'):
  if event['event']!='SOURCE_SALE_FINAL':continue
  for row in event['lots']:
   if D(row['parked_basis'])==Z:continue
   # Lot ID identifies exact executed buy; the buy journal carries the pair.
   records[row['lot_id']]['parked_basis_retired']+=D(row['parked_basis'])
   records[row['lot_id']]['parked_sale_net']+=D(row['parked_net'])
   records[row['lot_id']]['parked_realized_pnl']+=D(row['parked_net'])-D(row['parked_basis'])
 buy={event['lot_id']:event for event in load(directory/'release_events.json') if event['event']=='BUY_SOURCE_COMMIT'}
 marks={}
 for source in final['active_source_lots']:
  p=D(source['parked_basis'])
  if p==Z:continue
  lot=native[source['lot_id']];pair=lot['pair']
  if pair not in marks:
   # Every P-owned lot was acquired during this resumed month. Its latest
   # authentic observed mark must therefore be within this historical window.
   valid=np.flatnonzero(np.isfinite(tape[start_frame:end_frame,pairs[pair],0]))
   if not len(valid):raise ArithmeticError('P-funded asset lacks historical close')
   index=start_frame+int(valid[-1]);price=D(str(float(tape[index,pairs[pair],3])))
   marks[pair]=(price,metadata['start']+(index+1)*metadata['interval_seconds'])
  price,at=marks[pair];quantity=D(source['quantity'])*p/D(source['total_basis'])
  value=quantity*price;record=records[source['lot_id']]
  record['remaining_parked_basis']+=p;record['remaining_parked_quantity']+=quantity
  record['remaining_parked_market_value']+=value;record['remaining_parked_open_pnl']+=value-p
 by_asset=defaultdict(lambda:dict(parked_basis_retired=Z,parked_sale_net=Z,parked_realized_pnl=Z,
  remaining_parked_basis=Z,remaining_parked_quantity=Z,remaining_parked_market_value=Z,remaining_parked_open_pnl=Z))
 for lot_id,record in records.items():
  pair=buy[lot_id]['pair']
  for key,value in record.items():by_asset[pair][key]+=value
 rows=[]
 for pair,record in by_asset.items():
  record['pair']=pair;record['total_P_source_marked_pnl']=record['parked_realized_pnl']+record['remaining_parked_open_pnl']
  if pair in marks:
   record['last_actual_mark_price']=marks[pair][0];record['last_actual_close_timestamp']=marks[pair][1]
   record['mark_age_seconds_at_end']=end_timestamp-marks[pair][1]
  rows.append(record)
 close(sum((row['remaining_parked_basis'] for row in rows),Z),final['owned_parked_basis'],'Source remaining basis')
 close(sum((row['parked_basis_retired'] for row in rows),Z),final['parked_basis_retired'],'Source retired basis')
 close(sum((row['parked_sale_net'] for row in rows),Z),D(final['parked_whole_returned'])+D(final['parked_partial_returned']),'Source sale net')
 incremental_realized=D(result['new_realized_profit'])-D(baseline['new_realized_profit'])
 incremental_open=D(result['remaining_open_pnl'])-D(baseline['remaining_open_pnl'])
 incremental_equity=D(result['ending_equity'])-D(baseline['ending_equity'])
 close(incremental_equity,incremental_realized+incremental_open,'Control-relative PnL attribution')
 negative=sorted((r for r in rows if r['total_P_source_marked_pnl']<Z),key=lambda r:r['total_P_source_marked_pnl'])[:5]
 positive=sorted((r for r in rows if r['total_P_source_marked_pnl']>Z),key=lambda r:r['total_P_source_marked_pnl'],reverse=True)[:5]
 return dict(arm=result['arm'],complete_window=True,incremental_equity_vs_OFF=incremental_equity,
  incremental_realized_vs_OFF=incremental_realized,incremental_open_pnl_vs_OFF=incremental_open,
  P_source_realized=sum((r['parked_realized_pnl'] for r in rows),Z),P_source_remaining_open_pnl=sum((r['remaining_parked_open_pnl'] for r in rows),Z),
  P_source_total_marked_pnl=sum((r['total_P_source_marked_pnl'] for r in rows),Z),
  five_largest_P_source_losses=negative,five_largest_P_source_gains=positive,all_P_source_assets=rows,
  limits=['Descriptive in-sample attribution only; no future-selection claim.',
   'P source attribution is not a causal account delta: permitting cash also changes ordinary-funded decisions and inherited parents.',
   'Marked values use last actual historical close, not an estimated venue liquidation fill.'])
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--run',type=Path,action='append',required=True);p.add_argument('--control',type=Path,required=True);p.add_argument('--protocol',type=Path,required=True);p.add_argument('--prepared',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
 result=dict(passed=True,arms=[attribute(path,args.control,args.prepared,load(args.protocol)) for path in args.run])
 args.output.write_text(json.dumps(result,default=str,indent=2)+'\n')
 print(json.dumps([{'arm':r['arm'],'delta_equity':str(r['incremental_equity_vs_OFF']),'delta_realized':str(r['incremental_realized_vs_OFF']),'delta_open':str(r['incremental_open_pnl_vs_OFF']),'P_source_pnl':str(r['P_source_total_marked_pnl'])} for r in result['arms']]))
