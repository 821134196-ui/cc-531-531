/* 种蛋孵化管理 —— 原生 JS SPA（无依赖） */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.message || '请求失败'), { code: data.error });
  return data;
};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pct = (a, b) => b ? (a / b * 100).toFixed(1) + '%' : '—';

const ST_STATUS = { registered: ['已登记', 'gray'], incubating: ['入孵中', 'blue'],
  candled: ['照蛋完成', 'amber'], hatched: ['已出雏', 'green'], reviewed: ['已复核', 'green'] };
const KIND_CN = { mixed_source: '混放来源', storage_overdue: '储存超期', incubator_stop: '孵化箱停机',
  missed_tray: '照蛋漏盘', tray_shift: '托盘移位', late_hatch: '破壳延期' };
const COLORS = { healthy: '#2E9E6B', observe: '#E39F22', late: '#4C7DB8',
  infertile: '#9A948C', stopped: '#B07458', broken: '#D24530',
  in_incubator: '#B8B2A8' };
const LABELS = { healthy: '健康雏鸡', observe: '待观察', late: '破壳延期',
  infertile: '无精', stopped: '停育', broken: '破损', in_incubator: '照蛋后在孵' };

let META = null;
const actorId = () => +localStorage.getItem('hatch_actor') || 1;
const setActor = id => localStorage.setItem('hatch_actor', id);
const actor = () => META.users.find(u => u.id === actorId()) || META.users[0];
const ROLE_CN = { vet: '兽医', operator: '操作员', keeper: '饲养员' };

function flash(msg, ok = false) {
  let el = $('#flash');
  if (!el) { $('#app').insertAdjacentHTML('afterbegin', '<div id="flash" class="flash"></div>'); el = $('#flash'); }
  el.className = 'flash show ' + (ok ? 'ok' : 'err');
  el.textContent = (ok ? '✓ ' : '✕ ') + msg;
  clearTimeout(flash._t);
  flash._t = setTimeout(() => el.classList.remove('show'), 6000);
}
/* ---------------- 路由 ---------------- */
function route() {
  const [r, id, action] = location.hash.replace(/^#\/?/, '').split('/');
  $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.route === (r || 'batches')));
  const map = { batches: id ? batchDetailPage : batchesPage, incubators: id ? incubatorPage : incubatorsPage,
    moves: movesPage, incidents: incidentsPage };
  (map[r] || batchesPage)(id, action).catch(e => flash(e.message));
}
window.addEventListener('hashchange', route);

/* ---------------- 顶栏操作员 ---------------- */
async function boot() {
  META = await api('/api/meta');
  if (!META.users.some(u => u.id === actorId())) setActor(META.users[0].id);
  $('#actor').innerHTML = META.users.map(u =>
    `<option value="${u.id}" ${u.id === actorId() ? 'selected' : ''}>${esc(u.name)}（${ROLE_CN[u.role]}）</option>`).join('');
  $('#actor').onchange = e => { setActor(+e.target.value); renderRoleTag(); route(); };
  renderRoleTag();
  route();
}
function renderRoleTag() {
  const t = $('#roleTag');
  t.textContent = ROLE_CN[actor().role];
  t.className = 'role-tag ' + actor().role;
}
const head = title => `<div class="page-head"><div><h2>${title}</h2></div></div>`;

/* ---------------- 批次列表 ---------------- */
async function batchesPage() {
  const rows = await api('/api/batches');
  const app = $('#app');
  app.innerHTML = head('种蛋批次') + `
    <p class="muted" style="margin:-8px 0 14px">按来源鸡群、收集日期、储存温度与孵化箱批次登记；并列入孵数、各阶段损耗与出雏率。</p>
    <div id="flash" class="flash"></div>
    <div class="card">
      <h3>批次台账</h3>
      <table>
        <thead><tr><th>批次</th><th>来源鸡群</th><th>收集日期</th><th>储存</th>
          <th class="num">入孵数</th><th class="num">无精</th><th class="num">停育</th>
          <th class="num">破损</th><th class="num">健康/待观察</th><th class="num">出雏率</th>
          <th>状态</th><th>孵化箱</th></tr></thead>
        <tbody>${rows.map(r => {
          const b = r.balance, overdue = r.storage_age_days > r.max_storage_days;
          const [st, cls] = ST_STATUS[r.status];
          return `<tr style="cursor:pointer" onclick="location.hash='#/batches/${r.id}'">
            <td><a>${esc(r.code)}</a></td>
            <td>${esc(r.flock_code)} ${esc(r.flock_name)}</td>
            <td>${esc(r.collected_at.slice(0, 10))}
              ${overdue ? `<span class="pill red">存${r.storage_age_days}天·超期</span>` : `<span class="muted">${r.storage_age_days}天</span>`}</td>
            <td>${r.storage_temp_c}℃</td>
            <td class="num"><b>${b.set_in || '—'}</b></td>
            <td class="num">${b.infertile || '—'}</td>
            <td class="num">${b.stopped || '—'}</td>
            <td class="num">${b.broken || '—'}</td>
            <td class="num">${b.healthy || '—'}<span class="muted">/${b.observe || 0}</span></td>
            <td class="num">${r.hatch_rate_pct == null ? '—' : `<b>${r.hatch_rate_pct}%</b>`}</td>
            <td><span class="pill ${cls}">${st}</span></td>
            <td>${esc(r.incubator_code || '—')}</td></tr>`;
        }).join('')}</tbody></table></div>`;
}

/* ---------------- 批次详情 ---------------- */
async function batchDetailPage(id) {
  const d = await api(`/api/batches/${id}`);
  const b = d.batch, bal = d.balance;
  const [st, stCls] = ST_STATUS[b.status];
  const app = $('#app');
  app.innerHTML = `
    <div class="page-head">
      <div><h2>${esc(b.code)} <span class="pill ${stCls}">${st}</span></h2>
        <p>${esc(b.flock_code)} · ${esc(b.flock_name)} · 收集 ${esc(b.collected_at.slice(0, 10))}
           · 储存 ${b.storage_temp_c}℃ · ${d.storage_age_days} 天（上限 ${d.max_storage_days} 天）</p></div>
      <button class="ghost" onclick="history.back()">← 返回列表</button></div>
    <div id="flash" class="flash"></div>

    <div class="stats">
      ${stat('登记 / 入孵', bal.set_in, '枚', '基数：登记 ' + b.registered_egg_count)}
      ${stat('照蛋损耗', bal.candle_loss, '枚', `无精${bal.infertile}·停育${bal.stopped}·破损${bal.broken}`)}
      ${stat('照蛋后在孵', bal.after_candle, '枚', '基数：' + bal.set_in)}
      ${stat('健康雏鸡', bal.healthy, '只', '待观察 ' + bal.observe)}
      ${stat('出雏率', d.hatch_rate_pct == null ? '—' : d.hatch_rate_pct, '%', '基数：入孵 ' + bal.set_in)}
    </div>

    <div class="grid-2">
      <div class="card"><h3>各阶段数量去向（基数：入孵 ${bal.set_in} 枚）</h3>
        <div class="chart-wrap" id="funnelChart"></div>
        <div class="legend" id="funnelLegend"></div>
        <div class="base-note">每段标注枚数；灰色为待核销（破壳延期 / 在孵）。悬停查看占比，基数=入孵数。</div>
      </div>
      <div class="card"><h3>数量流水（只追加，禁止删改）</h3>
        <div style="max-height:260px;overflow:auto">
        <table><thead><tr><th>时间</th><th>类型</th><th class="num">变动</th><th>操作人</th></tr></thead>
        <tbody>${d.ledger.map(l => `<tr>
          <td class="muted" style="white-space:nowrap">${esc(l.created_at.slice(5, 16).replace('T', ' '))}</td>
          <td>${esc(l.note)}</td>
          <td class="num" style="color:${l.delta < 0 ? 'var(--red)' : 'var(--green-deep)'};font-weight:600">
            ${l.delta > 0 ? '+' : ''}${l.delta}</td>
          <td>${esc(l.operator_name)}</td></tr>`).join('') ||
          '<tr><td colspan="4" class="muted">暂无流水</td></tr>'}</tbody></table></div>
        <div class="base-note">守恒校验：${bal.set_in} − 损耗 ${bal.candle_loss} − 出雏 ${bal.hatch_out}
          = 剩余 ${bal.remaining} ${bal.balanced ? '✓ 账平' : '✕ 不平'}</div>
      </div>
    </div>

    <div class="card">
      <h3>托盘与盘位</h3>
      <table><thead><tr><th>托盘</th><th>当前位置</th><th class="num">在盘数</th>
        <th class="num">受精</th><th class="num">无精</th><th class="num">停育</th>
        <th class="num">破损</th><th>状态</th></tr></thead><tbody>
        ${d.trays.map(t => `<tr>
          <td>${esc(t.tray_code)}</td>
          <td>${t.incubator_code ? `${esc(t.incubator_code)} ${t.layer_no}层${t.slot_no}号`
            : '<span class="muted">已下架</span>'}</td>
          <td class="num">${t.egg_count}</td>
          <td class="num">${t.fertile ?? '—'}</td>
          <td class="num">${t.infertile ?? '—'}</td>
          <td class="num">${t.stopped ?? '—'}</td>
          <td class="num">${t.broken ?? '—'}</td>
          <td><span class="pill ${t.status === 'active' ? 'green' : 'gray'}">${
            { active: '在盘', quarantined: '隔离', removed: '下架' }[t.status]}</span></td></tr>`).join('')}
      </tbody></table>
    </div>

    <div class="grid-2">
      <div class="card"><h3>照蛋记录与异常照片（点击放大比对）</h3>${candlingBlock(d)}</div>
      <div class="card"><h3>异常处置记录</h3>${incidentsBlock(d.incidents)}</div>
    </div>

    <div class="grid-2">
      <div class="card"><h3>出雏统计</h3>${hatchBlock(d)}</div>
      <div class="card"><h3>健康复核（仅兽医可签署）</h3>${reviewBlock(d)}</div>
    </div>

    <div class="card"><h3>转盘 / 下架流水</h3>
      <table><thead><tr><th>时间</th><th>托盘</th><th>从</th><th>到</th><th>原因</th><th>操作人</th></tr></thead>
      <tbody>${d.movements.map(m => `<tr>
        <td class="muted" style="white-space:nowrap">${esc(m.created_at.slice(5, 16).replace('T', ' '))}</td>
        <td>${esc(m.tray_code)}</td>
        <td>${m.from_inc ? `${esc(m.from_inc)} ${m.fl_layer}-${m.fl_slot}` : '—'}</td>
        <td>${m.to_inc ? `${esc(m.to_inc)} ${m.tl_layer}-${m.tl_slot}` : '下架'}</td>
        <td>${reasonCn(m.reason)}</td><td>${esc(m.operator_name)}</td></tr>`).join('') ||
        '<tr><td colspan="6" class="muted">暂无</td></tr>'}</tbody></table></div>`;

  drawFunnel('#funnelChart', bal);
  bindPhotoModal();
  bindBatchActions(d);
}

function stat(k, v, unit, sub) {
  return `<div class="stat"><div class="k">${k}</div><div class="v">${v}<small> ${unit}</small></div>
    <div class="sub">${esc(sub || '')}</div></div>`;
}
function reasonCn(r) {
  return { set_in: '入孵上架', transfer: '转盘', evacuation: '停机疏散',
    position_correct: '移位纠正', hatch_offload: '出雏下架' }[r] || r;
}
function incidentsBlock(list) {
  if (!list.length) return '<p class="muted">无异常处置</p>';
  return `<ul class="timeline">${list.map(n => `
    <li><span class="pill ${n.status === 'handled' ? 'green' : 'red'}">${
      n.status === 'handled' ? '已处置' : '待处置'}</span>
      <b>${KIND_CN[n.kind]}</b> · ${esc(n.title)}
      <div class="muted">报告：${esc(n.reporter_name || '—')} · ${esc(n.created_at.slice(5, 16).replace('T', ' '))}</div>
      ${n.description ? `<div>${esc(n.description)}</div>` : ''}
      ${n.action ? `<div class="muted">处置：${esc(n.action)}（${esc(n.handler_name || '')}）</div>` : ''}
    </li>`).join('')}</ul>`;
}
function candlingBlock(d) {
  if (!d.candles.length) return '<p class="muted">尚未照蛋</p>';
  return d.candles.map(c => `
    <div style="padding:10px 0;border-bottom:1px solid #f0eee9">
      <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px">
        <b>${esc(c.tray_code)}</b>
        <span class="muted">${esc(c.candled_at.slice(5, 16).replace('T', ' '))} · ${esc(c.operator_name)}</span></div>
      <div style="font-size:12.5px;margin:4px 0">
        共 ${c.egg_count}：受精 <b style="color:var(--green-deep)">${c.fertile}</b>
        · 无精 ${c.infertile} · 停育 ${c.stopped} · 破损 ${c.broken}
        <span class="muted">（合计 ${c.fertile + c.infertile + c.stopped + c.broken}）</span></div>
      <div class="photo-grid">${c.photos.map(p => `
        <div class="photo-card" data-src="/data/${p.path}" data-kind="${esc(photoKind(p.kind))}">
          <img src="/data/${p.path}" alt="${esc(photoKind(p.kind))}" loading="lazy">
          <div class="cap"><span>${esc(photoKind(p.kind))}</span><span>⤢</span></div></div>`).join('')}</div>
    </div>`).join('');
}
function photoKind(k) {
  return { overview: '整盘留档', fertile: '受精·血管网', stopped: '停育·血环',
    broken: '破损·裂纹', infertile: '无精·透亮' }[k] || k;
}
function hatchBlock(d) {
  const b = d.batch, bal = d.balance;
  if (d.hatch) return `<dl class="kv">
      <dt>统计时间</dt><dd>${esc(d.hatch.hatched_at.slice(5, 16).replace('T', ' '))}</dd>
      <dt>健康雏鸡</dt><dd><b>${d.hatch.healthy}</b> 只</dd>
      <dt>待观察</dt><dd>${d.hatch.observe} 只</dd>
      <dt>破壳延期</dt><dd>${d.hatch.late} 只</dd>
      <dt>操作人</dt><dd>${META.users.find(u => u.id === d.hatch.operator_id)?.name || ''}</dd>
      ${d.hatch.note ? `<dt>备注</dt><dd>${esc(d.hatch.note)}</dd>` : ''}</dl>`;
  if (b.status !== 'candled') return '<p class="muted">照蛋收口后方可统计（基数：照蛋后在孵 ' + bal.after_candle + ' 枚）</p>';
  return `
    <p class="muted">三类合计必须 = 照蛋后在孵 <b>${bal.after_candle}</b> 枚；破壳延期需先有处置记录。</p>
    <div class="row">
      <div><label>健康雏鸡</label><input id="h_healthy" type="number" value="${bal.after_candle}"></div>
      <div><label>待观察个体</label><input id="h_observe" type="number" value="0"></div>
      <div><label>破壳延期</label><input id="h_late" type="number" value="0"></div>
    </div>
    <label>备注</label><input id="h_note" placeholder="如：健康雏鸡转育雏舍">
    <div style="margin-top:12px;display:flex;gap:8px">
      <button class="green" id="btnHatch">提交出雏统计（自动下架）</button>
      <button class="ghost" id="btnLate">登记破壳延期处置</button></div>`;
}
function reviewBlock(d) {
  if (d.review) {
    const concl = { pass: ['合格通过', 'green'], observe: ['部分待观察', 'amber'], fail: ['不合格', 'red'] }[d.review.conclusion];
    return `<dl class="kv"><dt>结论</dt><dd><span class="pill ${concl[1]}">${concl[0]}</span></dd>
      <dt>签署兽医</dt><dd><b>${esc(d.review.vet_name)}</b></dd>
      <dt>签署时间</dt><dd>${esc(d.review.signed_at.slice(5, 16).replace('T', ' '))}</dd>
      ${d.review.note ? `<dt>意见</dt><dd>${esc(d.review.note)}</dd>` : ''}</dl>
    <p class="muted" style="margin-top:8px">签署后不可更改；饲养员与操作员不能代签。</p>`;
  }
  const can = actor().role === 'vet';
  return `
    ${can ? '' : '<div class="strike">当前身份是' + ROLE_CN[actor().role] + '：健康结论仅兽医可签署，提交将被后端拒绝。</div>'}
    <label>健康结论</label>
    <select id="r_conclusion"><option value="pass">合格通过</option>
      <option value="observe">部分待观察</option><option value="fail">不合格</option></select>
    <label>兽医意见</label><textarea id="r_note" rows="3" placeholder="脐部、精神状态、转群意见…"></textarea>
    <div style="margin-top:12px"><button class="green" id="btnReview">${can ? '兽医签署' : '尝试提交（权限验证）'}</button></div>`;
}

function bindBatchActions(d) {
  const bid = d.batch.id;
  const btnH = $('#btnHatch');
  if (btnH) btnH.onclick = async () => {
    try {
      await api(`/api/batches/${bid}/hatch`, { method: 'POST', body: {
        healthy: +$('#h_healthy').value, observe: +$('#h_observe').value,
        late: +$('#h_late').value, operator_id: actorId(), note: $('#h_note').value } });
      flash('出雏统计已入账，托盘已下架', true); route();
    } catch (e) { flash(e.message); }
  };
  const btnL = $('#btnLate');
  if (btnL) btnL.onclick = async () => {
    try {
      const { id } = await api('/api/incidents', { method: 'POST', body: {
        kind: 'late_hatch', batch_id: bid, incubator_id: d.batch.incubator_id,
        title: '破壳延期', description: '出雏统计时存在破壳延期个体。', reporter_id: actorId() } });
      await api(`/api/incidents/${id}/handle`, { method: 'POST', body: {
        handler_id: actorId(), action: '转出雏盘延长观察 24 小时，保持湿度并定时巡查。' } });
      flash('破壳延期处置已记录，可填入延期数量后提交', true);
    } catch (e) { flash(e.message); }
  };
  const btnR = $('#btnReview');
  if (btnR) btnR.onclick = async () => {
    try {
      await api(`/api/batches/${bid}/review`, { method: 'POST', body: {
        user_id: actorId(), conclusion: $('#r_conclusion').value, note: $('#r_note').value } });
      flash('兽医签署完成', true); route();
    } catch (e) { flash(e.message); }
  };
}

/* ---------------- 图表：去向堆叠条 + 悬停 ---------------- */
function drawFunnel(sel, bal) {
  const W = 460, H = 190, padL = 8, barY = 56, barH = 46;
  const base = bal.set_in;
  const segs = [
    ['healthy', bal.healthy], ['observe', bal.observe],
    ['infertile', bal.infertile], ['stopped', bal.stopped], ['broken', bal.broken],
  ];
  if (bal.remaining > 0) segs.splice(2, 0, [bal.hatch_out > 0 ? 'late' : 'in_incubator', bal.remaining]);
  let x = padL;
  const parts = segs.filter(([, v]) => v > 0).map(([k, v]) => {
    const w = (v / base) * (W - padL * 2);
    const p = { k, v, x, w }; x += w; return p;
  });
  const svg = `
  <svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="各阶段数量去向">
    <text x="${padL}" y="20" font-size="12" fill="#96918a">堆叠占比 · 基数 N=${base}</text>
    <g>${parts.map(p => `
      <rect class="seg" data-k="${p.k}" x="${p.x + 1}" y="${barY}" width="${Math.max(0, p.w - 2)}"
        height="${barH}" rx="4" fill="${COLORS[p.k]}" ></rect>
      ${p.w > 26 ? `<text x="${p.x + p.w / 2}" y="${barY + barH / 2 + 4}" text-anchor="middle"
        font-size="12" font-weight="600" fill="#fff">${p.v}</text>` : ''}`).join('')}</g>
    <line x1="${padL}" y1="${barY + barH + 14}" x2="${W - padL}" y2="${barY + barH + 14}" stroke="#e4e1da"/>
    ${[0, .25, .5, .75, 1].map(f => {
      const xx = padL + f * (W - padL * 2);
      return `<text x="${xx}" y="${barY + barH + 30}" text-anchor="middle" font-size="11" fill="#96918a">${Math.round(f * 100)}%</text>`;
    }).join('')}
    <text x="${W - padL}" y="${H - 6}" text-anchor="end" font-size="11" fill="#96918a">
      健康 ${pct(bal.healthy, base)} · 照蛋损耗 ${bal.candle_loss} 枚</text>
  </svg>`;
  $(sel).innerHTML = svg;
  $('#funnelLegend').innerHTML = parts.map(p =>
    `<span><i style="background:${COLORS[p.k]}"></i>${LABELS[p.k]} ${p.v}</span>`).join('');
  $$('.seg', $(sel)).forEach(r => {
    r.onmousemove = e => {
      const tip = $('#tooltip');
      const part = parts.find(p => p.k === r.dataset.k);
      tip.innerHTML = `<b>${LABELS[r.dataset.k]}</b><br>${part.v} 枚 · 占入孵 ${(part.v / base * 100).toFixed(1)}%`;
      tip.style.opacity = 1;
      tip.style.left = Math.min(e.clientX + 12, innerWidth - 160) + 'px';
      tip.style.top = (e.clientY - 44) + 'px';
    };
    r.onmouseleave = () => $('#tooltip').style.opacity = 0;
  });
}

/* ---------------- 照片放大 ---------------- */
function bindPhotoModal() {
  const cards = $$('.photo-card');
  const bg = $('#modalBg'), box = $('#modalBox');
  cards.forEach((c, i) => c.onclick = () => {
    const group = $$('.photo-card');
    let cur = i;
    const show = () => {
      const g = group[cur];
      box.innerHTML = `<button class="close">×</button>
        <div style="text-align:center">
          <img src="${g.dataset.src}" style="max-width:80vw;max-height:74vh;border-radius:8px">
          <div style="margin-top:10px;color:var(--ink-2)">${esc(g.dataset.kind)}
            <span class="muted">（${cur + 1}/${group.length}，点击两侧箭头切换比对）</span></div>
          <div style="margin-top:10px;display:flex;gap:8px;justify-content:center">
            <button class="ghost small" id="pv">← 上一张</button>
            <button class="ghost small" id="nx">下一张 →</button></div></div>`;
      $('.close', box).onclick = close;
      $('#pv', box).onclick = () => { cur = (cur - 1 + group.length) % group.length; show(); };
      $('#nx', box).onclick = () => { cur = (cur + 1) % group.length; show(); };
    };
    const close = () => bg.classList.remove('show');
    bg.classList.add('show'); bg.onclick = e => { if (e.target === bg) close(); };
    show();
  });
}

/* ---------------- 孵化箱盘位 ---------------- */
async function incubatorsPage() {
  const incs = await api('/api/incubators');
  $('#app').innerHTML = head('孵化箱盘位') + `
    <p class="muted" style="margin:-8px 0 14px">按实际层架展示托盘编号；一台机器的同一槽位至多一盘，停机机器不能转入。</p>
    <div id="flash" class="flash"></div>
    <div class="grid-2">${incs.map(i => `
      <div class="card" style="cursor:pointer" onclick="location.hash='#/incubators/${i.id}'">
        <h3>${esc(i.code)} ${esc(i.name)}
          <span class="pill ${i.status === 'running' ? 'green' : 'red'}">${
            i.status === 'running' ? '运行中' : '停机'}</span></h3>
        <p class="muted">${i.layer_count} 层 × ${i.slot_count} 位 · 点击查看盘位图与温控信号</p>
      </div>`).join('')}</div>`;
}

async function incubatorPage(id) {
  const d = await api(`/api/incubators?id=${id}`);
  const inc = d.incubator;
  const layers = {};
  d.slots.forEach(s => { (layers[s.layer_no] ||= []).push(s); });
  $('#app').innerHTML = `
    <div class="page-head">
      <div><h2>${esc(inc.code)} ${esc(inc.name)}
        <span class="pill ${inc.status === 'running' ? 'green' : 'red'}">${
          inc.status === 'running' ? '运行中' : '停机'}</span></h2>
        <p>盘位图按实际层架渲染（${inc.layer_count} 层 × ${inc.slot_count} 槽）；温控与停机信号来自模拟适配器。</p></div>
      <div style="display:flex;gap:8px">
        ${inc.status === 'running'
          ? `<button class="ghost" id="btnStop" style="color:var(--red)">模拟中途停机</button>`
          : `<button class="green" id="btnResume">模拟恢复供电</button>`}
        <button class="ghost" onclick="history.back()">← 返回</button></div></div>
    <div id="flash" class="flash"></div>

    <div class="grid-2">
      <div class="card"><h3>盘位图（点击托盘可转盘/疏散）</h3>
        <div class="rack">
          <div class="rack-title">${esc(inc.code)} · ${esc(inc.name)}</div>
          <div class="axis">${Array.from({ length: inc.slot_count }, (_, i) =>
            `<span>${i + 1}号位</span>`).join('')}</div>
          ${Object.keys(layers).sort((a, b) => b - a).map(L => `
            <div class="layer">
              <div class="layer-no">${L}层</div>
              <div class="layer-bar">${layers[L].map(s => slotHtml(s, inc.status)).join('')}</div>
            </div>`).join('')}
        </div>
        <div class="legend">
          <span><i style="background:var(--green-tint);border:1px solid var(--green-deep)"></i>有托盘</span>
          <span><i style="background:#fff;border:1.5px dashed #c9c4bc"></i>空槽位</span></div>
      </div>
      <div class="card"><h3>温控曲线（模拟适配器，标准 37.8℃）</h3>
        <div id="tempChart" class="chart-wrap"></div>
        <h3 style="margin-top:18px">信号事件</h3>
        <table><thead><tr><th>时间</th><th>信号</th><th>说明</th></tr></thead><tbody>
        ${d.signals.map(s => `<tr><td class="muted" style="white-space:nowrap">${esc(s.occurred_at.slice(5, 16).replace('T', ' '))}</td>
          <td><span class="pill ${s.kind === 'power_on' ? 'green' : 'red'}">${s.kind === 'power_on' ? '恢复供电' : '断电'}</span></td>
          <td>${esc(s.detail)}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">无异常信号</td></tr>'}
        </tbody></table></div>
    </div>
    <div class="card" id="moveCard" style="display:none"><h3>转盘 / 疏散</h3>
      <div id="moveForm"></div></div>`;

  drawTemp('#tempChart', d.temps);
  $$('.slot.occ').forEach(el => el.onclick = () => openMoveForm(el.dataset, +id, inc.status));
  const bs = $('#btnStop');
  if (bs) bs.onclick = async () => {
    try { await api('/api/incubators/stop', { method: 'POST', body: {
      incubator_id: +id, operator_id: actorId(), detail: '模拟：供电模块告警断电' } });
      flash('停机信号与降温曲线已记录，请将箱内托盘疏散到运行中的机器', true); route();
    } catch (e) { flash(e.message); }
  };
  const br = $('#btnResume');
  if (br) br.onclick = async () => {
    const action = prompt('恢复供电前，填写疏散处置措施（可留空但建议记录）：',
      '箱内托盘已全部疏散并记录，空机试运行确认温度回升。');
    if (action === null) return;
    try { await api('/api/incubators/resume', { method: 'POST', body: {
      incubator_id: +id, operator_id: actorId(), evacuation_action: action } });
      flash('供电恢复，升温曲线已记录', true); route();
    } catch (e) { flash(e.message); }
  };
}

function slotHtml(s, incStatus) {
  if (!s.tray_id) return `<div class="slot" title="空槽位">空</div>`;
  return `<div class="slot occ" data-tray="${s.tray_id}" data-code="${esc(s.tray_code)}"
    data-batch="${esc(s.batch_code)}" data-flock="${esc(s.flock_code)}" data-q="${s.egg_count}"
    data-slot-layer="${s.layer_no}" data-slot-no="${s.slot_no}"
    title="托盘 ${s.tray_code}｜${s.batch_code}｜${s.flock_code}｜${s.egg_count} 枚">
    ${esc(s.tray_code)}<span class="q">${esc(s.batch_code)} · ${s.egg_count}枚</span></div>`;
}

async function openMoveForm(info, fromIncId, fromStatus) {
  const incs = META.incubators.filter(i => i.status === 'running' || i.id === +fromIncId);
  $('#moveCard').style.display = '';
  $('#moveForm').innerHTML = `
    <p>托盘 <b>${info.code}</b>（${info.batch} · ${info.flock} · ${info.q} 枚）当前
    ${info.slotLayer}层${info.slotNo}号。转盘后两名操作员同时抢同一目标槽位时，后提交者会被拒绝。</p>
    <div class="row">
      <div><label>目标孵化箱</label>
        <select id="mv_inc">${incs.map(i =>
          `<option value="${i.id}" ${i.id === fromIncId ? '' : 'selected'}>${i.code} ${esc(i.name)}</option>`).join('')}</select></div>
      <div><label>目标层</label><input id="mv_layer" type="number" min="1" value="3"></div>
      <div><label>目标槽位</label><input id="mv_slot" type="number" min="1" value="1"></div>
      <div><label>原因</label><select id="mv_reason">
        <option value="transfer">转盘</option>
        <option value="evacuation">停机疏散</option>
        <option value="position_correct">移位纠正</option></select></div>
    </div>
    <div style="margin-top:12px"><button class="green" id="mvGo">执行转盘</button>
      <span class="muted" style="margin-left:10px">以操作人 ${esc(actor().name)} 身份提交</span></div>`;
  $('#mvGo').onclick = async () => {
    try {
      await api('/api/move-tray', { method: 'POST', body: {
        tray_id: +info.tray, to_incubator_id: +$('#mv_inc').value,
        layer_no: +$('#mv_layer').value, slot_no: +$('#mv_slot').value,
        operator_id: actorId(), reason: $('#mv_reason').value } });
      flash('转盘成功，流水已记录', true); route();
    } catch (e) { flash(e.message); }
  };
  $('#moveCard').scrollIntoView({ behavior: 'smooth' });
}

/* ---------------- 温控折线 ---------------- */
function drawTemp(sel, temps) {
  if (!temps.length) { $(sel).innerHTML = '<p class="muted">暂无温度采样（停机时将生成降温曲线）</p>'; return; }
  const W = 460, H = 200, L = 38, R = 12, T = 16, B = 30;
  const vals = temps.map(t => t.temp_c);
  const mn = Math.min(...vals, 35.5), mx = Math.max(...vals, 38);
  const x = i => L + i / (temps.length - 1) * (W - L - R);
  const y = v => T + (mx - v) / (mx - mn) * (H - T - B);
  const path = temps.map((t, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(t.temp_c).toFixed(1)}`).join(' ');
  const area = `${path} L${x(temps.length - 1)},${H - B} L${L},${H - B} Z`;
  const stopPts = vals.map((v, i) => v < 36.0 ? i : -1).filter(i => i >= 0);
  $(sel).innerHTML = `
  <svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="孵化箱温度曲线">
    ${[38, 37, 36].map(g => `<g>
      <line x1="${L}" y1="${y(g)}" x2="${W - R}" y2="${y(g)}" stroke="#eeeae3"/>
      <text x="${L - 6}" y="${y(g) + 4}" text-anchor="end" font-size="10" fill="#96918a">${g}℃</text></g>`).join('')}
    <path d="${area}" fill="#7bae8e" opacity="0.12"/>
    <path d="${path}" fill="none" stroke="#4d8068" stroke-width="2"/>
    <line x1="${L}" y1="${y(37.8)}" x2="${W - R}" y2="${y(37.8)}" stroke="#96918a" stroke-dasharray="4 4"/>
    <text x="${W - R}" y="${y(37.8) - 4}" text-anchor="end" font-size="10" fill="#6b6761">设定 37.8℃</text>
    ${stopPts.map(i => `<circle cx="${x(i)}" cy="${y(vals[i])}" r="3.5" fill="#d24530"/>`).join('')}
    <text x="${L}" y="${H - 8}" font-size="10" fill="#96918a">${esc(temps[0].read_at.slice(5, 16).replace('T', ' '))}</text>
    <text x="${W - R}" y="${H - 8}" text-anchor="end" font-size="10" fill="#96918a">${esc(temps.at(-1).read_at.slice(5, 16).replace('T', ' '))}</text>
    <text x="${(L + W - R) / 2}" y="12" text-anchor="middle" font-size="11" fill="#96918a">
      最低 ${Math.min(...vals).toFixed(1)}℃ ${Math.min(...vals) < 36 ? '· 红点=低于 36℃ 受影响区间' : ''}</text>
  </svg>`;
  const tip = $('#tooltip');
  const svg = $(sel + ' svg');
  svg.onmousemove = e => {
    const rect = svg.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width * W;
    const i = Math.max(0, Math.min(temps.length - 1, Math.round((px - L) / (W - L - R) * (temps.length - 1))));
    tip.innerHTML = `<b>${temps[i].temp_c}℃</b><br>${esc(temps[i].read_at.replace('T', ' ').slice(5, 16))}`;
    tip.style.opacity = 1;
    tip.style.left = Math.min(e.clientX + 12, innerWidth - 180) + 'px';
    tip.style.top = (e.clientY - 44) + 'px';
  };
  svg.onmouseleave = () => tip.style.opacity = 0;
}

/* ---------------- 异常移盘总览 ---------------- */
async function movesPage() {
  const batches = await api('/api/batches');
  const rows = batches.flatMap(b => b.id ? [] : []);
  const details = await Promise.all(batches.map(b => api(`/api/batches/${b.id}`)));
  const moves = details.flatMap(d => d.movements.map(m => ({ ...m, batch: d.batch.code })))
    .filter(m => m.reason !== 'set_in').sort((a, b) => b.id - a.id);
  $('#app').innerHTML = head('异常移盘') + `
    <p class="muted" style="margin:-8px 0 14px">停机疏散、托盘移位纠正与跨箱转盘全部留痕；一盘蛋在任一时刻只属于一个槽位。</p>
    <div id="flash" class="flash"></div>
    <div class="card"><h3>移盘流水</h3>
      <table><thead><tr><th>时间</th><th>批次</th><th>托盘</th><th>从</th><th>到</th>
        <th>原因</th><th>操作人</th></tr></thead><tbody>
      ${moves.map(m => `<tr>
        <td class="muted" style="white-space:nowrap">${esc(m.created_at.slice(5, 16).replace('T', ' '))}</td>
        <td><a href="#/batches/${m.batch_id}">${esc(m.batch)}</a></td>
        <td>${esc(m.tray_code)}</td>
        <td>${m.from_inc ? `${esc(m.from_inc)} ${m.fl_layer}层${m.fl_slot}号` : '—'}</td>
        <td>${m.to_inc ? `${esc(m.to_inc)} ${m.tl_layer}层${m.tl_slot}号` : '下架'}</td>
        <td><span class="pill ${m.reason === 'evacuation' ? 'red' : m.reason === 'position_correct' ? 'amber' : 'blue'}">${reasonCn(m.reason)}</span></td>
        <td>${esc(m.operator_name)}</td></tr>`).join('') ||
        '<tr><td colspan="7" class="muted">暂无移盘记录</td></tr>'}</tbody></table></div>
    <div class="card"><h3>发起转盘</h3>
      <p class="muted">在“孵化箱盘位”页点击任意托盘即可转盘到指定层架空槽；目标槽被占用或机器停机时提交会被拒绝。</p>
      <button class="ghost" onclick="location.hash='#/incubators'">前往盘位图 →</button></div>`;
}

/* ---------------- 处置记录 ---------------- */
async function incidentsPage() {
  const list = await api('/api/incidents');
  $('#app').innerHTML = head('处置记录') + `
    <p class="muted" style="margin:-8px 0 14px">混放来源、储存超期、中途停机、照蛋漏盘、托盘移位、破壳延期六类异常均须登记并闭环。</p>
    <div id="flash" class="flash"></div>
    <div class="card"><h3>全部处置单（${list.length}）</h3>
      <table><thead><tr><th>单号</th><th>类型</th><th>标题</th><th>批次/设备</th>
        <th>状态</th><th>报告人</th><th>处置人</th><th>处置措施</th></tr></thead><tbody>
      ${list.map(n => `<tr>
        <td>${esc(n.code)}</td>
        <td><span class="pill ${n.status === 'handled' ? 'green' : 'red'}">${KIND_CN[n.kind]}</span></td>
        <td>${esc(n.title)}</td>
        <td>${[n.batch_code, n.incubator_code].filter(Boolean).map(esc).join(' / ') || '—'}</td>
        <td>${n.status === 'handled' ? '<span class="pill green">已闭环</span>' : '<span class="pill red">待处置</span>'}</td>
        <td>${esc(n.reporter_name || '—')}</td>
        <td>${esc(n.handler_name || '—')}</td>
        <td style="max-width:320px">${n.action ? esc(n.action) : '<span class="muted">—</span>'}</td></tr>`).join('')}
      </tbody></table></div>`;
}

boot();
