let state = null;

const el = id => document.getElementById(id);
const splitKeywords = value => value.split(/[,\n]/).map(x => x.trim()).filter(Boolean);
const formatTime = value => value ? new Date(value).toLocaleString("ko-KR") : "-";
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));

function renderSourceStatus(node, source) {
  const status = state.statuses[source.id];
  const statusNode = node.querySelector(".source-status");
  statusNode.classList.toggle("error", Boolean(status && !status.ok));
  if (status) {
    statusNode.textContent = `${status.ok ? "정상" : "오류"} · ${status.message} · ${formatTime(status.checked_at)}`;
  } else {
    statusNode.textContent = "아직 확인 전입니다.";
  }
}

function sourceCard(source) {
  const node = el("source-template").content.firstElementChild.cloneNode(true);
  node.dataset.id = source.id;
  node.querySelector(".source-enabled").checked = source.enabled;
  node.querySelector(".source-name").value = source.name;
  node.querySelector(".source-type").value = source.type;
  node.querySelector(".source-url").value = source.url;
  node.querySelector(".source-keywords").value = source.keywords.join("\n");
  renderSourceStatus(node, source);
  node.querySelector(".remove").onclick = () => node.remove();
  return node;
}

function renderSources() {
  const container = el("sources");
  container.innerHTML = "";
  state.sources.forEach(source => container.appendChild(sourceCard(source)));
  el("interval").value = state.interval_seconds;
}

function renderSourceStatuses() {
  state.sources.forEach(source => {
    const node = [...document.querySelectorAll(".source-card")]
      .find(card => card.dataset.id === source.id);
    if (node) renderSourceStatus(node, source);
  });
}

function renderItems() {
  const query = el("filter").value.trim().toLowerCase();
  const items = state.items.filter(item =>
    !query || `${item.title} ${item.source} ${(item.matched_keywords || []).join(" ")}`.toLowerCase().includes(query)
  );
  el("items").innerHTML = items.length ? items.map(item => `
    <article class="item">
      <a href="${escapeHtml(item.link)}" target="_blank" rel="noopener">${escapeHtml(item.title)}</a>
      <div class="meta">
        <span>${escapeHtml(item.source)}</span>
        <span>발견 ${escapeHtml(formatTime(item.found_at))}</span>
        ${item.live_status ? `<span class="tag live">${escapeHtml(item.live_status)} 라이브</span>` : ""}
        ${(item.matched_keywords || []).map(k => `<span class="tag">${escapeHtml(k)}</span>`).join("")}
      </div>
    </article>`).join("") : `<div class="empty">아직 발견한 링크가 없습니다.</div>`;
  el("item-count").textContent = state.items.length.toLocaleString();
  el("source-count").textContent = state.sources.filter(s => s.enabled).length;
  el("last-cycle").textContent = formatTime(state.last_cycle);
  renderCycleState();
}

function renderCycleState() {
  const running = Boolean(state?.cycle_state?.running);
  el("check-now").disabled = running;
  el("check-now").textContent = running ? "확인 중..." : "지금 확인";
}

async function refreshState(renderSourceSettings = false) {
  const response = await fetch("/api/state");
  if (!response.ok) throw new Error("상태를 불러오지 못했습니다.");
  state = await response.json();
  if (renderSourceSettings) renderSources();
  else renderSourceStatuses();
  renderItems();
}

async function load() {
  await refreshState(true);
}

function sendHeartbeat() {
  fetch("/api/heartbeat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
    keepalive: true
  });
}

function readSources() {
  return [...document.querySelectorAll(".source-card")].map((node, index) => ({
    id: node.dataset.id || `source-${Date.now()}-${index}`,
    enabled: node.querySelector(".source-enabled").checked,
    name: node.querySelector(".source-name").value.trim(),
    type: node.querySelector(".source-type").value,
    url: node.querySelector(".source-url").value.trim(),
    keywords: splitKeywords(node.querySelector(".source-keywords").value)
  }));
}

el("add-source").onclick = () => {
  const source = { id: `source-${Date.now()}`, enabled: true, name: "", type: "naver", url: "", keywords: [] };
  el("sources").appendChild(sourceCard(source));
};
el("save").onclick = async () => {
  await fetch("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ interval_seconds: Number(el("interval").value), sources: readSources() })
  });
  await load();
};
el("check-now").onclick = async () => {
  const previousFinishedAt = state.cycle_state?.finished_at;
  el("check-now").disabled = true;
  el("check-now").textContent = "확인 시작...";
  const response = await fetch("/api/check", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}"
  });
  if (!response.ok && response.status !== 409) {
    el("check-now").disabled = false;
    el("check-now").textContent = "지금 확인";
    return;
  }
  let observedRunning = response.status === 409;
  for (let attempt = 0; attempt < 240; attempt += 1) {
    await sleep(500);
    await refreshState();
    observedRunning ||= Boolean(state.cycle_state?.running);
    if (observedRunning && !state.cycle_state?.running) break;
    if (state.cycle_state?.finished_at !== previousFinishedAt) break;
  }
  await refreshState();
};
el("filter").oninput = renderItems;
load();
setInterval(async () => {
  await refreshState();
}, 10000);
sendHeartbeat();
setInterval(sendHeartbeat, 3000);
