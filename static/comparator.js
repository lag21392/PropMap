const CMP_KEY = "propmap.compare.ids";

export function getCompareIds() {
  try { return JSON.parse(localStorage.getItem(CMP_KEY) || "[]"); }
  catch { return []; }
}

export function toggleCompare(id) {
  const ids = getCompareIds();
  const i = ids.indexOf(id);
  if (i >= 0) ids.splice(i, 1);
  else {
    if (ids.length >= 4) ids.shift();
    ids.push(id);
  }
  localStorage.setItem(CMP_KEY, JSON.stringify(ids));
  if (typeof window !== "undefined" && window.updateCompareButton) window.updateCompareButton();
  if (!ids.includes(id)) syncOpenCompare();
  return ids;
}

function textOf(value) {
  if (value == null) return "";
  return String(value).trim();
}

function moneyOrBlank(item) {
  const label = typeof money === "function" ? money(item) : "";
  return label === "Consultar" ? "" : label;
}

function pctOf(value) {
  if (value == null || value === "") return "";
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  const sign = n > 0 ? "+" : "";
  return `${sign}${String(n).replace(".", ",")}%`;
}

function yesNo(value) {
  return value === true ? "Sí" : "";
}

function meters(value) {
  if (value == null || value === "" || Number(value) <= 0) return "";
  return `${fmt(value)} m²`;
}

function usd(value, suffix) {
  if (value == null || value === "" || Number(value) <= 0) return "";
  return `USD ${fmt(value)}${suffix || ""}`;
}

const COMPARE_ROWS = [
  ["Precio", (it) => moneyOrBlank(it)],
  ["USD/m²", (it) => usd(it.price_m2)],
  ["Ganga", (it) => textOf(it.deal_label)],
  ["Score", (it) => (it.profile && it.profile.total != null ? String(Math.round(it.profile.total)) : "")],
  ["Vs. barrio", (it) => pctOf(it.vs_barrio_pct)],
  ["Tipo", (it) => (typeof typeLabel === "function" ? typeLabel(it) : textOf(it.property_type))],
  ["Ambientes", (it) => {
    const n = typeof ambientesOf === "function" ? ambientesOf(it) : it.rooms;
    return n ? String(n) : "";
  }],
  ["Dormitorios", (it) => (it.bedrooms ? String(it.bedrooms) : "")],
  ["Baños", (it) => (it.bathrooms ? String(it.bathrooms) : "")],
  ["m² cubiertos", (it) => meters(it.covered_m2)],
  ["m² terreno", (it) => {
    if (it.property_type === "terreno") return meters(it.lot_m2 || it.total_m2 || it.covered_m2);
    const lot = it.lot_m2 || it.total_m2;
    if (!lot || (it.covered_m2 && Math.abs(lot - it.covered_m2) <= 1)) return "";
    return meters(lot);
  }],
  ["Cocheras", (it) => (it.parking ? String(it.parking) : yesNo(it.has_garage))],
  ["Expensas", (it) => textOf(it.expenses)],
  ["Antigüedad", (it) => (it.age_years != null && it.age_years !== "" ? `${it.age_years} años` : "")],
  ["Piso", (it) => textOf(it.floor)],
  ["Orientación", (it) => textOf(it.orientation)],
  ["Estado", (it) => textOf(it.condition)],
  ["Barrio", (it) => textOf(it.barrio)],
  ["Zona", (it) => textOf(it.zona)],
  ["Dirección", (it) => textOf((typeof streetAddress === "function" && streetAddress(it)) || it.address)],
  ["Ubicación", (it) => (it.has_exact_location || it.pin_grade === "exact" || it.pin_grade === "intersection" ? "Real" : (it.lat != null ? "Aproximada" : ""))],
  ["Apto crédito", (it) => yesNo(it.mortgage_credit)],
  ["Dueño directo", (it) => yesNo(it.owner_direct)],
  ["Expensas bajas", (it) => yesNo(it.low_expenses)],
  ["Venta urgente", (it) => yesNo(it.urgent_sale)],
  ["Balcón", (it) => yesNo(it.has_balcony)],
  ["Luminoso", (it) => yesNo(it.bright)],
  ["Patio", (it) => yesNo(it.has_patio)],
  ["Terraza", (it) => yesNo(it.has_terrace)],
  ["Vista abierta", (it) => yesNo(it.open_view)],
  ["Alquiler / mes", (it) => usd(it.monthly_rent_usd)],
  ["Renta contrato", (it) => pctOf(it.monthly_yield_pct)],
  ["Alquiler / noche", (it) => usd(it.nightly_usd)],
  ["Renta temporal", (it) => pctOf(it.temporal_yield_pct)],
];

const AXIS_LABELS = {
  price_m2: "Eje USD/m²",
  zona: "Eje zona",
  ambientes: "Eje ambientes",
  alquiler: "Eje alquiler",
  servicios: "Eje servicios",
};

function axisRows(items) {
  const keys = [];
  items.forEach((it) => {
    const order = (it.profile && it.profile.order) || Object.keys(AXIS_LABELS);
    order.forEach((key) => {
      if (!keys.includes(key) && AXIS_LABELS[key]) keys.push(key);
    });
  });
  return keys.map((key) => [
    AXIS_LABELS[key],
    (it) => {
      const axis = it.profile && it.profile.axes && it.profile.axes[key];
      return axis && axis.score != null ? String(Math.round(axis.score)) : "";
    },
  ]);
}

function headCell(it) {
  const img = typeof listingImage === "function" ? listingImage(it.image, "thumb") : "";
  const photo = img
    ? `<img src="${img}" alt="" />`
    : `<span class="card-ph" aria-hidden="true"></span>`;
  const link = typeof sourceLinkHtml === "function" ? sourceLinkHtml(it) : escapeHtml(it.source_label || "");
  const title = escapeHtml(it.title || "Aviso");
  return `<th scope="col">
    ${photo}
    <span class="compare-title">${title}</span>
    ${link}
    <button type="button" class="compare-remove" data-remove="${escapeHtml(it.id)}">Sacar</button>
  </th>`;
}

function tableHtml(items) {
  const rows = [...COMPARE_ROWS, ...axisRows(items)];
  const body = rows.map(([label, read]) => {
    const values = items.map((it) => textOf(read(it)));
    if (!values.some(Boolean)) return "";
    const cells = values.map((value) => `<td>${value ? escapeHtml(value) : "—"}</td>`).join("");
    return `<tr><th scope="row">${escapeHtml(label)}</th>${cells}</tr>`;
  }).join("");
  return `<div class="compare-scroll">
    <table class="compare-table">
      <thead>
        <tr>
          <th scope="col"></th>
          ${items.map(headCell).join("")}
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>
  </div>`;
}

let openItems = [];

function paintDialog(dlg, items) {
  const title = dlg.querySelector("h2");
  if (title) title.textContent = `Comparar ${items.length} avisos`;
  dlg.querySelector(".compare-scroll")?.remove();
  dlg.querySelector(".compare-head")?.insertAdjacentHTML("afterend", tableHtml(items));
}

function syncOpenCompare() {
  const dlg = document.querySelector(".compare-dialog");
  if (!dlg?.open) return;
  const ids = new Set(getCompareIds());
  openItems = openItems.filter((item) => ids.has(item.id));
  if (openItems.length < 2) dlg.close();
  else paintDialog(dlg, openItems);
}

export function removeCompare(id) {
  const ids = getCompareIds().filter((item) => item !== id);
  localStorage.setItem(CMP_KEY, JSON.stringify(ids));
  if (typeof window !== "undefined" && window.updateCompareButton) window.updateCompareButton();
  syncOpenCompare();
  return ids;
}

export async function openComparator() {
  const ids = getCompareIds();
  if (ids.length < 2) return;
  try {
    const res = await fetch(`/api/compare?ids=${encodeURIComponent(ids.join(","))}`);
    const data = await res.json();
    const items = (data.listings || []).slice(0, 4);
    if (items.length < 2) return;
    openItems = items;
    document.querySelector(".compare-dialog")?.remove();
    const dlg = document.createElement("dialog");
    dlg.className = "compare-dialog";
    dlg.innerHTML = `<form method="dialog">
      <div class="compare-head">
        <h2>Comparar ${items.length} avisos</h2>
        <button value="close" type="submit">Cerrar</button>
      </div>
      ${tableHtml(items)}
    </form>`;
    dlg.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-remove]");
      if (!btn) return;
      ev.preventDefault();
      removeCompare(btn.dataset.remove);
    });
    dlg.addEventListener("close", () => dlg.remove());
    document.body.appendChild(dlg);
    dlg.showModal();
  } catch (e) {
    console.error(e);
  }
}

if (typeof window !== "undefined") {
  window.getCompareIds = getCompareIds;
  window.toggleCompare = toggleCompare;
  window.removeCompare = removeCompare;
  window.openComparator = openComparator;
}
