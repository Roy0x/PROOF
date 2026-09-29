const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const escapeHtml = (v='') => String(v).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));

const viewTitles = {
  live: 'Independent outcome verification',
  dashboard: 'Reliability, measured against reality',
  packs: 'Evidence for every completed task',
  architecture: 'How PROOF separates action from verification'
};

$$('.nav').forEach(btn => btn.addEventListener('click', async () => {
  $$('.nav').forEach(x => x.classList.remove('active'));
  btn.classList.add('active');
  const target = btn.dataset.view;
  $$('.view').forEach(v => v.classList.remove('active'));
  $(`#view-${target}`).classList.add('active');
  $('#view-title').textContent = viewTitles[target];
  if (target === 'packs' || target === 'dashboard') await loadPacks();
}));

async function loadHealth(){
  try{
    const r = await fetch('/api/health'); const d = await r.json();
    $('#provider-dot').style.background = d.nebius_configured ? 'var(--ok)' : 'var(--warn)';
    $('#provider-title').textContent = d.nebius_configured ? 'Nebius connected' : 'Local fallback active';
    $('#provider-subtitle').textContent = d.nebius_configured ? d.model : 'Add NEBIUS_API_KEY for Nemotron';
  }catch(e){ $('#provider-title').textContent='Backend unavailable'; }
}

function timeline(step){
  const steps = [
    ['Worker claim','Agent says done'],
    ['Contract','Freeze success'],
    ['Verify','Ask environment'],
    ['Repair','Return evidence'],
    ['Reverify','Prove outcome']
  ];
  $('#timeline').innerHTML = steps.map((x,i)=>`<div class="timeline-step ${i < step ? 'done' : ''} ${i===step ? 'active':''}"><strong>${x[0]}</strong><small>${x[1]}</small></div>`).join('');
}

function renderContract(contract){
  $('#contract-block').innerHTML = `
    <div class="detail-title"><h4>Outcome Contract</h4><span class="tag">${escapeHtml(contract.generated_by)}</span></div>
    <div class="conditions">${contract.conditions.map((c,i)=>`<div class="condition"><span class="num">0${i+1}</span><div><strong>${escapeHtml(c.name)}</strong><small>${escapeHtml(c.description)}</small></div><span class="tag">${escapeHtml(c.method)}</span></div>`).join('')}</div>`;
  $('#contract-block').classList.remove('hidden');
}

function resultIcon(v){ return v==='PASS'?'✓':v==='FAIL'?'×':'!'; }
function resultClass(v){ return v==='PASS'?'pass':v==='FAIL'?'fail':'blocked'; }
function renderReport(report, title){
  return `<div class="detail-title"><h4>${escapeHtml(title)}</h4><span class="verdict ${report.verdict==='VERIFIED'?'verified':'failed'}">${escapeHtml(report.verdict)}</span></div>
    <div>${report.evidence.map(e=>`<div class="result-row"><span class="result-icon ${resultClass(e.verdict)}">${resultIcon(e.verdict)}</span><div><strong>${escapeHtml(e.condition_name)}</strong><small>${escapeHtml(e.method)}</small></div><span class="status">${escapeHtml(e.verdict)}</span><div class="observed"><b>Expected:</b> ${escapeHtml(e.expected)}<br><b>Observed:</b> ${escapeHtml(e.observed)}</div></div>`).join('')}</div>`;
}

async function runDemo(){
  const btn = $('#run-demo'); btn.disabled = true;
  $('#run-stage').classList.remove('hidden');
  ['#contract-block','#verification-block','#repair-block','#final-block'].forEach(id=>$(id).classList.add('hidden'));
  $('#run-status').className='verdict running'; $('#run-status').textContent='RUNNING';
  $('#run-title').textContent='Worker agent is executing the task'; timeline(0);
  window.scrollTo({top: $('#run-stage').offsetTop-24, behavior:'smooth'});

  await sleep(650);
  $('#run-title').textContent='Worker claims: “Login fixed and deployment successful.”';
  timeline(1);

  try{
    const response = await fetch('/api/demo/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal:$('#goal').value.trim() || 'Fix the login bug and deploy the application.'})});
    if(!response.ok) throw new Error(await response.text());
    const data = await response.json();

    await sleep(450); renderContract(data.contract); timeline(2); $('#run-title').textContent='PROOF is checking the actual deployed state';
    await sleep(650);
    $('#verification-block').innerHTML = renderReport(data.first_verification,'First verification — worker claim rejected');
    $('#verification-block').classList.remove('hidden');

    if(data.repair){
      timeline(3); $('#run-title').textContent='Evidence returned to worker for repair';
      $('#repair-block').innerHTML = `<div class="detail-title"><h4>Repair cycle</h4><span class="tag">EVIDENCE → WORKER</span></div><div class="repair-card"><strong>${escapeHtml(data.repair.diagnosis)}</strong><p>${escapeHtml(data.repair.worker_action)}</p></div>`;
      $('#repair-block').classList.remove('hidden');
      await sleep(700);
    }

    timeline(4); $('#run-title').textContent='Replaying the same frozen Outcome Contract';
    $('#final-block').innerHTML = renderReport(data.final_verification,'Reverification') + `<div class="final-card"><div><h3>OUTCOME ${escapeHtml(data.final_verification.verdict)}</h3><p>The worker's result now matches the independently observed environment.</p></div><span class="proof-id">PROOF #${data.proof_id}</span></div>`;
    $('#final-block').classList.remove('hidden');
    $('#run-status').className=`verdict ${data.final_verification.verdict==='VERIFIED'?'verified':'failed'}`;
    $('#run-status').textContent=data.final_verification.verdict;
    $('#run-title').textContent=data.final_verification.verdict==='VERIFIED'?'Independent verification complete':'Outcome still not verified';
    await loadPacks();
  }catch(err){
    $('#run-title').textContent='Run failed'; $('#run-status').className='verdict failed'; $('#run-status').textContent='ERROR';
    $('#final-block').innerHTML=`<div class="repair-card"><strong>Backend error</strong><p>${escapeHtml(err.message)}</p></div>`; $('#final-block').classList.remove('hidden');
  }finally{ btn.disabled=false; }
}

async function loadPacks(){
  try{
    const r=await fetch('/api/proof-packs'); const packs=await r.json();
    $('#stat-packs').textContent=packs.length;
    $('#stat-caught').textContent=packs.filter(p=>p.first_verdict!=='VERIFIED').length;
    $('#stat-verified').textContent=packs.filter(p=>p.final_verdict==='VERIFIED'&&p.repair_cycles>0).length;
    $('#packs-list').innerHTML = packs.length ? packs.map(p=>`<div class="pack"><span class="pack-id">#${p.id}</span><div><strong>${escapeHtml(p.goal)}</strong><small>${escapeHtml(p.created_at)}</small></div><span class="repair-count">${p.repair_cycles} repair cycle${p.repair_cycles===1?'':'s'}</span><span class="verdict ${p.final_verdict==='VERIFIED'?'verified':'failed'}">${escapeHtml(p.final_verdict)}</span></div>`).join('') : '<div class="empty">No verification runs yet. Run the live demo first.</div>';
  }catch(e){}
}

$('#run-demo').addEventListener('click',runDemo);
$('#refresh-packs').addEventListener('click',loadPacks);
loadHealth(); loadPacks(); timeline(-1);
