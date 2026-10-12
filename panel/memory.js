// Memory Center: owner-local UI over /v0/memory/*. Sensitive containers are blurred until "Show sensitive" is pressed.
let token = new URLSearchParams(location.search).get("access_token") || "";
try { if (token) localStorage.setItem("da_token", token); else token = localStorage.getItem("da_token") || ""; } catch (e) {}
const $ = id => document.getElementById(id);
const H = () => ({ "Authorization": "Bearer " + token, "Content-Type": "application/json", "X-Source": "panel" });
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const when = ts => ts ? new Date(ts * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : "—";
const SENSITIVE = {health_and_wellbeing: 1, finances: 1};
let status = null, container = "identity_and_biography", showSensitive = false;
$("back").href = "/?access_token=" + encodeURIComponent(token);

async function api(path, opts = {}) {
  const r = await fetch(path, Object.assign({headers: H()}, opts));
  if (r.status === 401) { $("conn").textContent = "token rejected"; $("conn").className = "chip bad"; throw new Error("401"); }
  const j = await r.json();
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}

async function loadStatus() {
  status = await api("/v0/memory/status");
  $("conn").textContent = status.enabled ? "live" : "disabled"; $("conn").className = "chip " + (status.enabled ? "ok" : "bad");
  if (!status.enabled) { $("st_loaded").textContent = status.reason || "disabled"; $("st_loaded").className = "chip bad"; return; }
  const r = status.records;
  $("st_loaded").textContent = status.loaded ? `loaded · ${r.active} active · ${r.pending} pending · ${r.superseded} superseded · ${r.expired} expired` : "not loaded (no active facts yet)";
  $("st_loaded").className = "chip " + (status.loaded ? "ok" : "warn");
  $("st_rev").textContent = `revision ${status.revision.revision} · sha ${status.revision.sha} · schema v${status.revision.schema_version} · ${status.backups} backups`;
  const s = status.seed || {};
  $("st_seed").textContent = s.loaded ? `Seed file: loaded · ${s.bytes} bytes · modified ${when(s.mtime)} · sha256 ${s.sha256} · last import ${when(status.last_import_at)}` : `Seed file: not loaded (${s.reason || "none imported"})`;
  const lt = status.last_turn ? ` · last turn: ${status.last_turn.committed} saved, ${status.last_turn.pending} pending, ${status.last_turn.superseded} corrected` : "";
  $("st_health").textContent = `Last retrieval ${when(status.last_retrieval_at)} · last capture ${when(status.last_capture_at)}${lt}`;
  const d = status.dot_aria || {};
  $("st_dot").textContent = `Dot Aria: ${d.synced ? "synced" : "separate store, not synced"} · transport: ${d.transport} · last reconciled bundle ${d.last_reconciled_bundle || "none"} ${d.last_reconciled_at ? when(d.last_reconciled_at) : ""}`;
  drawTabs();
}

function drawTabs() {
  const c = status.containers || {};
  $("tabs").innerHTML = Object.keys(c).map(k => {
    const n = c[k]; const pend = n.pending ? ` <b>+${n.pending} pending</b>` : "";
    return `<span class="chip${k === container ? " sel" : ""}${SENSITIVE[k] ? " warn" : ""}" data-c="${k}" role="button" tabindex="0">${esc(k.replace(/_/g, " "))} ${n.active}${pend}</span>`;
  }).join(" ");
  for (const el of $("tabs").querySelectorAll("[data-c]")) el.onclick = () => { container = el.dataset.c; drawTabs(); loadList(); };
  $("capture_on").checked = !!(status.capture || {})[container];
  $("add_container").innerHTML = Object.keys(c).map(k => `<option value="${k}"${k === container ? " selected" : ""}>${esc(k.replace(/_/g, " "))}</option>`).join("");
}

async function loadList() {
  const st = $("status").value, q = $("q").value.trim();
  const url = `/v0/memory/records?container=${encodeURIComponent(container)}${st ? "&status=" + st : ""}${q ? "&q=" + encodeURIComponent(q) : ""}&limit=200`;
  let recs; try { recs = (await api(url)).records; } catch (e) { $("list").innerHTML = `<div class="chip bad">${esc(String(e))}</div>`; return; }
  if (!recs.length) { $("list").innerHTML = `<div class="muted tiny">No ${esc(st || "")} facts in this container${q ? " for that search" : ""}.</div>`; return; }
  $("list").innerHTML = recs.map(r => {
    const sens = r.sensitivity === "high" ? " sensitive" + (showSensitive ? " show" : "") : "";
    const acts = r.status === "pending" ? `<button class="tiny" data-a="approve">approve</button> <button class="tiny" data-a="reject">reject</button>`
      : r.status === "active" ? `<button class="tiny" data-a="correct">correct</button> <button class="tiny" data-a="edit">edit</button> <button class="tiny" data-a="delete">delete</button>`
      : `<button class="tiny" data-a="restore">restore</button>`;
    return `<div class="rec${sens}" data-id="${r.id}"><div class="t">${r.key ? `<b>${esc(r.key)}:</b> ` : ""}${esc(r.text)}</div>
      <div class="tiny muted">${esc(r.subject)} · ${esc(r.created_by)} · ${esc(r.source_type)} ${esc(r.source_ref)} · first ${when(r.first_seen_at)} · confirmed ${when(r.last_confirmed_at)}${r.valid_until ? " · expires " + when(r.valid_until) : ""} · conf ${Number(r.confidence).toFixed(2)} · ${esc(r.sensitivity)} · rev ${r.revision} · <span class="chip">${esc(r.status)}</span>${r.supersedes ? " · supersedes " + esc(r.supersedes) : ""}${r.superseded_by ? " · superseded by " + esc(r.superseded_by) : ""} · ${(r.tags || []).map(esc).join(", ")}</div>
      <div class="row" style="margin-top:4px">${acts} <button class="tiny" data-a="why">why?</button></div></div>`;
  }).join("");
  for (const b of $("list").querySelectorAll("button[data-a]")) b.onclick = () => act(b.closest(".rec").dataset.id, b.dataset.a, b.closest(".rec"));
}

async function act(id, a, el) {
  try {
    if (a === "why") { const d = await api(`/v0/memory/records/${id}`); $("whycard").hidden = false;
      $("why").textContent = `Record ${id} · revision ${d.record.revision} · created by ${d.record.created_by} from ${d.record.source_type} ${d.record.source_ref} at ${when(d.record.provenance.created_at)} · text hash ${d.record.provenance.text_sha256}\n` +
        (d.supersedes ? `Supersedes: "${d.supersedes.text}" (${d.supersedes.id})\n` : "") + (d.superseded_by ? `Superseded by: ${typeof d.superseded_by === "string" ? d.superseded_by : d.superseded_by.id}\n` : "") +
        `\nHistory:\n` + d.history.map(h => `  ${when(h.ts)} ${h.action} by ${h.actor} ${JSON.stringify(h.detail)}`).join("\n") +
        `\n\nUsed by Aria (most recent first):\n` + (d.uses.length ? d.uses.map(u => `  ${when(u.ts)} ${u.action} ${JSON.stringify(u.detail)}`).join("\n") : "  never retrieved yet");
      $("whycard").scrollIntoView({behavior: "smooth"}); return; }
    if (a === "delete") { await api(`/v0/memory/records/${id}`, {method: "DELETE"}); }
    else if (a === "restore" || a === "approve" || a === "reject") { await api(`/v0/memory/records/${id}/${a}`, {method: "POST"}); }
    else if (a === "edit" || a === "correct") {
      const cur = el.querySelector(".t").textContent.replace(/^[^:]+:\s/, "");
      const text = window.prompt(a === "correct" ? "Corrected fact (the old one is kept as history):" : "Edit the text in place:", cur);
      if (text == null || !text.trim()) return;
      await api(`/v0/memory/records/${id}`, {method: "PATCH", body: JSON.stringify({text: text.trim(), correct: a === "correct"})});
    }
    await loadStatus(); await loadList();
  } catch (e) { $("op_state").textContent = "Failed: " + e; }
}

$("status").onchange = loadList; $("q").oninput = () => { clearTimeout(window.__qt); window.__qt = setTimeout(loadList, 250); };
$("btn_show").onclick = () => { showSensitive = !showSensitive; $("btn_show").textContent = showSensitive ? "Hide sensitive" : "Show sensitive"; loadList(); };
$("capture_on").onchange = async () => { try { await api("/v0/memory/capture", {method: "POST", body: JSON.stringify({container, on: $("capture_on").checked})}); await loadStatus(); } catch (e) { $("op_state").textContent = "Failed: " + e; } };
$("why_close").onclick = () => { $("whycard").hidden = true; };
$("btn_add").onclick = () => { $("addcard").hidden = false; $("add_text").focus(); };
$("add_cancel").onclick = () => { $("addcard").hidden = true; };
$("add_save").onclick = async () => {
  try { await api("/v0/memory/records", {method: "POST", body: JSON.stringify({container: $("add_container").value, subject: $("add_subject").value, key: $("add_key").value || null, text: $("add_text").value})});
    $("add_text").value = ""; $("addcard").hidden = true; container = $("add_container").value; await loadStatus(); await loadList(); } catch (e) { $("add_state").textContent = "Failed: " + e; }
};
$("btn_backup").onclick = async () => { try { const b = await api("/v0/memory/backup", {method: "POST"}); $("op_state").textContent = `Backup ${b.name} (${b.bytes} bytes)`; await loadStatus(); } catch (e) { $("op_state").textContent = "Failed: " + e; } };
$("btn_export").onclick = async () => {
  try { const b = await api("/v0/memory/export"); const blob = new Blob([JSON.stringify(b)], {type: "application/json"}); const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `aria-memory-bundle-r${b.revision.revision}-${b.revision.sha}.json`; a.click(); $("op_state").textContent = `Exported bundle ${b.bundle_id} (${b.records.length} records) to your Downloads folder — keep it private.`; }
  catch (e) { $("op_state").textContent = "Failed: " + e; }
};
$("bundle_file").onchange = async () => {
  const f = $("bundle_file").files[0]; if (!f) return;
  try { const r = await api("/v0/memory/import-bundle", {method: "POST", body: JSON.stringify({bundle: JSON.parse(await f.text())})});
    $("op_state").textContent = `Bundle merged: ${r.created} new, ${r.updated} updated, ${r.unchanged} unchanged.`; await loadStatus(); await loadList(); } catch (e) { $("op_state").textContent = "Failed: " + e; }
};
$("seed_file").onchange = () => { $("btn_import").disabled = !$("seed_file").files.length; };
$("btn_import").onclick = async () => {
  const f = $("seed_file").files[0]; if (!f) return;
  $("import_state").textContent = "importing…"; $("btn_import").disabled = true;
  try { const r = await api("/v0/memory/import", {method: "POST", body: JSON.stringify({markdown: await f.text()})});
    $("import_state").textContent = `Imported sha ${r.seed_sha256}: ${r.chunks} facts read, ${r.created} new, ${r.confirmed} unchanged, ${r.retired} retired.`; await loadStatus(); await loadList(); }
  catch (e) { $("import_state").textContent = "Refused: " + e; }
  $("btn_import").disabled = false;
};
loadStatus().then(loadList).catch(e => { $("conn").textContent = "unavailable: " + e; $("conn").className = "chip bad"; });
