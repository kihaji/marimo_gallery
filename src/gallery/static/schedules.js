"use strict";

let data = JSON.parse(document.getElementById("sched-data").textContent);
const slug = data.slug;
const loggedIn = Boolean(data.user);

// ---- cadence controls -------------------------------------------------------
const kindSel = document.getElementById("cadence-kind");
const everySel = document.getElementById("cadence-every");
const weekdaySel = document.getElementById("cadence-weekday");
const timeInput = document.getElementById("cadence-time");
const cronInput = document.getElementById("cadence-cron");
const WEEKDAY_NUM = { mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6, sun: 0 };

if (everySel) {
  for (const n of [1, 2, 3, 4, 6, 8, 12]) {
    everySel.insertAdjacentHTML("beforeend", `<option value="${n}">${n} hour${n > 1 ? "s" : ""}</option>`);
  }
  everySel.value = "6";
}

function cadencePayload() {
  const kind = kindSel.value;
  if (kind === "cron") return { cron: cronInput.value.trim() };
  if (kind === "hours") return { cadence: { kind, every: Number(everySel.value) } };
  if (kind === "daily") return { cadence: { kind, time: timeInput.value } };
  return { cadence: { kind, weekday: weekdaySel.value, time: timeInput.value } };
}

function cadencePreview() {
  const kind = kindSel.value;
  const [h, m] = (timeInput.value || "0:0").split(":").map(Number);
  if (kind === "hours") return `every ${everySel.value} hours (cron 0 */${everySel.value} * * *)`;
  if (kind === "daily") return `every day at ${timeInput.value} (cron ${m} ${h} * * *)`;
  if (kind === "weekly")
    return `every ${weekdaySel.options[weekdaySel.selectedIndex].text} at ${timeInput.value} (cron ${m} ${h} * * ${WEEKDAY_NUM[weekdaySel.value]})`;
  return cronInput.value.trim() || "…";
}

function updateCadenceUI() {
  if (!kindSel) return;
  const kind = kindSel.value;
  document.getElementById("f-every").hidden = kind !== "hours";
  document.getElementById("f-weekday").hidden = kind !== "weekly";
  document.getElementById("f-time").hidden = kind !== "daily" && kind !== "weekly";
  document.getElementById("f-cron").hidden = kind !== "cron";
  document.getElementById("cadence-preview").textContent = cadencePreview();
}

if (kindSel) {
  for (const el of [kindSel, everySel, weekdaySel, timeInput, cronInput]) {
    el.addEventListener("input", updateCadenceUI);
  }
  updateCadenceUI();
}

// ---- parameter form ----------------------------------------------------------
function paramField(spec) {
  const label = document.createElement("label");
  label.append(`${spec.label || spec.name} `);
  let input;
  if (spec.type === "choice") {
    input = document.createElement("select");
    for (const choice of spec.choices) {
      const opt = document.createElement("option");
      opt.value = opt.textContent = choice;
      input.append(opt);
    }
    if (spec.default != null) input.value = String(spec.default);
  } else if (spec.type === "boolean") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(spec.default);
  } else {
    input = document.createElement("input");
    input.type = spec.type === "number" ? "number" : "text";
    if (spec.type === "number") input.step = "any";
    if (spec.default != null) input.value = String(spec.default);
  }
  input.dataset.param = spec.name;
  input.dataset.type = spec.type;
  label.append(input);
  return label;
}

const paramWrap = document.getElementById("param-fields");
if (paramWrap) {
  for (const spec of data.parameters) paramWrap.append(paramField(spec));
  if (!data.parameters.length) {
    paramWrap.innerHTML = "<span class='muted'>This notebook declares no parameters.</span>";
  }
}

function collectParams() {
  const params = {};
  for (const input of document.querySelectorAll("[data-param]")) {
    const name = input.dataset.param;
    if (input.dataset.type === "boolean") params[name] = input.checked;
    else if (input.value !== "") params[name] = input.value;
  }
  return params;
}

// ---- rendering ----------------------------------------------------------------
const STATUS_LABEL = { queued: "queued", running: "running…", success: "✓ success", failed: "✗ failed", timeout: "⏱ timeout" };

function fmtLocal(iso) {
  return iso ? new Date(iso).toLocaleString() : "—";
}

function fmtParams(params) {
  const parts = Object.entries(params).map(([k, v]) => `${k}=${v}`);
  return parts.length ? parts.join(", ") : "—";
}

function duration(run) {
  if (!run.started_at || !run.finished_at) return "—";
  const secs = Math.round((new Date(run.finished_at) - new Date(run.started_at)) / 1000);
  return secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`;
}

function renderSchedules() {
  const tbody = document.querySelector("#schedule-table tbody");
  tbody.replaceChildren(
    ...data.schedules.map((s) => {
      const tr = document.createElement("tr");
      const actions = loggedIn
        ? `<button class="mini" data-toggle="${s.id}">${s.enabled ? "Disable" : "Enable"}</button>
           <button class="mini danger" data-delete="${s.id}">Delete</button>`
        : "";
      tr.innerHTML = `
        <td>${esc(s.name)}</td>
        <td title="${esc(s.cron)}">${esc(s.cadence_label)}</td>
        <td>${esc(fmtParams(s.params))}</td>
        <td>${s.enabled ? fmtLocal(s.next_run_at) : "—"}</td>
        <td>${s.enabled ? "on" : "off"}</td>
        <td class="row-actions">${actions}</td>`;
      return tr;
    })
  );
  document.getElementById("no-schedules").hidden = data.schedules.length > 0;
}

function renderRuns() {
  const tbody = document.querySelector("#run-table tbody");
  tbody.replaceChildren(
    ...data.runs.map((r) => {
      const tr = document.createElement("tr");
      const trigger = r.manual
        ? "manual"
        : esc(r.schedule_name || "schedule (deleted)");
      const links =
        r.status === "success"
          ? `<a href="/runs/${slug}/${r.id}/report" target="_blank" rel="noopener">Report</a> ·
             <a href="/runs/${slug}/${r.id}/log" target="_blank" rel="noopener">Log</a>`
          : r.status === "failed" || r.status === "timeout"
            ? `<a href="/runs/${slug}/${r.id}/log" target="_blank" rel="noopener">Log</a>`
            : "…";
      tr.innerHTML = `
        <td>${fmtLocal(r.started_at || r.created_at)}</td>
        <td>${trigger}</td>
        <td>${esc(fmtParams(r.params))}</td>
        <td>${duration(r)}</td>
        <td><span class="status status-${r.status}">${STATUS_LABEL[r.status] || r.status}</span></td>
        <td>${links}</td>`;
      return tr;
    })
  );
  document.getElementById("no-runs").hidden = data.runs.length > 0;
}

function esc(text) {
  const div = document.createElement("div");
  div.textContent = String(text);
  return div.innerHTML;
}

function render() {
  renderSchedules();
  renderRuns();
  if (data.active) scheduleRefresh(2000);
}

// ---- api ------------------------------------------------------------------------
let refreshTimer = null;
function scheduleRefresh(ms) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, ms);
}

async function refresh() {
  const resp = await fetch(`/api/schedules/${slug}`);
  if (resp.ok) {
    const user = data.user;
    data = await resp.json();
    data.user = user;
    render();
  }
}

async function api(method, path, body, errorEl) {
  const resp = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) {
    const message = (await resp.json().catch(() => ({}))).error || `request failed (${resp.status})`;
    if (errorEl) {
      errorEl.textContent = message;
      errorEl.hidden = false;
    }
    return null;
  }
  if (errorEl) errorEl.hidden = true;
  return resp.json();
}

const form = document.getElementById("create-form");
if (form) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = document.getElementById("create-error");
    const body = {
      name: form.elements.name.value.trim(),
      params: collectParams(),
      ...cadencePayload(),
    };
    const created = await api("POST", `/api/schedules/${slug}`, body, errorEl);
    if (created) {
      form.elements.name.value = "";
      await refresh();
    }
  });
}

const runNowBtn = document.getElementById("run-now");
if (runNowBtn) {
  runNowBtn.addEventListener("click", async () => {
    await api("POST", `/api/schedules/${slug}/run`, { params: collectParams() });
    await refresh();
  });
}

document.querySelector("#schedule-table tbody").addEventListener("click", async (e) => {
  const toggleId = e.target.dataset?.toggle;
  const deleteId = e.target.dataset?.delete;
  if (toggleId) {
    const schedule = data.schedules.find((s) => s.id === Number(toggleId));
    await api("PATCH", `/api/schedules/${slug}/${toggleId}`, { enabled: !schedule.enabled });
    await refresh();
  } else if (deleteId && confirm("Delete this schedule? Run history is kept.")) {
    await api("DELETE", `/api/schedules/${slug}/${deleteId}`);
    await refresh();
  }
});

// ---- theme (same behavior as gallery.js) -------------------------------------------
document.getElementById("theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("gallery-theme", next);
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  if (!localStorage.getItem("gallery-theme")) {
    document.documentElement.dataset.theme = e.matches ? "dark" : "light";
  }
});

render();
