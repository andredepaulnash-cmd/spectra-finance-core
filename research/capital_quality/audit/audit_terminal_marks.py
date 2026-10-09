"""Independent terminal inventory valuation from actual canonical closes."""
from decimal import Decimal as D,getcontext
from pathlib import Path
from datetime import datetime
import argparse,json
import numpy as np
getcontext().prec=60
Z=D(0);TOL=D('1e-18')
def load(p):return json.loads(Path(p).read_text())
def audit(directory,metadata,tape,protocol,cache):
 result=load(Path(directory)/'result.json');positions=load(Path(directory)/'terminal_positions.json')
 end=int(datetime.fromisoformat(protocol['end_exclusive_utc']).timestamp())
 endframe=(end-metadata['start'])//metadata['interval_seconds'];pairs={p:i for i,p in enumerate(metadata['pairs'])}
 total=Z;stale=Z;rows=[]
 for position in positions:
  pair=position['pair']
  if pair not in cache:
   valid=np.flatnonzero(np.isfinite(tape[:endframe,pairs[pair],0]))
   if not len(valid):raise ArithmeticError('Owned asset lacks canonical historical mark')
   index=int(valid[-1]);price=D(str(float(tape[index,pairs[pair],3])))
   closed=metadata['start']+(index+1)*metadata['interval_seconds']
   cache[pair]=(price,closed)
  price,closed=cache[pair];quantity=D(position['quantity']);value=quantity*price;age=end-closed
  total+=value
  if age>300:stale+=value
  rows.append(dict(pair=pair,quantity=str(quantity),last_actual_close_price=str(price),last_actual_close_timestamp=closed,mark_age_seconds=age,marked_value=str(value)))
 if abs(total-D(result['marked_inventory']))>TOL:raise ArithmeticError('Terminal marked inventory differs from authentic closes')
 if abs(D(result['ending_cash'])+total-D(result['ending_equity']))>TOL:raise ArithmeticError('Terminal authentic marked equity differs')
 return dict(passed=True,arm=result['arm'],held_assets=len(rows),independently_marked_inventory=str(total),stale_marked_inventory_over_300s=str(stale),stale_asset_count=sum(r['mark_age_seconds']>300 for r in rows),assets=rows,scope='Actual last historical close valuation only. Stale observed marks remain disclosed; this is not a current executable venue liquidation valuation.')
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--run',type=Path,action='append',required=True);p.add_argument('--prepared',type=Path,required=True);p.add_argument('--protocol',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 metadata=load(a.prepared/'metadata.json');tape=np.load(a.prepared/'ohlcv.npy',mmap_mode='r');protocol=load(a.protocol);cache={}
 result=dict(passed=True,arms=[audit(directory,metadata,tape,protocol,cache) for directory in a.run])
 a.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps([{k:r[k] for k in ['arm','passed','stale_asset_count','stale_marked_inventory_over_300s']} for r in result['arms']]))
