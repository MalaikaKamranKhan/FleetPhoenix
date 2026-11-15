'use strict';
const j$=s=>document.querySelector(s);
let current=null;
function b(label,style,op,tid,disabled=false){return `<button class="btn ${style}" data-op="${op}" data-tid="${tid}" ${disabled?'disabled':''}>${label}</button>`}
function show(s){current=s;j$('#phoneState').textContent=`${s.paused?'PAUSED':'RUNNING'} · T+${s.minute} simulated minutes · ${s.metrics.jobs_done}/${s.metrics.jobs_total} jobs completed · ${s.metrics.violations} constraint violations`;
 j$('#judgeExplain').textContent=s.explanation;
 j$('#judgeTractors').innerHTML=s.tractors.map(t=>{
 let buttons='';
 if(['working','idle'].includes(t.status))buttons=b('⚠ Confirm breakdown','danger','fail',t.id)+b('📡 Lose connection','blue','disconnect',t.id)+b('⏳ Delay telemetry 6 min','blue','delay',t.id)+b('⚡ Drain battery','gold','drain',t.id);
 if(t.status==='offline')buttons=b('✅ Confirm safely stopped','gold','confirm_stop',t.id)+b('📶 Restore signal','blue','restore',t.id);
 if(t.status==='failed'||t.status==='quarantined')buttons=b('🔧 Repair / Restore','blue','restore',t.id);
 if(t.status==='charging')buttons=b('🔋 Finish charging','primary','recharge',t.id);
 return `<div class="jtractor"><div class="jrow"><div><div class="jname">🚜 ${esc(t.name)} <span class="status ${statClass(t.status)}">${esc(statName(t.status))}</span></div><div class="jdetail">${esc(t.job||'No active job')} · ${fmt(t.battery)}% battery · ${esc(t.implements.join(', '))}</div></div></div><div class="actions">${buttons}</div></div>`}).join('');
}
async function update(){try{show(await readState())}catch(e){j$('#phoneState').textContent='Disconnected from fleet. Check Wi-Fi.'}}
async function run(op,params={}){try{const x=await sendAction(op,params);toast(x.message);show(x.state)}catch(e){toast(e.message,true)}}
j$('#judgeTractors').addEventListener('click',e=>{const target=e.target.closest('button[data-op]');if(!target)return;const op=target.dataset.op;run(op,{tractor:target.dataset.tid})});
j$('#start').addEventListener('click',()=>run('start'));
j$('#pause').addEventListener('click',()=>run('pause'));
j$('#reset').addEventListener('click',()=>{if(confirm('Reset the entire simulated farm?'))run('reset')});
j$('#addJob').addEventListener('submit',e=>{e.preventDefault();const form=new FormData(e.target);const payload=Object.fromEntries(form.entries());run('create_job',payload)});
update();setInterval(update,1200);
