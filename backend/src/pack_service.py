from sqlalchemy import select,func
from fastapi import HTTPException
from .models import Edition,Classroom,Pack,PackLine,Copy,Assignment,Purchase
from .common import inventory_lock,compatible,lock,add,pack,audit
from .feasibility import pack_feasibility

def create(s,d,u):
    room=s.get(Classroom,d.classroom_id)
    if not room:raise HTTPException(404,'班级不存在')
    subjects=[]
    for line in d.lines:
        if not s.get(Edition,line.edition_id):raise HTTPException(404,'课程版次不存在')
        subjects.append(s.get(Edition,line.edition_id).subject)
        if line.quantity>room.size:raise HTTPException(409,'课程份数不能超过班级人数')
    if len(set(subjects))!=len(subjects):raise HTTPException(409,'同一课程不能用多个版次重复列入配套')
    x=Pack(code=d.code,classroom_id=d.classroom_id,term=d.term);s.add(x);s.flush()
    for line in d.lines:s.add(PackLine(pack_id=x.id,**line.model_dump()))
    s.flush();audit(s,u,'建立班级课程配套 '+d.code);return pack(x)
def reserve(s,id,u):
    # 全站 Edition 父锁串行化库存检查与占书；Pack 行锁保证并发确认同一配套时排队
    inventory_lock(s);p=lock(s,Pack,id)
    if p.state=='cancelled':raise HTTPException(409,'取消配套不可续办')
    if p.state=='allocated':
        # 幂等：同一确认的重试/重复点击不再占书，直接回显当前已占册数
        rows=_reservation_rows(s,id)
        audit(s,u,'重复确认配套，未重复占书 '+p.code)
        return pack(p)|{'idempotent':True,'new_assignment_ids':[],'reserved_total':sum(r['reserved_quantity'] for r in rows),'lines':rows,'shortages':[]}
    # 主管确认时按当时库存再算一遍：必修可配量必须守住整班需求
    check=pack_feasibility(s,id)
    if not check['completable']:
        detail=[r for r in check['lines'] if not r['completable']]
        names=[_line_name(s,r) for r in detail]
        # 整体回滚本次占书：不产生任何 Assignment，也不开补购单
        s.rollback()
        raise HTTPException(409,'按当前库存凑不齐整班，已撤销本次占书且不开补购单：'
            + '；'.join('%s缺%d册（可配%d/需%d）'%(n,r['short_quantity'],r['available_capacity'],r['required_quantity']) for n,r in zip(names,detail)))
    new_ids=[]
    for line in s.scalars(select(PackLine).where(PackLine.pack_id==id).order_by(PackLine.id)):
        assigned=s.scalar(select(func.count()).select_from(Assignment).where(Assignment.line_id==line.id,Assignment.replacement_for_id.is_(None)))
        needed=max(0,line.quantity-assigned)
        copies=s.scalars(select(Copy).where(Copy.edition_id.in_(compatible(s,line.edition_id)),Copy.condition=='usable',Copy.state=='available').order_by(Copy.id).limit(needed).with_for_update()).all()if needed else[]
        # 前置可行性已保证库存充足；防御性兜底，不足即整体失败
        if len(copies)<needed:
            s.rollback();raise HTTPException(409,'库存并发变化导致凑不齐整班，已撤销本次占书且不开补购单')
        for copy in copies:
            copy.state='reserved';a=Assignment(line_id=line.id,copy_id=copy.id,deposit_cents=s.get(Edition,line.edition_id).deposit_cents);s.add(a);s.flush();new_ids.append(a.id)
    p.state='allocated'
    rows=_reservation_rows(s,id)
    audit(s,u,'整班配齐并锁定单本 '+p.code+' 共'+str(sum(r['reserved_quantity'] for r in rows))+'册')
    return pack(p)|{'idempotent':False,'new_assignment_ids':new_ids,'reserved_total':sum(r['reserved_quantity'] for r in rows),'lines':rows,'shortages':[]}
def _line_name(s,r):
    e=s.get(Edition,r['edition_id'])
    return (e.title+' '+e.version)if e else ('版次'+str(r['edition_id']))
def _reservation_rows(s,id):
    rows=[]
    for line in s.scalars(select(PackLine).where(PackLine.pack_id==id).order_by(PackLine.id)):
        reserved=s.scalar(select(func.count()).select_from(Assignment).where(Assignment.line_id==line.id,Assignment.replacement_for_id.is_(None)))
        e=s.get(Edition,line.edition_id)
        rows.append({'line_id':line.id,'edition_id':line.edition_id,'title':e.title,'version':e.version,'required_quantity':line.quantity,'reserved_quantity':reserved})
    return rows
def feasibility(s,id):
    lock(s,Pack,id)
    return pack_feasibility(s,id)
def receive(s,id,d,u):
    inventory_lock(s);po=lock(s,Purchase,id);line=s.get(PackLine,po.line_id);p=lock(s,Pack,line.pack_id)
    if p.state=='cancelled' or po.state!='open' or d.quantity>po.ordered-po.received:raise HTTPException(409,'补购数量超过未收缺册或订单已关闭')
    for n in range(d.quantity):s.add(Copy(barcode=d.barcode_prefix+'-'+str(po.received+n+1),edition_id=line.edition_id,condition='usable',state='available',source='缺册补购PO'+str(po.id)))
    po.received+=d.quantity
    if po.received==po.ordered:po.state='received'
    s.flush();audit(s,u,'补购到货验收 '+str(po.id));return pack(po)
