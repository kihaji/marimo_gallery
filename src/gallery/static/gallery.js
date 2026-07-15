"use strict";

let notebooks = JSON.parse(document.getElementById("nb-data").textContent);

const grid = document.getElementById("grid");
const tagbar = document.getElementById("tagbar");
const searchInput = document.getElementById("search");
const sortSelect = document.getElementById("sort");
const emptyMsg = document.getElementById("empty");
const template = document.getElementById("card-template");

// ---- state, seeded from the URL so filtered views are shareable ----------
const params = new URLSearchParams(location.search);
const state = {
  q: params.get("q") || "",
  tags: new Set((params.get("tags") || "").split(",").filter(Boolean)),
  sort: params.get("sort") || "title",
};
searchInput.value = state.q;
sortSelect.value = state.sort;

function syncUrl() {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.tags.size) p.set("tags", [...state.tags].join(","));
  if (state.sort !== "title") p.set("sort", state.sort);
  const qs = p.toString();
  history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

// ---- thumbnail fallback: SVG with initials on a slug-derived hue ----------
function fallbackThumb(nb) {
  let hash = 0;
  for (const c of nb.slug) hash = (hash * 31 + c.charCodeAt(0)) | 0;
  const hue = ((hash % 360) + 360) % 360;
  const initials = nb.title.split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='320' height='180'>
    <rect width='100%' height='100%' fill='hsl(${hue} 45% 40%)'/>
    <text x='50%' y='50%' fill='rgba(255,255,255,.85)' font-family='system-ui,sans-serif'
      font-size='64' font-weight='700' text-anchor='middle' dominant-baseline='central'>${initials}</text>
  </svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

// ---- rendering -------------------------------------------------------------
function render() {
  const q = state.q.trim().toLowerCase();
  let visible = notebooks.filter((nb) => {
    const haystack = `${nb.title} ${nb.description} ${nb.tags.join(" ")}`.toLowerCase();
    if (q && !haystack.includes(q)) return false;
    for (const t of state.tags) if (!nb.tags.includes(t)) return false;
    return true;
  });

  visible.sort((a, b) =>
    state.sort === "recent" ? b.mtime - a.mtime : a.title.localeCompare(b.title)
  );

  grid.replaceChildren(
    ...visible.map((nb) => {
      const card = template.content.cloneNode(true);
      const img = card.querySelector(".thumb");
      img.src = nb.has_thumbnail ? `/thumbnails/${nb.slug}` : fallbackThumb(nb);
      img.alt = nb.title;
      img.onerror = () => { img.onerror = null; img.src = fallbackThumb(nb); };
      card.querySelector(".thumb-link").href = nb.url;
      const titleLink = card.querySelector(".card-title a");
      titleLink.href = nb.url;
      titleLink.textContent = nb.title;
      card.querySelector(".card-desc").textContent = nb.description;
      card.querySelector(".card-tags").replaceChildren(
        ...nb.tags.map((t) => {
          const pill = document.createElement("button");
          pill.className = "tag" + (state.tags.has(t) ? " active" : "");
          pill.textContent = t;
          pill.onclick = () => toggleTag(t);
          return pill;
        })
      );
      card.querySelector(".badge-sandbox").hidden = !nb.sandbox;
      card.querySelector(".open-btn").href = nb.url;
      return card;
    })
  );
  emptyMsg.hidden = visible.length > 0;
  renderTagbar();
  syncUrl();
}

function renderTagbar() {
  const allTags = [...new Set(notebooks.flatMap((nb) => nb.tags))].sort();
  const pills = allTags.map((t) => {
    const pill = document.createElement("button");
    pill.className = "tag" + (state.tags.has(t) ? " active" : "");
    pill.textContent = t;
    pill.onclick = () => toggleTag(t);
    return pill;
  });
  if (state.tags.size) {
    const clear = document.createElement("button");
    clear.className = "tag clear";
    clear.textContent = "clear ✕";
    clear.onclick = () => { state.tags.clear(); render(); };
    pills.push(clear);
  }
  tagbar.replaceChildren(...pills);
}

function toggleTag(t) {
  state.tags.has(t) ? state.tags.delete(t) : state.tags.add(t);
  render();
}

// ---- controls ---------------------------------------------------------------
searchInput.addEventListener("input", () => { state.q = searchInput.value; render(); });
sortSelect.addEventListener("change", () => { state.sort = sortSelect.value; render(); });

document.getElementById("refresh").addEventListener("click", async () => {
  await fetch("/api/refresh", { method: "POST" });
  notebooks = await (await fetch("/api/notebooks")).json();
  render();
});

// ---- theme ------------------------------------------------------------------
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
