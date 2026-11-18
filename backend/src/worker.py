import os,time,json,logging
from pathlib import Path
import redis
from sqlalchemy import select
from .db import Session
from .models import Pack,PackLine,Edition,HistoricalBasket
from .common import lock
from .basket_rules import recommend
from .feasibility import annotate_suggestions
ARTIFACT_DIR=Path(os.environ.get('ARTIFACT_DIR','/artifacts'))
FEASIBILITY_RULE='按已批准兼容关系计算可用单本；建议科目可配册数须覆盖全班人数，且各班必修可配上限不得减少'
def process_pack(s,pack_id):
    """处理单个配套的共需分析：挖规则后按库存筛出能配齐全班的建议。返回 Pack。"""
    p=lock(s,Pack,pack_id)
    if p.ml_state!='queued':return p
    p.ml_state='running';s.commit()
    try:
        baskets=list(s.scalars(select(HistoricalBasket.subjects).order_by(HistoricalBasket.id)))
        subjects=list(s.scalars(select(Edition.subject).join(PackLine,PackLine.edition_id==Edition.id).where(PackLine.pack_id==p.id)))
        result=recommend(baskets,subjects)
        # 共需线索再经库存筛选：只保留按已批准兼容能配齐全班、且不挤占必修可配量的建议
        annotated,feasible=annotate_suggestions(s,p,result['suggestions'])
        result['suggestions']=annotated;result['feasible_suggestions']=feasible
        result['feasibility_rule']=FEASIBILITY_RULE
        p.recommendation=result;p.ml_state='ready';p.error=None
        ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
        (ARTIFACT_DIR/f'basket-{p.id}.json').write_text(json.dumps(result,ensure_ascii=False))
        s.commit()
    except Exception as ex:
        p.ml_state='failed';p.error=str(ex)[:450];s.commit();logging.exception('basket failed')
    return p
q=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
def main():
    while True:
        Path('/tmp/books-heartbeat').touch()
        try:
            item=q.blpop('book-baskets',timeout=2)
            if not item:continue
            with Session()as s:process_pack(s,int(item[1]))
        except Exception:logging.exception('queue unavailable');time.sleep(1)
if __name__=='__main__':main()
