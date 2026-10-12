"use strict";

const el = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[char]));
const shortId = (value, size = 14) => value ? `${String(value).slice(0, size)}…` : "—";
const displayValue = (value) => value === null || value === undefined ? "—" :
  typeof value === "string" ? value : JSON.stringify(value);

const timelineStages = [
  { title: "Worker execution", done: "worker_completed", running: "worker_running" },
  { title: "Contract frozen", done: "contract_frozen" },
  { title: "Plan validated", done: "plan_validated" },
  { title: "Independent check", done: "first_pack_saved", running: "verification_running" },
  { title: "Failed evidence", done: "failed_evidence_confirmed" },
  { title: "Authorized repair", done: "repair_applied", running: "repair_running" },
  { title: "Reverification", done: "second_pack_saved", running: "reverification_running" }
];

let current = null;
let firstPack = null;
let secondPack = null;
let selectedRun = 1;
let pollTimer = null;
let refreshing = false;

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (!response.ok) {
    const error = new Error(`Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function setStatus(status) {
  const chip = el("run-status");
  chip.className = `status-chip ${status === "complete" ? "complete" : status === "error" || status === "timeout" ? "error" :
    status === "running" || status === "queued" ? "running" : "idle"}`;
  chip.textContent = ({ complete: secondPack?.verdict || "COMPLETE", error: "RUN STOPPED", timeout: "TIMED OUT", running: "RUNNING", queued: "QUEUED" })[status] || "NOT STARTED";
  el("run-button").disabled = !!current?.worker_active;
  el("run-button").innerHTML = current?.worker_active ?
    '<span class="button-icon">◌</span> Verification in progress' :
    '<span class="button-icon">▶</span> Run verification';
}

function renderTimeline(job) {
  const events = new Map((job?.events || []).map((event) => [event.stage, event]));
  el("timeline-list").innerHTML = timelineStages.map((stage, index) => {
    const done = events.get(stage.done);
    const running = stage.running && events.get(stage.running);
    let state = done ? "done" : running ? "running" : "pending";
    if (stage.done === "first_pack_saved" && done?.verdict === "FAILED") state = "detected";
    const caption = done ? (done.verdict || (state === "detected" ? "FAILED" : "COMPLETE")) :
      running ? "RUNNING" : "AWAITING";
    return `<div class="timeline-item ${state}"><span class="timeline-number">0${index + 1} / 07</span>` +
      `<strong>${escapeHtml(stage.title)}</strong><small>${escapeHtml(caption)}</small></div>`;
  }).join("");
}

function renderOverview() {
  setStatus(current?.status);
  renderTimeline(current);
  const complete = current?.status === "complete" && firstPack && secondPack;
  el("hero").classList.toggle("compact", !!complete);
  const repair = secondPack?.repair;
  const steps = [
    [current?.worker_claim === "COMPLETED" ? "CLAIMED" : current?.worker_claim, current?.worker_claim],
    [firstPack?.verdict, firstPack?.verdict],
    [repair?.outcome === "applied" ? "REPAIRED" : repair?.outcome, repair?.outcome],
    [secondPack?.verdict, secondPack?.verdict]
  ];
  el("outcome-summary").classList.toggle("hidden", !complete);
  el("outcome-summary").innerHTML = complete ? steps.map(([label, observed], index) =>
    `<div class="outcome-step"><span>0${index + 1}</span><strong>${escapeHtml(label || "UNAVAILABLE")}</strong>` +
    `<small>${escapeHtml(observed || "UNAVAILABLE")}</small></div>`).join("") : "";
  el("current-run-id").textContent = current?.job_id ? shortId(current.job_id) : "—";
  el("execution-mode").textContent = `Offline demonstration · ${current?.execution_mode || "offline_mock"}`;
  el("worker-model").textContent = current?.worker_model || "—";
  el("contract-id").textContent = current?.contract_id ? shortId(current.contract_id, 18) : "—";
  el("plan-id").textContent = current?.plan_fingerprint ? shortId(current.plan_fingerprint, 20) : "—";
  el("worker-claim").textContent = current?.worker_claim || "AWAITING RUN";
  el("first-verdict").textContent = firstPack?.verdict || "AWAITING EVIDENCE";
  el("first-verdict-note").textContent = firstPack?.verdict === "FAILED" ?
    "Independent checks recorded a failed production run despite the worker claim." :
    "The verifier has not yet produced evidence.";
  el("first-verdict").parentElement.classList.toggle("awaiting", !firstPack);
  const schema = schemaEvidence(firstPack);
  const authId = firstPack?.contract?.conditions?.find((item) => item.acceptance_ref === "auth")?.id;
  const dashboardId = firstPack?.contract?.conditions?.find((item) => item.acceptance_ref === "dashboard")?.id;
  const auth = firstPack?.condition_results?.find((item) => item.condition_id === authId);
  const dashboard = firstPack?.condition_results?.find((item) => item.condition_id === dashboardId);
  const observations = [];
  if (schema?.status === "FAILED" && schema.expected === true && schema.observed === false)
    observations.push("Production auth_sessions schema check returned false.");
  if (auth?.status === "FAILED" && ["FAILED", "BLOCKED"].includes(dashboard?.status))
    observations.push("Authentication did not reach the expected dashboard.");
  el("failure-explanation").classList.toggle("hidden", !observations.length);
  el("failure-explanation").innerHTML = observations.length ?
    `<strong>RUN 1 · OBSERVED</strong><p>${observations.map(escapeHtml).join(" ")}</p>` : "";
  const error = el("run-error");
  if (current?.status === "error" || current?.status === "timeout") {
    error.classList.remove("hidden");
    error.textContent = `The offline run stopped (${current.error_code || "run_failed"}). ` +
      "No successful verdict was inferred. Any already-persisted Proof Pack remains available below.";
  } else {
    error.classList.add("hidden");
    error.textContent = "";
  }
}

function evidenceRow(item) {
  const detail = item.error_category ? `<div class="failure-note">${escapeHtml(item.error_category)}</div>` : "";
  const metadata = Object.entries(item.metadata || {}).map(([key, value]) =>
    `<span class="evidence-metadata">${escapeHtml(key.replaceAll("_", " "))}: ${escapeHtml(displayValue(value))}</span>`).join("");
  return `<div class="evidence-item"><div class="evidence-op">${escapeHtml(item.verifier)} / ${escapeHtml(item.operation)}` +
    `<div class="evidence-id"><span>ID ${escapeHtml(shortId(item.evidence_id, 12))}</span>` +
    `<button class="copy-id" type="button" data-copy-id="${escapeHtml(item.evidence_id)}" ` +
    `aria-label="Copy complete evidence ID ${escapeHtml(item.evidence_id)}">Copy ID</button></div>${metadata}</div>` +
    `<div><span class="value-label">EXPECTED</span><span class="value-text">${escapeHtml(displayValue(item.expected))}</span></div>` +
    `<div><span class="value-label">OBSERVED · ${escapeHtml(item.status)}</span><span class="value-text">${escapeHtml(displayValue(item.observed))}</span></div>${detail}</div>`;
}

function schemaEvidence(pack) {
  const schema = pack?.contract?.conditions?.find((item) => item.acceptance_ref === "schema");
  return schema ? pack.evidence.find((item) => item.condition_id === schema.id && item.operation === "json_value") : null;
}

function renderEvidence() {
  const pack = selectedRun === 2 ? secondPack : firstPack;
  el("show-first").classList.toggle("active", selectedRun === 1);
  el("show-second").classList.toggle("active", selectedRun === 2);
  el("show-first").disabled = !firstPack;
  el("show-second").disabled = !secondPack;
  el("evidence-empty").classList.toggle("hidden", !!pack);
  el("evidence-content").classList.toggle("hidden", !pack);
  if (!pack) return;
  el("evidence-run-label").textContent = selectedRun === 1 ? "RUN 1 · BEFORE REPAIR" : "RUN 2 · AFTER REPAIR";
  el("evidence-verdict").textContent = pack.verdict;
  el("evidence-verdict").classList.toggle("failed", pack.verdict === "FAILED");
  el("evidence-run-id").textContent = shortId(pack.run_id, 20);
  const descriptions = new Map(pack.contract.conditions.map((item) => [item.id, item]));
  el("conditions-list").innerHTML = pack.condition_results.map((result) => {
    const contractCondition = descriptions.get(result.condition_id) || {};
    const evidence = pack.evidence.filter((item) => result.evidence_ids.includes(item.evidence_id));
    return `<details class="condition-card" ${result.status === "FAILED" ? "open" : ""}>` +
      `<summary><span class="condition-ref">${escapeHtml(result.condition_id)}</span>` +
      `<span class="condition-description">${escapeHtml(result.description)}` +
      `<small>${escapeHtml(contractCondition.acceptance_ref || "outcome")}</small></span>` +
      `<span class="state-tag ${escapeHtml(result.status.toLowerCase())}">${escapeHtml(result.status)}</span></summary>` +
      `<div class="condition-details">${evidence.map(evidenceRow).join("") || '<p class="muted">No evidence recorded.</p>'}</div></details>`;
  }).join("");
  const before = schemaEvidence(firstPack);
  const after = schemaEvidence(secondPack);
  const comparison = el("schema-compare");
  comparison.classList.toggle("hidden", !(before && after));
  if (before && after) {
    comparison.innerHTML = `<h3>Production schema · same check, fresh evidence</h3><div class="compare-grid">` +
      `<div class="compare-cell before"><span>RUN 1 · ${escapeHtml(shortId(before.evidence_id, 12))}</span><strong>Observed ${escapeHtml(displayValue(before.observed))}</strong></div>` +
      `<div class="compare-arrow">→</div>` +
      `<div class="compare-cell after"><span>RUN 2 · ${escapeHtml(shortId(after.evidence_id, 12))}</span><strong>Observed ${escapeHtml(displayValue(after.observed))}</strong></div></div>`;
  }
}

function renderRepair() {
  const before = schemaEvidence(firstPack);
  const repair = secondPack?.repair;
  if (!before) {
    el("repair-content").innerHTML = '<p class="muted">No authoritative failed schema observation has been recorded.</p>';
  } else {
    const accepted = (current?.events || []).some((event) => event.stage === "failed_evidence_confirmed");
    const applied = (current?.events || []).find((event) => event.stage === "repair_applied");
    el("repair-content").innerHTML = `<div class="repair-heading">${accepted ? "FAILED EVIDENCE CONFIRMED" : "FAILED OBSERVATION"}</div>` +
      `<div class="repair-observation">auth_sessions_exists<br>expected <code>${escapeHtml(displayValue(before.expected))}</code>` +
      ` · observed <code>${escapeHtml(displayValue(before.observed))}</code><br>` +
      `evidence ${escapeHtml(before.evidence_id)}</div>` +
      `<div class="repair-row"><span>AI-selected migration</span><strong>${escapeHtml(repair?.migration_ref || applied?.migration_ref || "Awaiting decision")}</strong></div>` +
      `<div class="repair-row"><span>Trusted execution</span><strong>${escapeHtml(repair?.outcome || "Awaiting execution")}</strong></div>`;
  }
  if (!secondPack) {
    el("outcome-content").innerHTML = '<p class="muted">The second independent verification has not produced a Proof Pack.</p>';
    return;
  }
  const sameContract = firstPack && firstPack.contract_id === secondPack.contract_id &&
    firstPack.contract_version === secondPack.contract_version &&
    firstPack.plan_fingerprint === secondPack.plan_fingerprint;
  el("outcome-content").innerHTML = `<div class="outcome-big">${escapeHtml(secondPack.verdict)}</div>` +
    `<p>Fresh HTTP, Browser, and Shell evidence was collected after the authorized repair.</p>` +
    `<div class="repair-row"><span>Frozen contract / plan</span><strong>${sameContract ? "UNCHANGED" : "MISMATCH"}</strong></div>` +
    `<div class="fingerprint">${escapeHtml(secondPack.plan_fingerprint)}</div>`;
}

function renderPacks() {
  const packs = [firstPack, secondPack].filter(Boolean);
  if (!packs.length) {
    el("packs-list").innerHTML = '<div class="empty-state panel"><div class="empty-symbol">▣</div><strong>No Proof Packs yet</strong><p>Each independent verification run creates its own artifact.</p></div>';
    return;
  }
  el("packs-list").innerHTML = packs.map((pack, index) => {
    const label = index === 0 ? "FIRST VERIFICATION" : "REVERIFICATION";
    const href = `/api/workspace/runs/${encodeURIComponent(current.job_id)}/packs/${encodeURIComponent(pack.run_id)}/export`;
    return `<article class="panel pack-card"><div class="pack-top"><span>${label}</span>` +
      `<span class="state-tag ${escapeHtml(pack.verdict.toLowerCase())}">${escapeHtml(pack.verdict)}</span></div>` +
      `<h3>Proof Pack ${index + 1}</h3><p>Persisted independent evidence · redacted browser view</p>` +
      `<code>${escapeHtml(pack.run_id)}</code><div class="pack-actions">` +
      `<button class="button button-ghost" type="button" data-inspect="${escapeHtml(pack.run_id)}">Inspect JSON</button>` +
      `<a class="button button-ghost" href="${href}" download>Download redacted JSON</a></div></article>`;
  }).join("");
  el("packs-list").querySelectorAll("[data-inspect]").forEach((button) => button.addEventListener("click", () => {
    const pack = packs.find((item) => item.run_id === button.dataset.inspect);
    if (!pack) return;
    el("inspector-title").textContent = `Proof Pack · ${shortId(pack.run_id, 20)}`;
    el("pack-json").textContent = JSON.stringify(pack, null, 2);
    el("pack-inspector").classList.remove("hidden");
    el("pack-inspector").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }));
}

function renderAll() {
  renderOverview();
  renderEvidence();
  renderRepair();
  renderPacks();
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const snapshot = current ? await api(`/api/workspace/runs/${encodeURIComponent(current.job_id)}`) :
      await api("/api/workspace/runs/current");
    if (snapshot && (!current || current.job_id !== snapshot.job_id)) {
      firstPack = null; secondPack = null; selectedRun = 1;
    }
    current = snapshot;
    if (current?.pack_ids?.[0] && firstPack?.run_id !== current.pack_ids[0]) {
      firstPack = await api(`/api/workspace/runs/${encodeURIComponent(current.job_id)}/packs/${encodeURIComponent(current.pack_ids[0])}`);
    }
    if (current?.pack_ids?.[1] && secondPack?.run_id !== current.pack_ids[1]) {
      secondPack = await api(`/api/workspace/runs/${encodeURIComponent(current.job_id)}/packs/${encodeURIComponent(current.pack_ids[1])}`);
    }
    renderAll();
    if (!current || !current.worker_active) stopPolling();
    else startPolling();
  } catch (_) {
    stopPolling();
    el("run-error").classList.remove("hidden");
    el("run-error").textContent = "The workspace could not retrieve current evidence. Refresh the page to retry.";
  } finally {
    refreshing = false;
  }
}

function startPolling() {
  if (!pollTimer) pollTimer = window.setInterval(refresh, 1000);
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = null;
}

async function startRun() {
  el("run-button").disabled = true;
  try {
    const snapshot = await api("/api/workspace/runs", {
      method: "POST", headers: { "X-PROOF-Workspace": "offline" }
    });
    current = snapshot; firstPack = null; secondPack = null; selectedRun = 1;
    el("pack-inspector").classList.add("hidden");
    renderAll();
    startPolling();
    document.getElementById("overview").scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    if (error.status === 409) {
      current = null;
      await refresh();
    } else {
      el("run-error").classList.remove("hidden");
      el("run-error").textContent = "Could not start the offline verification run. No verdict was issued.";
    }
  } finally {
    if (!current || !current.worker_active) el("run-button").disabled = false;
  }
}

el("run-button").addEventListener("click", startRun);
el("show-first").addEventListener("click", () => { selectedRun = 1; renderEvidence(); });
el("show-second").addEventListener("click", () => { selectedRun = 2; renderEvidence(); });
el("close-inspector").addEventListener("click", () => el("pack-inspector").classList.add("hidden"));
el("conditions-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-id]");
  if (!button) return;
  try {
    await navigator.clipboard.writeText(button.dataset.copyId);
    el("copy-feedback").textContent = "Complete evidence ID copied.";
    button.textContent = "Copied";
    window.setTimeout(() => { button.textContent = "Copy ID"; }, 1800);
  } catch (_) {
    el("copy-feedback").textContent = "Could not copy the evidence ID. Inspect the Proof Pack for the complete value.";
  }
});
document.querySelectorAll(".rail-link").forEach((link) => link.addEventListener("click", () => {
  document.querySelectorAll(".rail-link").forEach((item) => item.classList.remove("active"));
  link.classList.add("active");
}));
renderAll();
refresh();
