const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const S = {
  settings: null,
  defaults: {},
  people: [],
  overview: null,
  auth: null,
  job: null,
  logsLast: 0,
  selectedPeople: new Set(),
  peopleInitialized: false,
  review: [],
  dirCb: null,
  dirPath: "",
  loginTimer: null,
  finishedNotified: false,
  route: "home",
  personName: "",
  siteOpen: new Set(),
  historyOpen: false,
  sr: { models: [], faceModels: [], settings: {}, gpu: {}, loaded: false, target: null, jobState: null, lastPreview: null, busy: false },
};

/* 图片墙分页状态：results（二级页）/ person（人员详情）各一份 */
const Gallery = {
  results: { person: "", status: "saved", framing: "", page: 1, lastPage: 0, loading: false, items: [], total: 0, hasMore: false, sel: new Set() },
  person: { person: "", status: "saved", framing: "", page: 1, lastPage: 0, loading: false, items: [], total: 0, hasMore: false, sel: new Set() },
};

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = (data && (data.detail || data.message)) || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function toast(msg, bad = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("bad", bad);
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), bad ? 4200 : 2600);
}

function num(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function fmtTime(iso) {
  if (!iso) return "—";
  return String(iso).replace("T", " ").slice(0, 16);
}

const FRAMING_CN = {
  full_body: "全身", upper_body_complete: "完整上半身", obscured_or_seated: "坐姿/遮挡",
  face_closeup: "脸部特写", upper_body_incomplete: "上半身不完整", unknown: "未判定",
};

function framingBadge(it) {
  if (!it || !it.framing) return "";
  const cls = it.framing === "full_body" || it.framing === "upper_body_complete" ? "ok"
    : (it.framing === "obscured_or_seated" ? "warn" : (it.framing === "unknown" ? "unknown" : "bad"));
  return `<span class="badge framing ${cls}" title="人物框高度占比 ${num(it.person_ratio)}">${esc(FRAMING_CN[it.framing] || it.framing)}</span>`;
}

/* ---------------- 路由 ---------------- */
const ROUTE_PAGES = ["run", "results", "review", "exclude", "storage", "auth", "settings"];

function parseRoute() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const parts = raw.split("/").filter(Boolean).map(s => {
    try { return decodeURIComponent(s); } catch { return s; }
  });
  if (parts[0] === "person" && parts[1]) return { page: "person", person: parts[1] };
  if (parts[0] && ROUTE_PAGES.includes(parts[0])) return { page: parts[0], person: "" };
  return { page: "home", person: "" };
}

function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function route() {
  const { page, person } = parseRoute();
  S.route = page;
  if (S.lb) closeLightbox();   // 切页时关闭灯箱，避免旧大图浮在页面上
  $$(".page").forEach(p => p.classList.toggle("active", p.id === `page-${page}`));
  $$("#nav [data-route]").forEach(a => {
    const target = a.dataset.route === "#/" ? "home" : a.dataset.route.replace("#/", "");
    a.classList.toggle("active", page === target || (page === "person" && target === "home"));
  });
  closeNav();
  document.querySelector(".sidebar").classList.remove("open");

  if (page === "home") refreshHome();
  else if (page === "person") openPerson(person);
  else if (page === "run") { renderRun(); pollJob(); }
  else if (page === "results") {
    await prepareResultsPage();
    if (S.route === "results") loadGallery("results", true);
  }
  else if (page === "review") refreshReview();
  else if (page === "exclude") refreshExclude();
  else if (page === "storage") refreshStorage();
  else if (page === "auth") refreshAuth();
  else if (page === "settings") refreshSettings();
}

function bindNav() {
  $("#navToggle").addEventListener("click", () => {
    const open = $("#sidebar").classList.toggle("open");
    $("#navMask").classList.toggle("show", open);
    document.body.classList.toggle("nav-open", open);
  });
  $("#navMask").addEventListener("click", closeNav);
  window.addEventListener("hashchange", route);
}

function closeNav() {
  $("#sidebar").classList.remove("open");
  $("#navMask").classList.remove("show");
  document.body.classList.remove("nav-open");
}

/* ---------------- 初始化 ---------------- */
async function init() {
  bindNav();
  bindHome();
  bindPerson();
  bindSettings();
  bindRun();
  bindResultsPage();
  bindReviewPage();
  bindExcludePage();
  bindStoragePage();
  bindAuthPage();
  bindDirModal();
  bindLightbox();
  bindGalleryOps();
  bindSr();
  setupGalleryInfiniteScroll();

  try {
    const st = await api("/api/state");
    if (st.backup) $("#footBackup").textContent = st.backup;
    if (st.browser) $("#footBrowser").textContent =
      `浏览器：${st.browser.channel}${st.browser.playwright_installed ? "" : "（未安装 Playwright）"}`;
  } catch (e) { /* 忽略 */ }

  await Promise.all([refreshPeople(), refreshSettings(), refreshAuth(), refreshExclude()]);
  refreshSrModels();
  route();
  setInterval(pollJob, 1500);
}

/* ---------------- 概览 ---------------- */
function bindHome() {
  $("#quickStart").addEventListener("click", () => startJob([]));
  $("#homeSettings").addEventListener("click", () => navigate("#/settings"));
  $("#homeRunLink").addEventListener("click", () => navigate("#/run"));
  $("#toggleAddPerson").addEventListener("click", () => {
    const form = $("#newPersonForm");
    form.hidden = !form.hidden;
    if (!form.hidden) $("#newPersonName").focus();
  });
  $("#newPersonForm").addEventListener("submit", async ev => {
    ev.preventDefault();
    const name = $("#newPersonName").value.trim();
    if (!name) return;
    try {
      await api("/api/people", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      $("#newPersonName").value = "";
      $("#newPersonForm").hidden = true;
      toast(`已添加 ${name}，点该人员卡片上传参考照`);
      await Promise.all([refreshPeople(), refreshHome()]);
    } catch (e) { toast(e.message, true); }
  });
  $("#homePeople").addEventListener("click", e => {
    const tile = e.target.closest("[data-name]");
    if (tile) navigate(`#/person/${encodeURIComponent(tile.dataset.name)}`);
  });
}

async function refreshHome() {
  try {
    const ov = await api("/api/overview");
    S.overview = ov;
    renderHomeStats(ov);
    renderHomePeople(ov.people || []);
    renderHomeLastRun(ov);
    renderHomeHealth(ov);
    updateReviewBadge(((ov.summary || {}).by_status || {}).review || 0);
  } catch (e) { toast(e.message, true); }
}

function renderHomeStats(ov) {
  const by = (ov.summary || {}).by_status || {};
  const people = ov.people || [];
  const cells = [
    ["成品", by.saved || 0, "#/results"],
    ["待复核", by.review || 0, "#/review"],
    ["人员", people.length, "#/"],
    ["索引记录", (ov.summary || {}).total || 0, "#/exclude"],
  ];
  $("#homeStats").innerHTML = cells.map(([k, v, link]) =>
    `<a class="stat-cell" href="${link}"><b>${num(v)}</b><span>${k}</span></a>`).join("");
}

function renderHomePeople(people) {
  const box = $("#homePeople");
  if (!people.length) {
    box.innerHTML = `<div class="empty">还没有人员。点「新增人员」添加人名，再进入详情上传 5–10 张单人参考照。</div>`;
    return;
  }
  box.innerHTML = people.map(p => {
    const r = p.refs || { total: 0, ok: 0, bad: 0, unchecked: 0 };
    const refText = r.total
      ? `参考照 ${r.total}${r.bad ? ` · <span class="bad">不合格 ${r.bad}</span>` : ""}${r.unchecked ? ` · <span class="warn">未质检 ${r.unchecked}</span>` : ""}`
      : `<span class="bad">还没有参考照</span>`;
    return `<button class="person-tile" data-name="${esc(p.name)}">
      <div class="tile-top"><b>${esc(p.name)}</b><span class="badge">成品 ${num(p.saved)}</span></div>
      <div class="tile-meta">待复核 ${num(p.review)} · 丢弃记录 ${num(p.rejected)}</div>
      <div class="tile-meta">${refText}</div>
      <div class="tile-meta muted">最近运行：${fmtTime(p.last_run)}</div>
    </button>`;
  }).join("");
}

function renderHomeLastRun(ov) {
  const job = ov.job || {};
  const box = $("#homeLastRun");
  if (job.state && job.state !== "idle") {
    const totals = (job.report && job.report.totals) || job.counts || {};
    box.innerHTML = `<div class="run-top">
      <span class="pill ${esc(job.state)}">${esc(STATE_TEXT[job.state] || job.state)}</span>
      <span class="muted">${job.person ? `当前：${esc(job.person)}` : ""}</span>
      <span class="muted">${job.elapsed ? `耗时 ${num(job.elapsed)}s` : ""}</span>
    </div>
    <div class="count-grid">${[
      ["收录", totals.saved_added], ["复核", totals.review_added],
      ["下载前拦截", totals.pre_blocked], ["下载后判重", totals.post_duplicates],
      ["重复(合计)", totals.duplicates], ["排除", totals.excluded],
      ["拒绝", totals.rejected], ["失败", totals.failed],
    ].map(([k, v]) => `<div class="count-cell"><b>${num(v)}</b><span>${k}</span></div>`).join("")}</div>`;
    return;
  }
  const people = ov.people || [];
  const latest = people.map(p => p.last_run).filter(Boolean).sort().pop() || "";
  box.innerHTML = latest
    ? `<p class="muted">本次服务还没有运行记录。索引中最近的活动时间：<b>${fmtTime(latest)}</b></p>`
    : `<p class="muted">还没有运行记录。设置好人员与站点后点右上角「开始任务」。</p>`;
}

function renderHomeHealth(ov) {
  const info = ov.browser || {};
  const sites = (ov.auth && ov.auth.sites) || {};
  const siteText = ["xiaohongshu", "weibo"].map(s => {
    const name = s === "xiaohongshu" ? "小红书" : "微博";
    const on = !!(sites[s] || {}).enabled;
    return `${name} ${on ? '<span class="ok">启用</span>' : '<span class="muted">停用</span>'}`;
  }).join(" · ");
  const idx = ov.index || {};
  const total = (ov.summary || {}).total || 0;
  $("#homeHealth").innerHTML = `
    <dt>索引库</dt><dd class="mono">${esc(idx.path || "")} ${idx.exists ? `<span class="ok">（${total} 条记录）</span>` : '<span class="bad">（不存在）</span>'}</dd>
    <dt>浏览器隔离</dt><dd class="${info.profile_isolated ? "ok" : "bad"}">${info.profile_isolated ? "已使用项目独立 profile，未占用系统默认 profile" : "危险：占用了系统默认 profile"}</dd>
    <dt>持久会话目录</dt><dd class="mono">${esc(info.profile_dir || "")}</dd>
    <dt>站点开关</dt><dd>${siteText}</dd>
    <dt>备份</dt><dd>${esc(ov.backup || "无")}</dd>`;
}

/* ---------------- 人员详情 ---------------- */
function bindPerson() {
  $("#personBack").addEventListener("click", () => navigate("#/"));
  $("#personDrop").addEventListener("click", () => $("#personFile").click());
  $("#personFile").addEventListener("change", () => {
    const files = Array.from($("#personFile").files || []);
    $("#personFile").value = "";
    if (files.length) uploadPersonRefs(files);
  });
  ["dragenter", "dragover"].forEach(ev => $("#personDrop").addEventListener(ev, e => {
    e.preventDefault(); $("#personDrop").classList.add("over");
  }));
  ["dragleave", "drop"].forEach(ev => $("#personDrop").addEventListener(ev, e => {
    e.preventDefault(); $("#personDrop").classList.remove("over");
  }));
  $("#personDrop").addEventListener("drop", e => {
    const files = Array.from(e.dataTransfer.files || []).filter(f => f.type.startsWith("image/"));
    if (files.length) uploadPersonRefs(files);
  });

  $("#personRefs").addEventListener("click", async e => {
    const btn = e.target.closest("[data-del]");
    if (!btn) return;
    if (!confirm("移除这张参考照？")) return;
    try {
      await api(`/api/ref?path=${encodeURIComponent(btn.dataset.del)}`, { method: "DELETE" });
      await openPerson(S.personName, true);
    } catch (err) { toast(err.message, true); }
  });

  $("#personSave").addEventListener("click", savePersonFields);
  $("#personCheckRefs").addEventListener("click", async () => {
    toast(`正在质检 ${S.personName} 的参考照…`);
    try {
      await api(`/api/people/${encodeURIComponent(S.personName)}/check`, { method: "POST" });
      await openPerson(S.personName, true);
      toast("质检完成");
    } catch (e) { toast(e.message, true); }
  });
  $("#personExport").addEventListener("click", () => exportPerson(S.personName));
  $("#personCalibrate").addEventListener("click", () => calibratePerson(S.personName));
  $("#personRerun").addEventListener("click", () => rerunPerson(S.personName));
  $("#personSrBatch").addEventListener("click", () => openSrDialog([], S.personName, null));
  $("#personSrReplace").addEventListener("click", () => quickSr("backup", [], S.personName));
  $("#personSrOverwrite").addEventListener("click", () => quickSr("overwrite", [], S.personName));

  $("#personGalleryStatus").addEventListener("change", () => {
    Gallery.person.status = $("#personGalleryStatus").value;
    loadGallery("person", true);
  });
  $("#personFraming").addEventListener("change", () => {
    Gallery.person.framing = $("#personFraming").value;
    loadGallery("person", true);
  });
  $("#personMore").addEventListener("click", () => loadGallery("person", false));
}

async function openPerson(name, keepScroll = false) {
  if (!name) { navigate("#/"); return; }
  if (name !== S.personName) {
    $("#personFraming").value = "";
    $("#personGalleryStatus").value = "saved";
  }
  S.personName = name;
  Gallery.person = { person: name, status: $("#personGalleryStatus").value,
                     framing: $("#personFraming").value, page: 1, lastPage: 0,
                     loading: false, items: [], total: 0, hasMore: false, sel: new Set() };
  try {
    const data = await api(`/api/people/${encodeURIComponent(name)}/detail`);
    renderPerson(data);
    await loadGallery("person", true);
    if (!keepScroll) window.scrollTo(0, 0);
  } catch (e) {
    toast(e.message, true);
    $("#personName").textContent = name;
    $("#personSub").textContent = "加载失败";
  }
}

function renderPerson(data) {
  const r = data.refs || [];
  const ok = r.filter(x => x.checked && x.ok).length;
  const bad = r.filter(x => x.checked && !x.ok).length;
  const unchecked = r.filter(x => !x.checked).length;
  $("#personName").textContent = data.name;
  $("#personSub").textContent = `成品 ${num(data.saved)} · 待复核 ${num(data.review)} · 最近运行 ${fmtTime(data.last_run)}`;
  $("#personRefSummary").innerHTML = r.length
    ? `共 ${r.length} 张 · <span class="ok">合格 ${ok}</span>${bad ? ` · <span class="bad">不合格 ${bad}</span>` : ""}${unchecked ? ` · <span class="warn">未质检 ${unchecked}</span>` : ""}`
    : `<span class="bad">还没有参考照</span>`;

  $("#personRefs").innerHTML = r.map(refHtml).join("") ||
    `<div class="empty" style="grid-column:1/-1">暂无参考照，拖拽上传 5–10 张单人正面照</div>`;

  $$("#personFields [data-f]").forEach(inp => {
    const key = inp.dataset.f;
    if (key === "queries") inp.value = (data.queries || []).join(", ");
    else inp.value = data[key];
  });

  $("#personLogs").textContent = (data.logs || []).join("\n")
    || "暂无该人运行日志（data/logs 下没有对应事件文件，运行任务后会生成）。";
}

function refHtml(ref) {
  const known = ref.checked === true;
  const ok = ref.ok === true;
  const cls = known ? (ok ? "ok" : "bad") : "";
  const tag = !known ? "未质检" : (ok ? "合格" : "不合格");
  const tagCls = !known ? "unknown" : (ok ? "ok" : "bad");
  const issue = (ref.issues || []).map(i => i.text).join("；");
  const detail = `${ref.width}×${ref.height}${ref.n_faces ? ` · 人脸 ${ref.face_px}px` : ""}`;
  return `<div class="ref ${cls}" title="${esc(issue || detail)}">
    <img src="${esc(ref.url)}" loading="lazy" alt="">
    <span class="ref-tag ${tagCls}">${tag}</span>
    <button class="ref-del" data-del="${esc(ref.path)}" title="移除">×</button>
    <div class="ref-meta">${esc(ref.name)}<br>${esc(detail)}</div>
  </div>`;
}

async function uploadPersonRefs(files) {
  const fd = new FormData();
  files.forEach(f => fd.append("files", f, f.name));
  toast(`正在上传并质检 ${files.length} 张…`);
  try {
    const res = await api(`/api/people/${encodeURIComponent(S.personName)}/references`, { method: "POST", body: fd });
    const ok = (res.results || []).filter(x => x.ok).length;
    const bad = (res.results || []).length - ok;
    toast(`上传完成：合格 ${ok} 张${bad ? `，不合格 ${bad} 张（已标红）` : ""}`, bad > 0);
    await openPerson(S.personName, true);
    refreshHome();
  } catch (e) { toast(e.message, true); }
}

async function savePersonFields() {
  const body = {
    queries: $('[data-f="queries"]', $("#personFields")).value,
    count: num($('[data-f="count"]', $("#personFields")).value, S.defaults.count || 5),
    accept_threshold: num($('[data-f="accept_threshold"]', $("#personFields")).value, 0.55),
    review_threshold: num($('[data-f="review_threshold"]', $("#personFields")).value, 0.35),
  };
  try {
    await api(`/api/people/${encodeURIComponent(S.personName)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    toast(`${S.personName} 的设置已保存`);
    await Promise.all([openPerson(S.personName, true), refreshPeople()]);
  } catch (e) { toast(e.message, true); }
}

async function exportPerson(person) {
  try {
    const res = await api("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ person }),
    });
    toast(`已生成总览：${res.path}`);
    window.open(res.url, "_blank");
  } catch (e) { toast(e.message, true); }
}

async function calibratePerson(person) {
  try {
    const res = await api(`/api/calibrate?person=${encodeURIComponent(person)}&limit=100`);
    const s = res.suggestion;
    toast(`建议 A=${s.accept_threshold} B=${s.review_threshold}（正 ${s.positive_n}/负 ${s.negative_n}）CSV：${res.csv_path}`);
  } catch (e) { toast(e.message, true); }
}

async function rerunPerson(person) {
  try {
    await api("/api/review/rerun", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ person }),
    });
    S.finishedNotified = false;
    S.logsLast = 0;
    $("#logBox").textContent = "";
    navigate("#/run");
    toast(`已启动 ${person} 的重跑任务`);
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 人员列表 / 设置 ---------------- */
async function refreshPeople() {
  try {
    const data = await api("/api/people");
    S.people = data.people || [];
    S.defaults = data.defaults || {};
    if (!S.peopleInitialized) {
      S.people.forEach(p => S.selectedPeople.add(p.name));
      S.peopleInitialized = true;
    }
    renderTaskPeople();
  } catch (e) { /* 忽略 */ }
}

function bindSettings() {
  $("#saveSettingsBtn").addEventListener("click", () => saveTaskSettings());
  $("#startJobBtn").addEventListener("click", () => startJob(Array.from(S.selectedPeople)));
  $("#addExcludeDir").addEventListener("click", () => addExcludeDir());
  $$("[data-dir-target]").forEach(btn => btn.addEventListener("click", () => {
    openDirPicker(path => { $(`#${btn.dataset.dirTarget}`).value = path; }, $(`#${btn.dataset.dirTarget}`).value);
  }));
  $("#taskPeople").addEventListener("click", e => {
    if (e.target.id === "selectAllPeople") {
      if (S.selectedPeople.size === S.people.length) S.selectedPeople.clear();
      else S.people.forEach(p => S.selectedPeople.add(p.name));
      renderTaskPeople();
    }
  });
  $("#restartSvc").addEventListener("click", () => restartService());
  $("#quitSvc").addEventListener("click", () => quitProgram());
}

/* ---------------- 服务管理（重启 / 退出程序） ---------------- */
async function refreshServiceState() {
  const el = $("#svcMode");
  if (!el) return;
  try {
    const st = await api("/api/admin/status");
    S.admin = st;
    el.textContent = st.managed
      ? `托盘托管 · 服务 PID ${st.pid} · 宿主 PID ${st.supervisor_pid}${st.busy ? " · 有任务在运行" : ""}`
      : "无宿主（--no-tray 或直接运行 facefind ui），页面无法自动重启";
    $("#restartSvc").disabled = !st.managed;
    $("#quitSvc").disabled = !st.managed;
  } catch (e) {
    el.textContent = "状态未知";
  }
}

function svcOverlay(title, text) {
  let el = $("#svcOverlay");
  if (!el) {
    el = document.createElement("div");
    el.id = "svcOverlay";
    el.className = "svc-overlay";
    el.innerHTML = `<div class="svc-card"><h2></h2><p></p><span class="svc-spin"></span></div>`;
    document.body.appendChild(el);
  }
  el.querySelector("h2").textContent = title;
  el.querySelector("p").textContent = text;
  el.hidden = false;
}

function hideSvcOverlay() {
  const el = $("#svcOverlay");
  if (el) el.hidden = true;
}

async function waitServiceBack(beforePid) {
  const deadline = Date.now() + 45000;
  await new Promise(r => setTimeout(r, 1200));   // 先让旧进程真正退出
  while (Date.now() < deadline) {
    try {
      const st = await api("/api/admin/status");
      if (st && st.managed && st.pid && st.pid !== beforePid) {
        hideSvcOverlay();
        toast("服务已恢复");
        route();
        return true;
      }
    } catch (e) { /* 服务尚未起来，继续等 */ }
    const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    svcOverlay("服务正在重启…", `约 ${left} 秒后自动重连，请不要关闭页面`);
    await new Promise(r => setTimeout(r, 1000));
  }
  hideSvcOverlay();
  toast("等待超时：服务未恢复，请查看托盘状态或 data/logs/supervisor_*.log", true);
  return false;
}

async function restartService() {
  const st = S.admin || await api("/api/admin/status");
  if (!st.managed) { toast("当前为无宿主模式，无法自动重启", true); return; }
  const warn = st.busy
    ? `\n\n警告：当前有任务正在运行（${st.job_state || "运行中"}），重启会立即中断它，未完成的候选会丢失。`
    : "";
  const yes = await lbConfirm("重启服务？", `重启会断开本页几秒，服务恢复后自动重连。${warn}`, "立即重启");
  if (!yes) return;
  svcOverlay("服务正在重启…", "正在停止旧进程…");
  try {
    await api("/api/admin/restart", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: true }),
    });
  } catch (e) { hideSvcOverlay(); toast(e.message, true); return; }
  await waitServiceBack(st.pid);
}

async function quitProgram() {
  const st = S.admin || await api("/api/admin/status");
  if (!st.managed) { toast("当前为无宿主模式，页面无法退出程序", true); return; }
  const warn = st.busy
    ? `\n\n警告：当前有任务正在运行（${st.job_state || "运行中"}），退出会立即中断它，未完成的候选会丢失。`
    : "";
  const yes = await lbConfirm("退出程序？",
    `将结束 UI 服务与托盘宿主，之后需重新双击 start_ui.bat 才能再启动。${warn}`, "确认退出");
  if (!yes) return;
  try {
    await api("/api/admin/shutdown", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: true }),
    });
  } catch (e) { toast(e.message, true); return; }
  svcOverlay("程序已退出", "可以关闭此页面；再次启动请双击 start_ui.bat");
  const spin = $("#svcOverlay") && $("#svcOverlay").querySelector(".svc-spin");
  if (spin) spin.remove();
}

async function refreshSettings() {
  try {
    S.settings = await api("/api/settings");
    renderSettings();
    renderExcludeDirs();
    renderTaskPeople();
    refreshServiceState();
  } catch (e) { toast(e.message, true); }
}

function renderSettings() {
  const s = S.settings;
  if (!s) return;
  $$("#sourceChips input").forEach(c => { c.checked = (s.sources || []).includes(c.value); });
  $("#acceptThreshold").value = s.accept_threshold;
  $("#reviewThreshold").value = s.review_threshold;
  $("#taskCount").value = s.count;
  $("#minFacePx").value = s.min_face_px;
  $("#maxPages").value = s.max_pages;
  $("#detailBudget").value = s.detail_budget;
  $("#reviewEnabled").checked = !!s.review_enabled;
  $("#modelProfile").value = s.profile || "balanced";
  $("#faceModel").value = s.face_model || "";
  $("#framingModel").value = s.framing_model || "";
  $("#framingMode").value = s.framing_mode || "prefer_full_body";
  $("#edgeCutTolerance").value = s.framing_edge_cut_tolerance;
  $("#minPersonRatio").value = s.framing_min_person_ratio;
  $("#outputDir").value = s.output_dir || "";
  $("#proxy").value = s.proxy || "";
  if ($("#excludeScanOnRun")) $("#excludeScanOnRun").value = s.exclude_scan_on_run || "auto";
  const sr = s.sr || {};
  $("#srModel").value = sr.model || "realesrgan_x4plus";
  $("#srScale").value = String(sr.scale || 4);
  $("#srFace").value = sr.face_restore || "gfpgan";
  $("#srFaceStrength").value = sr.face_strength != null ? sr.face_strength : 0.8;
  $("#srMaxSide").value = sr.max_side != null ? sr.max_side : 4096;
  $("#srTile").value = sr.tile || 0;
  $("#srQuality").value = sr.jpeg_quality || 95;
  $("#srKeepOriginal").checked = true;
  renderSrModelStatus();
  applySourceAvailability(S.auth);
}

function gatherSettings() {
  return {
    sources: $$("#sourceChips input:checked").map(c => c.value),
    accept_threshold: num($("#acceptThreshold").value, 0.55),
    review_threshold: num($("#reviewThreshold").value, 0.35),
    count: num($("#taskCount").value, 5),
    min_face_px: num($("#minFacePx").value, 80),
    max_pages: num($("#maxPages").value, 8),
    detail_budget: num($("#detailBudget").value, 30),
    review_enabled: $("#reviewEnabled").checked,
    profile: $("#modelProfile").value,
    face_model: $("#faceModel").value.trim(),
    framing_model: $("#framingModel").value.trim(),
    framing_mode: $("#framingMode").value,
    framing_edge_cut_tolerance: num($("#edgeCutTolerance").value, 0.03),
    framing_min_person_ratio: num($("#minPersonRatio").value, 0.45),
    output_dir: $("#outputDir").value.trim() || "downloads",
    proxy: $("#proxy").value.trim(),
    exclude_dirs: (S.settings && S.settings.exclude_dirs) || [],
    exclude_files: (S.settings && S.settings.exclude_files) || [],
    exclude_phash_threshold: (S.settings && S.settings.exclude_phash_threshold) || 8,
    exclude_scan_on_run: ($("#excludeScanOnRun") && $("#excludeScanOnRun").value) || "auto",
    sr: {
      model: $("#srModel").value,
      scale: num($("#srScale").value, 4),
      face_restore: $("#srFace").value,
      face_strength: num($("#srFaceStrength").value, 0.8),
      max_side: num($("#srMaxSide").value, 4096),
      tile: num($("#srTile").value, 0),
      jpeg_quality: num($("#srQuality").value, 95),
      keep_original: true,
    },
  };
}

async function saveTaskSettings(silent = false) {
  try {
    S.settings = await api("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(gatherSettings()),
    });
    renderSettings();
    renderExcludeDirs();
    renderExcludePage(S.excludeData);
    if (!silent) toast("配置已保存");
  } catch (e) { toast(e.message, true); }
}

function renderTaskPeople() {
  const box = $("#taskPeople");
  if (!box) return;
  if (!S.people.length) {
    box.innerHTML = `<span class="muted">暂无人员，请先在「概览」新增人员。</span>`;
    return;
  }
  box.innerHTML = S.people.map(p => `<label class="chip">
    <input type="checkbox" value="${esc(p.name)}" ${S.selectedPeople.has(p.name) ? "checked" : ""}> ${esc(p.name)}
  </label>`).join("") + `<button id="selectAllPeople" class="ghost">全选/全不选</button>`;
  $$("#taskPeople input").forEach(cb => cb.addEventListener("change", () => {
    if (cb.checked) S.selectedPeople.add(cb.value); else S.selectedPeople.delete(cb.value);
  }));
}

function renderExcludeDirs() {
  const box = $("#excludeDirList");
  const dirs = (S.settings && S.settings.exclude_dirs) || [];
  box.innerHTML = dirs.map(d => `<div class="path-item">
    <span class="p" title="${esc(d)}">${esc(d)}</span>
    <button class="ghost" data-remove-dir="${esc(d)}">移除</button>
  </div>`).join("") || `<span class="muted">未配置</span>`;
  $$("[data-remove-dir]", box).forEach(btn => btn.addEventListener("click", () => {
    removeExcludeEntry("dir", btn.dataset.removeDir);
  }));
}

async function removeExcludeEntry(kind, value) {
  const label = kind === "dir" ? "排除目录" : "排除文件";
  const shown = S.excludeData
    ? ((S.excludeData.roots || []).find(r => (r.raw || r.path) === value) || {}).path
    : "";
  if (!confirm(
    `移除${label}？\n\n${shown || value}\n\n` +
    "只从配置与排除索引里取消引用，不会删除磁盘上的目录或文件。\n" +
    "移除后下一次任务不再排除它。"
  )) return;
  try {
    const res = await api("/api/exclude/remove", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, value }),
    });
    if (S.settings) {
      if (kind === "dir") S.settings.exclude_dirs = res.exclude_dirs || [];
      else S.settings.exclude_files = res.exclude_files || [];
    }
    renderExcludeDirs();
    toast(`已移除${label}：${value}（磁盘文件未改动）`);
    refreshExclude();
  } catch (e) { toast(e.message, true); }
}

async function addExcludeDir() {
  openDirPicker(async path => {
    const dirs = new Set((S.settings.exclude_dirs || []).concat([path]));
    S.settings.exclude_dirs = Array.from(dirs);
    await saveTaskSettings(true);
    toast(`已添加排除目录：${path}`);
    refreshExclude();
  });
}

async function startJob(names) {
  if (!S.settings) await refreshSettings();
  const body = gatherSettings();
  body.names = names && names.length ? names : null;
  if (!body.sources.length) { toast("请至少选择一个站点", true); return; }
  try {
    await saveTaskSettings(true);
    const job = await api("/api/job/start", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    S.job = job;
    S.logsLast = 0;
    $("#logBox").textContent = "";
    S.finishedNotified = false;
    navigate("#/run");
    toast("任务已启动");
    pollJob();
  } catch (e) { toast(e.message, true); }
}

function applySourceAvailability(auth) {
  const sites = (auth && auth.sites) || {};
  $$("#sourceChips input").forEach(cb => {
    const site = cb.value;
    if (site === "baidu") return;
    const on = !!((sites[site] || {}).enabled);
    cb.disabled = !on;
    const label = cb.closest("label");
    if (label) label.classList.toggle("disabled", !on);
    if (!on) cb.checked = false;
  });
}

/* ---------------- 运行 ---------------- */
const STATE_TEXT = {
  idle: "空闲", running: "运行中", paused: "已暂停", stopping: "停止中",
  finished: "已完成", stopped: "已停止", error: "出错",
};

function bindRun() {
  $("#pauseBtn").addEventListener("click", () => api("/api/job/pause", { method: "POST" }).then(renderRun));
  $("#resumeBtn").addEventListener("click", () => api("/api/job/resume", { method: "POST" }).then(renderRun));
  $("#stopBtn").addEventListener("click", () => api("/api/job/stop", { method: "POST" }).then(renderRun));
  $("#clearLogBtn").addEventListener("click", () => { $("#logBox").textContent = ""; });
  $("#toggleHistory").addEventListener("click", () => {
    S.historyOpen = !S.historyOpen;
    $("#runHistory").hidden = !S.historyOpen;
    $("#toggleHistory").textContent = S.historyOpen ? "收起" : "展开";
    if (S.historyOpen) loadRunHistory();
  });
  $("#siteCards").addEventListener("click", e => {
    const btn = e.target.closest("[data-site-toggle]");
    if (!btn) return;
    const key = btn.dataset.siteToggle;
    if (S.siteOpen.has(key)) S.siteOpen.delete(key); else S.siteOpen.add(key);
    renderSites(S.job || {});
  });
}

function renderRun() {
  const job = S.job || { state: "idle", people: {}, counts: {} };
  const state = job.state || "idle";
  const pill = $("#jobState");
  pill.textContent = STATE_TEXT[state] || state;
  pill.className = `pill ${state}`;
  $("#jobPerson").textContent = job.person ? `当前：${job.person}` : "";
  $("#jobElapsed").textContent = job.elapsed ? `耗时 ${job.elapsed}s` : "";

  const slot = (job.people || {})[job.person] || null;
  const saved = slot ? num(slot.saved) : 0;
  const target = slot ? num(slot.target, 0) : 0;
  const pct = target > 0 ? Math.min(100, saved / target * 100) : (state === "finished" ? 100 : 0);
  $("#jobBar").style.width = `${pct}%`;

  const totals = job.report && job.report.totals ? job.report.totals : (job.counts || {});
  const cells = [
    ["收录", totals.saved_added ?? saved], ["复核", totals.review_added ?? 0],
    ["下载前拦截", totals.pre_blocked ?? 0], ["下载后判重", totals.post_duplicates ?? 0],
    ["重复(合计)", totals.duplicates ?? 0], ["排除", totals.excluded ?? 0],
    ["拒绝", totals.rejected ?? 0], ["失败", totals.failed ?? 0],
  ];
  $("#jobCounts").innerHTML = cells.map(([k, v]) => `<div class="count-cell"><b>${num(v)}</b><span>${k}</span></div>`).join("");
  $("#jobError").textContent = job.error || "";
  $("#startJobBtn").disabled = state === "running" || state === "paused" || state === "stopping";
  $("#pauseBtn").disabled = !(state === "running");
  $("#resumeBtn").disabled = !(state === "paused");
  $("#stopBtn").disabled = !(state === "running" || state === "paused" || state === "stopping");
  renderSites(job);
}

const SITE_STATE_TEXT = {
  running: "运行中", done: "已完成", skipped: "已跳过", disabled: "未启用",
  not_selected: "未选择", auth: "未登录", risk: "风控停止", error: "出错",
  stopped: "已停止", pending: "等待中", idle: "空闲",
};

const SITE_REJECT_KEYS = [
  ["framing", "构图不合格"], ["low_similarity", "相似度低"], ["face_too_small", "人脸过小/无脸"],
  ["blocked", "屏蔽"], ["duplicate", "重复"], ["excluded", "排除目录"], ["other", "其他"],
];

function renderSites(job) {
  const sites = (job && job.sites) || [];
  const p = (job && job.progress) || {};
  $("#siteOverallText").textContent = p.target
    ? `${num(p.saved)} / ${num(p.target)}（${num(p.pct)}%）`
    : `${num(p.saved)} / —`;
  $("#siteOverallBar").style.width = `${Math.min(100, num(p.pct))}%`;
  $("#siteUpdated").textContent = job && job.state !== "idle"
    ? `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`
    : "";
  if (!sites.length) {
    $("#siteCards").innerHTML = `<div class="empty">尚未启动任务。启动后按站点实时显示候选数、收录与判掉原因。</div>`;
    return;
  }
  $("#siteCards").innerHTML = sites.map(siteCard).join("");
}

function siteCard(s) {
  const state = s.status || "pending";
  const rejects = s.rejects || {};
  const people = s.people || [];
  const open = S.siteOpen.has(s.site);
  const pageText = s.pages ? `${num(s.page)}/${num(s.pages)}` : (s.page ? `${num(s.page)}` : "—");
  const pageLabel = s.unit === "round" ? "滚动" : "页";
  const rejectText = SITE_REJECT_KEYS.map(([k, label]) => `${label} <b>${num(rejects[k])}</b>`).join(" · ");
  return `<div class="site-card">
    <div class="site-head">
      <div class="site-title">
        <b>${esc(s.label || s.site)}</b>
        <span class="site-badge ${esc(state)}">${esc(SITE_STATE_TEXT[state] || state)}</span>
        ${s.status_reason ? `<span class="site-reason">${esc(s.status_reason)}</span>` : ""}
      </div>
      ${people.length ? `<button class="ghost" data-site-toggle="${esc(s.site)}">${open ? "收起人员" : "人员明细"}</button>` : ""}
    </div>
    <div class="site-metrics">
      <span>候选 <b>${num(s.candidates)}</b></span>
      <span>已处理 <b>${num(s.processed)}</b></span>
      <span>下载成功 <b>${num(s.downloaded)}</b></span>
      <span>下载前拦截 <b class="ok">${num(s.pre_blocked)}</b></span>
      <span>下载后判重 <b>${num(s.post_duplicates)}</b></span>
      <span>收录（本次） <b class="ok">${num(s.saved)}</b></span>
      <span>手动收录 <b class="ok">${num(s.manual)}</b></span>
      <span>待复核 <b>${num(s.review)}</b></span>
      <span>判掉 <b>${num(s.rejected)}</b></span>
      <span>失败 <b class="${num(s.failed) ? "bad" : ""}">${num(s.failed)}</b></span>
      <span>耗时 <b>${num(s.elapsed).toFixed(1)}s</b></span>
      <span>${pageLabel} <b>${esc(pageText)}</b></span>
    </div>
    <div class="site-rejects">${rejectText}</div>
    ${open ? `<div class="table-wrap mt"><table class="table">
      <thead><tr><th>人员</th><th>状态</th><th>候选</th><th>已处理</th><th>下载成功</th><th>收录</th><th>待复核</th><th>判掉</th><th>失败</th><th>耗时</th><th>${s.unit === "round" ? "滚动" : "页"}</th></tr></thead>
      <tbody>${people.map(sitePersonRow).join("")}</tbody>
    </table></div>` : ""}
  </div>`;
}

function sitePersonRow(row) {
  const state = row.status || "pending";
  const reason = row.status_reason ? ` title="${esc(row.status_reason)}"` : "";
  const pageText = row.pages ? `${num(row.page)}/${num(row.pages)}` : (row.page ? `${num(row.page)}` : "—");
  return `<tr>
    <td>${esc(row.person || "")}</td>
    <td><span class="site-badge ${esc(state)}"${reason}>${esc(SITE_STATE_TEXT[state] || state)}</span></td>
    <td>${num(row.candidates)}</td><td>${num(row.processed)}</td><td>${num(row.downloaded)}</td>
    <td>${num(row.saved)}</td><td>${num(row.review)}</td><td>${num(row.rejected)}</td>
    <td>${num(row.failed)}</td><td>${num(row.elapsed).toFixed(1)}s</td><td>${esc(pageText)}</td>
  </tr>`;
}

async function loadRunHistory() {
  try {
    const data = await api("/api/job/history?limit=10");
    renderRunHistory(data.runs || []);
  } catch (e) {
    $("#runHistory").innerHTML = `<div class="empty">读取历史失败：${esc(e.message)}</div>`;
  }
}

function renderRunHistory(runs) {
  if (!runs.length) {
    $("#runHistory").innerHTML = `<div class="empty">还没有完成运行的站点汇总。完成一次任务后这里会按运行展示。</div>`;
    return;
  }
  $("#runHistory").innerHTML = runs.map(run => `<details class="run-history-item">
    <summary><b>${fmtTime(run.finished_at || run.started_at)}</b>
      <span class="muted">人员 ${esc((run.people || []).join("、") || "—")} · 耗时 ${num(run.elapsed).toFixed(1)}s</span>
    </summary>
    <div class="table-wrap mt"><table class="table">
      <thead><tr><th>站点</th><th>状态</th><th>候选</th><th>已处理</th><th>下载成功</th><th>收录</th><th>待复核</th><th>判掉</th><th>构图</th><th>相似度</th><th>人脸过小</th><th>屏蔽</th><th>重复</th><th>排除目录</th><th>失败</th></tr></thead>
      <tbody>${(run.sites || []).map(s => {
        const rejects = s.rejects || {};
        return `<tr>
          <td>${esc(s.label || s.site)}</td>
          <td><span class="site-badge ${esc(s.status || "pending")}">${esc(SITE_STATE_TEXT[s.status] || s.status || "—")}</span></td>
          <td>${num(s.candidates)}</td><td>${num(s.processed)}</td><td>${num(s.downloaded)}</td>
          <td>${num(s.saved)}</td><td>${num(s.review)}</td><td>${num(s.rejected)}</td>
          <td>${num(rejects.framing)}</td><td>${num(rejects.low_similarity)}</td><td>${num(rejects.face_too_small)}</td>
          <td>${num(rejects.blocked)}</td><td>${num(rejects.duplicate)}</td><td>${num(rejects.excluded)}</td>
          <td>${num(s.failed)}</td>
        </tr>`;
      }).join("")}</tbody>
    </table></div>
  </details>`).join("");
}

async function pollJob() {
  try {
    const job = await api("/api/job");
    const prev = S.job;
    S.job = job;
    renderRun();
    await pollLogs(job);
    const doneStates = ["finished", "stopped", "error"];
    if (doneStates.includes(job.state) && prev && !doneStates.includes(prev.state) && !S.finishedNotified) {
      S.finishedNotified = true;
      const totals = job.report && job.report.totals ? job.report.totals : {};
      toast(`任务结束：收录 ${totals.saved_added || 0}，复核 ${totals.review_added || 0}，重复 ${totals.duplicates || 0}`);
      refreshPeople();
      refreshHome();
      if (S.route === "results") loadGallery("results", true);
      if (S.route === "person") openPerson(S.personName, true);
      if (S.route === "review") refreshReview();
      if (S.historyOpen) loadRunHistory();
    }
    if (job.state === "idle" && !job.busy) S.finishedNotified = true;
  } catch (e) { /* 服务可能已停止 */ }
}

async function pollLogs(job) {
  if (!job || (job.state === "idle" && !job.busy && S.logsLast === 0)) return;
  try {
    const data = await api(`/api/job/logs?since=${S.logsLast}`);
    if (data.items && data.items.length) {
      const box = $("#logBox");
      const nearBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
      box.textContent += data.items.map(x => `[${x.time}] ${x.msg}`).join("\n") + "\n";
      if (nearBottom) box.scrollTop = box.scrollHeight;
      if (box.textContent.length > 200000) box.textContent = box.textContent.slice(-150000);
    }
    S.logsLast = data.last || S.logsLast;
  } catch (e) { /* ignore */ }
}

function updateReviewBadge(n) {
  $("#reviewCount").textContent = n ? `(${n})` : "";
}

/* ---------------- 图片墙（分页 + 缩略图） ---------------- */
function bindResultsPage() {
  $("#refreshResults").addEventListener("click", async () => {
    await refreshHome();
    await prepareResultsPage();
    loadGallery("results", true);
  });
  $("#resultPerson").addEventListener("change", () => {
    Gallery.results.person = $("#resultPerson").value;
    loadGallery("results", true);
  });
  $("#galleryStatus").addEventListener("change", () => {
    Gallery.results.status = $("#galleryStatus").value;
    loadGallery("results", true);
  });
  $("#framingFilter").addEventListener("change", () => {
    Gallery.results.framing = $("#framingFilter").value;
    loadGallery("results", true);
  });
  $("#resultsMore").addEventListener("click", () => loadGallery("results", false));
}

async function prepareResultsPage() {
  if (!S.overview) {
    try { S.overview = await api("/api/overview"); } catch (e) { /* ignore */ }
  }
  const sel = $("#resultPerson");
  const people = (S.overview && S.overview.people) || [];
  const current = Gallery.results.person;
  sel.innerHTML = `<option value="">全部人员</option>` + people.map(p =>
    `<option value="${esc(p.name)}">${esc(p.name)}（${num(p.saved)}）</option>`).join("");
  sel.value = people.some(p => p.name === current) ? current : "";
  Gallery.results.person = sel.value;
  $("#framingFilter").value = Gallery.results.framing;
  $("#galleryStatus").value = Gallery.results.status;
}

function imgCard(it, page = 0) {
  const payload = esc(encodeURIComponent(JSON.stringify(it)));
  const src = it.thumb_url || it.image_url || "";
  const person = it.person ? `<span class="img-person">${esc(it.person)}</span>` : "";
  return `<div class="img-card" data-page="${page}" data-it="${payload}">
    <input type="checkbox" class="pick" title="选择用于批量操作">
    <button type="button" class="card-menu-btn" title="更多操作">⋯</button>
    <div class="card-menu" hidden>
      ${it.blocked ? `<button type="button" data-act="unblock">解除屏蔽</button>` : `
      <button type="button" data-act="delete">删除（移入回收）</button>
      <button type="button" data-act="block">屏蔽（永久不再出现）</button>
      <button type="button" data-act="deleteblock" class="danger">删除并屏蔽</button>`}
      <button type="button" data-act="source">查看来源</button>
      ${it.blocked ? "" : `
      <button type="button" data-act="sr">超分（另存，不替换）</button>
      <button type="button" data-act="sr-replace">超分并替换（可恢复）</button>
      <button type="button" data-act="sr-overwrite" class="danger">超分并覆盖（不可恢复）</button>
      <button type="button" data-act="preview">对比预览</button>`}
      ${it.can_undo_sr ? `<button type="button" data-act="sr-undo">撤销超分替换（恢复原图）</button>` : ""}
      ${it.manual_accept ? `<button type="button" data-act="undo">撤销手动收录</button>` : ""}
    </div>
    ${src ? `<img src="${esc(src)}" data-full="${esc(it.image_url || "")}" loading="lazy" alt="${esc(it.filename || "")}"
      onerror="this.onerror=null;this.src=this.dataset.full">` : `<div class="noimg">${it.file_missing ? "文件缺失（索引有记录，磁盘上找不到）" : "无预览（原文件已不存在）"}</div>`}
    <div class="img-meta">
      <b title="${esc(it.filename || "")}">${esc(it.filename || "")}</b> ${framingBadge(it)}
      ${it.blocked ? `<span class="badge blocked" title="已在屏蔽名单：正常视图不显示，采集不会拉回">已屏蔽</span>` : ""}
      ${it.has_sr ? `<span class="badge sr${it.sr_replace_mode === "overwrite" ? " hard" : ""}" title="${esc(it.sr_model)} · ${it.sr_scale}x · ${esc(it.sr_at)}${it.sr_replace_mode ? ` · ${it.sr_replace_mode}` : ""}${it.replaced_at ? ` · ${esc(it.replaced_at)}` : ""}">SR ${it.sr_scale}x${it.sr_replace_mode === "backup" ? " · 已替换" : it.sr_replace_mode === "overwrite" ? " · 已覆盖" : ""}</span>` : ""}
      ${it.manual_accept ? `<span class="badge manual">手动收录</span>` : ""}
      ${it.file_missing ? `<span class="badge bad" title="索引里有记录，但登记的正式文件在磁盘上不存在">文件缺失</span>` : ""}
      <br>${person}${it.width}×${it.height} · sim ${it.similarity != null ? Number(it.similarity).toFixed(3) : "-"}
      <br>清晰度 ${it.clarity != null ? Number(it.clarity).toFixed(1) : "-"} · ${esc(it.source || "")}
    </div>
  </div>`;
}

function rejectedCard(it, page = 0) {
  const payload = esc(encodeURIComponent(JSON.stringify(it)));
  const src = it.thumb_url || it.raw_thumb_url || it.image_url || it.raw_image_url || "";
  const remote = it.image_url && it.image_url.startsWith("/api/file") ? "" : (it.image_url || "");
  return `<div class="img-card rejected" data-page="${page}" data-it="${payload}">
    <input type="checkbox" class="pick" title="选择用于批量操作">
    <button type="button" class="card-menu-btn" title="更多操作">⋯</button>
    <div class="card-menu" hidden>
      <button type="button" data-act="accept">改为通过</button>
      <button type="button" data-act="delete">删除（移入回收）</button>
      <button type="button" data-act="block">屏蔽（不再下载）</button>
      <button type="button" data-act="deleteblock" class="danger">删除并屏蔽</button>
      <button type="button" data-act="source">查看来源</button>
    </div>
    ${src ? `<img src="${esc(src)}" data-full="${esc(it.raw_image_url || remote || "")}" loading="lazy" alt=""
      onerror="this.onerror=null;this.src=this.dataset.full">` : `<div class="noimg">无预览</div>`}
    <div class="img-meta">
      ${framingBadge(it)} <span class="bad">${esc(it.reason || "丢弃")}</span>
      <br>${it.person ? `${esc(it.person)} · ` : ""}${esc(it.framing_reason || "")}
      <br>sim ${it.similarity != null ? Number(it.similarity).toFixed(3) : "-"} · 人物占比 ${num(it.person_ratio)}
      <br><span class="muted">${it.has_raw ? "_raw 可用" : "需按 URL 重新下载"}</span>
      <button type="button" class="mini primary" data-quick="accept">改为通过</button>
    </div>
  </div>`;
}

async function loadGallery(kind, reset) {
  const st = Gallery[kind];
  const isPerson = kind === "person";
  const grid = isPerson ? $("#personGallery") : $("#resultsGrid");
  const more = isPerson ? $("#personMore") : $("#resultsMore");
  const hint = isPerson ? $("#personMoreHint") : $("#resultsMoreHint");

  // 自动加载与手动加载互斥：非重置请求在加载中或已无更多时直接忽略
  if (!reset && (st.loading || !st.hasMore)) return;

  if (reset) {
    st.page = 1;
    st.items = [];
    st.hasMore = false;
    st.lastPage = 0;
    updateBatchBar(kind);
    grid.innerHTML = "";
    resetGalleryScroll();
  }

  st.token = (st.token || 0) + 1;   // 请求令牌：重置后丢弃过期响应，避免串页
  const token = st.token;
  const pageNo = st.page;
  st.loading = true;
  more.hidden = true;
  if (hint) hint.textContent = "";

  const skel = appendGallerySkeletons(grid, reset ? 8 : 6);
  let ok = false;
  try {
    const qs = new URLSearchParams({
      person: st.person || "",
      status: st.status || "saved",
      framing: st.framing || "",
      page: String(pageNo),
      page_size: "24",
      include_rejected: st.status === "rejected" ? "0" : "1",
    });
    const data = await api(`/api/gallery?${qs}`);
    if (token !== st.token) return;

    const items = data.items || [];
    st.items = st.items.concat(items);
    st.total = num(data.total);
    st.hasMore = !!data.has_more;

    skel.remove();
    // 每 8 张一批插入，避免一次性重建整块列表
    for (let i = 0; i < items.length; i += 8) {
      const part = items.slice(i, i + 8)
        .map(it => (st.status === "rejected" ? rejectedCard(it, pageNo) : imgCard(it, pageNo))).join("");
      if (part) grid.insertAdjacentHTML("beforeend", part);
      if (i + 8 < items.length) await new Promise(r => setTimeout(r, 0));
    }
    if (!st.items.length) {
      const emptyText = st.status === "rejected"
        ? (st.framing ? `没有「${FRAMING_CN[st.framing] || st.framing}」的已丢弃记录。`
          : "还没有被丢弃的记录。上下文中被判掉的图会显示在这里，可手动改为通过。")
        : st.status === "review"
          ? "没有待复核的图片。"
          : (st.framing
            ? `没有「${FRAMING_CN[st.framing] || st.framing}」的成品图。`
            : "还没有成品图片。运行任务后，收录的图片会显示在这里。");
      grid.innerHTML = `<div class="empty">${emptyText}</div>`;
    }

    $$(".img-card", grid).forEach(card => {
      const it = galleryItemOf(card);
      const cb = card.querySelector(".pick");
      if (cb && it) cb.checked = st.sel.has(num(it.id));
    });
    st.lastPage = pageNo;
    updateBatchBar(kind);

    renderGalleryStats(kind, data);
    st.page += 1;
    ok = true;
  } catch (e) {
    if (token !== st.token) return;
    toast(e.message, true);
    if (reset) grid.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
    else if (hint) hint.textContent = `自动加载失败：${e.message}（可点「加载更多」重试）`;
  } finally {
    skel.remove();
    if (token === st.token) {
      st.loading = false;
      if (ok) {
        more.hidden = !st.hasMore;
        more.textContent = "加载更多";
        if (hint) hint.textContent = st.hasMore ? "" : (st.items.length ? "已全部加载" : "");
      } else if (st.hasMore) {
        more.hidden = false;   // 自动加载失败时保留手动兜底
      }
      // 请求结束后若哨兵仍在预加载范围内，继续补下一批（IntersectionObserver 不会重复触发）
      // 仅在本次成功时继续；失败则停下来，交给「加载更多」手动兜底（避免失败后无限重试）
      if (ok && st.hasMore && sentinelNear(kind)) {
        setTimeout(() => { if (!st.loading) loadGallery(kind, false); }, 0);
      }
    }
  }
}

function sentinelNear(kind) {
  const sentinel = $(kind === "person" ? "#personSentinel" : "#resultsSentinel");
  const main = document.querySelector("main");
  if (!sentinel || !main || !sentinel.offsetParent) return false;
  const s = sentinel.getBoundingClientRect();
  const m = main.getBoundingClientRect();
  return s.top <= m.bottom + 300;
}

function resetGalleryScroll() {
  const main = document.querySelector("main");
  if (main) main.scrollTop = 0;
}

function appendGallerySkeletons(grid, n) {
  const wrap = document.createElement("div");
  wrap.className = "skeleton-row";
  wrap.innerHTML = Array.from({ length: n }, () => `<div class="skeleton-card"></div>`).join("");
  grid.appendChild(wrap);
  return wrap;
}

function setupGalleryInfiniteScroll() {
  const main = document.querySelector("main");
  if (!main || typeof IntersectionObserver === "undefined") return;
  [["person", "#personSentinel"], ["results", "#resultsSentinel"]].forEach(([kind, sel]) => {
    const sentinel = $(sel);
    if (!sentinel) return;
    const io = new IntersectionObserver(entries => {
      for (const en of entries) {
        if (!en.isIntersecting) continue;
        const st = Gallery[kind];
        if (st.loading || !st.hasMore) continue;
        loadGallery(kind, false);
      }
    }, { root: main, rootMargin: "0px 0px 300px 0px", threshold: 0 });
    io.observe(sentinel);
    Gallery[kind].observer = io;
  });
}

function renderGalleryStats(kind, data) {
  const st = Gallery[kind];
  const stats = kind === "person" ? $("#personStats") : $("#framingStats");
  st.total = num(data.total);
  const labelText = Object.entries(data.framing_counts || {})
    .map(([k, v]) => `${FRAMING_CN[k] || k} ${v}`).join(" / ");
  const title = st.status === "rejected" ? "已丢弃" : st.status === "review" ? "待复核" : "成品";
  if (st.framing) {
    stats.innerHTML = `筛选「${FRAMING_CN[st.framing] || st.framing}」：<b>${num(data.total)}</b> 张${title === "成品" ? "" : `（${title}）`}`
      + (labelText ? ` · 全部构图分布：${labelText}` : "")
      + (num(data.rejected_total) ? ` · 丢弃记录 ${num(data.rejected_total)}` : "");
  } else {
    stats.innerHTML = `共 <b>${num(data.total)}</b> 张${title === "成品" ? "" : `（${title}）`}${labelText ? ` · ${labelText}` : ""}`
      + (num(data.rejected_total) ? ` · 丢弃记录 ${num(data.rejected_total)}` : "");
  }
}

/* ---------------- 图片墙操作：删除 / 屏蔽 / 删除并屏蔽 ---------------- */
function galleryKindOf(el) {
  if (el && el.closest("#resultsGrid")) return "results";
  if (el && el.closest("#personGallery")) return "person";
  return "";
}

function galleryGrid(kind) {
  return kind === "person" ? $("#personGallery") : $("#resultsGrid");
}

function galleryItemOf(card) {
  try { return JSON.parse(decodeURIComponent(card.dataset.it)); } catch { return null; }
}

function selectedGalleryIds(kind) {
  return Array.from(Gallery[kind].sel);
}

function syncGalleryChecks(kind) {
  $$(".img-card", galleryGrid(kind)).forEach(card => {
    const it = galleryItemOf(card);
    const cb = card.querySelector(".pick");
    if (cb && it) cb.checked = Gallery[kind].sel.has(num(it.id));
  });
  updateBatchBar(kind);
}

function updateBatchBar(kind) {
  if (!kind) return;
  const st = Gallery[kind];
  const bar = kind === "person" ? $("#personBatch") : $("#resultsBatch");
  const countEl = kind === "person" ? $("#personBatchCount") : $("#resultsBatchCount");
  const n = st.sel.size;
  bar.hidden = n === 0;
  const loaded = $$(".pick:checked", galleryGrid(kind)).length;
  const unloaded = Math.max(0, n - loaded);
  countEl.textContent = `已选 ${n} 张${unloaded ? `（含未加载页 ${unloaded} 张）` : ""}`;
}

function clearGallerySelection(kind) {
  const st = Gallery[kind];
  st.sel.clear();
  $$(".pick", galleryGrid(kind)).forEach(cb => { cb.checked = false; });
  updateBatchBar(kind);
}

async function selectGallery(kind, mode) {
  const st = Gallery[kind];
  if (mode === "none") {
    clearGallerySelection(kind);
    toast("已清空选择");
    return;
  }
  if (mode === "page") {
    const page = st.lastPage || 1;
    let added = 0;
    $$(`.img-card[data-page="${page}"]`, galleryGrid(kind)).forEach(card => {
      const it = galleryItemOf(card);
      if (it && num(it.id) && !st.sel.has(num(it.id))) { st.sel.add(num(it.id)); added += 1; }
    });
    syncGalleryChecks(kind);
    toast(added ? `已全选本页 ${added} 张` : "本页已全选");
    return;
  }
  if (mode === "invert") {
    try {
      const qs = new URLSearchParams({
        person: st.person || "", status: st.status || "saved", framing: st.framing || "",
      });
      const data = await api(`/api/gallery/ids?${qs}`);
      const ids = (data.ids || []).map(num).filter(Boolean);
      if (!ids.length) { toast("当前筛选条件下没有记录"); return; }
      let added = 0, removed = 0;
      ids.forEach(id => {
        if (st.sel.has(id)) { st.sel.delete(id); removed += 1; }
        else { st.sel.add(id); added += 1; }
      });
      syncGalleryChecks(kind);
      toast(`已反选：新增 ${added} 张，取消 ${removed} 张（当前筛选 ${ids.length} 张）`);
    } catch (e) { toast(e.message, true); }
    return;
  }
  try {
    const qs = new URLSearchParams({
      person: st.person || "", status: st.status || "saved", framing: st.framing || "",
    });
    const data = await api(`/api/gallery/ids?${qs}`);
    const ids = data.ids || [];
    if (!ids.length) { toast("当前筛选条件下没有记录"); return; }
    const warn = data.truncated ? `\n注意：单次最多选 10000 条，本次只选了 ${ids.length}/${num(data.total)} 条。` : "";
    if (!confirm(
      `全选当前筛选条件下的全部 ${ids.length} 张？\n（跨页选择，共 ${num(data.total)} 条记录）${warn}\n\n` +
      "选中后还需要点批量按钮才会真正执行操作。"
    )) return;
    ids.forEach(id => { const n = num(id); if (n) st.sel.add(n); });
    syncGalleryChecks(kind);
    toast(`已选中 ${st.sel.size} 张（跨页，切换筛选/翻页后保留）`);
  } catch (e) { toast(e.message, true); }
}

function removeGalleryCards(kind, ids) {
  const set = new Set(ids.map(String));
  $$(".img-card", galleryGrid(kind)).forEach(card => {
    const it = galleryItemOf(card);
    if (it && set.has(String(it.id))) card.remove();
  });
  const st = Gallery[kind];
  st.items = st.items.filter(x => !set.has(String(x.id)));
  ids.forEach(id => st.sel.delete(num(id)));
}

const GALLERY_ACTION_TEXT = {
  delete: n => `删除 ${n} 张图？\n删除仅移入回收目录（data/recycle/<人名>/），不会物理删除文件，可在「排除库 → 回收站」恢复。`,
  block: n => `屏蔽 ${n} 张图？\n屏蔽后永久不再出现（图片墙 / 筛选 / 后续采集都不会拉回），但不会删除文件；可在「排除库 → 屏蔽管理」解除。`,
  deleteblock: n => `删除并屏蔽 ${n} 张图？\n文件移入回收目录（可恢复），同时写入屏蔽名单，后续任务不再下载。`,
  accept: n => `将 ${n} 张已丢弃图片改为通过？\n优先使用 _raw 原文件；缺失时按记录 URL 重新下载。收录前会再检查屏蔽名单与排除目录；默认不受 count 上限限制。`,
  undo: n => `撤销 ${n} 张手动收录？\n成品文件会移入回收目录（可恢复），索引状态改回「已丢弃」，并清除手动收录标记。`,
};

async function doGalleryAction(kind, action, ids) {
  ids = (ids || []).map(num).filter(Boolean);
  if (action === "source") {
    const it = Gallery[kind].items.find(x => String(x.id) === String(ids[0]))
      || ($$(".img-card", galleryGrid(kind)).map(galleryItemOf).find(x => x && String(x.id) === String(ids[0])));
    if (it) openLightbox(it, kind);
    return;
  }
  if (action === "unblock") {
    if (!ids.length) { toast("请先勾选要解除屏蔽的图片", true); return; }
    if (!confirm(`解除屏蔽 ${ids.length} 张？\n只删除屏蔽名单条目，不动文件；解除后立即恢复显示。`)) return;
    try {
      const res = await api("/api/block/unblock", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }),
      });
      removeGalleryCards(kind, ids);
      toast(`已解除屏蔽 ${num(res.removed_images)} 张`);
      refreshGalleryMeta(kind);
    } catch (e) { toast(e.message, true); }
    return;
  }
  if (!ids.length) { toast("请先勾选要处理的图片", true); return; }
  if (action === "sr-replace" || action === "sr-overwrite") {
    // 一键超分并替换：跳过预览，直接按设置页默认参数执行
    const person = ids.length ? "" : (kind === "person" ? Gallery.person.person : "");
    quickSr(action === "sr-overwrite" ? "overwrite" : "backup", ids, person);
    return;
  }
  if (action === "sr" || action === "preview") {
    let it = null;
    if (ids.length === 1) {
      it = Gallery[kind].items.find(x => String(x.id) === String(ids[0]))
        || $$(".img-card", galleryGrid(kind)).map(galleryItemOf)
          .find(x => x && String(x.id) === String(ids[0]))
        || null;
    }
    openSrDialog(ids, kind === "person" ? Gallery.person.person : "", it);
    return;
  }
  if (action === "sr-undo") {
    if (!confirm(`撤销 ${ids.length} 张超分替换？\n原图将从 data/recycle/sr_backup 恢复为正式成品；SR 文件移入 data/recycle/sr_discard（不物理删除）。`)) return;
    try {
      const res = await api("/api/sr/undo", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }),
      });
      const errs = res.errors || [];
      toast(`已撤销 ${res.restored} 张超分替换${errs.length ? `；${errs.length} 个失败：${errs[0].error}` : ""}`, errs.length > 0);
      await refreshGalleryMeta(kind);
      loadGallery(kind, true);
    } catch (e) { toast(e.message, true); }
    return;
  }
  if (!GALLERY_ACTION_TEXT[action]) return;
  if (!confirm(GALLERY_ACTION_TEXT[action](ids.length))) return;
  try {
    if (action === "accept" || action === "undo") {
      const path = action === "accept" ? "/api/gallery/accept" : "/api/gallery/manual-undo";
      const body = action === "accept" ? { ids, manual_override_count: true } : { ids };
      const res = await api(path, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const done = res.accepted || res.undone || [];
      const errs = res.errors || [];
      if (done.length) removeGalleryCards(kind, done.map(x => x.id));
      const label = action === "accept" ? "已手动收录" : "已撤销手动收录";
      toast(`${label} ${done.length} 张${errs.length ? `；${errs.length} 个失败：${errs[0]}` : ""}`, errs.length > 0);
      clearGallerySelection(kind);
      await refreshGalleryMeta(kind);
      return;
    }
    const path = action === "block" ? "/api/gallery/block" : "/api/gallery/delete";
    const body = { ids };
    if (action === "deleteblock") body.block = true;
    const res = await api(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const removed = (res.deleted || []).map(x => x.id);
    const blocked = (res.blocked || []).map(x => x.id);
    const errs = res.errors || [];
    if (removed.length) removeGalleryCards(kind, removed);
    if (blocked.length) removeGalleryCards(kind, blocked);   // 屏蔽后不再出现在正常视图
    const done = removed.length || blocked.length;
    const label = action === "block" ? "已屏蔽" : action === "delete" ? "已删除" : "已删除并屏蔽";
    toast(`${label} ${done} 张${errs.length ? `；${errs.length} 个失败：${errs[0]}` : ""}`, errs.length > 0);
    clearGallerySelection(kind);
    await refreshGalleryMeta(kind);
  } catch (e) { toast(e.message, true); }
}

async function refreshGalleryMeta(kind) {
  const st = Gallery[kind];
  try {
    const qs = new URLSearchParams({
      person: st.person || "", status: st.status || "saved", framing: st.framing || "",
      page: "1", page_size: "1",
      include_rejected: st.status === "rejected" ? "0" : "1",
    });
    const data = await api(`/api/gallery?${qs}`);
    renderGalleryStats(kind, data);
    const grid = galleryGrid(kind);
    st.hasMore = !!data.has_more;
    (kind === "person" ? $("#personMore") : $("#resultsMore")).hidden = !st.hasMore;
    if (!num(data.total)) {
      const emptyText = st.status === "rejected" ? "还没有被丢弃的记录。"
        : st.status === "review" ? "没有待复核的图片。" : "还没有成品图片。";
      grid.innerHTML = `<div class="empty">${emptyText}</div>`;
    }
  } catch (e) { /* 计数刷新失败不影响操作结果 */ }

  if (kind === "person" && st.person) {
    try {
      const d = await api(`/api/people/${encodeURIComponent(st.person)}/detail`);
      $("#personSub").textContent =
        `成品 ${num(d.saved)} · 待复核 ${num(d.review)} · 最近运行 ${fmtTime(d.last_run)}`;
    } catch (e) { /* ignore */ }
  } else {
    try {
      S.overview = await api("/api/overview");
      const sel = $("#resultPerson");
      const cur = sel.value;
      sel.innerHTML = `<option value="">全部人员</option>` + ((S.overview.people || []).map(p =>
        `<option value="${esc(p.name)}">${esc(p.name)}（${num(p.saved)}）</option>`).join(""));
      sel.value = cur;
    } catch (e) { /* ignore */ }
  }
}

function bindGalleryOps() {
  document.addEventListener("click", e => {
    const menuBtn = e.target.closest(".card-menu-btn");
    if (menuBtn) {
      e.preventDefault();
      const card = menuBtn.closest(".img-card");
      const menu = card && card.querySelector(".card-menu");
      if (!menu) return;
      const wasOpen = !menu.hidden;
      $$(".card-menu").forEach(m => { m.hidden = true; });
      menu.hidden = wasOpen;
      return;
    }
    const quick = e.target.closest("[data-quick]");
    if (quick) {
      const card = quick.closest(".img-card");
      const kind = galleryKindOf(card);
      const it = card ? galleryItemOf(card) : null;
      if (kind && it) doGalleryAction(kind, quick.dataset.quick, [it.id]);
      return;
    }
    const selBtn = e.target.closest("[data-select]");
    if (selBtn) {
      const panel = selBtn.closest("#page-results, #page-person");
      const kind = panel && panel.id === "page-person" ? "person" : "results";
      selectGallery(kind, selBtn.dataset.select);
      return;
    }
    const actBtn = e.target.closest(".card-menu [data-act]");
    if (actBtn) {
      const card = actBtn.closest(".img-card");
      const kind = galleryKindOf(card);
      const it = card ? galleryItemOf(card) : null;
      actBtn.closest(".card-menu").hidden = true;
      if (kind && it) doGalleryAction(kind, actBtn.dataset.act, [it.id]);
      return;
    }
    const batchBtn = e.target.closest("[data-batch]");
    if (batchBtn) {
      const bar = batchBtn.closest("#resultsBatch, #personBatch");
      const kind = bar && bar.id === "personBatch" ? "person" : "results";
      if (batchBtn.dataset.batch === "clear") { clearGallerySelection(kind); return; }
      doGalleryAction(kind, batchBtn.dataset.batch, selectedGalleryIds(kind));
      return;
    }
    if (!e.target.closest(".card-menu")) $$(".card-menu").forEach(m => { m.hidden = true; });
  });
  document.addEventListener("change", e => {
    if (e.target.matches("#resultsGrid .pick, #personGallery .pick")) {
      const kind = galleryKindOf(e.target);
      const card = e.target.closest(".img-card");
      const it = card ? galleryItemOf(card) : null;
      const id = it ? num(it.id) : 0;
      if (id) {
        if (e.target.checked) Gallery[kind].sel.add(id);
        else Gallery[kind].sel.delete(id);
      }
      updateBatchBar(kind);
    }
  });
}

/* ---------------- 超分 ---------------- */
function bindSr() {
  $("#srModalClose").addEventListener("click", closeSrDialog);
  $("#srPreviewBtn").addEventListener("click", runSrPreview);
  $("#srApplyBtn").addEventListener("click", runSrApply);
  $("#srCancel").addEventListener("click", async () => {
    try {
      await api("/api/sr/cancel", { method: "POST" });
      toast("已请求取消超分");
    } catch (e) { toast(e.message, true); }
  });
  $("#srDlgModel").addEventListener("change", () => renderSrDialogNote());
  $$('input[name="srReplace"]').forEach(el =>
    el.addEventListener("change", () => updateSrReplace()));
  $("#srModal").addEventListener("click", e => {
    if (e.target.id === "srModal") closeSrDialog();
  });
  // 一键超分并覆盖：确认词弹窗
  $("#srQuickClose").addEventListener("click", closeSrQuick);
  $("#srQuickCancel").addEventListener("click", closeSrQuick);
  $("#srQuickGo").addEventListener("click", runSrQuickGo);
  $("#srQuickModal").addEventListener("click", e => {
    if (e.target.id === "srQuickModal") closeSrQuick();
  });
  $("#srQuickInput").addEventListener("input", syncSrQuickGo);
  $("#srQuickInput").addEventListener("keydown", e => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (!$("#srQuickGo").disabled) runSrQuickGo();
    }
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !$("#srQuickModal").hidden) {
      e.stopPropagation();
      closeSrQuick();
    }
  });
  // 预览图点击放大：用原图/结果的原尺寸 URL（非 /api/thumb）
  $("#srOriginalPane").addEventListener("click", e => {
    const img = e.target.closest("img");
    if (!img) return;
    openSrLightbox({ original: img.src, sr: currentSrPreviewUrl(), title: "原图 / 超分对比" });
  });
  $("#srResultPane").addEventListener("click", e => {
    const img = e.target.closest("img");
    if (!img) return;
    openSrLightbox({ original: originalPreviewUrl(), sr: img.src, title: "原图 / 超分对比" });
  });
  bindSrLightbox();
  setInterval(pollSr, 2000);
}

/* ---- 预览图 / 大图浮层 ---- */
function currentSrPreviewUrl() {
  if (S.sr.lastPreview && S.sr.lastPreview.sr_url) return S.sr.lastPreview.sr_url;
  const t = S.sr.target;
  return t && t.item && t.item.has_sr ? (t.item.sr_image_url || "") : "";
}

function originalPreviewUrl() {
  const t = S.sr.target;
  return t && t.item ? (t.item.image_url || "") : "";
}

function bindSrLightbox() {
  $("#srLbClose").addEventListener("click", closeSrLightbox);
  $("#srLbStage").addEventListener("click", e => {
    if (e.target.id === "srLbStage") closeSrLightbox();
  });
  $("#srLbOne").addEventListener("click", () => {
    const p = S.sr.lb && (S.sr.lb.active || S.sr.lb.panes[0]);
    if (!p || !p.loaded) return;
    const r = p.view.getBoundingClientRect();
    zoomSrLbPane(p, 1 / p.s, r.width / 2, r.height / 2);
  });
  $("#srLbReset").addEventListener("click", () => resetSrLb());
  $("#srLbFace").addEventListener("click", () => locateSrLbFace());
  $("#srLbSync").addEventListener("click", () => {
    const lb = S.sr.lb;
    if (!lb) return;
    lb.sync = !lb.sync;
    syncSrLbButton();
    if (lb.sync) applySrLbPane(lb.active || lb.panes[0]);
  });
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    if (!$("#srLb").hidden) { closeSrLightbox(); }
  });
  window.addEventListener("resize", () => {
    if (!$("#srLb").hidden && S.sr.lb) resetSrLb();
  });
}

function syncSrLbButton() {
  const lb = S.sr.lb;
  const btn = $("#srLbSync");
  btn.hidden = !(lb && lb.panes.length > 1);
  if (!lb) return;
  btn.textContent = `同步：${lb.sync ? "开" : "关"}`;
  btn.setAttribute("aria-pressed", lb.sync ? "true" : "false");
}

function openSrLightbox(opt) {
  S.sr.lb = { panes: [], sync: true, active: null, pointers: new Map() };
  $("#srLbStage").innerHTML = "";
  $("#srLbTitle").textContent = opt.title || "对比查看";
  $("#srLb").hidden = false;
  addSrLbPane("orig", "原图", opt.original || "");
  if (opt.sr) addSrLbPane("sr", "超分结果", opt.sr);
  const single = $("#srLbStage").children.length === 1;
  $$("#srLbStage .sr-lb-pane").forEach(el => el.classList.toggle("single", single));
  syncSrLbButton();
  updateSrLbReadout();
}

function closeSrLightbox() {
  $("#srLb").hidden = true;
  $("#srLbStage").innerHTML = "";
  S.sr.lb = null;
}

function addSrLbPane(role, label, url) {
  const el = document.createElement("div");
  el.className = "sr-lb-pane";
  el.dataset.role = role;
  el.innerHTML = `<div class="sr-lb-label">${esc(label)} 点击拖动 / 滚轮缩放</div>
    <div class="sr-lb-view"><div class="sr-lb-loading">大图加载中…</div><img alt="${esc(label)}"></div>`;
  const view = el.querySelector(".sr-lb-view");
  const img = el.querySelector("img");
  const loading = el.querySelector(".sr-lb-loading");
  const p = { role, label, url, el, view, img, loading,
    natW: 0, natH: 0, s: 1, tx: 0, ty: 0, fit: 1, loaded: false, drag: null, pinch: null };
  img.addEventListener("load", () => {
    p.natW = img.naturalWidth || 1;
    p.natH = img.naturalHeight || 1;
    p.loaded = true;
    loading.hidden = true;
    img.style.width = `${p.natW}px`;
    img.style.height = `${p.natH}px`;
    fitSrLbPane(p);
    if (!S.sr.lb.active) S.sr.lb.active = p;
    updateSrLbReadout();
  });
  img.addEventListener("error", () => { loading.textContent = "加载失败（原始文件可能不可访问）"; });
  el.addEventListener("wheel", e => {
    e.preventDefault();
    if (!p.loaded) return;
    const r = view.getBoundingClientRect();
    zoomSrLbPane(p, e.deltaY < 0 ? 1.18 : 1 / 1.18,
      e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  el.addEventListener("dblclick", e => {
    if (!p.loaded) return;
    const r = view.getBoundingClientRect();
    const target = Math.abs(p.s - 1) < 0.05 ? p.fit : 1;
    zoomSrLbPane(p, target / p.s, e.clientX - r.left, e.clientY - r.top);
  });
  el.addEventListener("pointerdown", e => {
    if (!p.loaded || e.button > 0) return;
    el.setPointerCapture(e.pointerId);
    S.sr.lb.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    S.sr.lb.active = p;
    if (S.sr.lb.pointers.size === 1) {
      p.drag = { x: e.clientX, y: e.clientY, tx: p.tx, ty: p.ty };
    } else if (S.sr.lb.pointers.size === 2) {
      const pts = [...S.sr.lb.pointers.values()].slice(-2);
      p.pinch = {
        dist: Math.max(1, Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y)),
        mid: { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 },
        s: p.s, tx: p.tx, ty: p.ty,
      };
      p.drag = null;
    }
  });
  el.addEventListener("pointermove", e => {
    if (!S.sr.lb.pointers.has(e.pointerId)) return;
    S.sr.lb.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const r = view.getBoundingClientRect();
    if (p.pinch && S.sr.lb.pointers.size >= 2) {
      const pts = [...S.sr.lb.pointers.values()].slice(-2);
      const dist = Math.max(1, Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y));
      const mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
      const ns = clampS(p.pinch.s * (dist / p.pinch.dist));
      const k = ns / p.pinch.s;
      p.s = ns;
      p.tx = (mid.x - r.left) - ((p.pinch.mid.x - r.left) - p.pinch.tx) * k;
      p.ty = (mid.y - r.top) - ((p.pinch.mid.y - r.top) - p.pinch.ty) * k;
      applySrLbPane(p);
    } else if (p.drag) {
      p.tx = p.drag.tx + (e.clientX - p.drag.x);
      p.ty = p.drag.ty + (e.clientY - p.drag.y);
      applySrLbPane(p);
    }
  });
  const endPointer = e => {
    S.sr.lb && S.sr.lb.pointers.delete(e.pointerId);
    if (!S.sr.lb || S.sr.lb.pointers.size < 2) p.pinch = null;
    if (!S.sr.lb || S.sr.lb.pointers.size === 0) p.drag = null;
  };
  el.addEventListener("pointerup", endPointer);
  el.addEventListener("pointercancel", endPointer);
  $("#srLbStage").appendChild(el);
  S.sr.lb.panes.push(p);
  img.src = url;
}

function clampS(s) { return Math.max(0.03, Math.min(16, s)); }

function fitSrLbPane(p) {
  const vw = p.view.clientWidth || 1;
  const vh = p.view.clientHeight || 1;
  if (!p.natW || !p.natH) return;
  // 不超过 1:1，避免把低分辨率图放大显示成“大图”
  const fit = Math.min(1, vw / p.natW, vh / p.natH);
  p.fit = fit;
  p.s = fit;
  p.tx = (vw - p.natW * fit) / 2;
  p.ty = (vh - p.natH * fit) / 2;
  drawSrLbPane(p);
}

function drawSrLbPane(p, syncOthers) {
  p.img.style.transform = `translate(${p.tx}px, ${p.ty}px) scale(${p.s})`;
  if (syncOthers && S.sr.lb && S.sr.lb.sync) {
    S.sr.lb.panes.forEach(o => { if (o !== p && o.loaded) syncSrLbPane(p, o); });
  }
}

function applySrLbPane(p) {
  S.sr.lb.active = p;
  drawSrLbPane(p, true);
  updateSrLbReadout();
}

function syncSrLbPane(src, dst) {
  const vw = dst.view.clientWidth || 1;
  const vh = dst.view.clientHeight || 1;
  const sw = src.view.clientWidth || 1;
  const sh = src.view.clientHeight || 1;
  if (!src.natW || !dst.natW) return;
  // 同步“画面相对区域”：SR 与原图分辨率不同，按相对比例换算缩放
  const fx = ((sw / 2 - src.tx) / src.s) / src.natW;
  const fy = ((sh / 2 - src.ty) / src.s) / src.natH;
  dst.s = clampS(src.s * (src.natW / dst.natW));
  dst.tx = vw / 2 - fx * dst.natW * dst.s;
  dst.ty = vh / 2 - fy * dst.natH * dst.s;
  drawSrLbPane(dst, false);
}

function zoomSrLbPane(p, factor, cx, cy) {
  const ns = clampS(p.s * factor);
  const k = ns / p.s;
  p.tx = cx - (cx - p.tx) * k;
  p.ty = cy - (cy - p.ty) * k;
  p.s = ns;
  applySrLbPane(p);
}

function resetSrLb() {
  const lb = S.sr.lb;
  if (!lb) return;
  lb.panes.forEach(p => fitSrLbPane(p));
  lb.active = lb.panes[0] || null;
  updateSrLbReadout();
}

function locateSrLbFace() {
  const lb = S.sr.lb;
  if (!lb || !lb.panes.length) return;
  const srPane = lb.panes.find(p => p.role === "sr" && p.loaded)
    || lb.panes.find(p => p.role === "orig" && p.loaded);
  if (!srPane) { toast("大图尚未加载完成", true); return; }
  const boxes = (S.sr.lastPreview && S.sr.lastPreview.face_boxes) || [];
  const box = srPane.role === "sr" ? boxes[0] : null;
  const fx = box ? (box[0] + box[2] / 2) / srPane.natW : 0.5;
  const fy = box ? (box[1] + box[3] / 2) / srPane.natH : 0.32;
  const vw = srPane.view.clientWidth || 1;
  const vh = srPane.view.clientHeight || 1;
  const scale = box
    ? Math.min(8, Math.max(srPane.fit, Math.min(vw, vh) * 0.45 / Math.max(1, box[2], box[3])))
    : Math.max(1, srPane.fit);
  srPane.s = clampS(scale);
  srPane.tx = vw / 2 - fx * srPane.natW * srPane.s;
  srPane.ty = vh / 2 - fy * srPane.natH * srPane.s;
  applySrLbPane(srPane);
  toast(box ? "已跳到最近检测到的人脸" : "未检测到人脸框，已定位到画面中上部");
}

function updateSrLbReadout() {
  const lb = S.sr.lb;
  const el = $("#srLbReadout");
  if (!el) return;
  const p = lb && (lb.active || lb.panes[0]);
  if (!p || !p.loaded) { el.textContent = "加载中…"; return; }
  const other = lb.panes.find(x => x !== p && x.loaded);
  el.textContent = `${Math.round(p.s * 100)}% · ${p.natW}×${p.natH}px`
    + (other ? `　｜　另一窗 ${Math.round(other.s * 100)}% · ${other.natW}×${other.natH}px` : "");
}

async function refreshSrModels() {
  try {
    const data = await api("/api/sr/models");
    S.sr.models = data.models || [];
    S.sr.faceModels = data.face_models || [];
    S.sr.weightsDir = data.weights_dir || "";
    S.sr.outputDir = data.output_dir || "";
    S.sr.gpu = data.gpu || {};
    S.sr.loaded = true;
    renderSrModelStatus();
    syncSrJob(data.job);
  } catch (e) {
    S.sr.loaded = true;
    renderSrModelStatus(e.message);
  }
}

function renderSrModelStatus(error = "") {
  const gpuEl = $("#srGpuInfo");
  const dirEl = $("#srWeightsDir");
  const list = $("#srModelStatusList");
  if (!gpuEl || !list) return;
  const gpu = S.sr.gpu || {};
  gpuEl.textContent = error ? `检测失败：${error}`
    : gpu.available
      ? `${gpu.device} · 可用 ${gpu.free_gb} / ${gpu.total_gb} GB`
      : "未检测到 CUDA";
  if (dirEl && S.sr.weightsDir) dirEl.textContent = S.sr.weightsDir;
  const models = S.sr.models || [];
  if (!models.length) {
    list.innerHTML = `<span class="muted">正在检测模型…</span>`;
    return;
  }
  const face = (S.sr.faceModels || []).find(x => x.id === "gfpgan");
  list.innerHTML = models.map(m => `<div class="sr-model-row">
      <b>${esc(m.label)}</b>
      <span class="tag ${m.installed ? "ok" : "bad"}">${m.installed ? `已安装 ${m.size_mb} MB` : "未安装"}</span>
      <span class="tag">${esc(m.tier)}</span>
      ${m.optional ? `<span class="tag bad">可选档</span>` : ""}
    </div>`).join("")
    + `<div class="sr-model-row"><b>GFPGAN v1.4 面部修复</b><span class="tag ${face && face.installed ? "ok" : "bad"}">`
    + `${face && face.installed ? `已安装 ${face.size_mb} MB` : "未安装"}</span></div>`;
}

function fillSrDialogModels() {
  const sel = $("#srDlgModel");
  if (!sel) return;
  const models = S.sr.models || [];
  sel.innerHTML = models.filter(m => m.installed).map(m =>
    `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("")
    || `<option value="">无可用模型</option>`;
  const optional = models.filter(m => !m.installed);
  if (optional.length) {
    sel.innerHTML += optional.map(m =>
      `<option value="${esc(m.id)}" disabled>${esc(m.label)}（未安装）</option>`).join("");
    }
}

function srDialogParams() {
  return {
    model: $("#srDlgModel").value,
    scale: num($("#srDlgScale").value, 4),
    face_restore: $("#srDlgFace").value,
    max_side: num($("#srDlgMaxSide").value, 4096),
  };
}

function srReplaceMode() {
  const el = document.querySelector('input[name="srReplace"]:checked');
  return el ? el.value : "keep";
}

function srBatchCount() {
  const t = S.sr.target || {};
  const n = num(S.sr.planCount);
  if (n) return n;
  return (t.ids || []).length;
}

function srConfirmNeed() {
  const n = srBatchCount();
  return n > 1 ? `彻底覆盖${n}` : "彻底覆盖";
}

function updateSrReplace() {
  const mode = srReplaceMode();
  const n = srBatchCount();
  const person = (S.sr.target && S.sr.target.person) || "";
  const impact = $("#srReplaceImpact");
  const row = $("#srConfirmRow");
  const word = $("#srConfirmWord");
  const hint = $("#srConfirmHint");
  if (mode === "overwrite") {
    row.hidden = false;
    hint.textContent = srConfirmNeed();
    word.placeholder = srConfirmNeed();
    impact.classList.add("warn");
    impact.innerHTML = `将影响 <b>${n || "?"}</b> 张：这 ${n || "?"} 张原图将被<b>物理删除</b>（回收站也不留备份），SR 结果成为正式成品。`
      + `<br>不可撤销。批量模式必须输入与执行条数一致的确认词「${esc(srConfirmNeed())}」。`;
  } else {
    row.hidden = true;
    if (word) word.value = "";
    impact.classList.remove("warn");
    if (mode === "backup") {
      impact.innerHTML = `将影响 <b>${n || "?"}</b> 张：原图移入 data/recycle/sr_backup/${esc(person || "<人名>")}/（带时间戳，不物理删除），`
        + `SR 结果成为正式成品；之后可在卡片菜单「撤销超分替换」恢复原图并回收 SR 文件。`;
    } else {
      impact.innerHTML = `将处理 <b>${n || "?"}</b> 张：原图保留不变，SR 结果另存到 data/sr/…，图片墙显示 SR 徽标。`;
    }
  }
  const btn = $("#srApplyBtn");
  if (btn && !(S.sr.jobState && (S.sr.jobState.busy || S.sr.jobState.state === "running"))) {
    btn.textContent = mode === "overwrite" ? "彻底覆盖（不可恢复）" : "开始超分";
  }
}

async function loadSrPlan() {
  const t = S.sr.target;
  if (!t) return;
  const body = {};
  if (t.ids.length) body.ids = t.ids;
  else if (t.person) body.person = t.person;
  else { S.sr.planCount = 0; updateSrReplace(); return; }
  try {
    const r = await api("/api/sr/plan", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    S.sr.planCount = num(r.count);
    if (t.person && !t.ids.length) {
      $("#srDlgCount").textContent =
        `将影响 ${num(r.count)} 张（「${t.person}」成品 + 待复核`
        + (num(r.skipped) ? `，跳过 ${num(r.skipped)} 张无本地文件` : "") + "）";
    } else if (t.ids.length > 1) {
      $("#srDlgCount").textContent = `将影响 ${num(r.count)} 张（选中 ${t.ids.length} 张`
        + (num(r.skipped) ? `，跳过 ${num(r.skipped)} 张` : "") + "）";
    }
  } catch (e) {
    S.sr.planCount = t.ids.length;
  }
  updateSrReplace();
}

function renderSrDialogNote() {
  const model = $("#srDlgModel").value;
  const spec = (S.sr.models || []).find(m => m.id === model);
  const note = $("#srDlgModelNote");
  if (note) {
    note.textContent = spec
      ? `${spec.note}　来源：${spec.source}`
        + (spec.max_side ? `　该模型长边上限 ${spec.max_side}px，设置更大值会被自动限制。` : "")
      : "";
  }
}

function openSrDialog(ids, person, item) {
  refreshSrModels().then(() => {
    const target = { ids: (ids || []).map(num).filter(Boolean), person: person || "", item: item || null };
    S.sr.target = target;
    S.sr.lastPreview = null;
    S.sr.planCount = 0;
    const keepRadio = document.querySelector('input[name="srReplace"][value="keep"]');
    if (keepRadio) keepRadio.checked = true;
    $("#srConfirmWord").value = "";
    const count = target.ids.length;
    $("#srModalTitle").textContent = target.person && !count
      ? `整批超分 · ${target.person}`
      : count > 1 ? `批量超分 · ${count} 张` : "超分 / 对比预览";
    $("#srDlgCount").textContent = target.person && !count
      ? `将处理「${target.person}」的全部成品与待复核图片`
      : count > 1 ? `共选中 ${count} 张，后台逐张处理` : "";
    fillSrDialogModels();
    const sr = (S.settings && S.settings.sr) || {};
    $("#srDlgScale").value = String(sr.scale || 4);
    $("#srDlgFace").value = sr.face_restore || "gfpgan";
    $("#srDlgMaxSide").value = sr.max_side != null ? sr.max_side : 4096;
    const wanted = item && item.has_sr ? item.sr_model : (sr.model || "realesrgan_x4plus");
    if ($(`#srDlgModel option[value="${CSS.escape(wanted || "")}"]`)) $("#srDlgModel").value = wanted;
    if (item && item.has_sr) {
      $("#srDlgScale").value = String(item.sr_scale || 4);
      $("#srDlgFace").value = item.sr_face || "none";
    }
    renderSrDialogNote();
    updateSrReplace();
    loadSrPlan();

    const orig = $("#srOriginalPane");
    if (item && item.image_url) {
      orig.classList.remove("muted");
      orig.innerHTML = `<img src="${esc(item.image_url)}" alt="原图">`;
    } else {
      orig.classList.add("muted");
      orig.innerHTML = `<div class="placeholder">${target.person ? `整批处理「${esc(target.person)}」` : "多张图片批量处理"}</div>`;
    }
    const pane = $("#srResultPane");
    if (item && item.has_sr && item.sr_image_url) {
      pane.classList.remove("muted");
      pane.innerHTML = `<img src="${esc(item.sr_image_url)}" alt="超分结果">`;
      $("#srResultLabel").textContent = "已保存的超分结果";
      $("#srResultMeta").textContent =
        `${item.sr_width}×${item.sr_height} · ${item.sr_model} · ${item.sr_scale}x`;
    } else {
      pane.classList.add("muted");
      pane.innerHTML = `<div class="placeholder">点「生成对比」预览，或直接开始超分</div>`;
      $("#srResultLabel").textContent = "超分结果";
      $("#srResultMeta").textContent = "";
    }
    const active = S.sr.jobState && (S.sr.jobState.busy || S.sr.jobState.state === "running");
    $("#srPreviewBtn").disabled = !(item && item.id) || active;
    $("#srPreviewBtn").textContent = "生成对比";
    $("#srApplyBtn").disabled = !(target.ids.length || target.person) || active;
    $("#srApplyBtn").textContent = active ? "超分任务进行中…" : "开始超分";
    $("#srModal").hidden = false;
  });
}

function closeSrDialog() {
  $("#srModal").hidden = true;
  S.sr.target = null;
  S.sr.lastPreview = null;
}

async function runSrPreview() {
  const item = S.sr.target && S.sr.target.item;
  if (!item || !item.id) return;
  const btn = $("#srPreviewBtn");
  btn.disabled = true;
  btn.textContent = "推理中…（HAT 可能 1–2 分钟）";
  const pane = $("#srResultPane");
  pane.classList.add("muted");
  pane.innerHTML = `<div class="placeholder">GPU 推理中，请稍候…</div>`;
  try {
    const payload = Object.assign({ id: item.id }, srDialogParams());
    const r = await api("/api/sr/preview", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    S.sr.lastPreview = r;
    pane.classList.remove("muted");
    pane.innerHTML = `<img src="${esc(r.sr_url)}" alt="超分预览">`;
    $("#srResultLabel").textContent = "对比预览（未保存）";
    const size = n => n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)}MB` : `${Math.round(n / 1024)}KB`;
    $("#srResultMeta").textContent =
      `${r.width}×${r.height}（原 ${r.original_width}×${r.original_height}） · ${r.elapsed}s · 峰值 ${r.peak_vram_gb}GB · tile ${r.tile} · 人脸 ${r.faces} · ${size(r.original_bytes)}→${size(r.sr_bytes)}`;
    toast(`对比完成：${r.elapsed}s，峰值显存 ${r.peak_vram_gb}GB`);
    $("#srApplyBtn").textContent = "采用此设置保存";
  } catch (e) {
    pane.innerHTML = `<div class="placeholder">预览失败：${esc(e.message)}</div>`;
    toast(e.message, true);
  } finally {
    btn.textContent = "重新生成对比";
    btn.disabled = !!(S.sr.jobState && S.sr.jobState.busy);
  }
}

async function runSrApply() {
  const t = S.sr.target;
  if (!t) return;
  const params = srDialogParams();
  const mode = srReplaceMode();
  const payload = Object.assign({}, params, { replace_mode: mode });
  if (t.ids.length) payload.ids = t.ids;
  else if (t.person) payload.person = t.person;
  else { toast("没有指定要超分的图片", true); return; }
  const n = Math.max(1, srBatchCount() || 0);
  if (mode === "overwrite") {
    const need = n > 1 ? `彻底覆盖${n}` : "彻底覆盖";
    const got = ($("#srConfirmWord").value || "").trim();
    if (got !== need) {
      toast(`确认词不正确：请输入「${need}」`, true);
      $("#srConfirmWord").focus();
      return;
    }
    const previewed = S.sr.lastPreview
      && (!t.item || String(S.sr.lastPreview.id) === String(t.item.id));
    if (!previewed) {
      // 无预览直接提交：额外一层确认
      if (!confirm(`尚未生成对比预览。\n确认彻底覆盖 ${n} 张原图？原图将被物理删除且不可恢复。`)) return;
    }
    if (n > 1 && !confirm(`批量彻底覆盖 ${n} 张（不可恢复，回收站无备份）？\n确认词「${need}」已校验通过。`)) return;
  }
  const btn = $("#srApplyBtn");
  btn.disabled = true;
  btn.textContent = "提交中…";
  try {
    const st = await api("/api/sr/run", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    closeSrDialog();
    syncSrJob(st);
    const modeText = mode === "overwrite" ? "彻底覆盖" : mode === "backup" ? "替换（可恢复）" : "另存";
    toast(`已开始超分：${st.total || 0} 张 · ${params.model} · ${modeText}`);
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
    btn.textContent = mode === "overwrite" ? "彻底覆盖（不可恢复）" : "开始超分";
  }
}

/* ---------------- 一键超分并替换（跳过预览） ---------------- */
function srDefaults() {
  const sr = (S.settings && S.settings.sr) || {};
  return {
    model: sr.model || "realesrgan_x4plus",
    scale: num(sr.scale, 4),
    face_restore: sr.face_restore || "gfpgan",
    max_side: sr.max_side != null ? num(sr.max_side) : 4096,
  };
}

async function submitQuickSr(mode, ids, person, word) {
  const p = srDefaults();
  const body = Object.assign({}, p, { replace_mode: mode, quick_action: true });
  if (ids.length) body.ids = ids;
  else if (person) body.person = person;
  if (mode === "overwrite") body.confirm_word = word;
  const st = await api("/api/sr/run", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  syncSrJob(st);
  return st;
}

async function quickSr(mode, ids, person) {
  ids = (ids || []).map(num).filter(Boolean);
  person = ids.length ? "" : (person || "");
  if (!ids.length && !person) { toast("请先选择要超分的图片", true); return; }
  if (S.sr.quickBusy) { toast("正在提交，请稍候…", true); return; }
  const st = S.sr.jobState;
  if (st && (st.busy || st.state === "running" || st.state === "stopping")) {
    toast("已有超分任务在运行，请等待完成或先取消", true);
    return;
  }
  if (S.job && ["running", "paused", "stopping"].includes(S.job.state)) {
    toast("下载任务正在运行，请等待完成后再超分（避免 GPU 争用）", true);
    return;
  }
  const key = `${mode}|${ids.join(",")}|${person}`;
  if (S.sr.lastQuickKey === key && Date.now() - (S.sr.lastQuickAt || 0) < 8000) {
    toast("该批次刚刚已提交，请勿重复提交", true);
    return;
  }
  S.sr.quickBusy = true;
  try {
    const planBody = ids.length ? { ids } : { person };
    const plan = await api("/api/sr/plan", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(planBody),
    });
    const n = num(plan.count);
    if (!n) {
      toast("没有可超分的成品图片（仅支持 saved/review 且本地文件存在）", true);
      return;
    }
    const p = srDefaults();
    if (mode === "overwrite") {
      openSrQuick({ mode, ids, person, count: n, params: p, key });
      return;
    }
    if (!confirm(
      `将影响 ${n} 张：超分并替换（可恢复）？\n\n` +
      `原图将移入 data/recycle/sr_backup/${person || "<人名>"}/（带时间戳，不物理删除），SR 结果成为正式成品。\n` +
      `之后可在卡片菜单「撤销超分替换」恢复原图并回收 SR 文件。\n\n` +
      `参数（设置页当前默认）：${p.model} · ${p.scale}x · 面部修复 ${p.face_restore} · 长边 ${p.max_side}px`
    )) return;
    S.sr.lastQuickKey = key;
    S.sr.lastQuickAt = Date.now();
    const job = await submitQuickSr("backup", ids, person, "");
    toast(`已开始超分并替换（可恢复）：${num(job.total) || n} 张 · ${p.model} · ${p.scale}x`);
  } catch (e) { toast(e.message, true); }
  finally { S.sr.quickBusy = false; }
}

function openSrQuick(q) {
  S.sr.quick = Object.assign({}, q, { busy: false });
  const need = q.count > 1 ? `彻底覆盖${q.count}` : "彻底覆盖";
  $("#srQuickTitle").textContent = "超分并覆盖（不可恢复）";
  $("#srQuickImpact").innerHTML =
    `将影响 <b>${q.count}</b> 张：这 ${q.count} 张原图将被<b>物理删除</b>（回收站也不留备份），SR 结果成为正式成品。<br>`
    + `不可撤销。${q.count > 1 ? `批量覆盖必须输入与执行条数一致的确认词「${esc(need)}」。` : ""}`;
  $("#srQuickParams").textContent =
    `参数（设置页当前默认）：${q.params.model} · ${q.params.scale}x · 面部修复 ${q.params.face_restore} · 长边 ${q.params.max_side}px`;
  $("#srQuickWord").textContent = need;
  $("#srQuickConfirmRow").hidden = false;
  $("#srQuickInput").value = "";
  $("#srQuickInput").placeholder = need;
  $("#srQuickError").hidden = true;
  $("#srQuickState").textContent = "";
  const go = $("#srQuickGo");
  go.disabled = true;
  go.textContent = "彻底覆盖（不可恢复）";
  $("#srQuickModal").hidden = false;
  setTimeout(() => $("#srQuickInput").focus(), 40);
}

function closeSrQuick() {
  $("#srQuickModal").hidden = true;
  S.sr.quick = null;
}

function syncSrQuickGo() {
  const q = S.sr.quick;
  if (!q) return;
  const need = q.count > 1 ? `彻底覆盖${q.count}` : "彻底覆盖";
  const got = ($("#srQuickInput").value || "").trim();
  $("#srQuickGo").disabled = !!q.busy || got !== need;
  $("#srQuickError").hidden = !got || got === need;
}

async function runSrQuickGo() {
  const q = S.sr.quick;
  if (!q || q.busy) return;
  const need = q.count > 1 ? `彻底覆盖${q.count}` : "彻底覆盖";
  const got = ($("#srQuickInput").value || "").trim();
  if (got !== need) { $("#srQuickError").hidden = false; return; }
  q.busy = true;
  const go = $("#srQuickGo");
  go.disabled = true;
  go.textContent = "提交中…";
  $("#srQuickState").textContent = "";
  try {
    S.sr.lastQuickKey = q.key;
    S.sr.lastQuickAt = Date.now();
    const job = await submitQuickSr("overwrite", q.ids, q.person, got);
    closeSrQuick();
    toast(`已开始超分并彻底覆盖（不可恢复）：${num(job.total) || q.count} 张`);
  } catch (e) {
    toast(e.message, true);
    $("#srQuickState").textContent = e.message;
    go.textContent = "彻底覆盖（不可恢复）";
    q.busy = false;
    syncSrQuickGo();
  }
}

async function pollSr() {
  try {
    const st = await api("/api/sr/status");
    syncSrJob(st);
  } catch (e) { /* 静默重试 */ }
}

function syncSrJob(st) {
  if (!st) return;
  const prev = S.sr.jobState;
  S.sr.jobState = st;
  const panel = $("#srProgress");
  const active = !!st.busy || st.state === "running" || st.state === "stopping";
  panel.hidden = !active;
  if (active) {
    $("#srProgressFill").style.width = `${Math.max(0, Math.min(100, num(st.pct)))}%`;
    const fail = num(st.failed) ? ` · 失败 ${num(st.failed)}` : "";
    $("#srProgressText").innerHTML =
      `${num(st.done)}/${num(st.total)} 张完成${fail} · ${num(st.pct)}%<br>${esc(st.current || "准备中…")}`;
    $("#srCancel").disabled = st.state === "stopping";
    $("#srCancel").textContent = st.state === "stopping" ? "正在停止…" : "取消";
  }
  const wasActive = prev && (prev.busy || prev.state === "running" || prev.state === "stopping");
  const nowDone = st.state === "finished" || st.state === "stopped" || st.state === "error";
  if (wasActive && nowDone) {
    if (st.state === "finished") {
      toast(`超分完成：${num(st.done)}/${num(st.total)} 张${num(st.failed) ? `，失败 ${num(st.failed)} 张` : ""}`);
    } else if (st.state === "stopped") {
      toast(`超分已取消：完成 ${num(st.done)}/${num(st.total)} 张`);
    } else {
      toast(`超分任务出错：${st.error || "未知错误"}`, true);
    }
    if (S.lb && !$("#lightbox").hidden) {
      // 灯箱打开时不整页重绘（避免打断大图）；灯箱内的超分已由 lbWaitSr 自行刷新
      if (S.lb.kind && !S.lb.waitJob) refreshGalleryMeta(S.lb.kind);
    } else if (S.route === "results" || S.route === "person") {
      route();
    } else {
      refreshHome();
    }
    refreshPeople();
  }
  const dlgOpen = !$("#srModal").hidden;
  if (dlgOpen) {
    $("#srApplyBtn").disabled = active || !(S.sr.target && (S.sr.target.ids.length || S.sr.target.person));
    if (!active && $("#srApplyBtn").textContent === "超分任务进行中…") $("#srApplyBtn").textContent = "开始超分";
  }
}

/* ---------------- 待复核 ---------------- */
function bindReviewPage() {
  $("#reviewAcceptBtn").addEventListener("click", () => {
    const checked = $$("#reviewGroups .pick:checked").map(c => JSON.parse(decodeURIComponent(c.closest(".review-card").dataset.it)));
    decide(checked, "accept");
  });
  $("#reviewDiscardBtn").addEventListener("click", () => {
    const checked = $$("#reviewGroups .pick:checked").map(c => JSON.parse(decodeURIComponent(c.closest(".review-card").dataset.it)));
    decide(checked, "discard");
  });
  $("#reviewGroups").addEventListener("click", e => {
    const rerun = e.target.closest("[data-act='rerun']");
    if (rerun) { rerunPerson(rerun.closest(".group").dataset.person); return; }
    const card = e.target.closest(".review-card");
    if (!card) return;
    const it = JSON.parse(decodeURIComponent(card.dataset.it));
    if (e.target.closest("[data-act='accept']")) { e.stopPropagation(); decide([it], "accept"); return; }
    if (e.target.closest("[data-act='discard']")) { e.stopPropagation(); decide([it], "discard"); return; }
    if (e.target.matches("button, input, a")) return;
    openLightbox(it);
  });
}

async function refreshReview() {
  try {
    const data = await api("/api/results");
    S.review = (data.people || []).filter(g => (g.review || []).length);
    renderReview();
    updateReviewBadge(S.review.reduce((a, g) => a + g.review.length, 0));
  } catch (e) { toast(e.message, true); }
}

function renderReview() {
  const box = $("#reviewGroups");
  if (!S.review.length) {
    box.innerHTML = `<div class="empty">没有待复核图片。低于 B 的会被丢弃，高于 A 的会直接收录。</div>`;
    return;
  }
  box.innerHTML = S.review.map(group => `<section class="group" data-person="${esc(group.person)}">
    <div class="group-head">
      <h3>${esc(group.person)}</h3>
      <span class="badge">${group.review.length} 张</span>
      <div class="actions"><button data-act="rerun" class="ghost">重跑该人</button></div>
    </div>
    <div class="img-grid">${group.review.map(it => `<div class="img-card review-card" data-it="${esc(encodeURIComponent(JSON.stringify(it)))}">
      <input type="checkbox" class="pick" title="批量选择">
      <img src="${esc(it.thumb_url || it.image_url)}" data-full="${esc(it.image_url || "")}" loading="lazy" alt=""
        onerror="this.onerror=null;this.src=this.dataset.full">
      <div class="img-meta">sim ${it.similarity != null ? Number(it.similarity).toFixed(3) : "-"} · 清晰度 ${it.clarity != null ? Number(it.clarity).toFixed(1) : "-"} ${framingBadge(it)}<br>${it.width}×${it.height} · ${esc(it.source || "")}</div>
      <div class="review-actions">
        <button data-act="accept">收录</button>
        <button data-act="discard" class="danger">丢弃</button>
      </div>
    </div>`).join("")}</div>
  </section>`).join("");
}

async function decide(items, action) {
  if (!items.length) { toast("请先勾选要处理的图片", true); return; }
  try {
    const res = await api("/api/review/decide", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, items: items.map(x => ({ person: x.person, file_path: x.file_path })) }),
    });
    const errs = res.errors || [];
    toast(`${action === "accept" ? "收录" : "丢弃"} ${res.moved} 张${errs.length ? `；${errs.length} 个失败：${errs[0]}` : ""}`, errs.length > 0);
    await Promise.all([refreshReview(), refreshHome()]);
    if (S.route === "results") loadGallery("results", true);
    if (S.route === "person") openPerson(S.personName, true);
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 存储容量 ---------------- */
function fmtBytes(n) {
  n = num(n);
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n;
  let i = -1;
  do { v /= 1024; i += 1; } while (v >= 1024 && i < units.length - 1);
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

async function refreshStorage() {
  try {
    const data = await api("/api/storage");
    S.storage = data;
    renderStorage(data);
  } catch (e) { toast(e.message, true); }
}

function storageSection(data, key) {
  return (data.sections || []).find(s => s.key === key) || { size: 0, files: 0, mtime: "", path: "" };
}

function storagePerson(data, name) {
  const empty = { size: 0, files: 0, mtime: "" };
  const p = (data.people || []).find(x => x.name === name);
  return p || { name, saved: empty, review: empty, raw: empty, recycle: empty, total: empty };
}

function renderStorage(data) {
  const total = data.total || { size: 0, files: 0, mtime: "" };
  $("#storageTotalText").textContent = `合计 ${fmtBytes(total.size)} · ${num(total.files)} 个文件`;
  const cards = [["总计", total], ...(data.sections || []).map(s => [s.label, s])];
  $("#storageSummary").innerHTML = cards.map(([label, m]) => `<div class="stat-card">
    <div class="k">${esc(label)}</div>
    <div class="v">${fmtBytes(m.size)}</div>
    <div class="s">${num(m.files)} 个文件 · ${esc(fmtTime(m.mtime))}</div>
  </div>`).join("");

  const max = Math.max(1, ...(data.sections || []).map(s => num(s.size)));
  $("#storageBars").innerHTML = (data.sections || []).map(s => `<div class="storage-bar">
    <span class="ellip">${esc(s.label)}</span>
    <div class="track"><div class="fill" style="width:${(num(s.size) / max * 100).toFixed(1)}%"></div></div>
    <span class="muted">${fmtBytes(s.size)} · ${num(s.files)}</span>
  </div>`).join("");

  $("#storageTable tbody").innerHTML = (data.sections || []).map(s => `<tr>
    <td>${esc(s.label)}</td><td>${fmtBytes(s.size)}</td><td>${num(s.files)}</td>
    <td>${esc(fmtTime(s.mtime))}</td>
    <td class="path" title="${esc(s.path)}">${esc(s.path)}</td>
  </tr>`).join("");

  $("#storagePersonTable tbody").innerHTML = (data.people || []).map(p => `<tr>
    <td>${esc(p.name)}</td>
    <td>${fmtBytes(p.saved.size)}<br><span class="muted">${num(p.saved.files)} 个</span></td>
    <td>${fmtBytes(p.review.size)}<br><span class="muted">${num(p.review.files)} 个</span></td>
    <td>${fmtBytes(p.raw.size)}<br><span class="muted">${num(p.raw.files)} 个</span></td>
    <td>${fmtBytes(p.recycle.size)}<br><span class="muted">${num(p.recycle.files)} 个</span></td>
    <td><b>${fmtBytes(p.total.size)}</b><br><span class="muted">${num(p.total.files)} 个</span></td>
  </tr>`).join("") || `<tr><td colspan="6" class="muted">暂无人员数据</td></tr>`;

  const sel = $("#storagePerson");
  const cur = sel.value;
  sel.innerHTML = (data.people || []).map(p =>
    `<option value="${esc(p.name)}">${esc(p.name)}（${fmtBytes(p.total.size)}）</option>`).join("")
    || `<option value="">（无人员数据）</option>`;
  if (cur && (data.people || []).some(p => p.name === cur)) sel.value = cur;
  renderStoragePerson(sel.value);
}

function renderStoragePerson(name) {
  const data = S.storage;
  if (!data) return;
  const p = storagePerson(data, name);
  const blocks = [["成品", p.saved], ["待复核", p.review], ["_raw", p.raw],
    ["回收", p.recycle], ["合计", p.total]];
  $("#storagePersonStats").innerHTML = blocks.map(([label, m]) => `<div class="stat-card">
    <div class="k">${esc(label)}</div>
    <div class="v">${fmtBytes(m.size)}</div>
    <div class="s">${num(m.files)} 个文件</div>
  </div>`).join("");
  const has = !!name;
  $("#clearPersonRaw").disabled = !has;
  $("#clearPersonRecycle").disabled = !has;
  $("#clearPersonDownloads").disabled = !has;
}

function storageEstimate(category, person) {
  const data = S.storage || { sections: [], people: [] };
  if (category === "downloads") {
    const p = storagePerson(data, person);
    return {
      files: num(p.saved.files) + num(p.review.files),
      size: num(p.saved.size) + num(p.review.size),
    };
  }
  if (category === "raw" && person) return storagePerson(data, person).raw;
  if (category === "recycle" && person) return storagePerson(data, person).recycle;
  return storageSection(data, category);
}

async function clearStorage(category, person) {
  if (!S.storage) return;
  const est = storageEstimate(category, person);
  let confirmText = "";
  if (category === "downloads") {
    if (!person) return;
    if (!num(est.files)) { toast("该人员没有成品文件", true); return; }
    const input = prompt(`将永久删除「${person}」的成品与待复核共 ${num(est.files)} 个文件（约 ${fmtBytes(est.size)}），并删除对应索引记录。\n图片墙中该人的条目会消失，此操作不可恢复。\n请输入人名「${person}」确认：`);
    if (input !== person) { toast("人名不一致，已取消", true); return; }
    confirmText = person;
  } else if (category === "recycle") {
    if (!num(est.files)) { toast("回收目录为空", true); return; }
    if (!confirm(`回收目录将被永久删除：${num(est.files)} 个文件，约 ${fmtBytes(est.size)}，删除后无法恢复。\n确定继续？`)) return;
    confirmText = "永久删除";
  } else {
    if (!num(est.files)) { toast("没有可清理的文件", true); return; }
    if (!confirm(`将删除 ${num(est.files)} 个文件，约释放 ${fmtBytes(est.size)}。\n确定继续？`)) return;
  }
  try {
    const res = await api("/api/storage/clear", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category, person: person || "", confirm: confirmText }),
    });
    toast(`${res.message}；实际释放 ${fmtBytes(res.freed)}`);
    await refreshStorage();
    if (category === "downloads") await refreshHome();
  } catch (e) { toast(e.message, true); }
}

function bindStoragePage() {
  $("#refreshStorage").addEventListener("click", refreshStorage);
  $("#storagePerson").addEventListener("change", e => renderStoragePerson(e.target.value));
  $("#clearPersonRaw").addEventListener("click", () => clearStorage("raw", $("#storagePerson").value));
  $("#clearPersonRecycle").addEventListener("click", () => clearStorage("recycle", $("#storagePerson").value));
  $("#clearPersonDownloads").addEventListener("click", () => clearStorage("downloads", $("#storagePerson").value));
  $("#clearRawAll").addEventListener("click", () => clearStorage("raw", ""));
  $("#clearThumbs").addEventListener("click", () => clearStorage("thumbs", ""));
  $("#clearRecycleAll").addEventListener("click", () => clearStorage("recycle", ""));
  $("#clearLogs").addEventListener("click", () => clearStorage("logs", ""));
}

/* ---------------- 排除库 ---------------- */
function bindExcludePage() {
  $("#rebuildExclude").addEventListener("click", async () => {
    try {
      const res = await api("/api/exclude/rebuild", { method: "POST" });
      toast(res.message || "已开始重建");
      pollExclude();
    } catch (e) { toast(e.message, true); }
  });
  $("#addExcludeDir2").addEventListener("click", () => addExcludeDir());
  $("#page-exclude").addEventListener("click", e => {
    const btn = e.target.closest("[data-remove-exclude]");
    if (!btn) return;
    removeExcludeEntry(btn.dataset.removeExclude, btn.dataset.value);
  });
  $("#refreshRecycle").addEventListener("click", refreshExclude);

  $("#blockedList").addEventListener("click", async e => {
    const btn = e.target.closest("[data-unblock]");
    if (!btn) return;
    if (!confirm("解除屏蔽？解除后该图（URL 与指纹）可能被后续任务再次下载。")) return;
    try {
      await api("/api/blocked/remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: num(btn.dataset.unblock) }),
      });
      toast("已解除屏蔽");
      refreshExclude();
    } catch (err) { toast(err.message, true); }
  });

  $("#recycleList").addEventListener("click", async e => {
    const btn = e.target.closest("[data-restore]");
    if (!btn) return;
    if (!confirm("恢复这张图到成品目录？")) return;
    try {
      const res = await api("/api/recycle/restore", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: [num(btn.dataset.restore)] }),
      });
      const n = (res.restored || []).length;
      const errs = res.errors || [];
      toast(`已恢复 ${n} 张${errs.length ? `；${errs[0]}` : ""}`, errs.length > 0);
      refreshExclude();
      refreshHome();
    } catch (err) { toast(err.message, true); }
  });

  $("#refreshBlockManage").addEventListener("click", () => refreshBlockManage());
  $("#blockImgList").addEventListener("click", async e => {
    const btn = e.target.closest("[data-unblock-img]");
    if (!btn) return;
    if (!confirm("解除这张图的屏蔽？\n只删除名单条目，不动文件；解除后立即恢复显示。")) return;
    try {
      const res = await api("/api/block/unblock", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: [num(btn.dataset.unblockImg)] }),
      });
      toast(`已解除屏蔽 ${num(res.removed_images)} 张`);
      refreshBlockManage();
    } catch (err) { toast(err.message, true); }
  });
  $("#blockPersonList").addEventListener("click", async e => {
    const btn = e.target.closest("[data-unblock-person]");
    if (!btn) return;
    const person = btn.dataset.unblockPerson;
    if (!confirm(`解除「${person}」的屏蔽？\n只删除名单条目，不动文件。`)) return;
    try {
      const res = await api("/api/block/unblock", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ people: [person] }),
      });
      toast(`已解除屏蔽人员 ${num(res.removed_people)} 个`);
      refreshBlockManage();
    } catch (err) { toast(err.message, true); }
  });
  $("#blockPersonAdd").addEventListener("click", async () => {
    const person = $("#blockPersonSelect").value;
    if (!person) { toast("请选择要屏蔽的人员", true); return; }
    const withFiles = $("#blockPersonWithFiles").checked;
    if (!confirm(
      `屏蔽人员「${person}」？\n\n` +
      "该人员将从图片墙与后续采集判定中消失（永久，可在本页解除）。\n" +
      (withFiles ? "已勾选：该人成品文件将移入回收站 data/recycle/" + person + "/（可恢复）。" : "未勾选：文件完全不动，只是不再显示/不再采集。")
    )) return;
    const btn = $("#blockPersonAdd");
    btn.disabled = true;
    try {
      const res = await api("/api/block/people", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ people: [person], with_files: withFiles }),
      });
      toast(`已屏蔽 ${person}${withFiles ? `，移入回收站 ${num(res.moved)} 个文件` : ""}${(res.errors || []).length ? `；${res.errors[0]}` : ""}`, (res.errors || []).length > 0);
      refreshBlockManage();
      refreshExclude();
    } catch (err) { toast(err.message, true); }
    finally { btn.disabled = false; }
  });
  $("#unblockAll").addEventListener("click", async () => {
    if (!confirm("解除全部屏蔽？\n图片与人员都会恢复显示/采集；不会改动任何文件。")) return;
    try {
      const res = await api("/api/block/unblock", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ all: true }),
      });
      toast(`已解除：图片 ${num(res.removed_images)} 张，人员 ${num(res.removed_people)} 个`);
      refreshBlockManage();
    } catch (err) { toast(err.message, true); }
  });
}

async function refreshBlockManage() {
  try {
    const data = await api("/api/block/list");
    renderBlockManage(data);
  } catch (e) { /* ignore */ }
}

function renderBlockManage(data) {
  if (!data || !$("#blockImgList")) return;
  const imgs = data.images || [], people = data.people || [];
  $("#blockManageCount").textContent = `图片 ${imgs.length} · 人员 ${people.length}`;
  $("#blockImgCount").textContent = imgs.length;
  $("#blockPeopleCount").textContent = people.length;
  $("#blockImgList").innerHTML = imgs.map(x => `<div class="blocked-item">
    ${x.thumb_url ? `<img src="${esc(x.thumb_url)}" loading="lazy" alt="">` : `<div class="noimg small">文件缺失</div>`}
    <div class="blocked-meta">
      <div><b>${esc(x.person || "—")}</b> · 状态 ${esc(x.status || "—")} · ${esc(fmtTime(x.blocked_at))}</div>
      <div class="ellip" title="${esc(x.filename || "")}">${esc(x.filename || "（无文件名）")}</div>
      <div class="muted mono ellip" title="${esc(x.url || "")}">${esc(x.url || "")}</div>
    </div>
    <button class="ghost" data-unblock-img="${num(x.id)}">解除</button>
  </div>`).join("") || `<span class="muted">暂无被屏蔽的图片。</span>`;
  $("#blockPersonList").innerHTML = people.map(x => `<div class="blocked-item">
    <div class="blocked-meta">
      <div><b>${esc(x.person)}</b> · ${esc(fmtTime(x.blocked_at))}${x.reason ? ` · ${esc(x.reason)}` : ""}</div>
      <div class="muted">成品 ${num(x.saved)} 张 · 待复核 ${num(x.review)} 张（文件未删除）</div>
    </div>
    <button class="ghost" data-unblock-person="${esc(x.person)}">解除</button>
  </div>`).join("") || `<span class="muted">暂无被屏蔽的人员。</span>`;
  const sel = $("#blockPersonSelect");
  if (sel && !sel.dataset.loaded) {
    api("/api/people").then(d => {
      sel.innerHTML = (d.people || []).map(p => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join("");
      sel.dataset.loaded = "1";
    }).catch(() => {});
  }
}

async function refreshExclude() {
  try {
    const data = await api("/api/exclude");
    S.excludeData = data;
    renderExcludePage(data);
  } catch (e) { /* ignore */ }
  try {
    const [blocked, recycle] = await Promise.all([api("/api/blocked"), api("/api/recycle")]);
    renderBlockedPage(blocked, recycle);
  } catch (e) { /* ignore */ }
  refreshBlockManage();
}

function renderBlockedPage(blocked, recycle) {
  $("#blockedCount").textContent = `${num(blocked.total)} 条`;
  $("#blockedList").innerHTML = (blocked.items || []).map(x => `<div class="blocked-item">
    ${x.thumb_url
      ? `<img src="${esc(x.thumb_url)}" loading="lazy" alt="">`
      : `<div class="noimg small">无缩略图</div>`}
    <div class="blocked-meta">
      <div><b>${esc(x.person || "—")}</b> · ${esc(x.source_site || "—")} · ${esc(fmtTime(x.created_at))} · ${esc(x.origin)}</div>
      <div class="mono ellip" title="${esc(x.url)}">${esc(x.url)}</div>
      <div class="muted">pHash ${esc(x.phash || "-")} · dHash ${esc(x.dhash || "-")}${x.reason ? ` · 原因：${esc(x.reason)}` : ""}</div>
    </div>
    <button class="ghost" data-unblock="${num(x.id)}">解除屏蔽</button>
  </div>`).join("") || `<span class="muted">暂无屏蔽条目。可在结果图片墙的 ⋯ 菜单中选择「屏蔽」。</span>`;

  $("#recycleCount").textContent = `${num(recycle.total)} 张`;
  $("#recycleList").innerHTML = (recycle.items || []).map(x => `<div class="blocked-item">
    ${x.thumb_url
      ? `<img src="${esc(x.thumb_url)}" loading="lazy" alt="">`
      : `<div class="noimg small">文件缺失</div>`}
    <div class="blocked-meta">
      <div><b>${esc(x.person || "—")}</b> · 删除于 ${esc(fmtTime(x.deleted_at))} · 原状态 ${esc(x.prev_status || "saved")}</div>
      <div class="ellip" title="${esc(x.original_path)}">${esc(x.filename || x.recycle_path)}</div>
      <div class="muted mono ellip" title="${esc(x.recycle_path)}">${esc(x.exists ? x.recycle_path : "回收文件不存在")}</div>
    </div>
    <button class="ghost" data-restore="${num(x.id)}" ${x.exists ? "" : "disabled"}>恢复</button>
  </div>`).join("") || `<span class="muted">回收站为空。删除成品后会出现在这里，可随时恢复。</span>`;
}

function renderExcludePage(data) {
  if (!data) return;
  $("#excludeRoots").innerHTML = (data.roots || []).map(r => `<div class="path-item">
    <span class="p" title="${esc(r.path)}">${esc(r.raw || r.path)}</span><span class="muted">${num(r.files)} 文件</span>
    <button class="ghost" data-remove-exclude="dir" data-value="${esc(r.raw || r.path)}">移除</button>
  </div>`).join("") || `<span class="muted">未配置排除目录</span>`;
  $("#excludeFiles").innerHTML = (data.files || []).map(f => `<div class="path-item">
    <span class="p" title="${esc(f)}">${esc(f)}</span>
    <button class="ghost" data-remove-exclude="file" data-value="${esc(f)}">移除</button>
  </div>`).join("") || `<span class="muted">无</span>`;
  $("#excludeStats").innerHTML = `<dt>已索引文件</dt><dd>${data.indexed}</dd>
    <dt>总大小</dt><dd>${(num(data.total_size) / 1048576).toFixed(1)} MB</dd>
    <dt>pHash 阈值</dt><dd>${data.phash_threshold}（0–16）</dd>
    <dt>裁剪容忍</dt><dd>${data.crop_tolerance ? "开" : "关"}</dd>
    <dt>索引库</dt><dd class="mono">${esc(data.db_path)}</dd>`;
  $("#excludeReport").textContent = (data.rebuild && data.rebuild.report) || "";
  $("#rebuildExclude").disabled = !!(data.rebuild && data.rebuild.running);
  const w = data.watch || {};
  const sync = w.sync || {};
  const healthy = w.running && w.sync_ok && !(w.interruptions || 0) && !(w.pending || 0)
    && !(w.inflight || 0) && !(w.consecutive_errors || 0);
  $("#excludeWatch").innerHTML = `<dt>实时监听</dt><dd>${w.running ? "运行中" : "未运行"}${
    w.error ? `<span class="muted">（${esc(w.error)}）</span>` : ""}</dd>
    <dt>启动对账</dt><dd>${sync.at ? `${esc(sync.at)}（${sync.seconds || 0}s）` : "未完成"}</dd>
    <dt>积压 / 中断</dt><dd>${num(w.pending || 0)} / ${num(w.interruptions || 0)}</dd>
    <dt>任务前扫描</dt><dd>${esc(data.scan_on_run || "auto")}（${healthy ? "可跳过" : "将执行扫描"}）</dd>`;
  const d = data.last_decision || {};
  $("#excludeDecision").textContent = d.at
    ? `${d.at} ${d.skipped ? "跳过扫描" : "已扫描"}：${d.reason}（${d.seconds}s）`
    : "尚无任务运行记录";
}

async function pollExclude() {
  for (let i = 0; i < 120; i++) {
    await new Promise(r => setTimeout(r, 2000));
    const data = await api("/api/exclude");
    renderExcludePage(data);
    if (!(data.rebuild && data.rebuild.running)) {
      toast("排除库索引已更新");
      return;
    }
  }
}

/* ---------------- 账号 ---------------- */
const SITE_NAMES = { xiaohongshu: "小红书", weibo: "微博" };

function bindAuthPage() {
  $("#checkAuthBtn").addEventListener("click", async () => {
    toast("正在检查登录态（会启动无头浏览器，请稍候）…");
    try {
      await api("/api/auth/check", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site: "all" }),
      });
      await refreshAuth();
      toast("检查完成");
    } catch (e) { toast(e.message, true); }
  });
}

async function refreshAuth() {
  try {
    const prev = S.auth;
    S.auth = await api("/api/auth");
    renderAuth();
    applySourceAvailability(S.auth);
    if (prev) {
      for (const site of Object.keys(SITE_NAMES)) {
        const before = (prev.sites || {})[site] || {};
        const now = (S.auth.sites || {})[site] || {};
        if (!before.browser_logged_in && now.browser_logged_in) {
          clearInterval(S.loginTimer);
          toast(now.needs_enable
            ? `登录成功（${SITE_NAMES[site]}），但该站点仍未启用，请点「一键启用」`
            : `登录成功：${SITE_NAMES[site]}`);
        }
      }
    }
  } catch (e) { /* ignore */ }
}

function renderAuth() {
  const a = S.auth;
  if (!a) return;
  const info = a.browser || {};
  const defaults = (info.default_profiles || []).map(p =>
    `<dt>${p.name === "chrome" ? "Chrome" : "Edge"} 默认目录</dt>
     <dd class="mono">${esc(p.path)} ${p.project_profile_inside ? '<span class="bad">（被占用，危险）</span>' : '<span class="muted">（未使用）</span>'}</dd>`).join("");
  $("#browserInfo").innerHTML = `<dt>Playwright</dt><dd class="${info.playwright_installed ? "ok" : "bad"}">${info.playwright_installed ? "已安装" : "未安装（python -m pip install playwright）"}</dd>
    <dt>浏览器通道</dt><dd>${esc(info.channel || "")}（仅复用可执行文件）</dd>
    <dt>持久会话目录（独立）</dt><dd class="mono">${esc(info.profile_dir || "")}</dd>
    <dt>隔离状态</dt><dd class="${info.profile_isolated ? "ok" : "bad"}">${info.profile_isolated ? "已隔离：不使用系统默认 profile" : "危险：占用了系统默认 profile"}</dd>
    ${defaults}
    <dt>Cookie 目录</dt><dd class="mono">${esc(info.cookie_dir || "")}（只读）</dd>`;

  const STATUS = {
    logged_in: { text: "已登录", cls: "ok" },
    logged_out: { text: "未登录", cls: "bad" },
    unknown: { text: "无法确认（需重试）", cls: "warn" },
  };
  $("#authCards").innerHTML = Object.keys(SITE_NAMES).map(site => {
    const s = (a.sites || {})[site] || {};
    const cookie = s.cookie || {};
    const b = s.browser;
    const account = s.account || {};
    const st = STATUS[s.status] || STATUS.unknown;
    const browserText = !b ? "尚未检查" : st.text;
    const browserCls = !b ? "unknown" : st.cls;
    const cookieText = cookie.exists
      ? (cookie.readable ? "已提供（可选）" : "存在但不可读")
      : "未提供（可选，浏览器会话已登录时不需要）";
    const cookieCls = !cookie.exists ? "muted" : (cookie.readable ? "ok" : "warn");
    const login = s.login;
    let who;
    if (account.nickname) who = `<strong class="ok">${esc(account.nickname)}</strong>`;
    else if (account.uid) who = `<strong class="warn">昵称未知</strong>`;
    else who = `<span class="muted">未检测到已登录账号</span>`;
    if (account.uid) who += ` · UID <span class="mono">${esc(account.uid)}</span>`;
    if (account.source) who += ` <span class="muted">(${esc(account.source)})</span>`;
    const enableBanner = s.needs_enable
      ? `<div class="notice warn">登录成功，但「${SITE_NAMES[site]}」仍是停用状态，任务不会访问该站点。
          <button data-act="enable" class="primary">一键启用</button></div>`
      : "";
    return `<article class="auth-card" data-site="${site}">
      <header><span class="dot ${browserCls}"></span><h3>${SITE_NAMES[site]}</h3>
        <span class="badge ${s.enabled ? "" : "warn"}">${s.enabled ? "已启用" : "已停用（任务不会访问）"}</span></header>
      ${enableBanner}
      <dl class="kv">
        <dt>当前登录账号</dt><dd>${who}</dd>
        <dt>浏览器登录</dt><dd class="${browserCls}">${browserText}${b && b.detail ? `（${esc(b.detail)}）` : ""}</dd>
        <dt>Cookie 文件</dt><dd class="${cookieCls}">${esc(cookieText)} · ${esc(cookie.file || "")}</dd>
        <dt>Cookie 字段（仅参考）</dt><dd class="muted">${esc((cookie.keys && cookie.keys.length ? cookie.keys : cookie.expected || []).join(", "))} · 不参与登录判定</dd>
        <dt>最后检查</dt><dd>${esc(s.last_check || "尚未检查")}</dd>
        ${login ? `<dt>登录窗口</dt><dd class="${login.state === "error" ? "bad" : ""}">${esc(login.state || "")} ${esc(login.detail || "")}
          ${login.log_path ? `<div class="hint mono">日志：${esc(login.log_path)}</div>` : ""}
          ${login.state === "error" ? `<button data-act="login" class="primary">重试</button>` : ""}</dd>` : ""}
      </dl>
      <div class="actions">
        <button data-act="login" class="primary">打开登录窗口</button>
        <button data-act="check">检查该站</button>
        <button data-act="logout" class="danger">登出 / 切换账号</button>
      </div>
      <label class="switch-line"><input type="checkbox" data-act="toggle" ${s.enabled ? "checked" : ""}> 启用该站点（停用后任务不再访问）</label>
    </article>`;
  }).join("");

  $$("#authCards [data-act='login']").forEach(btn => btn.addEventListener("click", async () => {
    const site = btn.closest(".auth-card").dataset.site;
    try {
      const res = await api("/api/auth/open-login", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ site }),
      });
      toast(res.message || "已打开登录窗口");
      clearInterval(S.loginTimer);
      S.loginTimer = setInterval(refreshAuth, 2500);
    } catch (e) { toast(e.message, true); }
  }));
  $$("#authCards [data-act='enable']").forEach(btn => btn.addEventListener("click", async () => {
    const site = btn.closest(".auth-card").dataset.site;
    try {
      const res = await api("/api/auth/toggle", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site, enabled: true }),
      });
      toast(res.message);
      await refreshAuth();
      renderSettings();
    } catch (e) { toast(e.message, true); }
  }));
  $$("#authCards [data-act='check']").forEach(btn => btn.addEventListener("click", async () => {
    const site = btn.closest(".auth-card").dataset.site;
    toast(`正在检查 ${SITE_NAMES[site]}…`);
    try {
      await api("/api/auth/check", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ site }),
      });
      await refreshAuth();
    } catch (e) { toast(e.message, true); }
  }));
  $$("#authCards [data-act='logout']").forEach(btn => btn.addEventListener("click", async () => {
    const site = btn.closest(".auth-card").dataset.site;
    if (!confirm(`确定退出 ${SITE_NAMES[site]}？只清理该站点会话，另一个站点不受影响。`)) return;
    toast("正在清理该站点会话…");
    try {
      const res = await api("/api/auth/logout", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ site }),
      });
      toast((res.warning ? res.warning + " " : "") + (res.detail || "已退出"));
      await refreshAuth();
    } catch (e) { toast(e.message, true); }
  }));
  $$("#authCards [data-act='toggle']").forEach(box => box.addEventListener("change", async () => {
    const site = box.closest(".auth-card").dataset.site;
    try {
      const res = await api("/api/auth/toggle", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site, enabled: box.checked }),
      });
      toast(res.message);
      await refreshAuth();
      renderSettings();
    } catch (e) {
      box.checked = !box.checked;
      toast(e.message, true);
    }
  }));
  $("#verifyIsolation").addEventListener("click", async () => {
    toast("正在启动一次无头浏览器并对比系统默认 profile…");
    try {
      const ev = await api("/api/auth/isolation?verify=1");
      const untouched = ev.default_profile_untouched ? '<span class="ok">未改动</span>' : '<span class="bad">有变化</span>';
      const match = ev.browser_profile_matches ? '<span class="ok">一致</span>' : '<span class="warn">未读取到</span>';
      $("#isolationResult").innerHTML = `<dt>实际 profile 路径</dt><dd class="mono">${esc(ev.browser_reported_profile_path || "（chrome://version 未读取到）")}</dd>
        <dt>与项目目录一致</dt><dd>${match}</dd>
        <dt>系统默认 profile 文件</dt><dd>检查 ${ev.default_files_checked} 个 · ${untouched}${(ev.default_files_changed || []).length ? `（变化：${esc(ev.default_files_changed.join(", "))}）` : ""}</dd>
        <dt>项目 profile 文件数</dt><dd>${ev.project_profile_files}</dd>
        <dt>检查时间</dt><dd>${esc(ev.checked_at)}</dd>`;
      toast(ev.default_profile_untouched ? "隔离验证通过：系统默认 profile 未被改动" : "注意：系统默认 profile 有文件变化，请查看详情", !ev.default_profile_untouched);
    } catch (e) { toast(e.message, true); }
  });
}

/* ---------------- 目录选择 ---------------- */
function bindDirModal() {
  $("#dirClose").addEventListener("click", closeDirPicker);
  $("#dirUp").addEventListener("click", () => {
    const parent = $("#dirUp").dataset.parent;
    if (parent) loadFs(parent);
  });
  $("#dirPick").addEventListener("click", () => {
    if (!S.dirPath) { toast("请先进入一个目录", true); return; }
    const cb = S.dirCb;
    closeDirPicker();
    if (cb) cb(S.dirPath);
  });
  $("#dirModal").addEventListener("click", e => { if (e.target.id === "dirModal") closeDirPicker(); });
}

function openDirPicker(cb, start = "") {
  S.dirCb = cb;
  $("#dirModal").hidden = false;
  loadFs(start);
}

function closeDirPicker() {
  $("#dirModal").hidden = true;
  S.dirCb = null;
}

async function loadFs(path) {
  try {
    const data = await api(`/api/fs?path=${encodeURIComponent(path || "")}`);
    S.dirPath = data.path || "";
    $("#dirPath").textContent = S.dirPath || "选择磁盘";
    const up = $("#dirUp");
    up.dataset.parent = data.parent || "";
    up.disabled = !data.parent && !data.path;
    $("#dirList").innerHTML = (data.dirs || []).map(d => `<button data-path="${esc(d)}">${esc(d)}</button>`).join("") || `<span class="muted">（无子目录）</span>`;
    $$("#dirList [data-path]").forEach(btn => btn.addEventListener("click", () => loadFs(btn.dataset.path)));
    $("#dirPick").disabled = !S.dirPath;
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 大图 / 灯箱 ---------------- */
function bindLightbox() {
  $("#lightbox").addEventListener("click", e => { if (e.target.id === "lightbox") closeLightbox(); });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      if ($("#lbConfirm") && !$("#lbConfirm").hidden) { $("#lbConfirmCancel").click(); return; }
      closeLightbox(); closeDirPicker(); return;
    }
    if ($("#lightbox").hidden || !S.lb) return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (e.key === "ArrowLeft") { e.preventDefault(); lbGo(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); lbGo(1); }
  });
  document.addEventListener("click", e => {
    const lbBtn = e.target.closest("[data-lb]");
    if (lbBtn && !$("#lightbox").hidden) { e.preventDefault(); lbAct(lbBtn.dataset.lb, lbBtn); return; }
    const card = e.target.closest("#resultsGrid .img-card, #personGallery .img-card");
    if (!card) return;
    if (e.target.closest("button, input, a")) return;
    try {
      const it = JSON.parse(decodeURIComponent(card.dataset.it));
      openLightbox(it, galleryKindOf(card));
    } catch { /* ignore */ }
  });
  document.addEventListener("change", e => {
    if (e.target.id !== "lbSel" || !S.lb) return;
    const it = lbCurrent();
    if (!it || !S.lb.kind) return;
    const sel = Gallery[S.lb.kind].sel;
    if (e.target.checked) sel.add(num(it.id)); else sel.delete(num(it.id));
    syncGalleryChecks(S.lb.kind);
    lbRefreshSelBar();
  });
}

function fileUrlOf(path) {
  return "/api/file?path=" + encodeURIComponent(String(path || ""));
}

function lbItems() {
  const lb = S.lb;
  if (!lb) return [];
  return lb.kind ? Gallery[lb.kind].items : (lb.single || []);
}

function lbCurrent() {
  const lb = S.lb;
  if (!lb) return null;
  return lbItems()[lb.index] || null;
}

function lbHasMore() {
  const lb = S.lb;
  return !!(lb && lb.kind && Gallery[lb.kind].hasMore);
}

function openLightbox(it, kind) {
  if (!it) return;
  let k = kind || "";
  if (!k && (S.route === "results" || S.route === "person")) {
    const guess = S.route === "person" ? "person" : "results";
    if (Gallery[guess].items.some(x => String(x.id) === String(it.id))) k = guess;
  }
  const list = k ? Gallery[k].items : [];
  let idx = k ? list.findIndex(x => String(x.id) === String(it.id)) : -1;
  S.lb = {
    kind: idx >= 0 ? k : "",
    index: Math.max(0, idx),
    single: idx >= 0 ? null : [it],
    view: "current",
    preview: null,
    srPanel: false,
    srBusy: false,
    srMode: "",
    srError: "",
    srProgress: "",
    srStart: 0,
    srTimer: null,
    waitJob: false,
    jobPct: null,
    batchBusy: false,
    batchCancel: false,
    batchState: "",
  };
  $("#lightbox").hidden = false;
  document.body.classList.add("lb-open");
  renderLightbox();
}

function closeLightbox() {
  const lb = S.lb;
  if (lb && lb.srTimer) { clearInterval(lb.srTimer); lb.srTimer = null; }
  if (lb) { lb.batchCancel = true; }
  $("#lightbox").hidden = true;
  document.body.classList.remove("lb-open");
  $("#lightboxInner").innerHTML = "";
  S.lb = null;
}

function lbSetImage(url) {
  const img = $("#lbImg"), note = $("#lbImgNote");
  if (!img) return;
  if (!url) {
    img.removeAttribute("src");
    img.style.visibility = "hidden";
    if (note) { note.hidden = false; note.textContent = "文件缺失（可能在回收站，或已被超分替换）"; }
    return;
  }
  if (note) note.hidden = true;
  img.style.visibility = "visible";
  img.classList.add("lb-fading");
  const pre = new Image();
  pre.onload = () => {
    if (!S.lb || !$("#lbImg")) return;
    img.src = url;
    requestAnimationFrame(() => img.classList.remove("lb-fading"));
  };
  pre.onerror = () => { img.classList.remove("lb-fading"); if (note) { note.hidden = false; note.textContent = "图片加载失败"; } };
  pre.src = url;
}

function lbDisplayUrl(it) {
  const lb = S.lb;
  if (!lb) return "";
  if (lb.view === "orig") {
    if (it.can_undo_sr && it.backup_path) return fileUrlOf(it.backup_path);
    return it.image_url || it.raw_image_url || it.thumb_url || "";
  }
  if (lb.view === "sr") {
    if (lb.preview && String(lb.preview.id) === String(it.id)) return lb.preview.sr_url;
    if (it.has_sr) return it.sr_image_url || it.image_url;
  }
  return it.image_url || it.raw_image_url || it.thumb_url || "";
}

function lbCanCompare(it) {
  const lb = S.lb;
  if (!lb) return false;
  if (lb.preview && String(lb.preview.id) === String(it.id)) return true;
  return !!(it.has_sr || (it.can_undo_sr && it.backup_path));
}

function lbResolution(it) {
  if (it.sr_replaced && it.has_sr && it.sr_width) return `${it.sr_width}×${it.sr_height}（超分）`;
  return `${it.width || "-"}×${it.height || "-"}`;
}

function renderLightbox() {
  const lb = S.lb;
  if (!lb) return;
  const it = lbCurrent();
  if (!it) { closeLightbox(); return; }
  if (lb.preview && String(lb.preview.id) !== String(it.id)) lb.preview = null;
  const items = lbItems();
  const atFirst = lb.index <= 0;
  const atLast = lb.index >= items.length - 1;
  const canNext = !atLast || lbHasMore();
  const sel = lb.kind ? Gallery[lb.kind].sel : new Set();
  const isSel = sel.has(num(it.id));
  const compare = lbCanCompare(it);
  const showPreviewState = lb.preview && String(lb.preview.id) === String(it.id);
  const srInfo = (it.has_sr && it.sr_model)
    ? `<div class="lb-kv">超分：${esc(it.sr_model)} ${it.sr_scale ? `${it.sr_scale}x` : ""}${it.sr_face ? ` · ${esc(it.sr_face)}` : ""}`
      + `${it.sr_replaced ? ` · <b>已替换原图</b>` : " · 已保存（未替换）"}${it.sr_at ? ` · ${esc(fmtTime(it.sr_at))}` : ""}</div>`
    : "";
  const modeText = { keep: "另存", backup: "替换（可恢复）", overwrite: "覆盖" }[it.sr_replace_mode || ""] || "";

  $("#lightboxInner").innerHTML = `
    <button type="button" class="lb-nav lb-prev" data-lb="prev" ${atFirst ? "disabled" : ""} title="${atFirst ? "已是第一张" : "上一张（←）"}">‹</button>
    <div class="lb-body">
      <div class="lb-main">
        <div class="lb-stage">
          <img id="lbImg" alt="">
          <div class="lb-img-note" id="lbImgNote" hidden></div>
        </div>
        <div class="lb-toolbar">
          <button type="button" class="lb-icon" data-lb="source" title="打开来源页面">↗</button>
          <button type="button" class="lb-icon" data-lb="copy-url" title="复制图片地址">⧉</button>
          <button type="button" class="lb-icon" data-lb="copy-path" title="复制文件路径">⌂</button>
          ${compare ? `<button type="button" class="lb-icon ${lb.view !== "current" ? "on" : ""}" data-lb="compare">${lb.view === "orig" ? "↩ 回到超分结果" : "⇄ 对比原图"}</button>` : ""}
          <span class="lb-count">${lb.index + 1} / ${items.length}${lbHasMore() ? "+" : ""}${lb.view !== "current" ? ` · ${lb.view === "orig" ? "原图" : "超分"}` : ""}</span>
        </div>
        <div class="lb-selbar">
          <label class="lb-sel-toggle"><input type="checkbox" id="lbSel" ${isSel ? "checked" : ""} ${lb.kind ? "" : "disabled"}> 选中</label>
          <span id="lbSelCount" class="muted">${lb.kind ? `已选 ${sel.size} 张` : "（待复核单张模式不支持多选）"}</span>
          <button type="button" class="mini" data-lb="batch-delete" ${lb.kind && sel.size ? "" : "disabled"}>批量删除</button>
          <button type="button" class="mini" data-lb="batch-block" ${lb.kind && sel.size ? "" : "disabled"}>批量屏蔽</button>
          <input id="lbBatchLimit" type="number" min="0" max="9999" placeholder="上限" title="批量超分上限，0 或留空表示全部" ${lb.kind && sel.size ? "" : "disabled"}>
          <button type="button" class="mini" data-lb="batch-sr" ${lb.kind && sel.size ? "" : "disabled"}>批量超分</button>
          <button type="button" class="mini ghost" data-lb="batch-clear" ${lb.kind && sel.size ? "" : "disabled"}>清空选择</button>
          <span id="lbBatchState" class="muted">${esc(lb.batchState)}</span>
          ${lb.batchBusy ? `<button type="button" class="mini danger" data-lb="batch-cancel">取消批量</button>` : ""}
        </div>
      </div>
      <div class="lightbox-meta">
        <div class="lb-title"><b>${esc(it.filename || "")}</b> ${framingBadge(it)}</div>
        <div class="lb-kv">${it.person ? `人员：${esc(it.person)}` : "人员：—"}</div>
        <div class="lb-kv">分辨率：${esc(lbResolution(it))}</div>
        <div class="lb-kv">sim ${it.similarity != null ? Number(it.similarity).toFixed(3) : "-"} · 清晰度 ${it.clarity != null ? Number(it.clarity).toFixed(1) : "-"}</div>
        <div class="lb-kv">站点：${esc(it.source || "—")}</div>
        ${it.framing_reason ? `<div class="lb-kv">构图：${esc(it.framing_reason)}</div>` : ""}
        <div class="lb-kv">来源页面：<a href="${esc(it.page_url || it.url || "#")}" target="_blank" rel="noreferrer">打开</a></div>
        <div class="lb-kv">图片地址：<a href="${esc(it.url || "#")}" target="_blank" rel="noreferrer">${esc((it.url || "").slice(0, 64))}</a></div>
        <div class="lb-kv lb-path" title="${esc(it.file_path || "")}">文件：${esc(it.file_path || "—")}</div>
        ${srInfo}
        ${showPreviewState ? `<div class="lb-kv ok">超分预览：${esc(lb.preview.model)} ${lb.preview.scale}x · ${lb.preview.elapsed}s${lb.preview.peak_vram_gb ? ` · 峰值 ${lb.preview.peak_vram_gb}GB` : ""}（未保存）</div>` : ""}
        ${modeText && it.has_sr ? `<div class="lb-kv">保存方式：${esc(modeText)}</div>` : ""}
        <div class="lb-actions">
          <button type="button" data-lb="sr" ${lb.srBusy ? "disabled" : ""}>超分</button>
          <button type="button" class="danger" data-lb="delete" ${lb.batchBusy ? "disabled" : ""}>删除</button>
          <button type="button" data-lb="block" ${it.blocked ? "disabled" : ""}>${it.blocked ? "已屏蔽" : "屏蔽"}</button>
        </div>
        ${it.can_undo_sr ? `<div class="lb-actions"><button type="button" class="ghost" data-lb="sr-undo" ${lb.srBusy ? "disabled" : ""}>撤销超分（恢复原图）</button></div>` : ""}
        <div class="lb-sr-panel" ${lb.srPanel ? "" : "hidden"}>
          <div class="lb-sr-head">超分设置 <button type="button" class="mini ghost" data-lb="sr-close">收起</button></div>
          <label class="lb-radio"><input type="radio" name="lbSrMode" value="preview" ${lb.srMode !== "backup" ? "checked" : ""}> <span>仅生成预览 <span class="muted">（不保存，可对比后再决定）</span></span></label>
          <label class="lb-radio"><input type="radio" name="lbSrMode" value="backup" ${lb.srMode === "backup" ? "checked" : ""}> <span>替换原图 <span class="muted">（可恢复：原图移入回收站备份）</span></span></label>
          <div class="lb-kv muted">参数（设置页默认）：${esc(srDefaults().model)} · ${srDefaults().scale}x · 面部修复 ${esc(srDefaults().face_restore)} · 长边 ${srDefaults().max_side}px</div>
          <div class="lb-sr-state" id="lbSrState">${esc(lb.srError || lb.srProgress || "")}</div>
          <div class="lb-sr-btns">
            <button type="button" id="lbSrGo" data-lb="sr-run" ${lb.srBusy ? "disabled" : ""}>${lb.srBusy ? "处理中…" : "开始超分"}</button>
            <button type="button" class="ghost" id="lbSrCancel" data-lb="sr-cancel" ${lb.waitJob ? "" : "hidden"}>取消任务</button>
          </div>
        </div>
        <div class="lb-close-row"><button id="lightboxClose" type="button" class="ghost" data-lb="close">关闭（Esc）</button></div>
      </div>
    </div>
    <button type="button" class="lb-nav lb-next" data-lb="next" ${canNext ? "" : "disabled"} title="${canNext ? "下一张（→）" : "已是最后一张"}">›</button>`;
  lbSetImage(lbDisplayUrl(it));
  $$('input[name="lbSrMode"]', $("#lightboxInner")).forEach(r =>
    r.addEventListener("change", () => { if (S.lb) S.lb.srMode = r.value; }));
  $("#lbBatchLimit").addEventListener("input", () => { if (S.lb) S.lb.batchLimit = $("#lbBatchLimit").value; });
  if (S.lb.batchLimit) $("#lbBatchLimit").value = S.lb.batchLimit;
  if (lb.srBusy) lbTick(elapsedText(lb.srStart));
}

function elapsedText(t0) {
  return t0 ? `${((Date.now() - t0) / 1000).toFixed(1)}s` : "…";
}

function lbTick(text) {
  const lb = S.lb;
  if (!lb || !lb.srBusy) return;
  const state = $("#lbSrState");
  const go = $("#lbSrGo");
  if (state) state.textContent = lb.srError || lb.srProgress || text || "";
  if (go) { go.disabled = true; go.textContent = lb.srMode === "preview" ? `推理中 ${elapsedText(lb.srStart)}` : "超分任务进行中…"; }
}

function lbStartTimer() {
  const lb = S.lb;
  if (!lb) return;
  if (lb.srTimer) clearInterval(lb.srTimer);
  lb.srTimer = setInterval(() => { if (!S.lb || !S.lb.srBusy) { clearInterval(lb.srTimer); return; } lbTick(); }, 300);
}

function lbStopTimer() {
  const lb = S.lb;
  if (lb && lb.srTimer) { clearInterval(lb.srTimer); lb.srTimer = null; }
}

function lbRefreshSelBar() {
  const lb = S.lb;
  if (!lb) return;
  const it = lbCurrent();
  if (!it) return;
  const sel = lb.kind ? Gallery[lb.kind].sel : new Set();
  const count = $("#lbSelCount");
  if (count) count.textContent = lb.kind ? `已选 ${sel.size} 张` : "（待复核单张模式不支持多选）";
  const cb = $("#lbSel");
  if (cb) cb.checked = lb.kind ? sel.has(num(it.id)) : false;
  $$('[data-lb^="batch-"]', $("#lightboxInner")).forEach(b => {
    if (b.tagName === "INPUT") b.disabled = !(lb.kind && sel.size);
    else b.disabled = !(lb.kind && sel.size);
  });
}

async function lbGo(delta) {
  const lb = S.lb;
  if (!lb || lb.batchBusy) return;
  if (lb.srBusy) { toast("超分任务进行中，完成或取消后再切换", true); return; }
  let ni = lb.index + delta;
  if (delta < 0 && ni < 0) { toast("已是第一张"); return; }
  if (delta > 0 && ni >= lbItems().length) {
    if (!(lb.kind && Gallery[lb.kind].hasMore)) { toast("已是最后一张"); return; }
    await loadGallery(lb.kind, false);
    if (!S.lb) return;
    if (ni >= lbItems().length) { toast("已是最后一张"); renderLightbox(); return; }
  }
  lb.index = ni;
  lb.view = "current";
  lb.srPanel = false;
  lb.srError = "";
  lb.srProgress = "";
  renderLightbox();
}

async function lbAct(action, btn) {
  const lb = S.lb;
  if (!lb) return;
  const it = lbCurrent();
  if (!it) return;
  if (lb.batchBusy && action !== "batch-cancel") { toast("批量处理中，请等待完成或取消", true); return; }
  switch (action) {
    case "prev": lbGo(-1); return;
    case "next": lbGo(1); return;
    case "close": closeLightbox(); return;
    case "source": {
      const url = it.page_url || it.url;
      if (!url) { toast("没有来源页面地址", true); return; }
      window.open(url, "_blank", "noopener");
      return;
    }
    case "copy-url": {
      if (!it.url) { toast("没有图片地址", true); return; }
      copyText(it.url, "图片地址"); return;
    }
    case "copy-path": {
      const p = (it.sr_replaced && it.sr_path) ? it.sr_path : it.file_path;
      if (!p) { toast("没有文件路径", true); return; }
      copyText(p, "文件路径"); return;
    }
    case "compare": {
      lb.view = lb.view === "orig" ? "current" : "orig";
      renderLightbox(); return;
    }
    case "sr": lb.srPanel = !lb.srPanel; renderLightbox(); return;
    case "sr-close": lb.srPanel = false; renderLightbox(); return;
    case "sr-run": runLbSr(); return;
    case "sr-cancel": {
      try { await api("/api/sr/cancel", { method: "POST" }); lb.srProgress = "正在取消…"; lbTick(); }
      catch (e) { toast(e.message, true); }
      return;
    }
    case "sr-undo": undoLbSr(it); return;
    case "delete": lbDelete(it); return;
    case "block": lbBlock(it); return;
    case "batch-delete": lbBatch("delete"); return;
    case "batch-block": lbBatch("block"); return;
    case "batch-sr": lbBatch("sr"); return;
    case "batch-cancel": {
      lb.batchCancel = true;
      lb.batchState = (lb.batchState || "") + "（正在取消，等当前批次完成）";
      renderLightbox();
      return;
    }
    case "batch-clear": {
      if (!lb.kind) return;
      clearGallerySelection(lb.kind);
      lb.batchState = "";
      renderLightbox();
      return;
    }
  }
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${label}已复制`);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); toast(`${label}已复制`); }
    catch { toast("复制失败（浏览器拒绝访问剪贴板）", true); }
    finally { ta.remove(); }
  }
}

function lbAfterRemove(id) {
  const lb = S.lb;
  if (!lb) return;
  if (lb.kind) removeGalleryCards(lb.kind, [id]);
  else lb.single = lb.single.filter(x => String(x.id) !== String(id));
  const arr = lbItems();
  if (!arr.length) {
    closeLightbox();
    toast("列表已空，灯箱已关闭");
    return;
  }
  if (lb.index >= arr.length) lb.index = arr.length - 1;
  renderLightbox();
}

function lbConfirm(title, body, okText = "确认", danger = true) {
  return new Promise(resolve => {
    const box = $("#lbConfirm");
    $("#lbConfirmTitle").textContent = title;
    $("#lbConfirmBody").textContent = body;
    const ok = $("#lbConfirmOk"), cancel = $("#lbConfirmCancel");
    ok.textContent = okText;
    ok.classList.toggle("danger", danger);
    box.hidden = false;
    const done = value => {
      box.hidden = true;
      ok.onclick = null;
      cancel.onclick = null;
      resolve(value);
    };
    ok.onclick = () => done(true);
    cancel.onclick = () => done(false);
    setTimeout(() => ok.focus(), 0);
  });
}

async function lbDelete(it) {
  const person = it.person || "<人名>";
  const kind = S.lb ? S.lb.kind : "";
  const yes = await lbConfirm(
    "删除这张图？",
    `《${it.filename || it.id}》\n\n` +
    `文件会移入回收站（不是永久删除）：\n  data/recycle/${person}/\n\n` +
    "可在「排除库 → 回收站」里恢复。若这张还需要永久不再出现，请用「屏蔽」。",
    "移入回收站");
  if (!yes) return;
  try {
    const res = await api("/api/gallery/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: [it.id] }),
    });
    const errs = res.errors || [];
    const done = (res.deleted || []).some(x => String(x.id) === String(it.id));
    if (!done) { toast(errs[0] || "删除失败", true); return; }
    toast(`已移入回收站：data/recycle/${person}/（可恢复）`);
    lbAfterRemove(it.id);
    if (kind) refreshGalleryMeta(kind);
  } catch (e) { toast(e.message, true); }
}

async function lbBlock(it) {
  const yes = await lbConfirm(
    "屏蔽这张图？",
    `《${it.filename || it.id}》\n\n` +
    "屏蔽后永久不再出现：图片墙 / 筛选结果 / 后续采集都不会再拉回。\n" +
    "不会删除文件，原文件仍在。\n\n" +
    "可在「排除库 → 屏蔽管理」解除屏蔽。",
    "屏蔽");
  if (!yes) return;
  try {
    const res = await api("/api/block/images", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: [it.id] }),
    });
    const errs = res.errors || [];
    if (!(res.blocked || []).length) { toast(errs[0] || "屏蔽失败", true); return; }
    toast("已屏蔽（文件未删除，可在屏蔽管理恢复）");
    lbAfterRemove(it.id);
  } catch (e) { toast(e.message, true); }
}

/* ---------- 灯箱内超分 ---------- */
function srErrText(msg) {
  const s = String(msg || "");
  if (/显存|out of memory|OOM|CUDA/i.test(s)) {
    return `显存不足：${s}\n建议：先取消/等待其它超分任务，或把长边上限调低（如 2048）后重试。`;
  }
  if (/未安装|未加载|not available|missing|权重/i.test(s)) {
    return `模型未加载：${s}\n建议：到「设置 → 超分」检查模型是否已下载。`;
  }
  return s || "未知错误";
}

async function runLbSr() {
  const lb = S.lb;
  const it = lbCurrent();
  if (!lb || !it || lb.srBusy) return;
  if (S.job && ["running", "paused", "stopping"].includes(S.job.state)) {
    toast("下载任务正在运行，请等待完成后再超分（避免 GPU 争用）", true);
    return;
  }
  const modeEl = document.querySelector('input[name="lbSrMode"]:checked');
  const mode = modeEl ? modeEl.value : "preview";
  const p = srDefaults();
  lb.srMode = mode;
  lb.srError = "";
  lb.srProgress = "";
  lb.srBusy = true;
  lb.srStart = Date.now();
  lbTick();
  lbStartTimer();
  try {
    if (mode === "preview") {
      const res = await api("/api/sr/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ id: it.id }, p)),
      });
      lb.preview = res;
      lb.view = "sr";
      lb.batchState = "";
      toast(`超分预览完成：${res.model} ${res.scale}x · ${res.elapsed}s（未保存）`);
    } else {
      const st = await api("/api/sr/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ ids: [it.id], replace_mode: "backup" }, p)),
      });
      lb.waitJob = true;
      lb.srProgress = `任务已提交：${st.total || 1} 张，等待 GPU…`;
      lbTick();
      await lbWaitSr(it);
      return;
    }
  } catch (e) {
    lb.srError = srErrText(e.message);
    toast(lb.srError, true);
  } finally {
    if (!lb.waitJob) {
      lb.srBusy = false;
      lbStopTimer();
      if (S.lb === lb) renderLightbox();
    }
  }
}

async function lbWaitSr(it) {
  const lb = S.lb;
  while (S.lb === lb && lb.waitJob) {
    await new Promise(r => setTimeout(r, 500));
    if (S.lb !== lb) return;
    let st = null;
    try { st = await api("/api/sr/status"); } catch { continue; }
    const active = !!st.busy || st.state === "running" || st.state === "stopping";
    lb.jobPct = st;
    S.sr.jobState = st;   // 与全局轮询保持一致，避免刚完成时按钮仍被判定为「有任务在跑」
    if (active) {
      lb.srProgress = `${num(st.done)}/${num(st.total)} 张 · ${num(st.pct)}%${st.current ? ` · ${st.current}` : ""}`;
      lbTick();
      continue;
    }
    lb.waitJob = false;
    lb.srBusy = false;
    lbStopTimer();
    const failed = num(st.failed);
    if (st.state === "error") {
      lb.srError = srErrText(st.error);
      toast(lb.srError, true);
    } else if (st.state === "stopped") {
      lb.batchState = `超分已取消：完成 ${num(st.done)}/${num(st.total)} 张`;
    } else {
      lb.batchState = `超分完成：${num(st.done)}/${num(st.total)} 张${failed ? `，失败 ${failed} 张` : ""}`;
      if (failed) {
        const bad = (st.items || []).filter(x => !x.ok).map(x => `#${x.id}`).slice(0, 4);
        if (bad.length) lb.batchState += `（失败：${bad.join(" ")}，详见任务进度/日志）`;
      }
      toast(`超分完成，结果已显示在灯箱内`);
      lb.view = "current";
    }
    await lbReloadCurrent(it.id);
    return;
  }
}

async function lbReloadCurrent(keepId) {
  const lb = S.lb;
  if (!lb) return;
  if (!lb.kind) { renderLightbox(); return; }
  await loadGallery(lb.kind, true);
  if (S.lb !== lb) return;
  const idx = Gallery[lb.kind].items.findIndex(x => String(x.id) === String(keepId));
  if (idx < 0) {
    if (Gallery[lb.kind].items.length) { lb.index = Math.min(lb.index, Gallery[lb.kind].items.length - 1); }
    else { closeLightbox(); return; }
  } else {
    lb.index = idx;
  }
  renderLightbox();
}

async function undoLbSr(it) {
  const yes = await lbConfirm(
    "撤销超分替换？",
    "原图会从 data/recycle/sr_backup 恢复为正式成品；\n" +
    "SR 结果移入 data/recycle/sr_discard（不物理删除）。",
    "撤销超分");
  if (!yes) return;
  try {
    const res = await api("/api/sr/undo", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: [it.id] }),
    });
    const errs = res.errors || [];
    if (!num(res.restored)) { toast(errs[0]?.error || "撤销失败", true); return; }
    toast("已撤销超分，原图已恢复");
    const lb = S.lb;
    if (lb) { lb.view = "current"; lb.preview = null; }
    await lbReloadCurrent(it.id);
  } catch (e) { toast(e.message, true); }
}

/* ---------- 灯箱批量操作 ---------- */
function lbSelectedIds() {
  const lb = S.lb;
  if (!lb || !lb.kind) return [];
  return Array.from(Gallery[lb.kind].sel);
}

async function lbBatch(action) {
  const lb = S.lb;
  if (!lb || !lb.kind) return;
  const ids = lbSelectedIds();
  if (!ids.length) { toast("请先勾选图片", true); return; }
  if (S.sr.jobState && (S.sr.jobState.busy || ["running", "stopping"].includes(S.sr.jobState.state))) {
    toast("已有超分任务在运行，请等待完成或先取消", true);
    return;
  }
  if (action === "sr") {
    const limit = num($("#lbBatchLimit") ? $("#lbBatchLimit").value : 0);
    const target = limit > 0 ? ids.slice(0, limit) : ids;
    if (!target.length) { toast("没有可超分的图片", true); return; }
    const yes = await lbConfirm(
      `批量超分 ${target.length} 张？`,
      `${limit > 0 && limit < ids.length ? `已按上限截取 ${limit} 张（共选 ${ids.length} 张）。\n` : ""}` +
      `保存方式：超分并替换（可恢复）\n原图移入 data/recycle/sr_backup/<人名>/，可逐张撤销。\n` +
      `参数：${srDefaults().model} · ${srDefaults().scale}x · ${srDefaults().face_restore} · 长边 ${srDefaults().max_side}px\n\n` +
      "任务期间请勿关闭页面，可在进度条上取消。",
      "开始批量超分");
    if (!yes) return;
    lb.batchBusy = true;
    lb.batchState = `批量超分：提交 ${target.length} 张…`;
    renderLightbox();
    try {
      const st = await api("/api/sr/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ ids: target, replace_mode: "backup" }, srDefaults())),
      });
      lb.batchBusy = false;
      lb.waitJob = true;
      lb.srBusy = true;
      lb.srStart = Date.now();
      lb.srMode = "batch";
      lb.batchState = `批量超分中：0/${num(st.total) || target.length}`;
      renderLightbox();
      const first = target[0];
      await lbWaitSr({ id: first });
    } catch (e) {
      lb.batchBusy = false;
      lb.batchState = srErrText(e.message);
      toast(lb.batchState, true);
      renderLightbox();
    }
    return;
  }
  const label = action === "delete" ? "删除" : "屏蔽";
  const msg = action === "delete"
    ? `文件移入回收站 data/recycle/<人名>/，可在「排除库 → 回收站」恢复。`
    : `永久不再出现（显示与采集），但不删除文件；可在「排除库 → 屏蔽管理」恢复。`;
  const yes = await lbConfirm(`批量${label} ${ids.length} 张？`, msg, `批量${label}`);
  if (!yes) return;
  lb.batchBusy = true;
  lb.batchCancel = false;
  const total = ids.length;
  let done = 0, failed = 0, skipped = 0;
  const errors = [];
  renderLightbox();
  const chunk = 10;
  for (let i = 0; i < total; i += chunk) {
    if (!S.lb || lb.batchCancel) break;
    const part = ids.slice(i, i + chunk);
    try {
      const path = action === "delete" ? "/api/gallery/delete" : "/api/block/images";
      const res = await api(path, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: part }),
      });
      const okIds = action === "delete" ? (res.deleted || []).map(x => x.id) : (res.blocked || []);
      done += okIds.length;
      skipped += part.length - okIds.length;
      const errs = res.errors || [];
      if (errs.length) { failed += errs.length; errors.push(...errs); }
      removeGalleryCards(lb.kind, okIds);
    } catch (e) {
      failed += part.length;
      errors.push(e.message);
    }
    const state = $("#lbBatchState");
    if (state) state.textContent = `批量${label}：${Math.min(i + chunk, total)}/${total} · 成功 ${done} · 失败 ${failed}`;
  }
  lb.batchBusy = false;
  if (!S.lb || S.lb !== lb) return;
  const cancelled = lb.batchCancel;
  lb.batchState = `批量${label}${cancelled ? "已取消" : "完成"}：成功 ${done}，失败 ${failed}，跳过 ${skipped}`
    + (errors.length ? `；首个错误：${errors[0]}` : "");
  toast(lb.batchState, failed > 0);
  const arr = lbItems();
  if (arr.length && lb.index >= arr.length) lb.index = arr.length - 1;
  if (!arr.length) { closeLightbox(); return; }
  renderLightbox();
}


init();
