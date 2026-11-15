'use strict';
let previousId=0;
const $=s=>document.querySelector(s);
function render(s){
 const js=jobIndex(s.jobs),m=s.metrics;
 $('#liveStatus').textContent=s.paused?'SIM PAUSED':'SIM RUNNING';
 $('#pauseButton').textContent=s.paused?'▶ Start simulation':'Ⅱ Pause simulation';
 $('#simTime').textContent=`T+${String(s.minute).padStart(3,'0')}m`;
 $('#metricWork').textContent=m.percent+'%';$('#metricUnits').textContent=m.completed_units+' / '+m.total_units+' units';
 $('#metricJobs').textContent=m.jobs_done+' / '+m.jobs_total;
 $('#metricActive').textContent=m.active+' / '+s.tractors.length;
 $('#metricHeld').textContent=m.held;
 $('#metricSafety').textContent=m.violations;
 $('#metricSafety').className='v '+(m.violations?'red':'mint');
 $('#jobsCount').textContent=s.jobs.length+' JOBS';
 const fields=['North Ridge','East Orchard','West Terrace'];
 $('#fields').innerHTML=fields.map(f=>{
  const jobs=s.jobs.filter(j=>j.field===f),total=jobs.reduce((n,j)=>n+j.total,0),done=jobs.reduce((n,j)=>n+j.done,0);
  const percent=progress(done,total),working=s.tractors.filter(t=>t.field===f&&t.status==='working');
  let tiles='';for(let i=0;i<24;i++){const p=i/24*100;tiles+=`<span class="crop ${p<percent?'done':working.length&&p<percent+12?'working':''}"></span>`}
  return `<div class="field"><h3>${esc(f)}</h3><p>${jobs.length} operations • ${working.length} tractors active</p><div class="rows">${tiles}</div><div class="fieldfoot"><span>${done}/${total} work units</span><strong>${fmt(percent)}%</strong></div></div>`
 }).join('');
 $('#tractors').innerHTML=s.tractors.map(t=>{
  const j=js[t.job],battery=fmt(t.battery);
  return `<div class="tractor"><div class="tractoricon">🚜</div><div class="tractorinfo"><div class="tractorname">${esc(t.name)} <span class="status ${statClass(t.status)}">${esc(statName(t.status))}</span></div><div class="meta">${j?esc(j.name):'No current assignment'} • ${esc(t.implements.join(' / '))}</div><div class="progress"><div class="bar" style="width:${t.battery}%"></div></div></div><div class="side"><div style="font-weight:850;font-size:14px">${battery}%</div><div class="meta">BATTERY</div></div></div>`;
 }).join('');
 $('#jobs').innerHTML=s.jobs.map(j=>`<div class="job"><div class="jobtop"><div><div class="jobtitle">${esc(j.name)}</div><div class="jobmeta">${esc(j.field)} · ${esc(j.implement.toUpperCase())} · Deadline T+${j.deadline}m ${j.depends?'· after '+esc(j.depends):''} · priority ${j.priority}</div></div><span class="tag ${esc(j.status)}">${esc(j.status.toUpperCase())}${j.assigned?' · '+esc(j.assigned):''}</span></div><div class="jobprogress"><div class="progress"><div class="bar" style="width:${progress(j.done,j.total)}%"></div></div><span>${j.done}/${j.total}</span></div></div>`).join('');
 $('#explanation').textContent=s.explanation;
 const a=s.agent_mesh, c=a.communication, o=a.orchestration||{};
 $('#agentSummary').innerHTML=`<span><b>${c.directed_messages}</b> directed msgs</span><span><b>${c.payload_bytes}</b> payload bytes</span><span><b>${c.idle_cycles_without_messages}</b> idle plans skipped</span><span><b>${a.memory_count}</b> memories</span><span><b>${o.blocked_plans||0}</b> bad plans blocked</span><span><b>${o.fallback_active?'ON':'off'}</b> fallback planner</span><span><b>${s.reserve??0}%</b> battery reserve</span>${a.last_checkpoint?`<span><b>${esc(a.last_checkpoint.job)} @ ${a.last_checkpoint.done}/${a.last_checkpoint.total}</b> last checkpoint</span>`:''}`;
 const broken=!!(o.faults&&o.faults.Planner);
 $('#faultButton').textContent=broken?'✓ Restore planner AI':'⚠ Break the planner AI';
 $('#agentMessages').innerHTML=a.messages.slice(0,12).map(m=>`<div class="agentline"><span class="agenttime">${esc(m.time)}</span><span class="agentpath">${esc(m.sender)} <span>→</span> ${esc(m.recipient)}</span><span class="agentverb">${esc(m.verb)}</span></div>`).join('');
 const llm=a.llm; $('#llmStatus').textContent=llm.configured?(llm.model+' · '+llm.status):'OFF (optional)';
 $('#advisorText').textContent=llm.advice;
 $('#askAdvisor').disabled=(!llm.configured||llm.pending);
 renderTests(s.tests);
 const g=s.agopenweb;
 if(g){
  $('#aowStatus').textContent=g.state==='sending'?`sending simulated GPS for ${g.feeding} (${g.fixes_sent} fixes)`:
   g.state==='signal_lost'?`${g.feeding} has no signal, so no GPS is sent (AgOpenWeb shows "GPS data lost")`:
   g.state==='waiting'?'waiting for AgOpenWeb at localhost:5174':'off';
  if(document.activeElement!==$('#aowFollow'))$('#aowFollow').value=g.follow;
 }else{$('#aowStatus').textContent='not running in this process';}
 $('#events').innerHTML=s.events.map(e=>`<div class="event"><span class="eventmark ${esc(e.kind)}"></span><span class="eventtime">${esc(e.time)}</span><div class="eventtext">${esc(e.message)}</div></div>`).join('');
}
let testsShown=null;
function renderTests(t){
 if(!t||t===testsShown||JSON.stringify(t)===JSON.stringify(testsShown))return;testsShown=t;
 const all=t.passed===t.total;
 $('#testScore').textContent=`${t.passed} / ${t.total} PASSED · ${t.ms} MS`;
 $('#testScore').className='section-chip '+(all?'mint':'red');
 const label=k=>k.replace(/_/g,' ');
 $('#testBoard').innerHTML=t.results.map(r=>`<div class="testrow ${r.passed?'':'fail'}"><span class="tid">${esc(r.id)}</span><span class="tname">${esc(r.name)}</span><span class="tres">${r.passed?'PASS':'FAIL'}</span><span class="tmetrics">${Object.entries(r.metrics).map(([k,v])=>`${esc(label(k))} <b>${esc(v)}</b>`).join(' · ')}</span></div>`).join('');
}
async function refresh(){try{render(await readState())}catch(e){$('#liveStatus').textContent='DISCONNECTED'}}
async function control(action){try{const x=await sendAction(action);toast(x.message);render(x.state)}catch(e){toast(e.message,true)}}
(async()=>{try{const i=await(await fetch('/api/info')).json();const u=i.url;window.FLEET_TOKEN=new URL(u).searchParams.get('t');$('#judgeLink').href=u;$('#lanUrl').textContent=i.lan; }catch(e){console.warn(e)}
 $('#pauseButton').addEventListener('click',async()=>{const s=await readState();control(s.paused?'start':'pause')});
 $('#resetButton').addEventListener('click',()=>{if(confirm('Reset tractor states, jobs, and event log?'))control('reset')});
 $('#askAdvisor').addEventListener('click',()=>control('ask_advisor'));
 const SAMPLE='name,field,implement,units,deadline,priority,depends\nPlow West Terrace,West Terrace,plow,12,120,2,\nSeed West Terrace,West Terrace,seed,10,170,2,Plow West Terrace\nIrrigate North Ridge,North Ridge,irrigate,15,160,1,';
 const BAD='name,field,implement,units,deadline,priority,depends\nPlow West Terrace,West Terrace,plow,12,,2,\nSpray East Orchard,East Orchard,spray,10,150,2,';
 $('#jobList').value=SAMPLE;
 $('#sampleJobs').addEventListener('click',()=>{$('#jobList').value=SAMPLE});
 $('#badSample').addEventListener('click',()=>{$('#jobList').value=BAD});
 $('#submitJobs').addEventListener('click',async()=>{try{const x=await sendAction('submit_jobs',{csv:$('#jobList').value});toast(x.message);render(x.state)}catch(e){toast(e.message,true);refresh()}});
 $('#aowFollow').addEventListener('change',async e=>{try{const x=await sendAction('agopenweb_follow',{tractor:e.target.value});toast(x.message);render(x.state)}catch(err){toast(err.message,true)}});
 $('#runTests').addEventListener('click',async e=>{const b=e.currentTarget;b.disabled=true;b.textContent='Running…';try{const x=await sendAction('run_tests');toast(x.message);render(x.state)}catch(err){toast(err.message,true)}finally{b.disabled=false;b.textContent='▶ Run all scenarios'}});
 $('#faultButton').addEventListener('click',async()=>{try{const x=await sendAction('fault',{agent:'Planner'});toast(x.message);render(x.state)}catch(e){toast(e.message,true)}});
 await refresh();setInterval(refresh,1100);
})();
