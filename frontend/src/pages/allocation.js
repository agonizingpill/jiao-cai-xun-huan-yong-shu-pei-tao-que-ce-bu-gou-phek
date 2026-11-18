import{title,checklist,copytag,options,field,writer,supervisor}from'../components/books.js';import{PACK_STATES}from'../constants.js';import{esc,data}from'../utils/format.js';import{perform,bindActions,toast}from'../utils/actions.js';import{api}from'../api.js';
function reservedTotal(p,d){return d.assignments.filter(a=>{let l=d.pack_lines.find(x=>x.id===a.line_id);return l&&l.pack_id===p.id&&a.replacement_for_id===null}).length}
function neededTotal(p,d){return d.pack_lines.filter(l=>l.pack_id===p.id).reduce((n,l)=>n+l.quantity,0)}
function livePanel(p){return `<div class="feasibility" data-feasibility="${p.id}"><p class="note">按当前库存复算中…</p></div>`}
function reserveButton(s,p){let done=p.state==='allocated';return `<button type="button" class="reserve-btn" data-reserve="${p.id}" data-code="${esc(p.code)}" ${!supervisor(s)||done?'disabled':''}>${done?'整班已配齐（重试不重复占书）':'确认配套 / 按当前库存占书'}</button>`}
export function render(s){let d=s.data;return title('04 / PICK & PURCHASE','凑不齐整班不占书、不开票。','主管确认时按当时库存再算一遍：必修可配册数守不住整班需求就整体撤销本次占书，不开补购单；重复确认不重复占书。')+`<section class="allocation-packs">${d.packs.map(p=>`<article class="allocation-sheet"><div><small>${esc(p.code)} / ${PACK_STATES[p.state]}</small><h2>${esc(d.classrooms.find(c=>c.id===p.classroom_id)?.name)}</h2><p class="note">已占 <b>${reservedTotal(p,d)}</b> / 应配 ${neededTotal(p,d)} 册</p></div>${checklist(p,d)}<div>${reserveButton(s,p)}${livePanel(p)}</div></article>`).join('')}</section><div class="allocation-work"><section><h2>班级预留书目</h2><table><thead><tr><th>课程与条码</th><th>配套 / 状态</th><th>学生领用</th></tr></thead><tbody>${d.assignments.map(a=>{let c=d.copies.find(c=>c.id===a.copy_id),line=d.pack_lines.find(l=>l.id===a.line_id),p=d.packs.find(p=>p.id===line.pack_id);return `<tr><td>${copytag(c,d)}</td><td>${esc(p.code)}<small>${esc(a.state)} ${a.student_id?' / '+esc(d.students.find(x=>x.id===a.student_id)?.code):''}</small></td><td>${a.state==='reserved'?`<form class="issue-form" data-id="${a.id}"><select name="student_id">${options(d.students.filter(x=>x.classroom_id===p.classroom_id),x=>x.code+' '+x.name)}</select><button ${!writer(s)?'disabled':''}>登记该生领用</button></form>`:'已领或已归还'}</td></tr>`}).join('')}</tbody></table></section><aside><h2>缺册补购票</h2><p class="note">新规则下整班凑不齐会整体撤销占书，不再自动开补购单；这里仅保留历史补购票的到货验收。</p>${d.purchases.map(po=>{let line=d.pack_lines.find(l=>l.id===po.line_id),edition=d.editions.find(e=>e.id===line.edition_id),p=d.packs.find(p=>p.id===line.pack_id);return `<article class="purchase-ticket"><span>PO ${String(po.id).padStart(3,'0')}</span><h3>${esc(edition.title)}</h3><p>${esc(edition.version)} · ${esc(p.code)}<br>缺册订购 ${po.ordered}本 / 已收 ${po.received}本</p>${po.state==='open'?`<form class="receive-form" data-id="${po.id}">${field('quantity','本次验收到货','number',po.ordered-po.received,'min="1"')}${field('barcode_prefix','新书条码前缀','text','PO'+po.id+'-NEW','required')}<button ${!supervisor(s)?'disabled':''}>逐本入库</button></form>`:'<p class="note">到货已齐；在左侧续配班级清单。</p>'}</article>`}).join('')||'<p class="empty">没有补购票：整班可配时直接占书，凑不齐则撤销并说明原因。</p>'}</aside></div>`}
function paintFeasibility(pid,fe){
  let box=document.querySelector(`[data-feasibility="${pid}"]`);if(!box)return;
  if(fe.completable){box.innerHTML=fe.lines.map(r=>`<p class="note ok">可配齐：需${r.required_quantity} · 当前可配${r.available_capacity}（已占${r.assigned_quantity}）</p>`).join('');}
  else{box.innerHTML=fe.lines.filter(r=>!r.completable).map(r=>`<p class="note reason">✕ 凑不齐整班：需${r.required_quantity} · 当前仅可配${r.available_capacity} · 缺${r.short_quantity}册</p>`).join('');
    let btn=document.querySelector(`[data-reserve="${pid}"]`);if(btn&&!btn.disabled)btn.classList.add('blocked');}
}
export function bind(s,reload){
  bindActions(reload);
  // 主管确认页面按当时库存逐配套复算一次（只读，真正把关仍在 reserve 接口内）
  s.data.packs.forEach(async p=>{try{paintFeasibility(p.id,await api('/packs/'+p.id+'/feasibility'))}catch(e){let box=document.querySelector(`[data-feasibility="${p.id}"]`);if(box)box.innerHTML=`<p class="note reason">复算失败：${esc(e.message)}</p>`}});
  document.querySelectorAll('.reserve-btn').forEach(btn=>btn.onclick=async()=>{
    let id=btn.dataset.reserve;
    try{
      let r=await api('/packs/'+id+'/reserve','POST',{});
      toast((r.idempotent?'该配套此前已配齐，本次未重复占书：':'整班已配齐并占书：')+r.reserved_total+'册（'+r.lines.map(x=>x.reserved_quantity+'/'+x.required_quantity).join('，')+'）');
      await reload();
    }catch(e){toast(e.message)}
  });
  document.querySelectorAll('.receive-form').forEach(f=>f.onsubmit=e=>{e.preventDefault();perform('/purchases/'+f.dataset.id+'/receive','POST',data(f,['quantity']),reload,'新书已逐本入库，可继续配套')});
  document.querySelectorAll('.issue-form').forEach(f=>f.onsubmit=e=>{e.preventDefault();perform('/assignments/'+f.dataset.id+'/issue','POST',data(f,['student_id']),reload,'学生领用已登记')})
}
