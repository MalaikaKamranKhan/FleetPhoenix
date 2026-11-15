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
 const a=s.agent_mesh, c=a.communication;
 $('#agentSummary').innerHTML=`<span><b>${c.directed_messages}</b> directed msgs</span><span><b>${c.payload_bytes}</b> payload bytes</span><span><b>${c.idle_cycles_without_messages}</b> idle plans skipped</span><span><b>${a.memory_count}</b> memories</span>`;
 $('#agentMessages').innerHTML=a.messages.slice(0,12).map(m=>`<div class="agentline"><span class="agenttime">${esc(m.time)}</span><span class="agentpath">${esc(m.sender)} <span>→</span> ${esc(m.recipient)}</span><span class="agentverb">${esc(m.verb)}</span></div>`).join('');
 const llm=a.llm; $('#llmStatus').textContent=llm.configured?(llm.model+' · '+llm.status):'OFF (optional)';
 $('#advisorText').textContent=llm.advice;
 $('#askAdvisor').disabled=(!llm.configured||llm.pending);
 $('#events').innerHTML=s.events.map(e=>`<div class="event"><span class="eventmark ${esc(e.kind)}"></span><span class="eventtime">${esc(e.time)}</span><div class="eventtext">${esc(e.message)}</div></div>`).join('');
}
async function refresh(){try{render(await readState())}catch(e){$('#liveStatus').textContent='DISCONNECTED'}}
async function control(action){try{const x=await sendAction(action);toast(x.message);render(x.state)}catch(e){toast(e.message,true)}}
(async()=>{try{const i=await(await fetch('/api/info')).json();const u=i.url;window.FLEET_TOKEN=new URL(u).searchParams.get('t');$('#judgeLink').href=u;$('#lanUrl').textContent=i.lan; }catch(e){console.warn(e)}
 $('#pauseButton').addEventListener('click',async()=>{const s=await readState();control(s.paused?'start':'pause')});
 $('#resetButton').addEventListener('click',()=>{if(confirm('Reset tractor states, jobs, and event log?'))control('reset')});
 $('#askAdvisor').addEventListener('click',()=>control('ask_advisor'));
 await refresh();setInterval(refresh,1100);
})();
