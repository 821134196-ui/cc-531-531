/* 种蛋孵化台账前端（原生 JS） */
const ROLE_NAMES = { operator: "操作员", keeper: "饲养员", vet: "兽医" };
const INCIDENT_NAMES = {
  mixed_source: "混放来源", over_storage: "储存超期", incubator_stop: "孵化箱中途停机",
  missed_tray: "照蛋漏盘", tray_shift: "托盘移位", delayed_pip: "破壳延期",
};
const REASON_NAMES = {
  set: "入孵", stopped: "照蛋-停育", cracked: "照蛋-破损",
  weak: "弱雏", dead: "死胎", healthy: "健康雏", watch: "待观察", move: "转盘",
};
const state = { view: "map", users: [], currentUserId: null, data: {} };

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (state.currentUserId) headers["X-User-Id"] = state.currentUserId;
  if (opts.body && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, { ...opts, headers });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `请求失败 ${r.status}`);
  return data;
}
const toast = (msg, bad = false) => {
  const t = el("div", "toast" + (bad ? " bad" : ""), esc(msg));
  $("#toast-root").append(t);
  setTimeout(() => t.remove(), 3200);
};

function currentUser() { return state.users.find(u => u.id === state.currentUserId); }
function canOperate() { const u = currentUser(); return u && u.role !== "keeper"; }
function isVet() { const u = currentUser(); return u?.role === "vet"; }

/* ---------------- 初始化与导航 ---------------- */
async function boot() {
  state.users = await api("/api/users");
  const saved = Number(localStorage.getItem("uid"));
  state.currentUserId = state.users.find(u => u.id === saved)?.id || state.users[0].id;
  renderUserBox();
  $("#user-select").addEventListener("change", e => {
    state.currentUserId = Number(e.target.value);
    localStorage.setItem("uid", state.currentUserId);
    renderUserBox(); render();
  });
  document.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    state.view = b.dataset.view;
    state.selectedBatch = null;
    render();
  }));
  await render();
  setInterval(() => { if (state.view === "map") renderMap(true); }, 15000);
}

function renderUserBox() {
  const sel = $("#user-select");
  sel.innerHTML = state.users.map(u =>
    `<option value="${u.id}" ${u.id === state.currentUserId ? "selected" : ""}>${esc(u.display_name)}</option>`
  ).join("");
  const u = currentUser();
  const chip = $("#user-role");
  chip.textContent = ROLE_NAMES[u.role];
  chip.className = "role-chip " + u.role;
}

async function render(silent = false) {
  try {
    if (state.view === "map") await renderMap();
    else if (state.view === "batches") {
      if (state.selectedBatch) await renderBatchDetail(state.selectedBatch);
      else await renderBatchList();
    } else if (state.view === "incidents") await renderIncidents();
  } catch (e) {
    if (!silent) toast(e.message, true);
  }
}

/* ---------------- 盘位图 ---------------- */
async function renderMap(silent) {
  const root = $("#view");
  const [incubators, temp, batches] = await Promise.all([
    api("/api/incubators"), api("/api/temp"), api("/api/batches")]);
  state.data.incubators = incubators;
  const cols = incubators[0]?.shelf_cols || 4;
  root.innerHTML = "";

  const activeEggs = batches.filter(b => !b.hatched).reduce((s, b) => s + (b.balance || 0), 0);
  const trayCount = incubators.reduce((n, i) => n + i.slots.filter(s => s.occupant).length, 0);
  const tempBar = el("div", "card");
  tempBar.innerHTML = `
    <h2>温控（模拟适配器） <span class="sub">设定 ${temp.set_temp_c}℃ · 采样 ${esc(temp.ts)}</span></h2>
      <div class="k-row">
      <div class="kpi ${temp.alarm ? "clay" : "green"}">
        <div class="k-label">当前箱温</div>
        <div class="k-value">${temp.temp_c}℃</div>
        <div class="k-base">基数：设定温度 ${temp.set_temp_c}℃，偏差 ±1.5℃ 告警</div>
      </div>
      <div class="kpi"><div class="k-label">在孵托盘</div>
        <div class="k-value">${trayCount}</div>
        <div class="k-base">基数：${incubators.length} 台孵化箱盘位实时占用</div></div>
      <div class="kpi"><div class="k-label">在孵蛋数</div>
        <div class="k-value">${activeEggs}</div>
        <div class="k-base">基数：未出雏批次数量流水结余合计</div></div>
      </div>`;
  root.append(tempBar);

  for (const inc of incubators) {
    const card = el("div", "card");
    const stopped = inc.state === "stopped";
    const openStop = await openIncidentFor(inc.id, "incubator_stop");
    card.innerHTML = `
      <div class="incubator-head">
        <span class="state-dot ${stopped ? "stopped" : "running"}"></span>
        <h2 style="margin:0">${esc(inc.name)} <span class="sub">${esc(inc.code)} · ${inc.shelf_rows}层×${inc.shelf_cols}位</span></h2>
        <span class="tag ${stopped ? "bad" : "green"}">${stopped ? "停机" : "运行中"}</span>
        ${inc.last_event ? `<span class="sub muted">最近信号：${inc.last_event.event} ${esc(inc.last_event.created_at || "")}</span>` : ""}
        <span style="margin-left:auto" class="row" style="gap:8px"></span>
      </div>`;
    const headActions = card.querySelector(".incubator-head > span:last-child");
    if (canOperate()) {
      if (stopped && openStop) {
        const btn = el("button", "btn small ghost", "查看停机处置");
        btn.onclick = () => openStopModal(openStop, inc);
        headActions.append(btn);
      } else if (!stopped) {
        const btn = el("button", "btn small danger", "模拟停机信号");
        btn.title = "由模拟孵化箱信号适配器注入停机事件";
        btn.onclick = () => triggerStop(inc);
        headActions.append(btn);
      }
    }

    for (let shelf = 1; shelf <= inc.shelf_rows; shelf++) {
      const sBox = el("div", "shelf");
      if (stopped) sBox.style.background = "#fdf3f2";
      sBox.append(el("div", "shelf-label", `第 ${shelf} 层`));
      const cells = el("div", "cells");
      cells.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
      for (const slot of inc.slots.filter(s => s.shelf_no === shelf)
        .sort((a, b) => a.pos_no - b.pos_no)) {
        const cell = el("div", "cell " + (slot.occupant ? "occupied" : "empty"));
        if (stopped) cell.classList.add("stopped-box");
        if (slot.occupant) {
          cell.innerHTML = `
            <span class="tray-code">${esc(slot.occupant.code)}</span>
            <span class="batch-code">${esc(slot.occupant.batch_code || "—")}</span>
            <span class="egg-n">点击转盘 / 照蛋</span>`;
          cell.onclick = () => openTrayModal(slot, inc);
        } else {
          cell.innerHTML = `<span class="pos">空位</span><span class="pos">${shelf}-${slot.pos_no}</span>`;
        }
        cells.append(cell);
      }
      sBox.append(cells);
      card.append(sBox);
    }
    root.append(card);
  }
}

async function openIncidentFor(incubatorId, type) {
  const all = await api("/api/incidents");
  return all.find(i => i.incubator_id === incubatorId && i.type === type && i.status === "open");
}

async function triggerStop(inc) {
  if (!confirm(`模拟孵化箱 ${inc.code} 中途停机？将自动生成停机异常并禁止向该箱转盘。`)) return;
  try {
    await api("/api/incubator/stop", { method: "POST", body: { incubator_id: inc.id, note: "模拟信号：停电" } });
    toast("停机信号已接收，已立案停机异常");
    render();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 托盘操作弹窗 ---------------- */
function modal(title, innerNode) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-mask"><div class="modal">
    <h3>${esc(title)}</h3><div class="modal-body"></div></div></div>`;
  const mask = root.querySelector(".modal-mask");
  mask.addEventListener("click", e => { if (e.target === mask) root.innerHTML = ""; });
  const body = root.querySelector(".modal-body");
  if (typeof innerNode === "string") body.innerHTML = innerNode; else body.append(innerNode);
  return body;
}
const closeModal = () => { $("#modal-root").innerHTML = ""; };

function openTrayModal(slot, inc) {
  const occ = slot.occupant;
  const body = modal(`托盘 ${occ.code} · ${inc.code} ${slot.shelf_no}层${slot.pos_no}位`);
  body.innerHTML = `
    <p class="muted">所属批次：<b>${esc(occ.batch_code || "未绑定")}</b></p>
    <div class="row">
      <button class="btn" id="m-candle">照蛋登记</button>
      <button class="btn ghost" id="m-move">转盘（异常/应急移盘）</button>
      <button class="btn ghost" id="m-batch">查看批次</button>
    </div>
    ${!canOperate() ? `<p class="tag warn" style="margin-top:12px">当前为饲养员身份，只能查看</p>` : ""}`;
  $("#m-candle").onclick = () => openCandleModal(slot, inc);
  $("#m-move").onclick = () => openMoveModal(slot, inc);
  $("#m-batch").onclick = async () => {
    const bs = await api("/api/batches");
    const b = bs.find(x => x.code === occ.batch_code);
    closeModal();
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    document.querySelector('[data-view="batches"]').classList.add("active");
    state.view = "batches"; state.selectedBatch = b.id; render();
  };
}

function openMoveModal(slot, inc) {
  const incubators = state.data.incubators;
  const body = modal(`转盘 · ${slot.occupant.code}`);
  body.innerHTML = `
    <div class="field"><label>目标孵化箱</label>
      <select id="mv-inc">${incubators.map(i =>
        `<option value="${i.code}" ${i.state === "stopped" ? "disabled" : ""}>${esc(i.name)} ${esc(i.code)} ${i.state === "stopped" ? "（停机）" : ""}</option>`).join("")}</select></div>
    <div class="pill-inputs">
      <div class="field"><label>目标层</label><input id="mv-shelf" type="number" value="1" min="1"></div>
      <div class="field"><label>目标位</label><input id="mv-pos" type="number" value="1" min="1"></div>
    </div>
    <div class="field"><label>移盘原因（关联异常时备注）</label><input id="mv-note" value="转盘"></div>
    <p class="muted" style="font-size:12.5px">并发保护：提交时会校验该盘仍在
      <b>${esc(inc.code)} ${slot.shelf_no}-${slot.pos_no}</b>；
      若已被其他操作员转走，本次转盘会被拒绝。</p>
    <div class="actions"><button class="btn ghost" id="mv-cancel">取消</button>
      <button class="btn" id="mv-ok">确认转盘</button></div>`;
  $("#mv-cancel").onclick = closeModal;
  $("#mv-ok").onclick = async () => {
    try {
      await api("/api/move", { method: "POST", body: {
        tray: slot.occupant.code, incubator: $("#mv-inc").value,
        shelf: Number($("#mv-shelf").value), pos: Number($("#mv-pos").value),
        note: $("#mv-note").value || "转盘",
        expected_slot_id: slot.id,
      }});
      closeModal(); toast("转盘成功，位置流水已留痕"); render();
    } catch (e) { toast(e.message, true); }
  };
}

function openCandleModal(slot) {
  const body = modal(`照蛋登记 · ${slot.occupant.code}`);
  body.innerHTML = `
    <p class="muted">受精 + 停育 + 破损必须等于该盘当前在孵蛋数，提交时逐盘核对。</p>
    <div class="pill-inputs">
      <div class="field"><label>受精</label><input id="cd-f" type="number" min="0" value="0"></div>
      <div class="field"><label>停育</label><input id="cd-s" type="number" min="0" value="0"></div>
      <div class="field"><label>破损</label><input id="cd-c" type="number" min="0" value="0"></div>
    </div>
    <div class="field"><label>照蛋照片（本地存储，可多张）</label><input id="cd-photo" type="file" accept="image/*" multiple></div>
    <div class="field"><label>备注</label><textarea id="cd-note"></textarea></div>
    <div class="actions"><button class="btn ghost" id="cd-cancel">取消</button>
      <button class="btn" id="cd-ok">提交照蛋</button></div>`;
  $("#cd-cancel").onclick = closeModal;
  $("#cd-ok").onclick = async () => {
    const fd = new FormData();
    fd.append("tray", slot.occupant.code);
    fd.append("fertile", $("#cd-f").value);
    fd.append("stopped", $("#cd-s").value);
    fd.append("cracked", $("#cd-c").value);
    fd.append("note", $("#cd-note").value);
    [...$("#cd-photo").files].forEach(f => fd.append("photos", f));
    try {
      await api("/api/candle", { method: "POST", body: fd });
      closeModal(); toast("照蛋已登记，损耗计入流水"); render();
    } catch (e) { toast(e.message, true); }
  };
}

/* ---------------- 停机处置弹窗 ---------------- */
function openStopModal(incident, inc) {
  const body = modal(`停机处置 · ${inc.code}`);
  body.innerHTML = `
    <p>${esc(incident.description)}</p>
    <div class="field"><label>处置措施</label><textarea id="st-disp">${esc(incident.disposition || "")}</textarea></div>
    <p class="muted">流程：将蛋盘转移到运行中的孵化箱（盘位图点盘操作）→ 签收处置 → 恢复孵化箱。未签收前无法复机。</p>
    <div class="actions">
      <button class="btn ghost" id="st-close">关闭</button>
      <button class="btn" id="st-handle">签收处置</button>
    </div>`;
  $("#st-close").onclick = closeModal;
  $("#st-handle").onclick = async () => {
    try {
      await api(`/api/incidents/${incident.id}/handle`, {
        method: "POST", body: { disposition: $("#st-disp").value },
      });
      await api("/api/incubator/resume", {
        method: "POST", body: { incubator_id: inc.id, incident_id: incident.id, note: "处置完成复机" },
      });
      closeModal(); toast("处置已签收，孵化箱恢复运行"); render();
    } catch (e) { toast(e.message, true); }
  };
}

/* ---------------- 批次列表 ---------------- */
async function renderBatchList() {
  const batches = await api("/api/batches");
  const root = $("#view");
  root.innerHTML = "";
  const card = el("div", "card");
  card.innerHTML = `<h2>种蛋批次 <span class="sub">并列入孵数、各阶段损耗与出雏率（基数均为入孵数）</span></h2>`;
  if (canOperate()) {
    const nb = el("button", "btn small", "登记批次并入孵");
    nb.style.marginBottom = "12px";
    nb.onclick = openNewBatchModal;
    card.append(nb);
  }
  const table = el("table");
  table.innerHTML = `
    <thead><tr>
      <th>批次</th><th>来源鸡群</th><th>收集日期</th><th>储存温度</th>
      <th class="num">入孵数</th><th class="num">照蛋停育</th><th class="num">照蛋破损</th>
      <th class="num">弱雏</th><th class="num">死胎</th>
      <th class="num">健康雏</th><th class="num">待观察</th><th class="num">结余</th>
      <th class="num">出雏率</th><th>状态</th>
    </tr></thead><tbody>
    ${batches.map(b => `
      <tr class="batch-row" data-id="${b.id}">
        <td><b>${esc(b.code)}</b></td>
        <td>${esc(b.source_flock)}</td>
        <td>${esc(b.collected_date)}</td>
        <td>${b.storage_temp_c}℃</td>
        <td class="num">${b.set_total || "—"}</td>
        <td class="num ${b.stopped ? "loss-cell" : ""}">${b.set_total ? b.stopped : "—"}</td>
        <td class="num ${b.cracked ? "loss-cell" : ""}">${b.set_total ? b.cracked : "—"}</td>
        <td class="num ${b.weak ? "loss-cell" : ""}">${b.hatched ? b.weak : "—"}</td>
        <td class="num ${b.dead ? "loss-cell" : ""}">${b.hatched ? b.dead : "—"}</td>
        <td class="num">${b.hatched ? b.healthy : "—"}</td>
        <td class="num">${b.hatched ? b.watch : "—"}</td>
        <td class="num">${b.set_total ? (b.balance === 0 ? "0 ✓" : b.balance) : "—"}</td>
        <td class="num">${b.hatch_rate == null ? `<span class="muted">未出雏</span>` : `<span class="rate-pill">${b.hatch_rate}%</span>`}</td>
        <td>${b.hatched ? `<span class="tag green">已出雏</span>` : b.set_total ? `<span class="tag blue">在孵</span>` : `<span class="tag warn">未入孵</span>`}</td>
      </tr>`).join("")}
    </tbody>`;
  card.append(table);
  root.append(card);
  table.querySelectorAll(".batch-row").forEach(tr =>
    tr.onclick = () => { state.selectedBatch = Number(tr.dataset.id); renderBatchDetail(state.selectedBatch); });
}

/* ---------------- 批次详情 ---------------- */
async function renderBatchDetail(batchId) {
  const s = await api(`/api/batches/${batchId}`);
  const b = s.batch;
  const root = $("#view");
  root.innerHTML = "";

  const back = el("button", "btn ghost small", "← 返回批次列表");
  back.style.marginBottom = "14px";
  back.onclick = () => { state.selectedBatch = null; renderBatchList(); };
  root.append(back);

  const head = el("div", "card");
  head.innerHTML = `
    <h2>${esc(b.code)} <span class="sub">${esc(b.source_flock)} · 收集 ${esc(b.collected_date)} · 储存 ${b.storage_temp_c}℃ · 已储存 ${s.storage_days} 天</span></h2>
    <div class="kpi-row">
      <div class="kpi"><div class="k-label">入孵基数</div><div class="k-value">${s.set_total}</div><div class="k-base">基数：入孵登记合计</div></div>
      <div class="kpi clay"><div class="k-label">照蛋损耗</div><div class="k-value">${s.candle_losses.stopped + s.candle_losses.cracked}</div><div class="k-base">停育${s.candle_losses.stopped} + 破损${s.candle_losses.cracked}（基数：入孵 ${s.set_total}）</div></div>
      <div class="kpi clay"><div class="k-label">出雏损耗</div><div class="k-value">${s.hatch_losses.weak + s.hatch_losses.dead}</div><div class="k-base">弱雏${s.hatch_losses.weak} + 死胎${s.hatch_losses.dead}（基数：照蛋后存活）</div></div>
      <div class="kpi green"><div class="k-label">健康雏</div><div class="k-value">${s.healthy || "—"}</div><div class="k-base">待观察 ${s.watch}（基数：入孵 ${s.set_total}）</div></div>
      <div class="kpi ${s.hatched ? "green" : "blue"}"><div class="k-label">出雏率</div><div class="k-value">${s.hatch_rate == null ? "—" : s.hatch_rate + "%"}</div><div class="k-base">基数：健康雏 ÷ 入孵数 ${s.set_total}</div></div>
    </div>`;
  root.append(head);

  // 数量漏斗（每根条标注基数）
  if (s.set_total) {
    const chartCard = el("div", "card");
    const afterCandle = s.set_total - s.candle_losses.stopped - s.candle_losses.cracked;
    const rows = [
      { label: "入孵蛋数", v: s.set_total, color: "var(--c-blue)", base: "基数：入孵登记" },
      { label: "照蛋后存活", v: afterCandle, color: "var(--c-green)", base: `基数：入孵 ${s.set_total} − 停育 ${s.candle_losses.stopped} − 破损 ${s.candle_losses.cracked}` },
      { label: "健康雏", v: s.healthy, color: "var(--green)", base: `基数：入孵 ${s.set_total}（出雏率分母）` },
      { label: "待观察", v: s.watch, color: "var(--status-warn)", base: `基数：照蛋后存活 ${afterCandle}` },
      { label: "各阶段损耗", v: s.candle_losses.stopped + s.candle_losses.cracked + s.hatch_losses.weak + s.hatch_losses.dead, color: "var(--c-clay)", base: `基数：入孵 ${s.set_total}` },
    ];
    const max = s.set_total;
    chartCard.innerHTML = `<h2>数量守恒漏斗 <span class="sub">每根条标注计算基数；条长以入孵数为 100%</span></h2>`;
    const chart = el("div", "bar-chart");
    chart.innerHTML = rows.map(r => `
      <div class="bar-row">
        <span class="bl">${r.label}</span>
        <div class="bar-track" title="${esc(r.base)}">
          <div class="bar-fill" style="width:${Math.max(r.v / max * 100, r.v ? 3 : 0)}%;background:${r.color}">
            ${r.v ? `<span class="bv">${r.v}</span>` : ""}
          </div>
        </div>
        <span class="bar-val">${r.v}</span>
      </div>`).join("");
    const note = el("div", "bar-base-note",
      `守恒校验：入孵 ${s.set_total} − 照蛋损耗 ${s.candle_losses.stopped + s.candle_losses.cracked} − 出雏损耗 ${s.hatch_losses.weak + s.hatch_losses.dead} − 健康 ${s.healthy} − 待观察 ${s.watch} = 结余 <b>${s.balance}</b>${s.balance === 0 && s.hatched ? "（归零 ✓）" : s.balance === s.set_total ? "（尚未照蛋）" : ""}`);
    chart.append(note);
    chartCard.append(chart);
    root.append(chartCard);
  }

  const grid = el("div", "grid2");

  // 左：托盘 + 流水
  const left = el("div");
  const trayCard = el("div", "card");
  trayCard.innerHTML = `<h2>托盘与盘位 <span class="sub">共 ${s.trays.length} 盘</span></h2>`;
  trayCard.innerHTML += `<table><thead><tr><th>托盘</th><th>当前盘位</th><th class="num">入孵蛋数</th><th>照蛋</th></tr></thead><tbody>
    ${s.trays.map(t => {
      const pos = t.incubator_code ? `${t.incubator_code} ${t.shelf_no}层${t.pos_no}位` : "已出箱";
      const setEggs = s.ledger.filter(l => l.tray_id === t.id && l.change_type === "set")
        .reduce((a, l) => a + l.delta, 0);
      const candled = s.candles.some(c => c.tray_id === t.id);
      return `<tr><td><b>${esc(t.code)}</b></td><td>${esc(pos)}</td><td class="num">${setEggs || "—"}</td><td>${candled ? "已照蛋" : `<span class="tag warn">未照蛋</span>`}</td></tr>`;
    }).join("")}</tbody></table>`;
  left.append(trayCard);

  const ledCard = el("div", "card");
  ledCard.innerHTML = `<h2>数量流水 <span class="sub">只增不删，损耗留痕</span></h2>`;
  ledCard.innerHTML += `<table class="ledger-table"><thead><tr>
    <th>阶段</th><th>类型</th><th>原因</th><th>托盘</th><th class="num">变动</th><th class="num">结余</th><th>操作员</th></tr></thead><tbody>
    ${s.ledger.map(l => {
      const dcls = l.delta < 0 ? "delta-neg" : l.delta === 0 ? "delta-zero" : "delta-pos";
      const stageName = { set: "入孵", candle: "照蛋", hatch: "出雏" }[l.stage];
      const ctName = { set: "入孵", loss: "损耗", harvest: "收成", move: "转盘" }[l.change_type];
      const t = s.trays.find(x => x.id === l.tray_id);
      return `<tr><td>${stageName}</td><td>${ctName}</td><td>${REASON_NAMES[l.reason] || esc(l.reason)}</td>
        <td>${t ? esc(t.code) : "—"}</td>
        <td class="num ${dcls}">${l.delta > 0 ? "+" : ""}${l.delta}</td>
        <td class="num">${l.balance_after}</td><td>${esc(l.operator)}</td></tr>`;
    }).join("")}</tbody></table>`;
  left.append(ledCard);

  // 右：照片 + 异常 + 出雏/复核
  const right = el("div");
  if (s.photos.length) {
    const pCard = el("div", "card");
    pCard.innerHTML = `<h2>照蛋照片 <span class="sub">点击放大，可并列比对</span></h2>`;
    const gridP = el("div", "photo-grid");
    s.photos.forEach((p, i) => {
      const pc = el("div", "photo-card");
      pc.innerHTML = `<img src="/api/photos/${p.id}" alt="${esc(p.caption || "")}" loading="lazy">
        <div class="pc-cap"><b>${esc(p.tray_code)}</b> · ${esc(p.caption || "")}</div>`;
      pc.onclick = () => openLightbox(s.photos, i);
      gridP.append(pc);
    });
    pCard.append(gridP);
    right.append(pCard);
  }

  if (s.incidents.length) {
    const iCard = el("div", "card");
    iCard.innerHTML = `<h2>异常处置记录 <span class="sub">${s.incidents.length} 条</span></h2>`;
    iCard.innerHTML += s.incidents.map(i => `
      <div style="border-left:3px solid var(--green);padding:8px 12px;margin-bottom:10px;background:var(--surface);border-radius:0 8px 8px 0">
        <span class="tag ${i.status === "handled" ? "green" : "warn"}">${INCIDENT_NAMES[i.type]}</span>
        <span class="tag ${i.status === "handled" ? "green" : "warn"}" style="margin-left:6px">${i.status === "handled" ? "已签收" : "待处置"}</span>
        <div style="margin-top:6px">${esc(i.description)}</div>
        <div class="incident-meta">处置：${esc(i.disposition)}${i.handled_at ? " · " + esc(i.handled_at) : ""}</div>
      </div>`).join("");
    right.append(iCard);
  }

  if (!s.hatched && s.set_total) {
    right.append(hatchForm(s));
  }
  right.append(reviewCard(s));

  grid.append(left, right);
  root.append(grid);
}

function hatchForm(s) {
  const card = el("div", "card");
  card.innerHTML = `
    <h2>出雏统计 <span class="sub">四项之和必须等于照蛋后在孵结余 ${s.balance}</span></h2>
    ${!canOperate() ? `<p class="tag warn">饲养员不可登记，仅可查看</p>` : ""}
    <div class="pill-inputs">
      <div class="field"><label>健康雏</label><input id="h-healthy" type="number" min="0" value="${s.balance}"></div>
      <div class="field"><label>待观察</label><input id="h-watch" type="number" min="0" value="0"></div>
      <div class="field"><label>弱雏</label><input id="h-weak" type="number" min="0" value="0"></div>
      <div class="field"><label>死胎</label><input id="h-dead" type="number" min="0" value="0"></div>
      <div class="field"><label>其中破壳延期</label><input id="h-delayed" type="number" min="0" value="0"></div>
    </div>
    <button class="btn" id="h-ok" ${canOperate() ? "" : "disabled"}>提交出雏统计</button>`;
  $("#h-ok", card).onclick = async () => {
    try {
      await api("/api/hatch", { method: "POST", body: {
        batch_id: s.batch.id,
        healthy: Number($("#h-healthy", card).value),
        watch: Number($("#h-watch", card).value),
        weak: Number($("#h-weak", card).value),
        dead: Number($("#h-dead", card).value),
        delayed: Number($("#h-delayed", card).value),
      }});
      toast("出雏统计已入账，数量结余归零"); renderBatchDetail(s.batch.id);
    } catch (e) { toast(e.message, true); }
  };
  return card;
}

function reviewCard(s) {
  const card = el("div", "card");
  if (s.review) {
    const cmap = { pass: ["green", "合格通过"], conditional: ["warn", "有条件通过"], reject: ["bad", "驳回"] };
    const [cls, label] = cmap[s.review.conclusion];
    card.innerHTML = `
      <h2>健康复核</h2>
      <div class="signed-box">
        <span class="tag ${cls}">${label}</span>
        <div><b>兽医签署：${esc(s.review.vet_name)}</b>
          <div class="incident-meta">${esc(s.review.signed_at)} · ${esc(s.review.note || "")}</div></div>
      </div>`;
    return card;
  }
  card.innerHTML = `
    <h2>健康复核 <span class="sub">仅兽医可签署，饲养员不能代签</span></h2>
    <div class="field"><label>结论</label>
      <select id="rv-c"><option value="pass">合格通过</option><option value="conditional">有条件通过</option><option value="reject">驳回</option></select></div>
    <div class="field"><label>复核意见</label><textarea id="rv-note"></textarea></div>
    ${isVet() ? `<button class="btn" id="rv-ok">兽医签署</button>`
      : `<p class="tag warn">当前身份（${ROLE_NAMES[currentUser().role]}）无权签署，请切换到陈兽医</p>`}`;
  if (isVet()) $("#rv-ok", card).onclick = async () => {
    try {
      await api("/api/reviews", { method: "POST", body: {
        batch_id: s.batch.id, conclusion: $("#rv-c", card).value, note: $("#rv-note", card).value,
      }});
      toast("健康结论已由兽医签署"); renderBatchDetail(s.batch.id);
    } catch (e) { toast(e.message, true); }
  };
  return card;
}

/* ---------------- 登记批次并入孵 ---------------- */
async function openNewBatchModal() {
  if (!state.data.incubators) state.data.incubators = await api("/api/incubators");
  const incubators = state.data.incubators;
  const body = modal("登记种蛋批次并入孵");
  body.innerHTML = `
    <div class="pill-inputs">
      <div class="field"><label>来源鸡群</label><input id="nb-flock" value="D栋·新养鸡群"></div>
      <div class="field"><label>收集日期</label><input id="nb-date" type="date" value="${new Date().toISOString().slice(0, 10)}"></div>
      <div class="field"><label>储存温度℃</label><input id="nb-temp" type="number" step="0.1" value="16.5"></div>
    </div>
    <div class="section-title">上盘（可两行）</div>
    ${[1, 2].map(i => `
      <div class="pill-inputs">
        <div class="field"><label>托盘编号</label><input id="nb-tray${i}" placeholder="如 T-A${i}" value="T-A${i}"></div>
        <div class="field"><label>孵化箱</label><select id="nb-inc${i}">${incubators.map(x =>
          `<option value="${x.code}">${esc(x.name)} ${esc(x.code)}</option>`).join("")}</select></div>
        <div class="field"><label>层</label><input id="nb-shelf${i}" type="number" value="3"></div>
        <div class="field"><label>位</label><input id="nb-pos${i}" type="number" value="${i + 2}"></div>
        <div class="field"><label>蛋数</label><input id="nb-eggs${i}" type="number" value="120"></div>
      </div>`).join("")}
    <p class="muted" style="font-size:12.5px">收集日距今超过 7 天将被阻断，须先在「异常处置」登记并签收储存超期处置。</p>
    <div class="actions"><button class="btn ghost" id="nb-cancel">取消</button>
      <button class="btn" id="nb-ok">登记并入孵</button></div>`;
  $("#nb-cancel").onclick = closeModal;
  $("#nb-ok").onclick = async () => {
    try {
      const { id: bid } = await api("/api/batches", { method: "POST", body: {
        source_flock: $("#nb-flock").value, collected_date: $("#nb-date").value,
        storage_temp_c: Number($("#nb-temp").value),
      }});
      const placements = [1, 2].map(i => ({
        tray: $(`#nb-tray${i}`).value.trim(),
        incubator: $(`#nb-inc${i}`).value,
        shelf: Number($(`#nb-shelf${i}`).value), pos: Number($(`#nb-pos${i}`).value),
        eggs: Number($(`#nb-eggs${i}`).value),
      })).filter(p => p.tray && p.eggs > 0);
      await api("/api/place", { method: "POST", body: { batch_id: bid, placements } });
      closeModal(); toast("批次已登记，蛋盘入孵完成");
      state.selectedBatch = bid; renderBatchDetail(bid);
    } catch (e) { toast(e.message, true); }
  };
}

/* ---------------- 照片灯箱（并列比对） ---------------- */
function openLightbox(photos, index) {
  const root = $("#lightbox-root");
  const cur = photos[index];
  root.innerHTML = `
    <button class="lb-close">×</button>
    <div class="lb-inner">
      <div class="lb-photo"><img src="/api/photos/${cur.id}" alt=""></div>
      <div class="lb-side">
        <h3 style="font-size:15px">${esc(cur.tray_code)} · ${esc(cur.caption || "照蛋照片")}</h3>
        <div class="muted" style="font-size:12.5px">${esc(cur.created_at || "")}</div>
        <div class="section-title">并列比对（点击切换主图）</div>
        <div class="lb-compare">
          ${photos.map((p, i) => `<img src="/api/photos/${p.id}" data-i="${i}" class="${i === index ? "sel" : ""}" title="${esc(p.caption || "")}">`).join("")}
        </div>
      </div>
    </div>`;
  root.classList.add("open");
  root.querySelector(".lb-close").onclick = closeLightbox;
  root.onclick = e => { if (e.target === root) closeLightbox(); };
  root.querySelectorAll(".lb-compare img").forEach(img => img.onclick = () => {
    closeLightbox(false); openLightbox(photos, Number(img.dataset.i));
  });
}
function closeLightbox(clear = true) {
  const root = $("#lightbox-root");
  root.classList.remove("open");
  if (clear) root.innerHTML = "";
}

/* ---------------- 异常处置页 ---------------- */
async function renderIncidents() {
  const list = await api("/api/incidents");
  const root = $("#view");
  root.innerHTML = "";
  const card = el("div", "card");
  card.innerHTML = `<h2>异常处置 <span class="sub">混放来源 / 储存超期 / 中途停机 / 照蛋漏盘 / 托盘移位 / 破壳延期，全程留痕</span></h2>`;
  if (canOperate()) {
    const nb = el("button", "btn small", "登记异常处置");
    nb.style.marginBottom = "12px";
    nb.onclick = openNewIncidentModal;
    card.append(nb);
  }
  const table = el("table");
  table.innerHTML = `<thead><tr>
    <th>类型</th><th>批次</th><th>托盘</th><th>孵化箱</th><th>描述与处置</th><th>状态</th><th>登记人</th><th></th></tr></thead><tbody>
    ${list.map(i => `
      <tr data-id="${i.id}">
        <td><span class="tag ${i.status === "handled" ? "green" : "warn"}">${INCIDENT_NAMES[i.type]}</span></td>
        <td>${esc(i.batch_code || "—")}</td>
        <td>${esc(i.tray_code || "—")}</td>
        <td>${esc(i.incubator_code || "—")}</td>
        <td style="max-width:380px">${esc(i.description)}<div class="incident-meta">处置：${esc(i.disposition)}</div></td>
        <td>${i.status === "handled" ? `<span class="tag green">已签收 ${esc(i.handled_at || "")}</span>` : `<span class="tag bad">待处置</span>`}</td>
        <td>${esc(i.operator)}</td>
        <td>${i.status === "open" && canOperate() ? `<button class="btn small ghost" data-handle="${i.id}">签收</button>` : ""}
            ${i.type === "incubator_stop" && i.status === "handled" ? `<span class="muted">可复机</span>` : ""}</td>
      </tr>`).join("")}</tbody>`;
  card.append(table);
  root.append(card);
  card.querySelectorAll("[data-handle]").forEach(btn => btn.onclick = async () => {
    const disposition = prompt("补充处置说明（可留空）：", "");
    if (disposition === null) return;
    try {
      await api(`/api/incidents/${btn.dataset.handle}/handle`, {
        method: "POST", body: { disposition: disposition || undefined },
      });
      toast("异常已签收"); render();
    } catch (e) { toast(e.message, true); }
  });
}

async function openNewIncidentModal() {
  const [batches, incubators] = await Promise.all([api("/api/batches"), api("/api/incubators")]);
  const body = modal("登记异常处置");
  body.innerHTML = `
    <div class="pill-inputs">
      <div class="field"><label>异常类型</label><select id="ni-type">
        ${Object.entries(INCIDENT_NAMES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}
      </select></div>
      <div class="field"><label>关联批次（可空）</label><select id="ni-batch">
        <option value="">—</option>
        ${batches.map(b => `<option value="${b.id}">${esc(b.code)}</option>`).join("")}
      </select></div>
      <div class="field"><label>关联孵化箱（可空）</label><select id="ni-inc">
        <option value="">—</option>
        ${incubators.map(i => `<option value="${i.id}">${esc(i.name)} ${esc(i.code)}</option>`).join("")}
      </select></div>
    </div>
    <div class="field"><label>情况描述</label><textarea id="ni-desc" placeholder="何时发现、涉及蛋盘/数量、现象"></textarea></div>
    <div class="field"><label>处置措施</label><textarea id="ni-disp"></textarea></div>
    <p class="muted" style="font-size:12.5px">登记后为「待处置」，须由操作员/兽医签收才生效（如储存超期需签收后方可入孵）。</p>
    <div class="actions"><button class="btn ghost" id="ni-cancel">取消</button>
      <button class="btn" id="ni-ok">登记</button>
      <button class="btn ghost" id="ni-ok-handle">登记并签收</button></div>`;
  $("#ni-cancel").onclick = closeModal;
  const submit = async handle => {
    const bid = $("#ni-batch").value, iid = $("#ni-inc").value;
    try {
      const { id } = await api("/api/incidents", { method: "POST", body: {
        type: $("#ni-type").value,
        batch_id: bid ? Number(bid) : null,
        incubator_id: iid ? Number(iid) : null,
        description: $("#ni-desc").value, disposition: $("#ni-disp").value,
      }});
      if (handle) await api(`/api/incidents/${id}/handle`, { method: "POST", body: {} });
      closeModal(); toast(handle ? "异常已登记并签收" : "异常已登记，待处置签收"); render();
    } catch (e) { toast(e.message, true); }
  };
  $("#ni-ok").onclick = () => submit(false);
  $("#ni-ok-handle").onclick = () => submit(true);
}

document.addEventListener("keydown", e => { if (e.key === "Escape") { closeModal(); closeLightbox(); } });
boot();