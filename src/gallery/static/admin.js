"use strict";

let data = JSON.parse(document.getElementById("admin-data").textContent);

function esc(text) {
  const div = document.createElement("div");
  div.textContent = String(text);
  return div.innerHTML;
}

function fmtLocal(iso) {
  return iso ? new Date(iso).toLocaleString() : "—";
}

// ---- rendering ----------------------------------------------------------------

function renderGroups() {
  const tbody = document.querySelector("#group-table tbody");
  tbody.replaceChildren(
    ...data.groups.map((g) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${esc(g.name)}</td>
        <td>${g.member_count}</td>
        <td class="row-actions"><button class="mini danger" data-delete-group="${g.id}">Delete</button></td>`;
      return tr;
    })
  );
  document.getElementById("no-groups").hidden = data.groups.length > 0;
}

function membershipCell(user) {
  if (!data.groups.length) return "<span class='muted'>no groups defined</span>";
  return data.groups
    .map((g) => {
      const member = user.groups.includes(g.name);
      return `<label class="member-toggle"><input type="checkbox"
        data-user="${user.id}" data-group="${g.id}" ${member ? "checked" : ""}>
        ${esc(g.name)}</label>`;
    })
    .join(" ");
}

function renderUsers() {
  const tbody = document.querySelector("#user-table tbody");
  tbody.replaceChildren(
    ...data.users.map((u) => {
      const tr = document.createElement("tr");
      const you = u.dn === data.you;
      tr.innerHTML = `
        <td>${esc(u.display_name || u.dn)}${you ? " <span class='muted'>(you)</span>" : ""}</td>
        <td class="dn">${esc(u.dn)}</td>
        <td>${fmtLocal(u.last_seen_at)}</td>
        <td>${membershipCell(u)}</td>
        <td><input type="checkbox" data-admin="${u.id}" ${u.is_admin ? "checked" : ""}
             ${you ? "disabled title='You cannot demote yourself'" : ""}></td>`;
      return tr;
    })
  );
}

function render() {
  renderGroups();
  renderUsers();
}

// ---- api ------------------------------------------------------------------------

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

async function refresh() {
  const you = data.you;
  const fresh = await api("GET", "/api/admin");
  if (fresh) {
    data = fresh;
    data.you = you;
    render();
  }
}

document.getElementById("group-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("group-name");
  const errorEl = document.getElementById("group-error");
  const created = await api("POST", "/api/admin/groups", { name: input.value.trim() }, errorEl);
  if (created) {
    input.value = "";
    await refresh();
  }
});

document.querySelector("#group-table tbody").addEventListener("click", async (e) => {
  const groupId = e.target.dataset?.deleteGroup;
  if (groupId && confirm("Delete this group? Members are detached, notebooks gated on it become admin-only, and schedules shared with it become private again.")) {
    await api("DELETE", `/api/admin/groups/${groupId}`);
    await refresh();
  }
});

document.querySelector("#user-table tbody").addEventListener("change", async (e) => {
  const el = e.target;
  if (el.dataset.user && el.dataset.group) {
    await api("POST", "/api/admin/membership", {
      user_id: Number(el.dataset.user),
      group_id: Number(el.dataset.group),
      member: el.checked,
    });
    await refresh();
  } else if (el.dataset.admin) {
    await api("POST", `/api/admin/users/${el.dataset.admin}/admin`, { is_admin: el.checked });
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
