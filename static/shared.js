'use strict';
const API='/api/state';
const fmt=(n,d=0)=>Number(n).toFixed(d);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const progress=(n,d)=>Math.max(0,Math.min(100,100*n/Math.max(1,d)));
const statClass=s=>['failed','offline','quarantined','charging'].includes(s)?s:'';
const statName=s=>({working:'WORKING',idle:'READY',failed:'FAILED',offline:'SIGNAL LOST',quarantined:'SAFE STOP',charging:'CHARGING'})[s]||s;
async function readState(){const r=await fetch(API,{cache:'no-store'});if(!r.ok)throw new Error('Server unavailable');return r.json()}
function token(){return new URLSearchParams(location.search).get('t')||window.FLEET_TOKEN||''}
async function sendAction(action,rest={}){
 const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Fleet-Token':token()},body:JSON.stringify({action,...rest})});
 const x=await r.json();if(!r.ok)throw new Error(x.error||'Action failed');return x;
}
function toast(message,isError=false){let el=document.querySelector('#notice');if(!el)return;el.textContent=message;el.className='notice show'+(isError?' error':'');setTimeout(()=>{if(el.textContent===message)el.className='notice'},4500)}
function jobIndex(jobs){return Object.fromEntries(jobs.map(j=>[j.id,j]))}
