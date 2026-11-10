/* SparkTech — клиентская логика.
   Работает только с локальным API (относительные URL). Данные телефона никуда
   не отправляются: все запросы идут на 127.0.0.1, где запущен сам SparkTech. */
"use strict";

const TOKEN = window.__SPARKTECH_TOKEN__ || "";

const state = {
  devices: [],
  serial: null,
  sdk: null,
  model: null,
  settings: {},
  scan: null,          // результат последнего сканирования
  scanId: null,
  items: [],           // все элементы результата
  selected: new Set(),
  hidden: new Set(),   // «оставленные» пользователем (скрыты из списка)
  filters: { query: "", category: "", risk: "", sort: "size", desc: true },
  pageSize: 100,
  shown: 100,
  scanJob: null,
  actions: [],
  selectedActions: new Set(),
  actionsMeta: {},
};

/* ----------------------------------------------------------------- утилиты */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.appendChild(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
  }
  return el;
}

function fmtBytes(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let v = Number(bytes), i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i === 0 ? 0 : v >= 100 ? 0 : 1)} ${units[i]}`;
}

function plural(n, one, few, many) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return `${n} ${one}`;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${n} ${few}`;
  return `${n} ${many}`;
}

function fmtDate(epoch) {
  if (!epoch) return "дата неизвестна";
  try {
    return new Date(epoch * 1000).toLocaleDateString("ru-RU", { year: "numeric", month: "short", day: "numeric" });
  } catch { return "дата неизвестна"; }
}

function toast(message, kind = "info", ttl = 5200) {
  const stack = $("#toast-stack");
  const el = h("div", { class: `toast ${kind}` }, message);
  stack.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 220); }, ttl);
}

async function api(path, { method = "GET", body = null, timeout = 90000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const res = await fetch(path, {
      method,
      headers: {
        "X-SparkTech-Token": TOKEN,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
      cache: "no-store",
    });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (!res.ok || (data && data.ok === false)) {
      const message = (data && (data.error || data.message)) || `Ошибка ${res.status}`;
      const err = new Error(message);
      err.status = res.status;
      err.code = data && data.code;
      throw err;
    }
    return data;
  } catch (err) {
    if (err.name === "AbortError") throw new Error("Превышено время ожидания ответа приложения");
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/* ------------------------------------------------------------------- навигация */
const VIEWS = ["connect", "clean", "optimize", "log", "settings"];

function switchView(name) {
  if (!VIEWS.includes(name)) return;
  const navBtn = $(`.nav-item[data-view="${name}"]`);
  if (navBtn && navBtn.disabled) return;
  VIEWS.forEach((v) => {
    $(`#view-${v}`).classList.toggle("hidden", v !== name);
    const b = $(`.nav-item[data-view="${v}"]`);
    if (b) b.classList.toggle("active", v === name);
  });
  if (name === "optimize" && state.serial) loadActions();
  if (name === "log") loadLog();
  if (name === "settings") loadSettings();
  if (name === "clean" && !state.scan) restoreSavedSettingsToUi();
  window.scrollTo({ top: 0 });
}

$$(".nav-item").forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));
$$("[data-goto]").forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.goto)));

/* --------------------------------------------------------------- подключение */
function setConnection(status, label, detail) {
  const card = $("#status-card");
  card.classList.remove("ready", "wait", "error");
  const hero = $("#connect-hero");
  hero.classList.remove("ready", "wait", "error");
  const icon = $("#hero-icon");
  const dotClass = { ready: "ready", ready_multiple: "ready", unauthorized: "wait", no_device: "", unavailable: "error", error: "error", no_adb: "error", adb_error: "error" }[status] ?? "";

  if (dotClass) card.classList.add(dotClass);
  if (["ready", "ready_multiple", "unauthorized", "unavailable", "error", "no_adb", "adb_error"].includes(status)) {
    hero.classList.add(["ready", "ready_multiple"].includes(status) ? "ready" : status === "unauthorized" ? "wait" : "error");
  }

  const icons = { ready: "✅", ready_multiple: "📱", unauthorized: "🔐", unavailable: "⚠️", error: "⚠️", no_adb: "🧩", adb_error: "🧩", no_device: "📱" };
  icon.textContent = icons[status] ?? "📱";

  $("#status-title").textContent = label;
  $("#status-sub").textContent = detail || "";
  $("#hero-title").textContent = label;
  $("#hero-text").textContent = detail || "";

  const ready = status === "ready" || status === "ready_multiple";
  $$(".nav-item[data-view='clean'], .nav-item[data-view='optimize']").forEach((b) => { b.disabled = !ready; });
  $("#begin-clean").disabled = !ready;
  $("#begin-optimize").disabled = !ready;
}

function renderHeroActions(payload) {
  const box = $("#hero-actions");
  box.innerHTML = "";
  if (!payload.adb || !payload.adb.found) {
    box.appendChild(h("button", { class: "btn primary", onclick: () => switchView("settings") }, "Указать путь к adb"));
    box.appendChild(h("a", {
      class: "btn ghost", href: payload.adb?.download_url || "#", target: "_blank", rel: "noreferrer noopener",
    }, "Официальная страница Platform-Tools"));
    box.appendChild(h("button", { class: "btn ghost", onclick: refreshDevices }, "Проверить снова"));
    return;
  }
  if (payload.status === "unauthorized") {
    box.appendChild(h("button", { class: "btn primary", onclick: refreshDevices }, "Я подтвердил на телефоне — проверить"));
    box.appendChild(h("button", { class: "btn ghost", onclick: restartAdb }, "Перезапустить adb-сервер"));
    return;
  }
  if (payload.status === "unavailable") {
    box.appendChild(h("button", { class: "btn primary", onclick: refreshDevices }, "Проверить снова"));
    box.appendChild(h("button", { class: "btn ghost", onclick: restartAdb }, "Перезапустить adb-сервер"));
    return;
  }
  if (payload.status === "ready" || payload.status === "ready_multiple") {
    box.appendChild(h("button", { class: "btn primary", onclick: () => switchView("clean") }, "Перейти к очистке"));
    box.appendChild(h("button", { class: "btn ghost", onclick: () => switchView("optimize") }, "Перейти к оптимизации"));
  }
}

function renderDevices(payload) {
  const list = $("#device-list");
  list.innerHTML = "";
  const devices = payload.devices || [];
  $("#device-count").textContent = String(devices.length);
  if (!devices.length) {
    list.appendChild(h("div", { class: "empty muted" }, payload.adb && payload.adb.found
      ? "Устройства не найдены. Проверьте кабель и отладку по USB."
      : "ADB не найден — подключение невозможно."));
  }
  devices.forEach((d) => {
    const pill = d.ready ? "ok" : d.state === "unauthorized" ? "warn" : "bad";
    const meta = [d.manufacturer, d.android_version ? `Android ${d.android_version}` : null, d.aba || d.abi, d.serial]
      .filter(Boolean).join(" · ");
    list.appendChild(h("div", { class: `device-item ${d.ready ? "ready" : ""}` },
      h("div", { class: "device-main" },
        h("div", { class: "device-name" }, d.model || d.product || d.serial),
        h("div", { class: "device-meta" }, meta || d.serial)),
      h("span", { class: `pill ${pill}` }, d.state_label)));
  });

  const picker = $("#device-picker");
  const select = $("#device-select");
  const ready = devices.filter((d) => d.ready);
  if (ready.length > 1) {
    picker.classList.remove("hidden");
    const previous = state.serial;
    select.innerHTML = "";
    ready.forEach((d) => {
      select.appendChild(h("option", { value: d.serial }, `${d.model || d.serial} — ${d.serial}`));
    });
    if (previous && ready.some((d) => d.serial === previous)) select.value = previous;
    state.serial = select.value;
    state.sdk = ready.find((d) => d.serial === state.serial)?.sdk ?? null;
  } else {
    picker.classList.add("hidden");
  }
  if (ready.length === 1) {
    state.serial = ready[0].serial;
    state.sdk = ready[0].sdk ?? null;
    state.model = ready[0].model;
  } else if (!ready.length) {
    state.serial = null;
    state.sdk = null;
    state.model = null;
  } else if (ready.length > 1) {
    const chosen = ready.find((d) => d.serial === state.serial) || ready[0];
    state.serial = chosen.serial;
    state.sdk = chosen.sdk ?? null;
    state.model = chosen.model;
  }

  const adb = payload.adb || {};
  const badge = $("#adb-badge");
  badge.textContent = adb.found ? `adb ${adb.version || ""}`.trim() : "не найден";
  badge.className = "tag";
  $("#adb-info").innerHTML = "";
  if (adb.found) {
    $("#adb-info").appendChild(h("div", {}, "Найден: ", h("code", {}, adb.path || "adb из PATH")));
    if (payload.adb?.error) $("#adb-info").appendChild(h("div", { class: "warn-line" }, payload.adb.error));
  } else {
    $("#adb-info").appendChild(h("div", {}, adb.error || "Официальный adb не обнаружен."));
    $("#adb-info").appendChild(h("div", { class: "small" }, "Скачайте Platform-Tools с developer.android.com и укажите путь в настройках."));
  }
}

async function refreshDevices() {
  $("#refresh-devices").classList.add("spin");
  try {
    const payload = await api("/api/devices", { timeout: 45000 });
    state.devices = payload.devices || [];
    renderDevices(payload);
    setConnection(payload.status, payload.status_label, payload.adb && payload.adb.error ? payload.adb.error : undefined);
    renderHeroActions(payload);
    if (payload.status === "ready") {
      $$(".nav-item[data-view='clean'], .nav-item[data-view='optimize']").forEach((b) => { b.disabled = false; });
    }
  } catch (err) {
    setConnection("error", "Ошибка проверки подключения", err.message);
    $("#hero-actions").innerHTML = "";
    $("#hero-actions").appendChild(h("button", { class: "btn primary", onclick: refreshDevices }, "Проверить снова"));
    toast(err.message, "err");
  } finally {
    $("#refresh-devices").classList.remove("spin");
  }
}

async function restartAdb() {
  try {
    toast("Перезапуск adb-сервера…", "warn");
    await api("/api/devices", { timeout: 20000 }); // гарантируем, что сервер поднят
    refreshDevices();
  } catch (err) { toast(err.message, "err"); }
}

$("#refresh-devices").addEventListener("click", refreshDevices);
$("#device-select").addEventListener("change", (e) => {
  state.serial = e.target.value;
  state.sdk = state.devices.find((d) => d.serial === state.serial)?.sdk ?? null;
  state.actions = [];
  loadActions();
});

/* ------------------------------------------------------------------ сканирование */
function scanSettingsPayload() {
  return {
    big_file_threshold: Number($("#set-threshold").value),
    min_size: Number($("#set-min-size").value),
    include_apk: $("#set-apk").checked,
    include_archives: $("#set-archives").checked,
    include_temp: $("#set-temp").checked,
    include_downloads: $("#set-downloads").checked,
    find_duplicates: $("#set-dups").checked,
    full_hash: $("#set-fullhash").checked,
    include_obb: $("#set-obb").checked,
  };
}

function restoreSavedSettingsToUi() {
  const s = state.settings || {};
  if (s.big_file_threshold) {
    const opt = $$("#set-threshold option").find((o) => Number(o.value) === Number(s.big_file_threshold));
    if (opt) $("#set-threshold").value = opt.value;
  }
  if (s.min_size !== undefined) {
    const opt = $$("#set-min-size option").find((o) => Number(o.value) === Number(s.min_size));
    if (opt) $("#set-min-size").value = opt.value;
  }
  if (typeof s.include_apk === "boolean") $("#set-apk").checked = s.include_apk;
  if (typeof s.include_archives === "boolean") $("#set-archives").checked = s.include_archives;
  if (typeof s.include_temp === "boolean") $("#set-temp").checked = s.include_temp;
  if (typeof s.include_downloads === "boolean") $("#set-downloads").checked = s.include_downloads;
  if (typeof s.find_duplicates === "boolean") $("#set-dups").checked = s.find_duplicates;
  if (typeof s.full_hash === "boolean") $("#set-fullhash").checked = s.full_hash;
  if (typeof s.include_obb === "boolean") $("#set-obb").checked = s.include_obb;
}

$("#clean-settings-btn").addEventListener("click", () => {
  $("#scan-settings-inline").classList.toggle("hidden");
});

$("#scan-start").addEventListener("click", startScan);
$("#scan-cancel").addEventListener("click", cancelScan);

async function startScan() {
  if (!state.serial) { toast("Сначала подключите телефон", "warn"); switchView("connect"); return; }
  const settings = scanSettingsPayload();
  $("#scan-progress-wrap").classList.remove("hidden");
  $("#scan-cancel").classList.remove("hidden");
  $("#scan-start").disabled = true;
  $("#results-block").classList.add("hidden");
  $("#delete-report").innerHTML = "";
  setScanState("Сканирование…", "Читаем доступные каталоги телефона. Не отключайте кабель.");
  $("#scan-progress-bar").className = "progress-bar indeterminate";
  $("#scan-progress-text").textContent = "Подготовка…";
  $("#scan-progress-stats").textContent = "";
  state.selected.clear();

  try {
    const { job_id } = await api("/api/scan", { method: "POST", body: { serial: state.serial, sdk: state.sdk, settings } });
    state.scanJob = job_id;
    const job = await pollJob(job_id, onScanEvent);
    finishScanUi();
    state.scanJob = null;
    if (job.status === "error") {
      setScanState("Сканирование не удалось", job.error || "Неизвестная ошибка");
      if (/Устройство|недоступно|не найдено/i.test(job.error || "")) refreshDevices();
      toast(job.error || "Сканирование не удалось", "err", 9000);
      return;
    }
    const scanId = job.result && job.result.scan_id;
    if (!scanId) {
      setScanState(job.status === "cancelled" ? "Сканирование отменено" : "Нет результата", job.error || "Результат не сформирован.");
      return;
    }
    $("#scan-progress-bar").style.width = "100%";
    await loadScanResult(scanId, job);
  } catch (err) {
    finishScanUi();
    setScanState("Сканирование не удалось", err.message);
    if (/Устройство|не найдено|недоступно/i.test(err.message)) refreshDevices();
    toast(err.message, "err", 8000);
  }
}

function onScanEvent(job, event) {
  if (event.stage === "discover") {
    $("#scan-progress-text").textContent = event.message || "Поиск каталогов";
  } else if (event.stage === "scan") {
    $("#scan-progress-bar").classList.remove("indeterminate");
    const total = event.roots_total || 1;
    const idx = event.root_index || 1;
    const pct = Math.min(99, ((idx - 1) / total) * 100 + 3);
    $("#scan-progress-bar").style.width = `${pct}%`;
    $("#scan-progress-text").textContent = event.message || "Чтение каталогов";
    $("#scan-progress-stats").textContent = [
      event.files !== undefined ? `Найдено файлов: ${event.files.toLocaleString("ru-RU")}` : "",
      event.elapsed !== undefined ? `${event.elapsed} с` : "",
    ].filter(Boolean).join(" · ");
  } else if (event.stage === "classify") {
    $("#scan-progress-bar").style.width = "92%";
    $("#scan-progress-text").textContent = "Анализ найденных файлов…";
  } else if (event.stage === "duplicates") {
    $("#scan-progress-bar").style.width = "96%";
    $("#scan-progress-text").textContent = event.message || "Поиск дубликатов…";
    if (event.total) $("#scan-progress-stats").textContent = `${event.done || 0} из ${event.total} групп`;
  } else if (event.stage === "cancel") {
    $("#scan-progress-text").textContent = "Отмена…";
  }
}

async function pollJob(jobId, onEvent, { interval = 500 } = {}) {
  let since = 0;
  for (;;) {
    const { job } = await api(`/api/jobs/${jobId}?since=${since}`, { timeout: 30000 });
    (job.events || []).forEach((ev) => onEvent(job, ev));
    since = job.event_index;
    if (job.status !== "running") return job;
    await new Promise((r) => setTimeout(r, interval));
  }
}

async function cancelScan() {
  if (!state.scanJob) return;
  try {
    await api("/api/jobs/cancel", { method: "POST", body: { job_id: state.scanJob } });
    $("#scan-cancel").disabled = true;
    $("#scan-progress-text").textContent = "Отмена сканирования…";
  } catch (err) { toast(err.message, "err"); }
}

function setScanState(title, text) {
  $("#scan-state-title").textContent = title;
  $("#scan-state-text").textContent = text;
}

function finishScanUi() {
  $("#scan-cancel").classList.add("hidden");
  $("#scan-cancel").disabled = false;
  $("#scan-start").disabled = false;
  $("#scan-progress-bar").classList.remove("indeterminate");
}

async function loadScanResult(scanId, job) {
  const data = await api(`/api/result/${scanId}`, { timeout: 60000 });
  state.scan = data.result;
  state.items = data.items;
  state.scanId = scanId;
  state.selected.clear();
  state.hidden.clear();
  state.shown = state.pageSize;
  renderResults();
  const cancelled = (job && job.status === "cancelled") || state.scan.cancelled;
  if (cancelled) {
    setScanState(
      "Сканирование отменено — результат неполный",
      `Прочитано файлов: ${state.scan.total_files_seen.toLocaleString("ru-RU")}. Показаны только те каталоги, которые успели обработаться.`
    );
  } else if (state.scan.areas?.some((a) => a.status === "denied")) {
    setScanState(
      "Сканирование завершено частично",
      `Прочитано файлов: ${state.scan.total_files_seen.toLocaleString("ru-RU")}. Часть каталогов недоступна — это указано в блоке «Что удалось проверить».`
    );
  } else {
    setScanState("Сканирование завершено", `Проверено файлов: ${state.scan.total_files_seen.toLocaleString("ru-RU")}. Ничего не удалено — решение за вами.`);
  }
}

/* ------------------------------------------------------------- отрисовка таблицы */
function categoryOptions() {
  const select = $("#filter-category");
  const current = select.value;
  const cats = new Map();
  (state.scan?.summary || []).forEach((s) => cats.set(s.key, s.label));
  select.innerHTML = "";
  select.appendChild(h("option", { value: "" }, "Все категории"));
  cats.forEach((label, key) => select.appendChild(h("option", { value: key }, label)));
  select.value = current && cats.has(current) ? current : "";
}

function filteredItems() {
  const { query, category, risk, sort, desc } = state.filters;
  let list = state.items.filter((i) => !state.hidden.has(i.path));
  if (query) {
    const q = query.toLowerCase();
    list = list.filter((i) => i.path.toLowerCase().includes(q) || i.name.toLowerCase().includes(q));
  }
  if (category) list = list.filter((i) => i.category_key === category || i.categories.includes(category));
  if (risk) list = list.filter((i) => i.risk === risk);
  const keyFns = {
    size: (i) => i.size,
    date: (i) => i.mtime || 0,
    name: (i) => i.name.toLowerCase(),
    risk: (i) => ({ low: 0, medium: 1, high: 2 }[i.risk] ?? 1),
  };
  const kf = keyFns[sort] || keyFns.size;
  list = list.slice().sort((a, b) => {
    const ka = kf(a), kb = kf(b);
    if (ka < kb) return desc ? 1 : -1;
    if (ka > kb) return desc ? -1 : 1;
    return 0;
  });
  return list;
}

function renderResults() {
  $("#results-block").classList.remove("hidden");
  renderStats();
  categoryOptions();
  renderTable();
  renderCoverage();
}

function updateSelectionUi() {
  const count = state.selected.size;
  const btn = $("#delete-selected");
  if (btn) {
    btn.classList.toggle("hidden", count === 0);
    btn.textContent = `Удалить выбранные (${count})`;
  }
}

function renderStats() {
  updateSelectionUi();
  const row = $("#stats-row");
  row.innerHTML = "";
  const scan = state.scan;
  const selectedSize = state.items.filter((i) => state.selected.has(i.path)).reduce((a, i) => a + i.size, 0);
  const stats = [
    ["Найдено файлов", scan.count.toLocaleString("ru-RU"), "в результате анализа"],
    ["Объём найденного", fmtBytes(scan.total_size), "не равно «мусору»"],
    ["Выбрано", `${state.selected.size} / ${fmtBytes(selectedSize)}`, "к удалению"],
    ["Свободно памяти", fmtBytes(scan.storage_free), scan.storage_total ? `из ${fmtBytes(scan.storage_total)}` : "по данным df"],
  ];
  stats.forEach(([label, value, sub]) => {
    row.appendChild(h("div", { class: "stat" },
      h("div", { class: "stat-label" }, label),
      h("div", { class: "stat-value" }, value),
      h("div", { class: "stat-sub" }, sub)));
  });
}

function categoryChip(cat) {
  const labels = {
    big: "Крупный", download: "Загрузки", archive: "Архив", apk: "APK",
    temp: "Временный", duplicate: "Дубликат", original: "Оригинал",
  };
  return h("span", { class: `chip ${cat}` }, labels[cat] || cat);
}

function renderTable() {
  const body = $("#results-body");
  body.innerHTML = "";
  const list = filteredItems();
  $("#visible-count").textContent = `Показано ${Math.min(state.shown, list.length)} из ${list.length} (всего ${state.items.length})`;
  $("#load-more").classList.toggle("hidden", list.length <= state.shown);

  if (!list.length) {
    body.appendChild(h("div", { class: "empty muted" },
      state.items.length ? "Под текущие фильтры ничего не подходит." : "Подходящих файлов не найдено. Это нормальный результат — значит, в доступных каталогах нет файлов выбранных категорий."));
    return;
  }

  list.slice(0, state.shown).forEach((item) => {
    const selected = state.selected.has(item.path);
    const row = h("div", { class: `row ${selected ? "selected" : ""}`, dataset: { path: item.path } },
      h("input", {
        type: "checkbox", checked: selected,
        "aria-label": `Выбрать ${item.name}`,
        onchange: (e) => {
          if (e.target.checked) state.selected.add(item.path); else state.selected.delete(item.path);
          renderStats();
          e.target.closest(".row").classList.toggle("selected", e.target.checked);
        },
      }),
      h("div", { class: "row-main" },
        h("div", { class: "row-name", title: item.name }, item.name),
        h("div", { class: "row-path", title: item.path }, item.path)),
      h("div", { class: "row-cat" }, ...item.categories.map(categoryChip)),
      h("div", { class: "num" }, fmtBytes(item.size)),
      h("div", { class: `risk ${item.risk}` }, h("span", { class: "risk-dot" }), h("span", {}, item.risk_label.replace(" осторожность", ""))),
      h("div", { class: "row-actions-cell" },
        h("button", { class: "tiny-btn why", onclick: (e) => toggleDetails(e, item) }, "Почему?"),
        h("button", {
          class: "tiny-btn",
          onclick: () => keepItem(item.path),
          title: "Убрать из результатов и не удалять",
        }, "Оставить"),
        h("button", {
          class: "tiny-btn",
          onclick: () => askDelete([item.path]),
          title: "Удалить этот файл",
        }, "Удалить")));
    body.appendChild(row);
  });

  if (state.shown > 0 && list.length > state.shown) {
    $("#load-more").disabled = false;
  }
}

function toggleDetails(event, item) {
  const row = event.target.closest(".row");
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains("row-details")) { existing.remove(); return; }
  const details = h("div", { class: "row-details" },
    ...(item.reasons || []).map((r) => h("div", { class: "reason" }, r)),
    ...(item.warnings || []).map((w) => h("div", { class: "warn-line" }, w)),
    h("div", { class: "reason" }, `Изменён: ${fmtDate(item.mtime)} · Тип: ${item.extension || "без расширения"} · Осторожность: ${item.risk_label}`),
    item.duplicate_of ? h("div", { class: "reason" }, `Дубликат файла: ${item.duplicate_of}`) : null);
  row.after(details);
}

function keepItem(path) {
  state.hidden.add(path);
  state.selected.delete(path);
  renderStats();
  renderTable();
  toast("Файл убран из результатов. Он не будет удалён.", "ok", 3200);
}

$("#delete-selected").addEventListener("click", () => {
  if (!state.selected.size) return;
  askDelete([...state.selected]);
});

$("#load-more").addEventListener("click", () => { state.shown += state.pageSize; renderTable(); });
$("#filter-query").addEventListener("input", (e) => { state.filters.query = e.target.value; state.shown = state.pageSize; renderTable(); });
$("#filter-category").addEventListener("change", (e) => { state.filters.category = e.target.value; renderTable(); });
$("#filter-risk").addEventListener("change", (e) => { state.filters.risk = e.target.value; renderTable(); });
$("#sort-by").addEventListener("change", (e) => { state.filters.sort = e.target.value; renderTable(); });
$("#sort-dir").addEventListener("click", (e) => {
  state.filters.desc = !state.filters.desc;
  e.target.textContent = state.filters.desc ? "↓" : "↑";
  renderTable();
});
$("#select-all").addEventListener("change", (e) => {
  const list = filteredItems();
  if (e.target.checked) list.forEach((i) => state.selected.add(i.path));
  else state.selected.clear();
  renderStats();
  renderTable();
});

$("#export-csv").addEventListener("click", () => {
  const list = filteredItems();
  if (!list.length) { toast("Нет данных для экспорта", "warn"); return; }
  const esc = (s) => `"${String(s).replace(/"/g, '""')}"`;
  const rows = [["Путь", "Имя", "Размер (байт)", "Категории", "Осторожность", "Причины"].join(";")];
  list.forEach((i) => rows.push([esc(i.path), esc(i.name), i.size, esc(i.category_label), esc(i.risk_label), esc((i.reasons || []).join(" "))].join(";")));
  const blob = new Blob(["\ufeff" + rows.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `sparktech-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1500);
  toast("Список сохранён в CSV на этом компьютере", "ok");
});

function renderCoverage() {
  const scan = state.scan;
  const list = $("#coverage-list");
  list.innerHTML = "";
  const scanned = (scan.areas || []).filter((a) => a.status === "scanned").length;
  $("#coverage-tag").textContent = `${scanned} из ${(scan.areas || []).length} каталогов`;
  (scan.areas || []).forEach((area) => {
    list.appendChild(h("div", { class: "coverage-item" },
      h("div", {},
        h("code", {}, area.path),
        area.note ? h("div", { class: "muted xsmall" }, area.note) : null),
      h("div", { class: `status status-${area.status}` }, `${area.status_label} · ${plural(area.files, "файл", "файла", "файлов")}`)));
  });
  const warnings = $("#coverage-warnings");
  warnings.innerHTML = "";
  (scan.warnings || []).forEach((w) => warnings.appendChild(h("div", { class: "warning-box" }, w)));
}

/* -------------------------------------------------------------------- удаление */
function askDelete(paths) {
  if (!state.serial) { toast("Устройство не подключено", "err"); return; }
  const items = paths.map((p) => state.items.find((i) => i.path === p)).filter(Boolean);
  if (!items.length) { toast("Файлы не найдены в текущем результате", "warn"); return; }
  const total = items.reduce((a, i) => a + i.size, 0);
  $("#modal-count").textContent = items.length.toLocaleString("ru-RU");
  $("#modal-size").textContent = fmtBytes(total);
  $("#modal-confirm-check").checked = false;
  $("#modal-delete-confirm").disabled = true;
  $("#modal-delete-confirm").dataset.paths = JSON.stringify(paths);
  const list = $("#modal-list");
  list.innerHTML = "";
  items.slice(0, 300).forEach((i) => {
    list.appendChild(h("div", { class: "item" },
      h("div", {},
        h("div", {}, i.name),
        h("div", { class: "p" }, i.path)),
      h("div", { class: "muted" }, fmtBytes(i.size))));
  });
  if (items.length > 300) {
    list.appendChild(h("div", { class: "item muted" }, `…и ещё ${items.length - 300} файлов`));
  }
  $("#modal-delete").classList.remove("hidden");
}

$("#modal-confirm-check").addEventListener("change", (e) => {
  $("#modal-delete-confirm").disabled = !e.target.checked;
});

$("#modal-delete-confirm").addEventListener("click", async (e) => {
  const paths = JSON.parse(e.target.dataset.paths || "[]");
  if (!paths.length) return;
  e.target.disabled = true;
  try {
    const { job_id } = await api("/api/delete", {
      method: "POST",
      body: { serial: state.serial, scan_id: state.scanId, paths, confirm: true },
    });
    $("#modal-delete").classList.add("hidden");
    toast("Удаление выполняется…", "warn");
    const job = await pollJob(job_id, () => {});
    await loadScanResult(state.scanId, null).catch(() => {});
    renderDeleteReport(job);
  } catch (err) {
    toast(err.message, "err", 8000);
  } finally {
    e.target.disabled = false;
  }
});

function renderDeleteReport(job) {
  const box = $("#delete-report");
  box.innerHTML = "";
  box.classList.remove("hidden");
  const report = job.result || {};
  const deleted = report.deleted || [];
  const failed = report.failed || [];
  const head = h("div", { class: "report-head" },
    h("h3", {}, job.status === "error" ? "Операция удаления прервана" : "Отчёт об удалении"),
    h("span", { class: "tag" }, `удалено ${deleted.length} · ошибок ${failed.length}`));
  const body = h("div", { class: "report-body" });
  if (job.status === "error") {
    body.appendChild(h("div", { class: "warning-box" }, job.error || "Неизвестная ошибка"));
  }
  if (deleted.length) {
    body.appendChild(h("div", { class: "muted xsmall" }, "Успешно удалено:"));
    deleted.forEach((r) => body.appendChild(h("div", { class: "report-row" },
      h("span", { class: "msg" }, r.path),
      h("span", { class: "badge success" }, "удалено"))));
  }
  if (failed.length) {
    body.appendChild(h("div", { class: "muted xsmall" }, "Не удалено:"));
    failed.forEach((r) => body.appendChild(h("div", { class: "report-row" },
      h("span", { class: "msg" }, `${r.path} — ${r.message}`),
      h("span", { class: "badge failed" }, "ошибка"))));
  }
  if (!deleted.length && !failed.length) {
    body.appendChild(h("div", { class: "muted small" }, "Операция не затронула ни одного файла."));
  }
  body.appendChild(h("div", { class: "report-note" }, "Восстановление удалённых файлов не поддерживается и не обещается."));
  box.appendChild(h("div", { class: "report" }, head, body));
}

/* ------------------------------------------------------------------ оптимизация */
async function loadActions() {
  if (!state.serial) return;
  const box = $("#actions-list");
  box.innerHTML = "";
  box.appendChild(h("div", { class: "empty muted" }, "Проверка доступности действий на устройстве…"));
  try {
    const data = await api(`/api/actions?serial=${encodeURIComponent(state.serial)}&sdk=${state.sdk ?? ""}`, { timeout: 120000 });
    state.actions = data.actions || [];
    state.actionsMeta = { manual: data.manual || [], saved: data.saved || {} };
    renderActions();
  } catch (err) {
    box.innerHTML = "";
    box.appendChild(h("div", { class: "warning-box" }, err.message));
  }
}

function renderActions() {
  const box = $("#actions-list");
  box.innerHTML = "";
  const available = state.actions.filter((a) => a.available).length;
  $("#actions-count").textContent = `доступно ${available} из ${state.actions.length}`;
  $("#optimize-run").disabled = true;
  state.selectedActions.clear();

  state.actions.forEach((a) => {
    const checked = false;
    const item = h("div", { class: `action-item ${a.available ? "" : "unavailable"}` });
    const checkbox = h("input", {
      type: "checkbox",
      disabled: !a.available,
      "aria-label": a.title,
      onchange: (e) => {
        if (e.target.checked) state.selectedActions.add(a.id);
        else state.selectedActions.delete(a.id);
        $("#optimize-run").disabled = state.selectedActions.size === 0;
      },
    });
    const details = h("div", { class: "action-details hidden" },
      h("div", { class: "detail-box" }, h("h4", {}, "Что делает"), h("p", {}, a.what)),
      h("div", { class: "detail-box" }, h("h4", {}, "Эффект"), h("p", {}, a.effect)),
      h("div", { class: "detail-box" }, h("h4", {}, "Ограничения"), h("p", {}, a.limits)),
      h("div", { class: "detail-box" }, h("h4", {}, "Побочные последствия"), h("p", {}, a.side_effects)),
      h("div", { class: "detail-box" }, h("h4", {}, "Отмена"), h("p", {}, a.reversible)),
      h("div", { class: "detail-box" }, h("h4", {}, "Доступность"), h("p", {}, a.availability_note || (a.available ? "Доступно" : "Недоступно"))));

    const head = h("div", {
      class: "action-head",
      onclick: (e) => {
        if (e.target.tagName === "INPUT") return;
        details.classList.toggle("hidden");
      },
    },
      checkbox,
      h("div", { style: "flex:1;min-width:0" },
        h("div", { class: "action-title" }, a.title),
        h("div", { class: "action-what" }, a.what.slice(0, 150) + (a.what.length > 150 ? "…" : "")),
        a.requires_confirm ? h("div", { class: "danger-flag" }, "⚠ требует отдельного подтверждения при выполнении") : null,
        a.available ? null : h("div", { class: "danger-flag" }, a.availability_note)),
      h("div", { class: "action-side" },
        h("span", { class: `pill ${a.available ? "ok" : "bad"}` }, a.available ? "доступно" : "недоступно"),
        h("span", { class: "tiny-btn" }, "детали")));

    item.appendChild(head);
    item.appendChild(details);
    box.appendChild(item);
  });

  const manual = $("#manual-actions");
  manual.innerHTML = "";
  (state.actionsMeta.manual || []).forEach((m) => {
    manual.appendChild(h("div", { class: "manual-item" },
      h("h4", {}, m.title),
      h("p", {}, m.what),
      h("p", { class: "why" }, m.why_not_auto)));
  });

  if (Object.keys(state.actionsMeta.saved || {}).length) {
    const savedBox = h("div", { class: "callout" },
      h("div", {}, "Сохранены исходные значения настроек — их можно вернуть:"),
      ...Object.entries(state.actionsMeta.saved).map(([key, value]) => {
        const actionId = { stay_on_while_plugged_in: "stay_awake", window_animation_scale: "window_animation", transition_animation_scale: "transition_animation", animator_duration_scale: "animator_duration" }[key];
        return h("div", { class: "kv" },
          h("span", {}, h("code", {}, `${key} = ${value}`)),
          actionId ? h("button", { class: "btn ghost small", onclick: () => undoAction(actionId) }, "Вернуть") : null);
      }));
    $("#view-optimize").appendChild(savedBox);
  }
}

async function undoAction(actionId) {
  try {
    const { outcome } = await api("/api/undo", { method: "POST", body: { serial: state.serial, action_id: actionId } });
    toast(`${outcome.status_label}: ${outcome.message}`, outcome.status === "success" ? "ok" : "warn", 7000);
    loadActions();
  } catch (err) { toast(err.message, "err"); }
}

$("#optimize-run").addEventListener("click", () => {
  const chosen = state.actions.filter((a) => state.selectedActions.has(a.id));
  $("#optimize-summary").innerHTML = "";
  const summary = $("#optimize-summary");
  chosen.forEach((a) => {
    summary.appendChild(h("div", { class: "item" },
      h("div", {},
        h("div", {}, a.title),
        h("div", { class: "p" }, `${a.effect}`)),
      a.requires_confirm ? h("span", { class: "badge user_action" }, "подтвердить") : h("span", { class: "badge success" }, "готово")));
  });
  const extra = $("#optimize-extra-confirm");
  extra.innerHTML = "";
  const needsConfirm = chosen.filter((a) => a.requires_confirm);
  if (needsConfirm.length) {
    extra.appendChild(h("label", { class: "check" },
      h("input", { type: "checkbox", id: "confirm-risky" }),
      `Подтверждаю выполнение: ${needsConfirm.map((a) => a.title).join(", ")}`));
  }
  $("#modal-optimize").classList.remove("hidden");
});

$("#modal-optimize-confirm").addEventListener("click", async () => {
  const chosen = [...state.selectedActions];
  const risky = state.actions.filter((a) => a.requires_confirm && state.selectedActions.has(a.id)).map((a) => a.id);
  const confirmBox = $("#confirm-risky");
  const confirmed = risky.filter(() => confirmBox && confirmBox.checked);
  $("#modal-optimize").classList.add("hidden");
  try {
    const { job_id } = await api("/api/optimize", {
      method: "POST",
      body: { serial: state.serial, sdk: state.sdk, actions: chosen, confirmed_actions: confirmed },
    });
    toast("Действия выполняются на устройстве…", "warn");
    const job = await pollJob(job_id, () => {});
    renderOptimizeReport(job);
    loadActions();
  } catch (err) { toast(err.message, "err", 8000); }
});

function renderOptimizeReport(job) {
  const box = $("#optimize-report");
  box.innerHTML = "";
  box.classList.remove("hidden");
  const outcomes = (job.result && job.result.outcomes) || [];
  const body = h("div", { class: "report-body" });
  if (job.status === "error") body.appendChild(h("div", { class: "warning-box" }, job.error || "Ошибка выполнения"));
  outcomes.forEach((o) => {
    const row = h("div", { class: "report-row" },
      h("div", { style: "min-width:0" },
        h("div", {}, titleForAction(o.action_id)),
        h("div", { class: "msg" }, o.message),
        ...(o.details || []).map((d) => h("div", { class: "report-note" }, d))),
      h("span", { class: `badge ${o.status}` }, o.status_label));
    if (o.can_undo) {
      row.appendChild(h("button", { class: "btn ghost small", onclick: () => undoAction(o.action_id) }, "Отменить"));
    }
    body.appendChild(row);
  });
  if (!outcomes.length) body.appendChild(h("div", { class: "muted small" }, "Ни одно действие не было выполнено."));
  box.appendChild(h("div", { class: "report" },
    h("div", { class: "report-head" }, h("h3", {}, "Отчёт по действиям"), h("span", { class: "tag" }, "проверено на устройстве")),
    body));
}

function titleForAction(id) {
  const a = state.actions.find((x) => x.id === id);
  return a ? a.title : id;
}

/* ---------------------------------------------------------------------- журнал */
async function loadLog() {
  try {
    const data = await api("/api/log");
    const list = $("#log-list");
    list.innerHTML = "";
    if (!data.entries.length) { list.appendChild(h("div", { class: "empty muted" }, "Записей пока нет")); return; }
    data.entries.slice().reverse().forEach((e) => {
      const { ts, event, ...rest } = e;
      const meta = Object.entries(rest).map(([k, v]) => `${k}=${v}`).join(" · ");
      list.appendChild(h("div", { class: "log-row" },
        h("span", { class: "ts" }, ts ? new Date(ts).toLocaleString("ru-RU") : ""),
        h("span", { class: "ev" }, event),
        h("span", { class: "meta", title: meta }, meta)));
    });
  } catch (err) { toast(err.message, "err"); }
}
$("#log-refresh").addEventListener("click", loadLog);

/* -------------------------------------------------------------------- настройки */
async function loadSettings() {
  try {
    const data = await api("/api/settings");
    state.settings = data.settings || {};
    $("#adb-path-input").value = data.adb_path || "";
    $("#adb-download-link").href = data.download_url;
    $("#settings-adb-badge").textContent = data.adb_found ? "найден" : "не найден";
    $("#data-dir").textContent = data.data_dir;
    $("#log-path").textContent = `Файл журнала: ${data.data_dir}\\operations.log`;
    $("#adb-license-note").textContent =
      "Platform-Tools распространяются Google по Android SDK License Agreement. SparkTech не скачивает и не распространяет adb: используйте официальную сборку с developer.android.com.";
    const box = $("#default-settings");
    box.innerHTML = "";
    box.appendChild(h("div", { class: "kv" }, h("span", {}, "Порог крупного файла"), h("code", {}, fmtBytes(state.settings.big_file_threshold))));
    box.appendChild(h("div", { class: "kv" }, h("span", {}, "Минимальный размер"), h("code", {}, state.settings.min_size ? fmtBytes(state.settings.min_size) : "любой")));
    box.appendChild(h("div", { class: "kv" }, h("span", {}, "Установочные APK"), h("code", {}, state.settings.include_apk ? "да" : "нет")));
    box.appendChild(h("div", { class: "kv" }, h("span", {}, "Архивы"), h("code", {}, state.settings.include_archives ? "да" : "нет")));
    box.appendChild(h("div", { class: "kv" }, h("span", {}, "Временные файлы"), h("code", {}, state.settings.include_temp ? "да" : "нет")));
    box.appendChild(h("div", { class: "kv" }, h("span", {}, "Поиск дубликатов"), h("code", {}, state.settings.find_duplicates ? "да" : "нет")));
    box.appendChild(h("div", { class: "kv" }, h("span", {}, "Полное хеширование"), h("code", {}, state.settings.full_hash ? "да" : "нет")));
  } catch (err) { toast(err.message, "err"); }
}

$("#save-adb-path").addEventListener("click", async () => {
  const path = $("#adb-path-input").value.trim();
  try {
    await api("/api/settings", { method: "POST", body: { adb_path: path } });
    toast("Путь к adb сохранён", "ok");
    await refreshDevices();
    loadSettings();
  } catch (err) { toast(err.message, "err"); }
});

$("#open-data-dir").addEventListener("click", () => {
  toast("Каталог данных указан рядом. Откройте его в проводнике Windows.", "info", 6000);
});

$("#shutdown-app").addEventListener("click", async () => {
  try {
    await api("/api/shutdown", { method: "POST" });
    document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;color:#98a6bf;font-family:Segoe UI,sans-serif">SparkTech завершён. Окно можно закрыть.</div>';
  } catch (err) { toast(err.message, "err"); }
});

/* ------------------------------------------------------------------ модальные */
$$("[data-close]").forEach((btn) => btn.addEventListener("click", () => {
  $(`#${btn.dataset.close}`).classList.add("hidden");
}));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $$(".modal-backdrop").forEach((m) => m.classList.add("hidden"));
});

/* ------------------------------------------------------------------- запуск */
async function boot() {
  try {
    const health = await api("/api/health", { timeout: 5000 });
    $("#version-label").textContent = `${health.app} ${health.version}`;
  } catch { /* не критично */ }
  try {
    const st = await api("/api/state", { timeout: 45000 });
    state.settings = st.settings || {};
    renderDevices({ devices: st.devices || [], adb: st.adb || {} });
    setConnection(st.status, st.status_label, st.adb && st.adb.error ? st.adb.error : undefined);
    renderHeroActions({ adb: st.adb, status: st.status });
    restoreSavedSettingsToUi();
  } catch (err) {
    setConnection("error", "Приложение не отвечает", err.message);
    toast(`Нет связи с локальным сервером SparkTech: ${err.message}`, "err", 12000);
  }
  // Периодическая проверка подключения (лёгкая, без «тяжёлых» запросов).
  setInterval(async () => {
    if (document.hidden) return;
    try {
      const st = await api("/api/state", { timeout: 20000 });
      const wasReady = !!state.serial;
      renderDevices({ devices: st.devices || [], adb: st.adb || {} });
      setConnection(st.status, st.status_label, st.adb && st.adb.error ? st.adb.error : undefined);
      if (wasReady && !state.serial) {
        toast("Телефон отключён. Подключите кабель и подтвердите отладку заново.", "warn", 8000);
        state.wasDisconnected = true;
      } else if (!wasReady && state.serial && state.wasDisconnected) {
        state.wasDisconnected = false;
        toast("Телефон снова подключён и готов к работе", "ok");
      }
    } catch { /* игнорируем временные сбои опроса */ }
  }, 6000);
}

boot();
