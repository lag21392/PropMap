const statusEl = document.getElementById("flujoStatus");
const note = document.getElementById("flujoNote");
const tabs = document.querySelector(".flujo-tabs");
const NS = "http://www.w3.org/2000/svg";
const drawn = { flow: false, er: false, mod: false };
let payload = null;

function askPassword() {
  const saved = sessionStorage.getItem("propmap_search_pw");
  if (saved) return saved;
  const typed = window.prompt("Contraseña de administración");
  if (typed) sessionStorage.setItem("propmap_search_pw", typed);
  return typed || "";
}

function showTab(name) {
  document.querySelectorAll("[data-pane]").forEach((pane) => {
    pane.hidden = pane.getAttribute("data-pane") !== name;
  });
  document.querySelectorAll(".flujo-tabs [role=tab]").forEach((tab) => {
    tab.setAttribute("aria-selected", tab.getAttribute("data-tab") === name ? "true" : "false");
  });
  paint(name);
}

function bindTabs() {
  tabs.querySelectorAll("[data-tab]").forEach((tab) => {
    tab.addEventListener("click", () => showTab(tab.getAttribute("data-tab")));
  });
}

async function loadAll() {
  let res = await fetch("/api/lineage", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ password: "" }),
  });
  if (res.status === 401) {
    const password = askPassword();
    if (!password) {
      statusEl.textContent = "Hace falta la contraseña de administración.";
      return;
    }
    res = await fetch("/api/lineage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ password }),
    });
  }
  if (!res.ok) {
    sessionStorage.removeItem("propmap_search_pw");
    statusEl.textContent = "Contraseña incorrecta o el mapa no está disponible.";
    return;
  }
  payload = await res.json();
  statusEl.hidden = true;
  tabs.hidden = false;
  showTab("flow");
}

function paint(name) {
  if (!payload || drawn[name]) return;
  drawn[name] = true;
  const job =
    name === "flow"
      ? drawElk(document.getElementById("flujoSvg"), payload.flow || payload)
      : name === "er"
        ? drawER(payload.er || {})
        : drawElk(document.getElementById("modSvg"), payload.modules || { nodes: [], edges: [] }, {
            width: 168,
            height: 44,
          });
  Promise.resolve(job).catch(() => {
    drawn[name] = false;
    statusEl.hidden = false;
    statusEl.textContent = "No pude dibujar esta vista.";
  });
}

function metaOf(data) {
  const byId = new Map();
  for (const node of data.nodes || []) byId.set(node.id, node);
  return byId;
}

async function drawElk(svg, data, size = { width: 148, height: 58 }) {
  if (!svg) return;
  if (!window.ELK) throw new Error("elkjs");
  const meta = metaOf(data);
  const elk = new window.ELK();
  const layout = await elk.layout({
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.spacing.nodeNode": "22",
      "elk.layered.spacing.nodeNodeBetweenLayers": "48",
      "elk.edgeRouting": "ORTHOGONAL",
    },
    children: (data.nodes || []).map((node) => ({
      id: node.id,
      width: Math.max(size.width, (node.label || node.id).length * 7.2 + 24),
      height: size.height,
    })),
    edges: (data.edges || []).map((edge) => ({
      id: edge.id,
      sources: [edge.source],
      targets: [edge.target],
    })),
  });
  const pad = 24;
  const width = (layout.width || 800) + pad * 2;
  const height = (layout.height || 400) + pad * 2;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.replaceChildren();
  const g = document.createElementNS(NS, "g");
  g.setAttribute("transform", `translate(${pad} ${pad})`);
  for (const edge of layout.edges || []) {
    const d = (edge.sections || [])
      .map((section) => {
        const bits = [`M ${section.startPoint.x} ${section.startPoint.y}`];
        for (const bend of section.bendPoints || []) bits.push(`L ${bend.x} ${bend.y}`);
        bits.push(`L ${section.endPoint.x} ${section.endPoint.y}`);
        return bits.join(" ");
      })
      .join(" ");
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("class", "flujo-edge");
    g.appendChild(path);
  }
  for (const node of layout.children || []) {
    const info = meta.get(node.id) || {};
    const box = document.createElementNS(NS, "g");
    box.setAttribute("class", `flujo-node is-${info.kind || "store"}`);
    box.setAttribute("transform", `translate(${node.x} ${node.y})`);
    box.style.cursor = "pointer";
    const rect = document.createElementNS(NS, "rect");
    rect.setAttribute("width", String(node.width));
    rect.setAttribute("height", String(node.height));
    rect.setAttribute("rx", "8");
    const title = document.createElementNS(NS, "text");
    title.setAttribute("x", "12");
    title.setAttribute("y", String(Math.round(node.height / 2) + 4));
    title.setAttribute("class", "flujo-label");
    title.textContent = info.label || node.id;
    box.append(rect, title);
    box.addEventListener("click", () => explain(info.label || node.id, info.note, info.columns, info.rows));
    g.appendChild(box);
  }
  svg.appendChild(g);
}

async function drawER(er) {
  const host = document.getElementById("erStage");
  if (!host) return;
  if (!er.mermaid) {
    host.textContent = "No pude leer las tablas.";
    return;
  }
  if (!window.mermaid) {
    host.innerHTML = `<pre class="flujo-src"></pre>`;
    host.querySelector("pre").textContent = er.mermaid;
  } else {
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "base",
      themeVariables: {
        primaryColor: "#e7f0e3",
        primaryTextColor: "#1c2a22",
        primaryBorderColor: "#2a4a3c",
        lineColor: "#2a4a3c",
        fontFamily: "IBM Plex Mono, ui-monospace, monospace",
      },
    });
    try {
      const drawnEr = await window.mermaid.render(`propmapEr-${Date.now()}`, er.mermaid);
      host.innerHTML = drawnEr.svg;
      host.querySelector("svg")?.classList.add("flujo-er-svg");
    } catch (err) {
      host.innerHTML = `<pre class="flujo-src"></pre>`;
      host.querySelector("pre").textContent = er.mermaid;
    }
  }
  const ranked = [...(er.tables || [])].sort((a, b) => (b.rows || 0) - (a.rows || 0));
  const list = document.createElement("ul");
  list.className = "flujo-tables";
  for (const table of ranked) {
    const item = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = table.rows != null ? `${table.id} · ${table.rows}` : table.id;
    btn.addEventListener("click", () => explain(table.label, table.note, table.columns, table.rows));
    item.appendChild(btn);
    list.appendChild(item);
  }
  host.appendChild(list);
}

function explain(title, body, columns, rows) {
  note.hidden = false;
  const count = rows == null ? "" : `<p>${rows} filas.</p>`;
  const rowItems = (columns || [])
    .map((col) => {
      const marks = [col.pk ? "PK" : "", col.fk ? "FK" : ""].filter(Boolean).join(" ");
      return `<li><code>${col.name}</code> ${col.type || ""}${marks ? ` · ${marks}` : ""}</li>`;
    })
    .join("");
  note.innerHTML = `<b>${title}</b><p>${body || ""}</p>${count}${rowItems ? `<ul class="flujo-cols">${rowItems}</ul>` : ""}`;
}

bindTabs();
loadAll().catch(() => {
  statusEl.hidden = false;
  statusEl.textContent = "No pude armar el mapa del sistema.";
});
