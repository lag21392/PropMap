const map = L.map("map", {
  zoomControl: true,
  dragging: true,
  scrollWheelZoom: true,
  doubleClickZoom: true,
  boxZoom: true,
  inertia: true,
}).setView([-38.4161, -63.6167], 4);

L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19,
}).addTo(map);

const exactCluster = L.markerClusterGroup({
  showCoverageOnHover: false,
  maxClusterRadius: 42,
  spiderfyOnMaxZoom: false,
  zoomToBoundsOnClick: false,
  singleMarkerMode: false,
});
map.addLayer(exactCluster);
const pinLinkLayer = L.layerGroup().addTo(map);
exactCluster.on("clusterclick", (ev) => {
  const items = listingsFromMarkers(ev.layer.getAllChildMarkers());
  openPickPopup(ev.layer.getLatLng(), items, "Avisos en este grupo");
});

const zoneLayer = L.layerGroup().addTo(map);
let zoneMarkersByKey = {};
let stackMarkers = [];
let approxBox = null;

let allListings = [];
let facebook = [];
let markersById = {};
let pollTimer = null;
let wasRunning = false;
let cityWasRunning = {};
let noteTimers = {};
let selectedId = null;
let loadInFlight = false;
let lastListingsFp = "";
let listingsReady = false;
let scrapeRunning = false;
let viewSeq = 0;
let listingsRev = 0;
let loadRetries = 0;
let lastStatus = {};
const isAdmin = location.pathname.replace(/\/+$/, "") === "/admin";
document.body.classList.toggle("is-admin", isAdmin);

function waitCopy(kind) {
  if (kind === "scrape") return "Buscando propiedades en este lugar… van a ir apareciendo solas.";
  if (kind === "city") return "Cargando avisos de este lugar…";
  return "Cargando avisos…";
}

function listingsWaitHtml(copy) {
  return `<div class="list-loading" role="status" aria-live="polite">
    <span class="list-loading-pulse" aria-hidden="true"></span>
    <p class="list-loading-title">Cargando avisos</p>
    <p class="list-loading-copy">${copy}</p>
    <div class="card-skel" aria-hidden="true"></div>
    <div class="card-skel" aria-hidden="true"></div>
    <div class="card-skel" aria-hidden="true"></div>
  </div>`;
}

function showListingsWait(kind) {
  listingsReady = false;
  const copy = waitCopy(kind);
  const list = $("list");
  if ($("listCount")) $("listCount").textContent = "…";
  if (list) {
    list.setAttribute("aria-busy", "true");
    list.innerHTML = listingsWaitHtml(copy);
  }
  const overlay = $("mapLoading");
  if (overlay) {
    overlay.hidden = false;
    const line = overlay.querySelector("[data-loading-copy]");
    if (line) line.textContent = copy;
  }
  if ($("statusLine") && kind !== "scrape") $("statusLine").textContent = copy;
}

function hideListingsWait() {
  listingsReady = true;
  $("list")?.setAttribute("aria-busy", "false");
  const overlay = $("mapLoading");
  if (overlay) overlay.hidden = true;
}

const colors = {
  oportunidad: "#1f7a4a",
  bueno: "#6a8f2e",
  mercado: "#3d6f8a",
  caro: "#c45c3a",
  "revisar (muy barato)": "#a07a2e",
  "revisar (outlier)": "#a07a2e",
  "revisar (dato raro)": "#a07a2e",
  "revisar (señales)": "#a07a2e",
  "sin precio": "#6b7a72",
  "sin comparables": "#6b7a72",
  "loteo (fuera de mediana)": "#8a7760",
};

const CITY_VIEWS = {};
let focusedCity = "";

const TYPE_LABEL = {
  casa: "Casa",
  departamento: "Depto",
  ph: "PH",
  terreno: "Terreno",
  local: "Local",
  oficina: "Oficina",
  galpon: "Galpón",
};

const $ = (id) => document.getElementById(id);
let currentUser = null;
const LOCAL_PINS_KEY = "propmap.pins.v1";

function isExactPin(item) {
  return Boolean(item && item.has_exact_location && !item.location_approx);
}

loadTrackers();

async function load(opts = {}) {
  const city = currentCity();
  if (loadInFlight && opts.live) return;
  if (!opts.live) viewSeq += 1;
  const seq = viewSeq;
  loadInFlight = true;
  const shouldWait = !listingsReady || opts.waitKind === "city";
  if (shouldWait) showListingsWait(opts.waitKind || "init");
  let data;
  try {
    const q = new URLSearchParams();
    if (opts.live) q.set("live", "1");
    q.set("city", city);
    if (opts.live && listingsRev) q.set("since", String(listingsRev));
    const res = await fetch("/api/listings?" + q.toString());
    if (!res.ok) throw new Error(String(res.status));
    data = await res.json();
    if (!(data.warming && !(data.listings || []).length)) loadRetries = 0;
  } catch {
    if (seq !== viewSeq || currentCity() !== city) return;
    if (listingsReady && opts.live) return;
    loadRetries += 1;
    showListingsWait(opts.waitKind || "init");
    const delay = Math.min(3000, 350 * loadRetries);
    setTimeout(() => load({ ...opts, live: Boolean(opts.live) }), delay);
    return;
  } finally {
    loadInFlight = false;
  }
  if (!data) return;
  if (seq !== viewSeq || currentCity() !== city) return;
  if (data.warming && !(data.listings || []).length) {
    loadRetries += 1;
    showListingsWait(opts.waitKind || "init");
    setTimeout(() => load({ waitKind: opts.waitKind || "init" }), Math.min(2000, 300 + loadRetries * 40));
    return;
  }
  if (data.rev != null) listingsRev = data.rev;
  if (data.unchanged) {
    if (!allListings.length) {
      listingsRev = 0;
      hideListingsWait();
      load({ waitKind: "city" });
      return;
    }
    hideListingsWait();
    return;
  }
  const fp = `${(data.listings || []).length}:${(data.listings || []).filter((x) => x.has_exact_location).length}:${(data.listings || []).map((x) => x.id).join("|")}`;
  if (opts.live && fp === lastListingsFp && listingsReady) return;
  lastListingsFp = fp;
  allListings = applyLocalPins(data.listings || []);
  facebook = data.facebook || [];
  window.lastStats = data.stats || {};
  window.lastCities = data.cities || [];
  hideListingsWait();
  renderKpis(data.stats || {});
  fillCities(data.cities || []);
  if (currentCity() !== city) {
    listingsRev = 0;
    lastListingsFp = "";
    load({ waitKind: "city" });
    return;
  }
  fillZonas(cityItems());
  fillBarrios();
  render();
  renderStats(data.stats || {});
  loadMarket();
  if (!opts.keepStatus) {
    $("statusLine").textContent = data.last_run
      ? `Última búsqueda: ${new Date(data.last_run).toLocaleString("es-AR")} · dólar blue ${fmt(data.usd_ars)}`
      : "Tocá “Buscar avisos” para actualizar la base en segundo plano. El mapa no se traba.";
  }
  renderFacebook();
}

function renderKpis(stats) {
  $("kpis").innerHTML = [
    kpi(stats.total || 0, "avisos reunidos"),
    kpi(stats.deals || 0, "oportunidades"),
    kpi(stats.median_usd ? `USD ${fmt(stats.median_usd)}` : "—", "mediana de precio"),
    kpi(stats.median_m2 ? `USD ${fmt(stats.median_m2)}` : "—", "mediana USD/m²"),
  ].join("");
}


function renderKpisFromItems(items) {
  const comps = items.filter((x) => !x.exclude_from_comps);
  const usd = comps.map((x) => x.price_usd).filter(Boolean).sort((a, b) => a - b);
  const m2 = comps.map((x) => x.price_m2).filter(Boolean).sort((a, b) => a - b);
  const mid = (arr) => (arr.length ? arr[Math.floor(arr.length / 2)] : 0);
  $("kpis").innerHTML = [
    kpi(items.length, "avisos en esta vista"),
    kpi(items.filter((x) => x.deal_label === "oportunidad").length, "oportunidades"),
    kpi(mid(usd) ? `USD ${fmt(mid(usd))}` : "—", "mediana de precio"),
    kpi(mid(m2) ? `USD ${fmt(mid(m2))}` : "—", "mediana USD/m\u00b2"),
  ].join("");
}

function kpi(value, label) {
  return `<div class="kpi"><b>${value}</b><span>${label}</span></div>`;
}

function currentCity() {
  return $("cityFilter")?.value || localStorage.getItem("propmap.city") || "caba";
}

function inCityView(item, view) {
  if (!view || !item.lat || !item.lon) return false;
  const inRadius = distM(item, view) / 1000 <= (view.radiusKm || 35);
  if (view.bbox && view.bbox.length === 4) {
    const [s, w, n, e] = view.bbox;
    const inBox = item.lat >= s && item.lat <= n && item.lon >= w && item.lon <= e;
    return inBox || inRadius;
  }
  return inRadius;
}

function placeIdsFor(city) {
  const row = (window.lastCities || []).find((c) => c.id === city);
  if (row && row.place_ids && row.place_ids.length) return new Set(row.place_ids);
  return new Set([city]);
}

function belongsToCity(item, city) {
  if (!city) return true;
  if (placeIdsFor(city).has(item.city || "") || (item.city || "") === city) return true;
  return false;
}

function cityItems() {
  const city = currentCity();
  return allListings.filter((x) => belongsToCity(x, city));
}

function rememberCityView(place) {
  if (!place || !place.id) return;
  CITY_VIEWS[place.id] = {
    lat: place.lat,
    lon: place.lon,
    zoom: place.zoom || 13,
    radiusKm: place.radius_km || 25,
    bbox: place.bbox || null,
  };
}

function fillCities(cities) {
  const select = $("cityFilter");
  const manual = document.querySelector("#manualForm [name=city]");
  if (!select) return;
  const saved = localStorage.getItem("propmap.city") || "";
  const current = select.value || saved || "caba";
  const rows = [...(cities || [])];
  if (current && !rows.some((c) => c.id === current) && CITY_VIEWS[current]) {
    const view = CITY_VIEWS[current];
    rows.push({
      id: current,
      label: (window.lastCities || []).find((c) => c.id === current)?.label
        || current.replace(/-/g, " "),
      lat: view.lat,
      lon: view.lon,
      zoom: view.zoom,
      radius_km: view.radiusKm,
      bbox: view.bbox,
    });
  }
  if (!rows.length) return;
  const options = rows.map((c) => `<option value="${c.id}">${c.label}</option>`).join("");
  select.innerHTML = options;
  if (manual) manual.innerHTML = options;
  const ids = rows.map((c) => c.id);
  select.value = ids.includes(current) ? current : (ids.includes("caba") ? "caba" : ids[0]);
  localStorage.setItem("propmap.city", select.value);
  rows.forEach((c) => {
    const prev = CITY_VIEWS[c.id];
    rememberCityView(c);
    if (c.bbox && JSON.stringify(prev?.bbox || null) !== JSON.stringify(c.bbox)) {
      if (focusedCity === c.id) focusedCity = "";
    }
    if (c.barrios && c.barrios.length) {
      window.placeBarrios = window.placeBarrios || {};
      window.placeBarrios[c.id] = c.barrios.map((b) => b.name);
    }
  });
  const view = CITY_VIEWS[select.value];
  if (view && $("placeQuery") && !$("placeQuery").value) {
    $("placeQuery").placeholder = `ej. ${cities.find((c) => c.id === select.value)?.label || "una ciudad"}`;
  }
  ensureCityView(select.value);
}

function fillZonas(items) {
  const select = $("zonaFilter");
  const current = select.value;
  const zonas = [...new Set(items.map((x) => x.zona).filter(Boolean))].sort((a, b) => a.localeCompare(b, "es"));
  select.innerHTML = `<option value="">Todas</option>` + zonas.map((z) => `<option>${escapeHtml(z)}</option>`).join("");
  select.value = current;
}

function fillBarrios() {
  const select = $("barrioFilter");
  if (!select) return;
  const city = currentCity();
  const current = select.value;
  const fromItems = cityItems().map((x) => x.barrio).filter((n) => n && n !== "Sin clasificar");
  const fromKnown = (window.placeBarrios && window.placeBarrios[city]) || [];
  const names = [...new Set([...fromItems, ...fromKnown])].sort((a, b) => a.localeCompare(b, "es"));
  select.innerHTML = `<option value="">Todos</option>` + names.map((n) => `<option>${escapeHtml(n)}</option>`).join("");
  select.value = names.includes(current) ? current : "";
}

function filtered() {
  const city = currentCity();
  const type = $("typeFilter").value;
  const zona = $("zonaFilter").value;
  const barrio = $("barrioFilter")?.value || "";
  const max = Number($("maxPrice").value || 0);
  const minDeal = Number($("dealBar")?.value || 0);
  const favs = $("favOnly").checked;
  return allListings.filter((item) => {
    if (city && !belongsToCity(item, city)) return false;
    if (type && item.property_type !== type) return false;
    if (zona && item.zona !== zona) return false;
    if (barrio && item.barrio !== barrio) return false;
    if (max && (!item.price_usd || item.price_usd > max)) return false;
    if (minDeal > 0 && (item.deal_score || 0) < minDeal) return false;
    if (minDeal >= 40 && item.is_outlier) return false;
    if (favs && !item.favorite) return false;
    return true;
  });
}

function zoneKey(item) {
  const city = item.city || "caba";
  if (item.barrio && item.barrio !== "Sin clasificar") return `${city}|b|${item.barrio}`;
  return `${city}|z|${item.zona || "Sin zona"}`;
}

function approxCellKey(item) {
  if (item.approx_cell) return `${item.city || "caba"}|c|${item.approx_cell}`;
  if (item.lat && item.lon) {
    return `${item.city || "caba"}|g|${Number(item.lat).toFixed(4)}|${Number(item.lon).toFixed(4)}`;
  }
  return zoneKey(item);
}

function cellBounds(lat, lon, spanM) {
  const half = (spanM || 180) / 2;
  const dlat = half / 111320;
  const dlon = half / (111320 * Math.max(0.2, Math.cos((lat * Math.PI) / 180)));
  return [[lat - dlat, lon - dlon], [lat + dlat, lon + dlon]];
}

function showApproxBox(item, latlng) {
  if (approxBox) {
    map.removeLayer(approxBox);
    approxBox = null;
  }
  if (!item || isExactPin(item)) return;
  const lat = latlng?.lat ?? item.lat;
  const lon = latlng?.lng ?? item.lon;
  if (lat == null || lon == null) return;
  const color = colors[item.deal_label] || "#3d6f8a";
  approxBox = L.rectangle(cellBounds(lat, lon, item.approx_span_m || 180), {
    color,
    weight: 2,
    fillColor: color,
    fillOpacity: 0.12,
    interactive: false,
  }).addTo(map);
}

function zoneCenter(group) {
  const withPin = (group.items || []).filter((item) => item.lat && item.lon);
  if (withPin.length) {
    return [
      withPin.reduce((sum, item) => sum + item.lat, 0) / withPin.length,
      withPin.reduce((sum, item) => sum + item.lon, 0) / withPin.length,
    ];
  }
  const view = CITY_VIEWS[group.city || currentCity()];
  return view ? [view.lat, view.lon] : [-38.4161, -63.6167];
}

function zoneIcon(group) {
  const selected = group.items.some((item) => item.id === selectedId);
  const deal = group.items.find((item) => item.deal_label === "oportunidad")
    || group.items.find((item) => item.deal_label === "bueno")
    || group.items[0];
  const color = colors[deal?.deal_label] || "#7eb6d6";
  return L.divIcon({
    className: `zone-pin approx${selected ? " is-selected" : ""}`,
    html: `<span style="border-color:${color};color:${color}"><b>${group.items.length}</b></span>`,
    iconSize: [44, 44],
    iconAnchor: [22, 22],
    popupAnchor: [0, -18],
  });
}

function pickItemRow(item) {
  return `<button type="button" class="zone-item${item.id === selectedId ? " is-selected" : ""}" data-id="${item.id}">
      <span class="kind ${cssId(item.property_type)}">${escapeHtml(typeLabel(item))}</span>
      <span>${money(item)} · ${escapeHtml(sizeBits(item).join(" · ") || "sin m²")}</span>
      <span class="tag ${cssId(item.deal_label || "")}">${escapeHtml(item.deal_label || "")}${item.vs_barrio_pct != null ? ` · ${item.vs_barrio_pct > 0 ? "-" : "+"}${Math.abs(item.vs_barrio_pct)}%` : ""}</span>
    </button>`;
}

function zonePopupHtml(group) {
  const rows = sorted(group.items).slice(0, 10).map(pickItemRow).join("");
  const extra = group.items.length > 10 ? `<p class="muted">+${group.items.length - 10} más en la lista de la izquierda</p>` : "";
  return `<div class="popup zone-popup">
    <strong>${escapeHtml(group.label)}</strong>
    <p>${group.items.length} aviso${group.items.length === 1 ? "" : "s"} · ubicación aproximada (manzana)</p>
    ${rows}${extra}
  </div>`;
}
function pinIcon(item) {
  const color = colors[item.deal_label] || "#7eb6d6";
  const fav = item.favorite ? " fav" : "";
  const shape = isExactPin(item) ? "round" : "square";
  return L.divIcon({
    className: `prop-pin ${shape}${fav}`,
    html: `<span style="background:${color}"></span>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
    popupAnchor: [0, -10],
  });
}

function distM(a, b) {
  const toRad = (deg) => (deg * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.min(1, Math.sqrt(h)));
}

function offsetAround(lat, lon, index, total, meters) {
  const ang = (2 * Math.PI * index) / total - Math.PI / 2;
  const dlat = (meters * Math.sin(ang)) / 111320;
  const dlon = (meters * Math.cos(ang)) / (111320 * Math.cos((lat * Math.PI) / 180) || 1);
  return [lat + dlat, lon + dlon];
}

function hubIcon() {
  return L.divIcon({
    className: "pin-hub",
    html: "<span></span>",
    iconSize: [10, 10],
    iconAnchor: [5, 5],
  });
}

function groupExactPins(items) {
  const exact = items.filter((item) => isExactPin(item) && item.lat && item.lon);
  const used = new Set();
  const groups = [];
  exact.forEach((item) => {
    if (used.has(item.id)) return;
    const group = { items: [item], lat: item.lat, lon: item.lon };
    used.add(item.id);
    exact.forEach((other) => {
      if (used.has(other.id)) return;
      if (distM(item, other) <= 15) {
        group.items.push(other);
        used.add(other.id);
      }
    });
    group.lat = group.items.reduce((sum, row) => sum + row.lat, 0) / group.items.length;
    group.lon = group.items.reduce((sum, row) => sum + row.lon, 0) / group.items.length;
    groups.push(group);
  });
  return groups;
}

function stackIcon(group) {
  if (group.items.length === 1) return pinIcon(group.items[0]);
  const selected = group.items.some((item) => item.id === selectedId);
  const deal = group.items.find((item) => item.deal_label === "oportunidad")
    || group.items.find((item) => item.deal_label === "bueno")
    || group.items[0];
  const color = colors[deal?.deal_label] || "#7eb6d6";
  return L.divIcon({
    className: `zone-pin stack-pin${selected ? " is-selected" : ""}`,
    html: `<span style="border-color:${color};color:${color}"><b>${group.items.length}</b></span>`,
    iconSize: [34, 34],
    iconAnchor: [17, 17],
    popupAnchor: [0, -14],
  });
}

function pickListHtml(items, title) {
  const shown = sorted(items).slice(0, 12);
  const rows = shown.map(pickItemRow).join("");
  const extra = items.length > 12 ? `<p class="muted">+${items.length - 12} más en la lista de la izquierda</p>` : "";
  return `<div class="popup zone-popup">
    <strong>${escapeHtml(title)}</strong>
    <p>${items.length} avisos · elegí uno para ver la foto y los datos</p>
    ${rows}${extra}
  </div>`;
}

function listingPopupLatLng(item) {
  if (isExactPin(item) && item.lat && item.lon) {
    return L.latLng(item.lat, item.lon);
  }
  const zone = zoneMarkersByKey[approxCellKey(item)];
  if (zone) return L.latLng(zone.lat, zone.lon);
  if (item.lat && item.lon) return L.latLng(item.lat, item.lon);
  return null;
}

function openListingPopup(item, latlng) {
  const here = latlng || listingPopupLatLng(item);
  if (!item || !here) return;
  L.popup({ maxWidth: 300, minWidth: 220, autoPan: true })
    .setLatLng(here)
    .setContent(popupHtml(item))
    .openOn(map);
  bindPinControls(item);
}

function bindPickRows(latlng) {
  document.querySelectorAll(".zone-popup .zone-item").forEach((btn) => {
    btn.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const current = allListings.find((x) => x.id === btn.dataset.id);
      if (!current) return;
      selectListing(current, { focusMap: false, at: latlng });
    };
  });
}

function listingsFromMarkers(markers) {
  const seen = new Set();
  const items = [];
  (markers || []).forEach((marker) => {
    const pack = marker._stackGroup?.items || (marker._listing ? [marker._listing] : []);
    pack.forEach((item) => {
      if (seen.has(item.id)) return;
      seen.add(item.id);
      items.push(item);
    });
  });
  return items;
}

function openPickPopup(latlng, items, title) {
  if (!items.length) return;
  if (items.length === 1) {
    selectListing(items[0], { focusMap: false, at: latlng });
    return;
  }
  L.popup({ maxWidth: 320, minWidth: 220, className: "pick-popup" })
    .setLatLng(latlng)
    .setContent(pickListHtml(items, title))
    .openOn(map);
  bindPickRows(latlng);
}

function sorted(items) {
  const key = $("sortBy")?.value || "deal";
  const copy = items.slice();
  const usefulM2 = (x) => (x.property_type === "terreno"
    ? (x.lot_m2 || x.total_m2 || x.covered_m2 || 0)
    : (x.covered_m2 || x.total_m2 || 0));
  copy.sort((a, b) => {
    if (a.favorite !== b.favorite) return a.favorite ? -1 : 1;
    let cmp = 0;
    if (key === "price-asc") cmp = (a.price_usd || 1e15) - (b.price_usd || 1e15);
    else if (key === "price-desc") cmp = (b.price_usd || 0) - (a.price_usd || 0);
    else if (key === "m2") cmp = usefulM2(b) - usefulM2(a);
    else if (key === "m2price") cmp = (a.price_m2 || 1e15) - (b.price_m2 || 1e15);
    else if (key === "type") cmp = typeLabel(a).localeCompare(typeLabel(b), "es");
    else if (key === "barrio") cmp = (a.barrio || "").localeCompare(b.barrio || "", "es");
    else if (key === "contacted") cmp = Number(Boolean(b.contacted)) - Number(Boolean(a.contacted));
    else if (key === "yield-month") cmp = (b.monthly_yield_pct || 0) - (a.monthly_yield_pct || 0);
    else if (key === "yield-temp") cmp = (b.temporal_yield_pct || 0) - (a.temporal_yield_pct || 0);
    if (cmp) return cmp;
    return (b.deal_score || b.quality_score || b.score || 0) - (a.deal_score || a.quality_score || a.score || 0);
  });
  return copy;
}

function render() {
  if (!listingsReady) {
    if ($("listCount")) $("listCount").textContent = "…";
    return;
  }
  const items = sorted(filtered());
  $("listCount").textContent = `${items.length}`;
  renderKpisFromItems(items);
  const cards = items.map(cardHtml).join("");
  updateSearchingBanner();
  if (!cards) {
    if (scrapeRunning || placeIsQueued()) {
      $("list").innerHTML = listingsWaitHtml(waitCopy("scrape"));
    } else if (allListings.length) {
      $("list").innerHTML = `<p class="status">Nada con esos filtros.</p>`;
    } else {
      $("list").innerHTML = `<p class="status">Todavía no hay avisos en este lugar. Tocá Buscar avisos para actualizar la base en segundo plano, o elegí otra localidad.</p>`;
    }
  } else {
    const banner = scrapeRunning
      ? `<p class="list-banner" role="status">Se actualiza la base en segundo plano. Podés filtrar y cambiar de lugar.</p>`
      : "";
    $("list").innerHTML = banner + cards;
  }
  const overlay = $("mapLoading");
  if (overlay) {
    const keep = (scrapeRunning || placeIsQueued()) && !items.length;
    overlay.hidden = !keep;
    if (keep) {
      const line = overlay.querySelector("[data-loading-copy]");
      if (line) line.textContent = waitCopy("scrape");
    }
  }
  exactCluster.clearLayers();
  pinLinkLayer.clearLayers();
  zoneLayer.clearLayers();
  markersById = {};
  zoneMarkersByKey = {};
  stackMarkers = [];
  const zoneGroups = {};
  groupExactPins(items).forEach((group) => {
    const n = group.items.length;
    const spreadM = n === 2 ? 6 : 8;
    if (n > 1) {
      pinLinkLayer.addLayer(L.marker([group.lat, group.lon], { icon: hubIcon(), interactive: false, keyboard: false }));
    }
    group.items.forEach((item, i) => {
      const [lat, lon] = n === 1 ? [group.lat, group.lon] : offsetAround(group.lat, group.lon, i, n, spreadM);
      if (n > 1) {
        pinLinkLayer.addLayer(
          L.polyline([[group.lat, group.lon], [lat, lon]], {
            color: "#2a4a3c",
            weight: 2,
            opacity: 0.75,
            lineCap: "round",
            interactive: false,
          })
        );
      }
      const marker = L.marker([lat, lon], { icon: pinIcon(item), riseOnHover: true });
      marker._listing = item;
      marker.on("click", () => selectListing(item, { focusMap: false, at: L.latLng(lat, lon) }));
      markersById[item.id] = marker;
      stackMarkers.push({ marker, group: { items: [item], lat, lon } });
      exactCluster.addLayer(marker);
    });
  });
  items.forEach((item) => {
    if (isExactPin(item) && item.lat && item.lon) return;
    const key = approxCellKey(item);
    if (!zoneGroups[key]) {
      zoneGroups[key] = {
        key,
        city: item.city || "caba",
        label: (item.barrio && item.barrio !== "Sin clasificar") ? item.barrio : (item.zona || "Zona sin clasificar"),
        items: [],
      };
    }
    zoneGroups[key].items.push(item);
  });
  Object.values(zoneGroups).forEach((group) => {
    const [lat, lon] = zoneCenter(group);
    const one = group.items.length === 1;
    const marker = L.marker([lat, lon], {
      icon: one ? pinIcon(group.items[0]) : zoneIcon(group),
      riseOnHover: true,
      zIndexOffset: 200,
    });
    marker.on("click", () => {
      if (one) selectListing(group.items[0], { focusMap: false, at: L.latLng(lat, lon) });
      else openPickPopup(L.latLng(lat, lon), group.items, `${group.label} · ubicación aproximada`);
    });
    group.items.forEach((item) => {
      markersById[item.id] = marker;
    });
    zoneMarkersByKey[group.key] = { marker, group, lat, lon };
    zoneLayer.addLayer(marker);
  });
  bindListEvents();
  highlightSelected();
  if (selectedId) {
    const current = allListings.find((x) => x.id === selectedId);
    if (current) showDetail(current);
  }
}

function selectListing(item, { focusMap = true, at = null } = {}) {
  if (!item) return;
  if (window.innerWidth <= 980) setTab("detail");
  track("listing", { listing: item.id });
  selectedId = item.id;
  highlightSelected();
  refreshZoneIcons();
  showDetail(item);
  document.getElementById(`card-${cssId(item.id)}`)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  openListingPopup(item, at);
  showApproxBox(item, at || listingPopupLatLng(item));
  if (focusMap) focusListing(item);
}

function refreshZoneIcons() {
  Object.values(zoneMarkersByKey).forEach(({ marker, group }) => {
    marker.setIcon(group.items.length === 1 ? pinIcon(group.items[0]) : zoneIcon(group));
  });
  stackMarkers.forEach(({ marker, group }) => {
    marker.setIcon(group.items.length === 1 ? pinIcon(group.items[0]) : stackIcon(group));
  });
}

function highlightSelected() {
  document.querySelectorAll(".card.is-selected").forEach((node) => node.classList.remove("is-selected"));
  if (!selectedId) return;
  document.getElementById(`card-${cssId(selectedId)}`)?.classList.add("is-selected");
}

function closeDetail() {
  selectedId = null;
  highlightSelected();
  refreshZoneIcons();
  if (approxBox) {
    map.removeLayer(approxBox);
    approxBox = null;
  }
  const pane = $("detailPane");
  if (!pane) return;
  pane.classList.add("is-empty");
  pane.innerHTML = `<p class="muted">Elegí un aviso en la lista o en el mapa para ver la ficha.</p>`;
}

function focusListing(item) {
  if (!item) return;
  if (!isExactPin(item)) {
    const zone = zoneMarkersByKey[approxCellKey(item)];
    if (zone) {
      map.setView([zone.lat, zone.lon], 14, { animate: true });
      zone.marker.setIcon(zoneIcon(zone.group));
    }
    return;
  }
  if (!item.lat || !item.lon) return;
  const marker = markersById[item.id];
  const go = () => {
    map.setView([item.lat, item.lon], 18, { animate: true });
  };
  if (marker && exactCluster.hasLayer(marker) && typeof exactCluster.zoomToShowLayer === "function") {
    exactCluster.zoomToShowLayer(marker, go);
  } else {
    go();
  }
}

function typeLabel(item) {
  return TYPE_LABEL[item.property_type] || item.property_type || "Aviso";
}

function locationKindLabel(item) {
  if (isExactPin(item)) {
    return item.location_kind === "intersection" ? "Intersección en el mapa" : "Calle y altura";
  }
  if (item.location_kind === "intersection" || (item.intersection && !item.street_number)) {
    return "Intersección aproximada";
  }
  if (item.lat == null || item.lon == null) return "Sin punto en el mapa";
  return "Aproximada (cuadrado en el mapa)";
}

function locationLine(item) {
  const address = (item.address || "").trim();
  const barrio = (item.barrio || "").trim();
  const parts = [];
  if (address) parts.push(address);
  else if (item.approx_address) parts.push(item.approx_address);
  else if (barrio && barrio !== "Sin clasificar") parts.push(barrio);
  if (item.intersection) parts.push(`Intersección ${item.intersection}`);
  if (item.between) parts.push(`Entre ${item.between}`);
  if (!isExactPin(item) && item.lat != null) parts.push("ubicación aprox.");
  return parts.join(" · ") || locationKindLabel(item);
}

function listingTags(item) {
  const seen = new Set();
  const out = [];
  for (const raw of [...(item.tags || []), ...(item.amenities || [])]) {
    const tag = String(raw || "").trim();
    const key = tag.toLowerCase();
    if (!tag || seen.has(key)) continue;
    seen.add(key);
    out.push(tag);
  }
  return out;
}

function pct(n) {
  if (n == null) return "—";
  return `${String(n).replace(".", ",")}%`;
}

function rentStrip(item) {
  if (item.monthly_yield_pct == null && item.temporal_yield_pct == null) return "";
  const rows = [];
  if (item.monthly_rent_usd) {
    rows.push(`<span>Contrato <b>${pct(item.monthly_yield_pct)}</b> · USD ${fmt(item.monthly_rent_usd)}/mes</span>`);
  }
  if (item.nightly_usd) {
    rows.push(`<span>Temporal <b>${pct(item.temporal_yield_pct)}</b> · USD ${fmt(item.nightly_usd)}/noche</span>`);
  }
  return rows.length ? `<div class="rent-strip">${rows.join("")}</div>` : "";
}

function rentEstimate(item) {
  if (item.monthly_yield_pct == null && item.temporal_yield_pct == null) return "";
  const month = item.monthly_rent_usd
    ? `<div class="rent-row"><span>Con contrato</span><b>USD ${fmt(item.monthly_rent_usd)}/mes · ${pct(item.monthly_yield_pct)} anual</b><small>${escapeHtml(item.rental_month_scope || "según la ciudad")}${item.rental_month_n ? ` · ${item.rental_month_n} avisos` : ""}</small></div>`
    : `<div class="rent-row"><span>Con contrato</span><b>sin comps aún</b></div>`;
  const night = item.nightly_usd
    ? `<div class="rent-row"><span>Temporal</span><b>USD ${fmt(item.nightly_usd)}/noche · ${pct(item.temporal_yield_pct)} anual</b><small>${escapeHtml(item.rental_night_scope || "según la ciudad")}${item.occupancy_pct ? ` · ocupación ${item.occupancy_pct}%` : ""}${item.rental_night_n ? ` · ${item.rental_night_n} avisos` : ""}</small></div>`
    : `<div class="rent-row"><span>Temporal</span><b>sin comps aún</b></div>`;
  return `<div class="rent-estimate">
    <h4>Cuánto podría alquilar</h4>
    ${month}
    ${night}
    <p class="muted">Estimación por zona, tipo y tamaño. El % es neto: contrato descuenta vacancia y gastos; temporal usa la ocupación típica del lugar.</p>
  </div>`;
}

function sourceLabel(item) {
  return item.source_label || ({
    zonaprop: "ZonaProp",
    mercadolibre: "Mercado Libre",
    properati: "Properati",
    argenprop: "Argenprop",
    manual: "Carga manual",
  }[item.source] || item.source || "");
}

function sourceRows(item) {
  if (item.sources && item.sources.length) return item.sources;
  return [{ source: item.source, url: item.url, label: sourceLabel(item) }];
}

function sourceSummary(item) {
  const labels = [...new Set(sourceRows(item).map((row) => row.label || row.source).filter(Boolean))];
  return labels.join(" · ") || sourceLabel(item);
}

function sourceLinkHtml(item, className) {
  const cls = className ? ` class="${className}"` : "";
  const links = sourceRows(item).filter((row) => row.url).map((row) =>
    `<a${cls} href="${row.url}" target="_blank" rel="noopener">${escapeHtml(row.label || row.source || "Aviso")}</a>`
  );
  return links.join(" · ");
}

function sizeBits(item) {
  const bits = [];
  if (item.property_type === "terreno") {
    const lot = item.lot_m2 || item.total_m2 || item.covered_m2;
    if (lot) bits.push(`${fmt(lot)} m² terreno`);
  } else {
    if (item.covered_m2) bits.push(`${fmt(item.covered_m2)} m² cubiertos`);
    const lot = item.lot_m2 || item.total_m2;
    if (lot && (!item.covered_m2 || Math.abs(lot - item.covered_m2) > 1)) {
      bits.push(`${fmt(lot)} m² terreno`);
    }
  }
  if (item.bedrooms && item.property_type !== "terreno") bits.unshift(`${item.bedrooms} dorm`);
  if (item.price_m2) bits.push(`USD ${fmt(item.price_m2)}/m²`);
  return bits;
}
function cardHtml(item) {
  const img = item.image || "https://images.unsplash.com/photo-1500375592092-40eb2168fd21?auto=format&fit=crop&w=200&q=60";
  const kind = typeLabel(item);
  const bits = sizeBits(item);
  const chips = listingTags(item).slice(0, 4).map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join("");
  const selected = item.id === selectedId ? " is-selected" : "";
  const contacted = item.contacted ? " is-contacted" : "";
  const approx = isExactPin(item) ? "" : " is-approx";
  const place = item.address || item.barrio || "";
  const crossing = item.intersection ? ` · intersección ${item.intersection}` : "";
  const between = item.between ? ` · entre ${item.between}` : "";
  return `<article class="card${item.favorite ? " is-fav" : ""}${selected}${contacted}${approx}" id="card-${cssId(item.id)}" data-id="${item.id}">
    <img src="${img}" alt="" />
    <div>
      <div class="card-top">
        <span class="kind ${cssId(item.property_type)}">${escapeHtml(kind)}</span>
        <button class="fav-btn" type="button" data-fav="${item.id}" title="Favorito">${item.favorite ? "\u2605" : "\u2606"}</button>
      </div>
      <div class="price">${money(item)}</div>
      <div class="headline">${escapeHtml(bits.join(" · ") || item.title || "")}</div>
      <div class="meta">${escapeHtml(place)}${escapeHtml(crossing)}${escapeHtml(between)} · ${escapeHtml(sourceSummary(item))}${isExactPin(item) ? "" : " · ubicación aprox."}${item.mortgage_credit === true ? " · apto crédito" : ""}${item.contacted ? " · contactado" : ""}</div>
      <span class="tag ${cssId(item.deal_label)}">${escapeHtml(item.deal_label || "")}${item.vs_barrio_pct != null ? ` · ${item.vs_barrio_pct > 0 ? "-" : "+"}${Math.abs(item.vs_barrio_pct)}% vs barrio` : ""}</span>
      ${rentStrip(item)}
      ${item.quality_label ? `<span class="tag quality">${escapeHtml(item.quality_label)} · ${fmt(item.quality_score)}</span>` : ""}
      ${chips ? `<div class="chips">${chips}</div>` : ""}
    </div>
  </article>`;
}

function popupHtml(item) {
  const loc = escapeHtml(locationLine(item));
  const kind = typeLabel(item);
  const blurb = (item.description || "").replace(/\s+/g, " ").trim().slice(0, 180);
  return `<div class="popup">
    ${item.image ? `<img src="${item.image}" alt="" />` : ""}
    <div class="card-top">
      <span class="kind ${cssId(item.property_type)}">${escapeHtml(kind)}</span>
      <button class="fav-btn" type="button" data-fav="${item.id}">${item.favorite ? "\u2605" : "\u2606"}</button>
    </div>
    <b>${escapeHtml(kind)} · ${escapeHtml(item.title)}</b><br/>
    ${money(item)} · ${sizeBits(item).join(" · ") || "sin m²"}<br/>
    ${loc}<br/>
    ${item.price_m2 ? `USD ${fmt(item.price_m2)} / m\u00b2` : "sin USD/m\u00b2"}
    ${item.deal_label ? `<br/><b>${escapeHtml(item.deal_label)}</b>${item.vs_barrio_pct != null ? ` · ${item.vs_barrio_pct > 0 ? "-" : "+"}${Math.abs(item.vs_barrio_pct)}% vs comparables` : ""}` : ""}
    ${item.monthly_rent_usd || item.nightly_usd ? `<br/>${item.monthly_rent_usd ? `contrato ${pct(item.monthly_yield_pct)} · USD ${fmt(item.monthly_rent_usd)}/mes` : ""}${item.monthly_rent_usd && item.nightly_usd ? " · " : ""}${item.nightly_usd ? `temporal ${pct(item.temporal_yield_pct)} · USD ${fmt(item.nightly_usd)}/noche` : ""}` : ""}
    ${item.quality_label ? `<br/>${escapeHtml(item.quality_label)}` : ""}<br/>
    ${escapeHtml(sourceSummary(item))}
    ${blurb ? `<p class="popup-blurb">${escapeHtml(blurb)}${(item.description || "").length > 180 ? "…" : ""}</p>` : ""}
    ${sourceLinkHtml(item) ? `<br/>${sourceLinkHtml(item)}` : ""}
  </div>`;
}

function bindListEvents() {
  document.querySelectorAll(".card").forEach((node) => {
    node.onclick = (ev) => {
      if (ev.target.closest(".fav-btn, .note-box")) return;
      const item = allListings.find((x) => x.id === node.dataset.id);
      if (item) selectListing(item);
    };
  });
  bindPinControls();
}

function bindPinControls(item) {
  document.querySelectorAll(".fav-btn").forEach((btn) => {
    btn.onclick = async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = btn.dataset.fav;
      const listing = allListings.find((x) => x.id === id);
      if (!listing) return;
      await savePin(id, { favorite: !listing.favorite });
    };
  });
  document.querySelectorAll(".note-box").forEach((box) => {
    box.onpointerdown = (ev) => ev.stopPropagation();
    box.onclick = (ev) => ev.stopPropagation();
    box.oninput = () => {
      const id = box.dataset.notes;
      const listing = allListings.find((x) => x.id === id);
      if (listing) listing.notes = box.value;
      clearTimeout(noteTimers[id]);
      noteTimers[id] = setTimeout(() => savePin(id, { notes: box.value }), 400);
    };
  });
  if (item) {
    document.querySelectorAll(`[data-notes="${CSS.escape(item.id)}"]`).forEach((box) => {
      if (document.activeElement !== box) box.value = item.notes || "";
    });
  }
}

function cssAttr(value) {
  return String(value || "").replace(/"/g, '\\"');
}

async function savePin(id, payload) {
  const item = allListings.find((x) => x.id === id);
  if (!item) return;
  if (payload.favorite != null) item.favorite = payload.favorite;
  if (payload.notes != null) item.notes = payload.notes;
  if (payload.contacted != null) item.contacted = payload.contacted;
  if (!currentUser || !currentUser.email_verified) {
    writeLocalPin(id, item);
    if (currentUser && !currentUser.email_verified) {
      flashAuth("Validá tu mail para guardar en el servidor. Por ahora queda en este navegador.");
    }
    rerenderKeep();
    return;
  }
  const res = await fetch("/api/pin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, ...payload }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    writeLocalPin(id, item);
    flashAuth(data.detail || "No se pudo guardar en la cuenta.");
    rerenderKeep();
    return;
  }
  item.favorite = data.favorite;
  item.notes = data.notes;
  if (data.contacted != null) item.contacted = data.contacted;
  rerenderKeep();
}

function rerenderKeep() {
  const keep = selectedId;
  render();
  if (keep) {
    selectedId = keep;
    highlightSelected();
    const current = allListings.find((x) => x.id === keep);
    if (current) showDetail(current);
  }
}

function readLocalPins() {
  try {
    return JSON.parse(localStorage.getItem(LOCAL_PINS_KEY) || "{}") || {};
  } catch {
    return {};
  }
}

function writeLocalPin(id, item) {
  const all = readLocalPins();
  all[id] = {
    favorite: Boolean(item.favorite),
    notes: item.notes || "",
    contacted: Boolean(item.contacted),
  };
  try { localStorage.setItem(LOCAL_PINS_KEY, JSON.stringify(all)); } catch (_) {}
}

function applyLocalPins(listings) {
  if (currentUser?.email_verified) return listings;
  const local = readLocalPins();
  return (listings || []).map((item) => {
    const pin = local[item.id];
    if (!pin) return item;
    return { ...item, favorite: Boolean(pin.favorite), notes: pin.notes || "", contacted: Boolean(pin.contacted) };
  });
}

function showDetail(item) {
  const pane = $("detailPane");
  if (!pane) return;
  pane.classList.remove("is-empty");
  const kind = typeLabel(item);
  const chips = listingTags(item).map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join("");
  const reasons = (item.deal_reasons || []).map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join("");
  const fixes = (item.data_fixes || []).map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join("");
  const locLine = locationLine(item);
  const cityName = (window.lastCities || []).find((c) => c.id === item.city)?.label
    || (item.city || "").replace(/-/g, " ");
  const published = item.published_at
    ? new Date(item.published_at).toLocaleString("es-AR")
    : "—";
  const facts = [
    ["Dirección", item.address || "—"],
    ["Dirección aproximada", item.approx_address && item.approx_address !== item.address ? item.approx_address : "—"],
    ["Barrio", item.barrio || "—"],
    ["Zona", item.zona || "—"],
    ["Ciudad", cityName],
    ["Tipo", kind],
    ["Precio", money(item)],
    ["Cubiertos", item.covered_m2 ? `${fmt(item.covered_m2)} m²` : "—"],
    ["Terreno / lote", (item.lot_m2 || item.total_m2) ? `${fmt(item.lot_m2 || item.total_m2)} m²` : "—"],
    ["Ambientes", item.rooms ?? "—"],
    ["Dormitorios", item.bedrooms ?? "—"],
    ["Baños", item.bathrooms ?? "—"],
    ["Cochera", item.parking ? "Sí" : "—"],
    ["Antigüedad", item.age_years != null ? `${item.age_years} años` : "—"],
    ["Expensas", item.expenses ? `USD ${fmt(item.expenses)}` : "—"],
    ["USD/m²", item.price_m2 ? `USD ${fmt(item.price_m2)}` : "—"],
    ["Score ganga", item.deal_score != null ? fmt(item.deal_score) : "—"],
    ["Calidad", item.quality_score != null ? `${fmt(item.quality_score)} · ${item.quality_label || ""}` : "—"],
    ["Ubicación", locLine],
    ["Intersección", item.intersection || "—"],
    ["Entre calles", item.between || "—"],
    ["Punto en el mapa", locationKindLabel(item)],
    ["Apto crédito", item.mortgage_credit === true ? "Sí" : item.mortgage_credit === false ? "No dice" : "—"],
    ["Piso", item.floor ?? "—"],
    ["Orientación", item.orientation || "—"],
    ["Estado", item.condition || "—"],
    ["Publicado", published],
    ["Inmobiliaria", item.publisher || "—"],
    ["Fuente", sourceSummary(item)],
  ].map(([k, v]) => `<div class="fact"><span>${escapeHtml(String(k))}</span><b>${escapeHtml(String(v))}</b></div>`).join("");
  const canEdit = Boolean(item.contacted);
  pane.innerHTML = `
    <button type="button" class="ghost detail-close" id="closeDetail">cerrar</button>
    ${item.image ? `<img class="detail-hero" src="${item.image}" alt="" />` : ""}
    <div class="detail-head">
      <span class="kind ${cssId(item.property_type)}">${escapeHtml(kind)}</span>
      ${item.mortgage_credit === true ? `<span class="kind credit">Apto crédito</span>` : ""}
      <strong>${money(item)}</strong>
    </div>
    <h3>${escapeHtml(kind)} · ${escapeHtml(item.title)}</h3>
    <p class="meta">${escapeHtml(locLine)} · ${escapeHtml(sourceSummary(item))}</p>
    ${sourceLinkHtml(item, "detail-link") || "<p class='muted'>Sin link al aviso original</p>"}
    ${rentEstimate(item)}
    <div class="facts">${facts}</div>
    <div class="price-track" id="priceTrack"><p class="muted">Cargando historial de precio…</p></div>
    <p>${escapeHtml(item.deal_label || "")}${item.vs_barrio_pct != null ? ` · ${item.vs_barrio_pct > 0 ? "-" : "+"}${Math.abs(item.vs_barrio_pct)}% vs barrio` : ""}</p>
    ${reasons ? `<div class="chips">${reasons}</div>` : ""}
    ${fixes ? `<div class="chips">${fixes}</div>` : ""}
    ${chips ? `<div class="chips">${chips}</div>` : ""}
    <h4>Descripción</h4>
    <p class="desc">${escapeHtml(item.description || "Sin descripción todavía. Tocá buscar avisos para leer la ficha completa.")}</p>
    <form class="edit-form${canEdit ? "" : " is-locked"}" id="editForm">
      <label class="check">
        <input type="checkbox" id="contactedChk" ${item.contacted ? "checked" : ""} />
        Contacté este aviso (permite editar datos)
      </label>
      <p class="muted edit-hint">${canEdit ? "Podés corregir precio, m² o dirección. El scrape no pisa tus cambios." : "Marcá “contacté” para poder editar precio, m² y dirección."}</p>
      <div class="edit-fields">
        <label>Precio USD <input id="editPrice" type="number" step="100" value="${item.price_usd || item.price || ""}" /></label>
        <div class="row">
          <label>m² cubiertos <input id="editCovered" type="number" value="${item.covered_m2 || ""}" /></label>
          <label>m² terreno <input id="editLot" type="number" value="${item.lot_m2 || item.total_m2 || ""}" /></label>
        </div>
        <div class="row">
          <label>Dormitorios <input id="editBeds" type="number" value="${item.bedrooms ?? ""}" /></label>
          <label>Baños <input id="editBaths" type="number" step="0.5" value="${item.bathrooms ?? ""}" /></label>
        </div>
        <label>Dirección <input id="editAddress" value="${escapeHtml(item.address || "")}" /></label>
        <label>Notas <textarea id="editNotes" rows="2">${escapeHtml(item.notes || "")}</textarea></label>
        <button class="primary" type="button" id="saveEdits">Guardar cambios</button>
      </div>
    </form>
  `;
  $("closeDetail")?.addEventListener("click", closeDetail);
  $("contactedChk")?.addEventListener("change", async (ev) => {
    await saveListing(item.id, { contacted: ev.target.checked });
  });
  $("saveEdits")?.addEventListener("click", async () => {
    const payload = {
      contacted: true,
      currency: "USD",
      address: $("editAddress")?.value || "",
      notes: $("editNotes")?.value || "",
    };
    const price = numOrNull("editPrice");
    const covered = numOrNull("editCovered");
    const lot = numOrNull("editLot");
    const beds = numOrNull("editBeds");
    const baths = numOrNull("editBaths");
    if (price != null) payload.price = price;
    if (covered != null) payload.covered_m2 = covered;
    if (lot != null) payload.total_m2 = lot;
    if (beds != null) payload.bedrooms = beds;
    if (baths != null) payload.bathrooms = baths;
    await saveListing(item.id, payload);
  });
  bindPinControls();
  fillPriceTrack(item);
}

function numOrNull(id) {
  const raw = $(id)?.value;
  if (raw === "" || raw == null) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

async function saveListing(id, payload) {
  if (!currentUser) {
    openAuth("login");
    flashAuth("Entrá con tu cuenta para editar un aviso.");
    return;
  }
  if (!currentUser.email_verified) {
    openSettings();
    flashAuth("Validá tu mail para editar avisos.");
    return;
  }
  const res = await fetch("/api/listing", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, ...payload }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    flashAuth(typeof data.detail === "string" ? data.detail : "No se pudo guardar.");
    return;
  }
  const idx = allListings.findIndex((x) => x.id === id);
  if (data.listing && idx >= 0) allListings[idx] = { ...allListings[idx], ...data.listing };
  else if (idx >= 0) {
    if (payload.notes != null) allListings[idx].notes = payload.notes;
    if (payload.contacted != null) allListings[idx].contacted = payload.contacted;
  }
  selectedId = id;
  render();
  const current = allListings.find((x) => x.id === id);
  if (current) showDetail(current);
}

function renderStats(stats) {
  const city = currentCity();
  const rows = (stats.by_barrio || []).filter((r) => r.count && (!r.city || r.city === city));
  $("statsBody").innerHTML = rows.map((r) => `<tr>
    <td>${escapeHtml(r.name)}</td>
    <td>${r.count}</td>
    <td>${r.median_usd ? fmt(r.median_usd) : "—"}</td>
    <td>${r.median_m2 ? fmt(r.median_m2) : "—"}</td>
    <td>${r.deals}</td>
  </tr>`).join("");
}

async function loadMarket() {
  const city = currentCity();
  const type = $("typeFilter")?.value || "";
  let data;
  try {
    data = await (await fetch(`/api/market?city=${encodeURIComponent(city)}&type=${encodeURIComponent(type)}`)).json();
  } catch {
    return;
  }
  renderMarket(data);
}

function sparkline(values, { upIsBad = true } = {}) {
  const nums = (values || []).map(Number).filter((n) => Number.isFinite(n) && n > 0);
  if (!nums.length) return `<p class="muted">Sin serie todavía. Cada búsqueda suma un punto al gráfico.</p>`;
  const w = 280;
  const h = 84;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const pts = nums.map((v, i) => {
    const x = 8 + (i / Math.max(nums.length - 1, 1)) * (w - 16);
    const y = h - 18 - ((v - min) / span) * (h - 28);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const last = nums[nums.length - 1];
  const first = nums[0];
  const rising = last > first * 1.008;
  const color = rising ? (upIsBad ? "#c45c3a" : "#1f7a4a") : (upIsBad ? "#1f7a4a" : "#c45c3a");
  const lastPt = pts[pts.length - 1].split(",");
  const firstLabel = values.length ? "" : "";
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Evolución del USD por m²">
    <polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round" />
    <circle cx="${lastPt[0]}" cy="${lastPt[1]}" r="3.2" fill="${color}" />
    <text class="axis" x="8" y="${h - 4}">USD ${fmt(min)}/m²</text>
    <text class="axis" x="${w - 8}" y="${h - 4}" text-anchor="end">USD ${fmt(max)}/m²</text>
  </svg>${firstLabel}`;
}

function marketRow(item, extra) {
  const strong = item.strong ? " is-strong" : "";
  const fresh = item.fresh ? `<em class="fresh">nueva</em>` : "";
  return `<button type="button" class="market-row${strong}" data-id="${escapeHtml(item.id)}">
    <b>${escapeHtml(typeLabel(item))} · ${money(item)}${fresh}</b>
    <span>${escapeHtml(extra)}</span>
  </button>`;
}

function renderMarket(data) {
  if (!data || !$("marketPulse")) return;
  $("marketHeadline").textContent = data.headline || "";
  const latest = data.latest || {};
  const trend = data.trend || "new";
  const delta = data.delta_pct;
  const trendCls = trend === "up" ? " is-up" : trend === "down" ? " is-down" : "";
  const trendText = delta == null
    ? "sin curva aún"
    : `${delta > 0 ? "+" : ""}${String(delta).replace(".", ",")}%`;
  if ($("marketReadout")) {
    $("marketReadout").innerHTML = `
      <div class="market-stat"><span>USD/m² ahora</span><b>${latest.median_m2 ? fmt(latest.median_m2) : "—"}</b></div>
      <div class="market-stat${trendCls}"><span>Suba / baja</span><b>${trendText}</b></div>
      <div class="market-stat"><span>Oportunidades</span><b>${(data.mix && data.mix.oportunidad) || 0}</b></div>
    `;
  }
  const y = data.yields || {};
  if ($("marketRent")) {
    $("marketRent").innerHTML = `
      <div class="market-stat"><span>Con contrato</span><b>${y.median_monthly_yield ? pct(y.median_monthly_yield) : "—"}</b><small>${y.median_monthly_rent ? `se alquila ~USD ${fmt(y.median_monthly_rent)}/mes` : "sin comps"}</small></div>
      <div class="market-stat"><span>Temporal</span><b>${y.median_temporal_yield ? pct(y.median_temporal_yield) : "—"}</b><small>${y.median_nightly ? `~USD ${fmt(y.median_nightly)}/noche · occ. ${y.occupancy_pct || "—"}%` : "sin comps"}</small></div>
      <p class="muted">${y.rent_comps_monthly || 0} alquileres y ${y.rent_comps_nightly || 0} temporales de la zona</p>
    `;
  }
  const types = data.by_type || [];
  const maxM2 = Math.max(...types.map((row) => row.median_m2 || 0), 1);
  if ($("marketBars")) {
    $("marketBars").innerHTML = types.map((row) => `
      <div class="mbar">
        <span>${escapeHtml(row.label)}</span>
        <i style="width:${Math.max(8, ((row.median_m2 || 0) / maxM2) * 100)}%"></i>
        <b>USD ${fmt(row.median_m2 || 0)}/m²</b>
      </div>
    `).join("");
  }
  const mix = data.mix || {};
  const mixTotal = Object.values(mix).reduce((sum, n) => sum + n, 0) || 1;
  if ($("marketMix")) {
    $("marketMix").innerHTML = ["oportunidad", "bueno", "mercado", "caro"].map((key) =>
      `<i class="${key}" style="width:${((mix[key] || 0) / mixTotal) * 100}%" title="${key}"></i>`
    ).join("");
  }
  $("marketTypes").innerHTML = types.map((row) => {
    const cls = row.trend === "up" ? " is-up" : row.trend === "down" ? " is-down" : "";
    const d = row.delta_pct == null ? "hoy" : `${row.delta_pct > 0 ? "+" : ""}${String(row.delta_pct).replace(".", ",")}%`;
    return `<span class="market-type${cls}">${escapeHtml(row.label)} · ${d}</span>`;
  }).join("");
  const drops = data.drops || [];
  $("marketDrops").innerHTML = drops.length
    ? drops.map((item) => marketRow(item, `${item.change_pct}% · era USD ${fmt(item.old_usd)} · ${item.barrio || ""}`)).join("")
    : `<p class="muted">Cuando un aviso baje de precio, aparece acá.</p>`;
  const deals = data.new_deals || [];
  $("marketDeals").innerHTML = deals.length
    ? deals.map((item) => marketRow(item, `${item.vs_barrio_pct != null ? `-${Math.abs(item.vs_barrio_pct)}% vs zona` : item.deal_label} · ${item.barrio || ""}`)).join("")
    : `<p class="muted">No hay gangas marcadas en esta vista.</p>`;
  document.querySelectorAll(".market-row").forEach((btn) => {
    btn.onclick = () => {
      const item = allListings.find((x) => x.id === btn.dataset.id);
      if (item) selectListing(item);
    };
  });
}

async function fillPriceTrack(item) {
  const box = $("priceTrack");
  if (!box) return;
  let data;
  try {
    data = await (await fetch(`/api/listing-history?id=${encodeURIComponent(item.id)}`)).json();
  } catch {
    return;
  }
  const points = data.points || [];
  const chart = sparkline(points.map((p) => p.price_usd), { upIsBad: true });
  const rows = points.map((p) => {
    const when = p.seen_at ? new Date(p.seen_at).toLocaleString("es-AR", { day: "numeric", month: "short" }) : "";
    return `<li>${when} · USD ${fmt(p.price_usd || 0)}${p.price_m2 ? ` · USD ${fmt(p.price_m2)}/m²` : ""}</li>`;
  }).join("");
  const change = data.change_pct == null
    ? "Un solo registro por ahora."
    : `Variación desde el primer dato: ${data.change_pct > 0 ? "+" : ""}${String(data.change_pct).replace(".", ",")}%.`;
  box.innerHTML = `
    <h4>Seguimiento de precio</h4>
    <p class="muted">${change}</p>
    <div class="market-chart">${chart}</div>
    ${rows ? `<ol>${rows}</ol>` : ""}
  `;
}

function money(item) {
  const usd = item.price_usd || ((item.currency || "USD").toUpperCase() === "USD" ? item.price : null);
  if (!usd) return "Consultar";
  return `USD ${fmt(usd)}`;
}

function fmt(n) {
  return Number(n).toLocaleString("es-AR", { maximumFractionDigits: 0 });
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function cssId(value) {
  return String(value || "").replace(/[^a-z0-9]+/gi, "-");
}

["typeFilter", "zonaFilter", "barrioFilter", "maxPrice", "favOnly", "dealBar", "sortBy"].forEach((id) => {
  if (!$(id)) return;
  $(id).addEventListener("input", () => {
    render();
  });
  $(id).addEventListener("change", () => {
    render();
    if (id === "typeFilter") loadMarket();
  });
});

let ignoreCityChange = false;

$("cityFilter")?.addEventListener("change", () => {
  if (ignoreCityChange) return;
  lastListingsFp = "";
  allListings = [];
  listingsRev = 0;
  localStorage.setItem("propmap.city", currentCity());
  focusedCity = "";
  ensureCityView(currentCity());
  if ($("placeQuery")) {
    const opt = $("cityFilter").selectedOptions[0];
    $("placeQuery").value = opt ? opt.textContent : "";
  }
  showListingsWait("city");
  load({ waitKind: "city" });
});

$("dealBar")?.addEventListener("input", () => {
  const bar = $("dealBar");
  const v = Number(bar?.value || 0);
  if (!bar) return;
  bar.title = v <= 0 ? "Sin filtro de ganga" : `Score ≥ ${v}`;
});
document.addEventListener("click", (ev) => {
  document.querySelectorAll(".deal-help[open]").forEach((el) => {
    if (!el.contains(ev.target)) el.removeAttribute("open");
  });
});

const SEARCH_PW_KEY = "propmap_search_pw";

function askSearchPassword() {
  const saved = sessionStorage.getItem(SEARCH_PW_KEY);
  if (saved) return Promise.resolve(saved);
  return new Promise((resolve, reject) => {
    const dlg = $("searchAuthModal");
    const input = $("searchAuthPin");
    const err = $("searchAuthError");
    if (!dlg || !input) {
      const typed = window.prompt("Contraseña para buscar avisos");
      if (typed) resolve(typed);
      else reject(new Error("cancel"));
      return;
    }
    if (err) err.hidden = true;
    input.value = "";
    const finish = (ok, value) => {
      $("searchAuthCancel").onclick = null;
      $("searchAuthForm").onsubmit = null;
      dlg.close();
      if (ok) resolve(value);
      else reject(new Error("cancel"));
    };
    $("searchAuthCancel").onclick = () => finish(false);
    $("searchAuthForm").onsubmit = (ev) => {
      ev.preventDefault();
      const pw = input.value.trim();
      if (!pw) return;
      finish(true, pw);
    };
    dlg.showModal();
    input.focus();
  });
}

$("refreshBtn").onclick = async () => {
  let password;
  try {
    password = await askSearchPassword();
  } catch {
    return;
  }
  const query = ($("placeQuery")?.value || "").trim();
  const city = currentCity();
  $("refreshBtn").disabled = true;
  if ($("pauseBtn")) $("pauseBtn").disabled = false;
  hideSuggest();
  const body = query ? { query, city, password } : { city, password };
  const res = await fetch("/api/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    sessionStorage.removeItem(SEARCH_PW_KEY);
    $("refreshBtn").disabled = false;
    if ($("pauseBtn")) $("pauseBtn").disabled = true;
    const err = $("searchAuthError");
    if (err) err.hidden = false;
    $("statusLine").textContent = "Contraseña incorrecta. No se buscó nada.";
    return;
  }
  sessionStorage.setItem(SEARCH_PW_KEY, password);
  if (data.place) rememberPlace(data.place);
  const label = data.place?.label || data.city || "ese lugar";
  $("statusLine").textContent = data.message
    ? `${scrubSearchText(data.message)} Podés cambiar de localidad, filtros y mapa.`
    : `Búsqueda en segundo plano en ${label}. Podés cambiar de localidad, filtros y mapa.`;
  pollStatus(true);
};

$("pauseBtn")?.addEventListener("click", async () => {
  $("pauseBtn").disabled = true;
  let password = sessionStorage.getItem(SEARCH_PW_KEY);
  try {
    password = password || await askSearchPassword();
  } catch {
    return;
  }
  await fetch("/api/pause", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  pollStatus(true);
});

function fastJobIds(s) {
  const jobs = s.jobs || {};
  return (s.running_cities || []).filter((id) => (jobs[id] || {}).mode === "fast");
}

function backgroundStatus(s) {
  const ids = s.running_cities || [];
  if (!ids.length) return "";
  const jobs = s.jobs || {};
  const msg = scrubSearchText((jobs[ids[0]] || {}).message || s.message || "Buscando avisos en segundo plano.");
  const extra = ids.length > 1 ? ` (${ids.length} lugares a la vez)` : "";
  const queued = (s.queue || []).filter((row) => row.due && !row.running).slice(0, 2).map((row) => row.label);
  const next = queued.length ? ` Después: ${queued.join(" · ")}.` : "";
  return `${msg}${extra}.${next}`;
}

function scrubSearchText(text) {
  const raw = String(text || "");
  if (/zona\s*prop|argenprop|properati|mercado\s*libre|mercadolibre/i.test(raw)) {
    return "Buscando avisos…";
  }
  if (/ficha|llm/i.test(raw)) {
    return "Buscando avisos…";
  }
  return raw;
}

function placeIsQueued() {
  const city = currentCity();
  return (lastStatus.queue || []).some((row) => row.id === city && (row.due || row.running));
}

function updateSearchingBanner() {
  const box = $("searchingBanner");
  if (!box) return;
  const show = !allListings.length && (scrapeRunning || placeIsQueued());
  box.hidden = !show;
  const label = $("cityFilter")?.selectedOptions?.[0]?.textContent || "este lugar";
  if (!show) return;
  box.textContent = `Buscando propiedades en ${label}… van a ir apareciendo acá.`;
}

function updatePipelineLine(s) {
  const line = $("pipelineLine");
  if (!line) return;
  if (!isAdmin) {
    line.hidden = true;
    return;
  }
  const details = s.details || {};
  const llm = s.llm || {};
  const fichas = Number(details.pending || 0);
  const llmPending = Number(llm.pending || 0);
  const bits = [];
  if (details.downloading || fichas) {
    bits.push(fichas ? `fichas ${fichas} en cola` : "bajando fichas");
  }
  if (llm.cleaning || llmPending) {
    bits.push(llmPending ? `LLM ${llmPending} en cola` : "LLM limpiando");
  }
  if (!bits.length) {
    line.hidden = true;
    line.textContent = "";
    return;
  }
  line.hidden = false;
  line.textContent = `Interno · ${bits.join(" · ")}. Mejora datos en segundo plano; no hace falta para ver el mapa.`;
}

function readCookie(name) {
  const match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return match ? decodeURIComponent(match[1]) : "";
}

function visitorId() {
  try {
    let id = readCookie("propmap_vid") || localStorage.getItem("propmap.vid");
    if (!id) id = (crypto.randomUUID && crypto.randomUUID()) || `v${Date.now()}`;
    localStorage.setItem("propmap.vid", id);
    document.cookie = `propmap_vid=${encodeURIComponent(id)};path=/;max-age=31536000;SameSite=Lax`;
    return id;
  } catch {
    return readCookie("propmap_vid") || "";
  }
}

function track(n, extra) {
  const params = new URLSearchParams(location.search);
  const body = {
    n,
    p: location.pathname,
    r: document.referrer,
    q: location.search.replace(/^\?/, ""),
    utm: params.get("utm_source") || params.get("ref") || "",
    city: typeof currentCity === "function" ? currentCity() : "",
    vid: visitorId(),
    ...(extra || {}),
  };
  try {
    navigator.sendBeacon("/api/t", new Blob([JSON.stringify(body)], { type: "application/json" }));
  } catch {
    fetch("/api/t", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      keepalive: true,
    }).catch(() => {});
  }
}

function setTab(tab, push) {
  const allowed = ["map", "list", "detail", "market"];
  const next = allowed.includes(tab) ? tab : "map";
  document.body.classList.remove("tab-map", "tab-list", "tab-detail", "tab-market");
  document.body.classList.add(`tab-${next}`);
  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.setAttribute("aria-selected", String(btn.dataset.tab === next));
  });
  if (push !== false) {
    const url = new URL(location.href);
    url.searchParams.set("tab", next);
    history.replaceState(null, "", url);
  }
  if (next === "map") setTimeout(() => map.invalidateSize(), 60);
}

function armPoll() {
  clearInterval(pollTimer);
  const hurry = scrapeRunning || !allListings.length;
  pollTimer = setInterval(() => pollStatus(), hurry ? 1200 : 2800);
}

async function pollStatus(force) {
  let s;
  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(String(res.status));
    s = await res.json();
  } catch {
    armPoll();
    return;
  }
  lastStatus = s;
  const city = currentCity();
  const fastJobs = fastJobIds(s);
  const running = s.running_cities || [];
  const viewingThis = running.includes(city);
  const revChanged = (s.listings_rev || 0) !== listingsRev;
  const scrapeWas = scrapeRunning;
  scrapeRunning = viewingThis || placeIsQueued();
  if (listingsReady && scrapeWas !== scrapeRunning) render();
  if ($("refreshBtn")) $("refreshBtn").disabled = fastJobs.includes(city);
  if ($("pauseBtn")) $("pauseBtn").disabled = !fastJobs.length;
  updateSearchingBanner();
  updatePipelineLine(s);
  if (running.length) {
    $("statusLine").textContent = backgroundStatus(s);
    if (viewingThis || revChanged) {
      await load({ keepStatus: true, live: true });
    }
  } else if (revChanged) {
    await load({ keepStatus: true, live: true });
  } else if (cityWasRunning[city] || force) {
    await load();
  } else if (s.last_run) {
    $("statusLine").textContent = s.message
      ? `Última búsqueda: ${new Date(s.last_run).toLocaleString("es-AR")} · ${scrubSearchText(s.message)}`
      : `Última búsqueda: ${new Date(s.last_run).toLocaleString("es-AR")}`;
  } else if (s.message) {
    $("statusLine").textContent = scrubSearchText(s.message);
  }
  cityWasRunning[city] = viewingThis;
  wasRunning = viewingThis;
  armPoll();
}

$("manualBtn").onclick = () => {
  const city = $("manualForm")?.querySelector("[name=city]");
  if (city) city.value = currentCity();
  $("manualModal").showModal();
};
$("closeManual").onclick = () => $("manualModal").close();
$("saveManual").onclick = async (ev) => {
  ev.preventDefault();
  const form = $("manualForm");
  const payload = Object.fromEntries(new FormData(form).entries());
  ["price", "covered_m2", "total_m2", "bedrooms", "bathrooms"].forEach((k) => {
    if (payload[k] === "") delete payload[k];
    else if (payload[k]) payload[k] = Number(payload[k]);
  });
  let password = sessionStorage.getItem(SEARCH_PW_KEY);
  try {
    password = password || await askSearchPassword();
  } catch {
    return;
  }
  payload.password = password;
  await fetch("/api/manual", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  $("manualModal").close();
  form.reset();
  await load();
};

function renderFacebook() {
  $("fbLinks").innerHTML = (facebook || []).map((x) => `<a href="${x.url}" target="_blank" rel="noopener">${x.label}</a>`).join(" · ");
}

$("toggleStats").onclick = () => {
  $("statsCard").classList.toggle("collapsed");
};

$("togglePulse")?.addEventListener("click", () => {
  const box = $("marketPulse");
  if (!box) return;
  box.classList.toggle("collapsed");
  $("togglePulse").textContent = box.classList.contains("collapsed") ? "mostrar" : "ocultar";
});

$("toggleLegend")?.addEventListener("click", () => {
  const box = $("mapLegend");
  if (!box) return;
  box.classList.toggle("collapsed");
  $("toggleLegend").textContent = box.classList.contains("collapsed") ? "mostrar" : "ocultar";
});

function viewBounds(view) {
  if (!view) return null;
  if (view.bbox && view.bbox.length === 4) {
    const [s, w, n, e] = view.bbox;
    return [[s, w], [n, e]];
  }
  if (view.lat == null || view.lon == null) return null;
  const km = Math.max(view.radiusKm || 12, 6);
  const dlat = km / 111;
  const dlon = km / (111 * Math.max(0.2, Math.cos((view.lat * Math.PI) / 180)));
  return [[view.lat - dlat, view.lon - dlon], [view.lat + dlat, view.lon + dlon]];
}

function focusCity(cityId) {
  const view = CITY_VIEWS[cityId];
  if (!view) return;
  const bounds = viewBounds(view);
  if (bounds) {
    map.fitBounds(bounds, { padding: [28, 28], maxZoom: 13, animate: true });
    return;
  }
  map.setView([view.lat, view.lon], view.zoom || 13, { animate: true });
}

function ensureCityView(cityId) {
  if (!cityId || focusedCity === cityId) return;
  focusedCity = cityId;
  focusCity(cityId);
}

function rememberPlace(place) {
  if (!place || !place.id) return;
  rememberCityView(place);
  const select = $("cityFilter");
  if (select && ![...select.options].some((o) => o.value === place.id)) {
    const opt = document.createElement("option");
    opt.value = place.id;
    opt.textContent = place.label || place.id;
    select.appendChild(opt);
  }
  window.placeBarrios = window.placeBarrios || {};
  if (place.barrios && place.barrios.length) {
    window.placeBarrios[place.id] = place.barrios.map((b) => b.name);
  }
}

function applyPlace(place) {
  if (!place || !place.id) return;
  rememberPlace(place);
  const select = $("cityFilter");
  if (select) {
    ignoreCityChange = true;
    select.value = place.id;
    ignoreCityChange = false;
  }
  localStorage.setItem("propmap.city", place.id);
  focusedCity = "";
  if ($("placeQuery")) $("placeQuery").value = place.label || "";
  focusCity(place.id);
  fillZonas(cityItems());
  fillBarrios();
  if (listingsReady) render();
  renderStats(window.lastStats || {});
}

let placeTimer = null;
let placeHits = [];
let placeActive = -1;

function hideSuggest() {
  const box = $("placeSuggest");
  if (box) {
    box.hidden = true;
    box.innerHTML = "";
  }
  placeHits = [];
  placeActive = -1;
}

function showSuggest(places) {
  const box = $("placeSuggest");
  if (!box) return;
  placeHits = places || [];
  placeActive = placeHits.length ? 0 : -1;
  if (!placeHits.length) {
    hideSuggest();
    return;
  }
  box.hidden = false;
  box.innerHTML = placeHits.map((p, i) => `
    <button type="button" data-idx="${i}" class="${i === placeActive ? "is-active" : ""}">
      ${escapeHtml(p.label || p.id)}
      <small>${escapeHtml(p.hint || p.province || "")}</small>
    </button>
  `).join("");
  box.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => choosePlace(placeHits[Number(btn.dataset.idx)]);
  });
}

async function choosePlace(place) {
  hideSuggest();
  applyPlace(place);
  lastListingsFp = "";
  allListings = [];
  listingsRev = 0;
  track("place", { city: place.id });
  if (window.innerWidth <= 980) setTab("map");
  showListingsWait("scrape");
  const loading = load({ waitKind: "city" });
  fetch("/api/place", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query: place.label || place.id,
      city: place.id,
      label: place.label,
      lat: place.lat,
      lon: place.lon,
      province: place.province,
    }),
  })
    .then((res) => res.json())
    .then((data) => {
      if (data && data.city) applyPlace(data.city);
    })
    .catch(() => {});
  await loading;
}

$("placeQuery")?.addEventListener("input", () => {
  const q = ($("placeQuery").value || "").trim();
  clearTimeout(placeTimer);
  if (q.length < 2) {
    hideSuggest();
    return;
  }
  placeTimer = setTimeout(async () => {
    const data = await (await fetch(`/api/places?q=${encodeURIComponent(q)}`)).json();
    if (($("placeQuery").value || "").trim() !== q) return;
    showSuggest(data.places || []);
  }, 280);
});

$("placeQuery")?.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") {
    hideSuggest();
    return;
  }
  if (ev.key === "ArrowDown" && placeHits.length) {
    ev.preventDefault();
    placeActive = (placeActive + 1) % placeHits.length;
    showSuggest(placeHits);
  }
  if (ev.key === "ArrowUp" && placeHits.length) {
    ev.preventDefault();
    placeActive = (placeActive - 1 + placeHits.length) % placeHits.length;
    showSuggest(placeHits);
  }
  if (ev.key === "Enter" && placeHits.length && placeActive >= 0) {
    ev.preventDefault();
    choosePlace(placeHits[placeActive]);
  }
});

document.addEventListener("click", (ev) => {
  const wrap = $("placeQuery")?.parentElement;
  if (wrap && !wrap.contains(ev.target)) hideSuggest();
});

const WELCOME_KEY = "propmap-welcome-v1";

function openHelp() {
  $("helpModal")?.showModal();
}

function dismissWelcome() {
  try { localStorage.setItem(WELCOME_KEY, "1"); } catch (_) {}
  const modal = $("welcomeModal");
  if (modal?.open) modal.close();
}

$("helpBtn")?.addEventListener("click", openHelp);
$("welcomeStart")?.addEventListener("click", dismissWelcome);
$("welcomeModal")?.addEventListener("close", () => {
  try { localStorage.setItem(WELCOME_KEY, "1"); } catch (_) {}
});

try {
  if (!localStorage.getItem(WELCOME_KEY)) {
    localStorage.setItem(WELCOME_KEY, "1");
  }
} catch (_) {}

document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
  btn.addEventListener("click", () => setTab(btn.dataset.tab));
});
const startTab = new URL(location.href).searchParams.get("tab");
if (startTab) setTab(startTab, false);

async function loadAnalytics() {
  if (!isAdmin || !$("analyticsBox")) return;
  let password = sessionStorage.getItem(SEARCH_PW_KEY);
  try {
    password = password || await askSearchPassword();
  } catch {
    return;
  }
  sessionStorage.setItem(SEARCH_PW_KEY, password);
  const res = await fetch("/api/admin/stats", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password, days: 14 }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return;
  $("analyticsBox").hidden = false;
  const days = (data.days || []).map((row) => `${row.day.slice(8)}:${row.visitors}`).join(" · ");
  const places = (data.places || []).slice(0, 8).map((row) => `<div class="stat-row"><span>${row.name}</span><b>${row.count}</b></div>`).join("");
  const refs = (data.referrers || []).slice(0, 8).map((row) => `<div class="stat-row"><span>${labelSource(row.name)}</span><b>${row.count}</b></div>`).join("");
  const devices = (data.devices || []).map((row) => `<div class="stat-row"><span>${row.name}</span><b>${row.count}</b></div>`).join("");
  $("analyticsReadout").innerHTML = `
    <div class="kpis">
      <div class="kpi"><b>${data.visitors || 0}</b><span>visitantes</span></div>
      <div class="kpi"><b>${data.pageviews || 0}</b><span>visitas</span></div>
    </div>
    <p class="muted">${days || "todavía no hay días"} · ${data.events || 0} eventos en 14 días</p>
    <h3>De dónde entran</h3>${refs || "<p class='muted'>Todavía no hay orígenes.</p>"}
    <h3>Lugares</h3>${places || "<p class='muted'>Nadie eligió un lugar todavía.</p>"}
    <h3>Dispositivo</h3>${devices || "<p class='muted'>—</p>"}
  `;
}

function labelSource(name) {
  const key = String(name || "").toLowerCase();
  if (!key || key === "directo") return "Directo / sin origen";
  if (key.includes("google")) return "Google";
  if (key.includes("instagram") || key === "ig") return "Instagram";
  if (key.includes("facebook") || key === "fb") return "Facebook";
  if (key.includes("whatsapp") || key === "wa") return "WhatsApp";
  if (key.includes("t.co") || key.includes("twitter") || key.includes("x.com")) return "X / Twitter";
  return name;
}
$("reloadStats")?.addEventListener("click", () => loadAnalytics());
if (isAdmin) setTimeout(loadAnalytics, 400);

function apiDetail(data, fallback) {
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg;
  return fallback;
}

function flashAuth(text) {
  const err = $("authError");
  const set = $("settingsMsg");
  if (err && $("authModal")?.open) {
    err.hidden = false;
    err.textContent = text;
  }
  if (set) {
    set.hidden = false;
    set.textContent = text;
  }
}

function paintAccountBtn() {
  const label = currentUser ? currentUser.username : "Cuenta";
  const title = currentUser ? "Ajustes de la cuenta" : "Entrar o crear cuenta";
  ["accountBtn", "mapAccountBtn"].forEach((id) => {
    const btn = $(id);
    if (!btn) return;
    btn.classList.toggle("is-on", Boolean(currentUser));
    btn.textContent = label;
    btn.title = title;
  });
}

function fillSettings() {
  if (!currentUser) return;
  if ($("setUser")) $("setUser").textContent = currentUser.username;
  if ($("setEmail")) $("setEmail").textContent = currentUser.email_masked;
  if ($("setVerified")) {
    $("setVerified").textContent = currentUser.email_verified ? "Mail validado" : "Pendiente de validación";
  }
}

function setAuthTab(tab) {
  const register = tab === "register";
  document.querySelectorAll("[data-auth-tab]").forEach((btn) => {
    const on = btn.dataset.authTab === tab;
    btn.classList.toggle("is-on", on);
    btn.setAttribute("aria-selected", String(on));
  });
  if ($("authTitle")) $("authTitle").textContent = register ? "Crear cuenta" : "Entrar";
  if ($("authLead")) {
    $("authLead").textContent = register
      ? "Pedimos usuario, mail y contraseña. El mail y los favoritos se cifran. Hay que validar el mail."
      : "Favoritos y notas se guardan cifrados en tu cuenta. El mapa se puede usar sin registrarse.";
  }
  if ($("authEmailWrap")) $("authEmailWrap").hidden = !register;
  if ($("authTermsWrap")) $("authTermsWrap").hidden = !register;
  if ($("authEmail")) $("authEmail").required = register;
  if ($("authPass")) $("authPass").autocomplete = register ? "new-password" : "current-password";
  if ($("authSubmit")) $("authSubmit").textContent = register ? "Registrarme" : "Entrar";
  $("authForm")?.setAttribute("data-tab", tab);
}

function openAuth(tab) {
  setAuthTab(tab || "login");
  if ($("authError")) $("authError").hidden = true;
  if ($("authOk")) $("authOk").hidden = true;
  $("authModal")?.showModal();
}

function openSettings() {
  if (!currentUser) {
    openAuth("login");
    return;
  }
  fillSettings();
  if ($("settingsMsg")) $("settingsMsg").hidden = true;
  $("settingsModal")?.showModal();
}

async function hydrateAuth() {
  try {
    const res = await fetch("/api/auth/me");
    const data = await res.json();
    currentUser = data.user || null;
  } catch {
    currentUser = null;
  }
  paintAccountBtn();
  if (currentUser?.email_verified) await importLocalPins();
  const cuenta = new URL(location.href).searchParams.get("cuenta");
  if (cuenta === "ok") {
    flashAuth("Mail validado. Ya podés guardar favoritos cifrados.");
    if (currentUser) openSettings();
    else openAuth("login");
  } else if (cuenta === "error") {
    openAuth("login");
    flashAuth("Ese enlace no sirvió. Entrá y pedí otro mail de validación.");
  }
}

async function importLocalPins() {
  const pins = readLocalPins();
  if (!Object.keys(pins).length) return;
  try {
    const res = await fetch("/api/auth/import-pins", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pins }),
    });
    if (res.ok) localStorage.removeItem(LOCAL_PINS_KEY);
  } catch (_) {}
}

$("accountBtn")?.addEventListener("click", () => {
  if (currentUser) openSettings();
  else openAuth("login");
});
$("mapAccountBtn")?.addEventListener("click", () => {
  if (currentUser) openSettings();
  else openAuth("login");
});
document.querySelectorAll("[data-auth-tab]").forEach((btn) => {
  btn.addEventListener("click", () => setAuthTab(btn.dataset.authTab));
});
$("authCancel")?.addEventListener("click", () => $("authModal")?.close());
$("settingsClose")?.addEventListener("click", () => $("settingsModal")?.close());
$("authForm")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const register = $("authForm")?.getAttribute("data-tab") === "register";
  const err = $("authError");
  const ok = $("authOk");
  if (err) err.hidden = true;
  if (ok) ok.hidden = true;
  const body = {
    username: $("authUser")?.value || "",
    password: $("authPass")?.value || "",
  };
  if (register) {
    body.email = $("authEmail")?.value || "";
    body.accept_terms = Boolean($("authTerms")?.checked);
  }
  const res = await fetch(register ? "/api/auth/register" : "/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (err) {
      err.hidden = false;
      err.textContent = apiDetail(data, "No se pudo completar.");
    }
    return;
  }
  if (register) {
    if (data.user) {
      currentUser = data.user;
      paintAccountBtn();
    }
    if (ok) {
      ok.hidden = false;
      ok.textContent = data.message || "Revisá tu mail para validar la cuenta.";
    }
    if (data.verify_url && ok) {
      ok.innerHTML = `${escapeHtml(data.message || "Validá el mail.")} <a href="${escapeHtml(data.verify_url)}">Validar ahora</a>`;
    }
    return;
  }
  currentUser = data.user;
  paintAccountBtn();
  await importLocalPins();
  $("authModal")?.close();
  load({ live: false });
  if (!currentUser.email_verified) openSettings();
});
$("logoutBtn")?.addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  currentUser = null;
  paintAccountBtn();
  $("settingsModal")?.close();
  load({ live: false });
});
$("resendVerify")?.addEventListener("click", async () => {
  const res = await fetch("/api/auth/resend", { method: "POST" });
  const data = await res.json().catch(() => ({}));
  const msg = $("settingsMsg");
  if (!msg) return;
  msg.hidden = false;
  if (!res.ok) {
    msg.textContent = apiDetail(data, "No se pudo reenviar.");
    return;
  }
  msg.textContent = data.message || "Mail reenviado.";
  if (data.verify_url) {
    msg.innerHTML = `${escapeHtml(data.message || "Mail reenviado.")} <a href="${escapeHtml(data.verify_url)}">Validar ahora</a>`;
  }
});
$("deleteAccountBtn")?.addEventListener("click", async () => {
  const res = await fetch("/api/auth/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      password: $("deletePass")?.value || "",
      confirm: $("deleteConfirm")?.value || "",
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    flashAuth(apiDetail(data, "No se pudo eliminar la cuenta."));
    return;
  }
  currentUser = null;
  paintAccountBtn();
  $("settingsModal")?.close();
  load({ live: false });
});

function setFold(box, open) {
  box.classList.toggle("is-open", open);
  const btn = box.querySelector(".fold-head");
  const body = box.querySelector(".fold-body");
  if (btn) btn.setAttribute("aria-expanded", String(open));
  if (body) body.hidden = !open;
}

function bindFolds() {
  document.querySelectorAll("[data-fold]").forEach((box) => {
    const btn = box.querySelector(".fold-head");
    if (!btn) return;
    const key = `propmap.fold.${box.dataset.fold}`;
    let open = true;
    try {
      const stored = localStorage.getItem(key);
      if (stored === "0") open = false;
      if (stored === "1") open = true;
    } catch (_) {}
    setFold(box, open);
    btn.addEventListener("click", () => {
      const next = !box.classList.contains("is-open");
      setFold(box, next);
      try { localStorage.setItem(key, next ? "1" : "0"); } catch (_) {}
    });
  });
}

function applySiteMail(data) {
  const mail = (data && data.mail) || {};
  const addr = mail.address || "";
  const inbox = mail.inbox || "";
  document.querySelectorAll("[data-site-mail]").forEach((el) => {
    if (!addr) return;
    if (el.tagName === "A") {
      el.href = `mailto:${addr}`;
      el.textContent = addr;
    } else {
      el.textContent = addr;
    }
  });
  document.querySelectorAll("[data-mail-inbox]").forEach((el) => {
    if (!inbox) return;
    el.hidden = false;
    if (el.tagName === "A") el.href = inbox;
  });
}

async function loadTrackers() {
  for (let i = 0; i < 12; i += 1) {
    try {
      const res = await fetch("/api/config");
      const data = await res.json();
      applySiteMail(data);
      const matomo = data.matomo || {};
      if (matomo.app && $("matomoLink")) $("matomoLink").href = matomo.app;
      if (matomo.siteId && matomo.src && !window._paq) {
        const paq = window._paq = [];
        paq.push(["setTrackerUrl", matomo.tracker || "/matomo.php"]);
        paq.push(["setSiteId", String(matomo.siteId)]);
        paq.push(["enableHeartBeatTimer"]);
        paq.push(["trackPageView"]);
        paq.push(["enableLinkTracking"]);
        const s = document.createElement("script");
        s.async = true;
        s.src = matomo.src;
        document.head.appendChild(s);
      }
      if (matomo.siteId) return;
    } catch (_) {}
    await new Promise((resolve) => setTimeout(resolve, 2500));
  }
}

bindFolds();
track("pageview");
hydrateAuth().finally(() => {
  load();
  pollStatus();
  armPoll();
});
