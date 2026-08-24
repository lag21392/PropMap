const map = L.map("map", {
  zoomControl: true,
  dragging: true,
  scrollWheelZoom: true,
  doubleClickZoom: true,
  boxZoom: true,
  inertia: true,
}).setView([-42.7692, -65.0385], 12);

L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  attribution: "&copy; OpenStreetMap &copy; CARTO",
  subdomains: "abcd",
  maxZoom: 19,
}).addTo(map);

const barrioLayer = L.layerGroup().addTo(map);
const exactCluster = L.markerClusterGroup({
  showCoverageOnHover: false,
  maxClusterRadius: 42,
  spiderfyOnMaxZoom: false,
  zoomToBoundsOnClick: false,
  singleMarkerMode: false,
});
map.addLayer(exactCluster);
exactCluster.on("clusterclick", (ev) => {
  const items = listingsFromMarkers(ev.layer.getAllChildMarkers());
  openPickPopup(ev.layer.getLatLng(), items, "Avisos en este grupo");
});

const zoneLayer = L.layerGroup().addTo(map);
let zoneMarkersByKey = {};
let stackMarkers = [];

let allListings = [];
let allBarrios = [];
let facebook = [];
let markersById = {};
let pollTimer = null;
let wasRunning = false;
let cityWasRunning = {};
let noteTimers = {};
let selectedId = null;
let loadInFlight = false;
let lastListingsFp = "";

const colors = {
  oportunidad: "#7dffb3",
  bueno: "#b7e36d",
  mercado: "#7eb6d6",
  caro: "#ff8b6b",
  "revisar (muy barato)": "#f0c36a",
  "revisar (outlier)": "#f0c36a",
  "revisar (dato raro)": "#f0c36a",
  "revisar (señales)": "#f0c36a",
  "sin precio": "#8aa",
  "sin comparables": "#8aa",
  "loteo (fuera de mediana)": "#c4b5a0",
};

const CITY_VIEWS = {
  "puerto-madryn": { lat: -42.7692, lon: -65.0385, zoom: 12, radiusKm: 38 },
  trelew: { lat: -43.2489, lon: -65.3051, zoom: 13, radiusKm: 18 },
  rawson: { lat: -43.3002, lon: -65.1023, zoom: 13, radiusKm: 12 },
  gaiman: { lat: -43.2897, lon: -65.4927, zoom: 14, radiusKm: 12 },
  "playa-union": { lat: -43.338, lon: -65.047, zoom: 14, radiusKm: 10 },
  "microcentro-caba": { lat: -34.6037, lon: -58.3816, zoom: 15, radiusKm: 8 },
};

const TYPE_LABEL = { casa: "Casa", departamento: "Depto", ph: "PH", terreno: "Terreno" };

const MAJOR_BARRIOS = new Set([
  "Centro",
  "Zona Sur",
  "Zona Norte",
  "Punta Cuevas",
  "El Doradillo",
  "Playa Paraná",
  "Cerro Avanzado",
  "Quintas del Mirador",
  "Bahía Nueva",
  "Microcentro",
  "San Nicolás",
]);

const $ = (id) => document.getElementById(id);

async function load(opts = {}) {
  if (loadInFlight && opts.live) return;
  loadInFlight = true;
  let data;
  try {
    const q = new URLSearchParams();
    if (opts.live) q.set("live", "1");
    q.set("city", currentCity());
    data = await (await fetch("/api/listings?" + q.toString())).json();
  } finally {
    loadInFlight = false;
  }
  if (!data) return;
  const fp = `${(data.listings || []).length}:${(data.listings || []).filter((x) => x.has_exact_location).length}:${(data.listings || []).map((x) => x.id).join("|")}`;
  if (opts.live && fp === lastListingsFp) return;
  lastListingsFp = fp;
  allListings = data.listings || [];
  allBarrios = data.barrios || [];
  facebook = data.facebook || [];
  window.lastStats = data.stats || {};
  window.lastCities = data.cities || [];
  renderKpis(data.stats || {});
  fillCities(data.cities || []);
  fillZonas(cityItems());
  fillBarrios();
  renderBarrios(allBarrios);
  render();
  renderStats(data.stats || {});
  loadMarket();
  if (!opts.keepStatus) {
    $("statusLine").textContent = data.last_run
      ? `Última búsqueda: ${new Date(data.last_run).toLocaleString("es-AR")} · dólar blue ${fmt(data.usd_ars)}`
      : "Tocá “Buscar avisos” para leer los portales en segundo plano.";
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
  return $("cityFilter")?.value || "puerto-madryn";
}

function belongsToCity(item, city) {
  if ((item.city || "puerto-madryn") !== city) return false;
  const view = CITY_VIEWS[city];
  if (item.lat && item.lon && view && distM(item, view) / 1000 > (view.radiusKm || 35)) {
    return false;
  }
  const blob = `${item.title || ""} ${item.address || ""}`.toLowerCase();
  if (/puerto madryn|puerto-madryn/.test(blob)) return true;
  if (/escobar|pilar chico|nordelta|carmen de areco|garin|garín|en trelew|ruta 6|capital federal/.test(blob)) {
    return false;
  }
  return true;
}

function cityItems() {
  const city = currentCity();
  return allListings.filter((x) => belongsToCity(x, city));
}

function fillCities(cities) {
  const select = $("cityFilter");
  if (!select || !cities.length) return;
  const current = select.value;
  select.innerHTML = cities.map((c) => `<option value="${c.id}">${c.label}</option>`).join("");
  const ids = cities.map((c) => c.id);
  select.value = ids.includes(current) ? current : (ids.includes("puerto-madryn") ? "puerto-madryn" : ids[0]);
  cities.forEach((c) => {
    CITY_VIEWS[c.id] = { lat: c.lat, lon: c.lon, zoom: c.zoom || 13, radiusKm: c.radius_km || 25 };
    if (c.barrios && c.barrios.length) {
      window.placeBarrios = window.placeBarrios || {};
      window.placeBarrios[c.id] = c.barrios.map((b) => b.name);
    }
  });
  const view = CITY_VIEWS[select.value];
  if (view && $("placeQuery") && !$("placeQuery").value) {
    $("placeQuery").placeholder = `ej. ${cities.find((c) => c.id === select.value)?.label || "Playa Unión"}`;
  }
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
  const fromMap = allBarrios.filter((b) => (b.city || "puerto-madryn") === city).map((b) => b.name);
  const fromItems = cityItems().map((x) => x.barrio).filter(Boolean);
  const fromKnown = (window.placeBarrios && window.placeBarrios[city]) || [];
  const names = [...new Set([...fromMap, ...fromItems, ...fromKnown])].sort((a, b) => a.localeCompare(b, "es"));
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
  const city = item.city || "puerto-madryn";
  if (item.barrio && item.barrio !== "Sin clasificar") return `${city}|b|${item.barrio}`;
  return `${city}|z|${item.zona || "Sin zona"}`;
}

function zoneCenter(group) {
  const city = group.city || "puerto-madryn";
  const byName = allBarrios.find((b) => (b.city || "puerto-madryn") === city && b.name === group.label);
  if (byName) return [byName.lat, byName.lon];
  const byZona = allBarrios.filter((b) => (b.city || "puerto-madryn") === city && b.zona === group.label);
  if (byZona.length) {
    return [
      byZona.reduce((sum, b) => sum + b.lat, 0) / byZona.length,
      byZona.reduce((sum, b) => sum + b.lon, 0) / byZona.length,
    ];
  }
  const view = CITY_VIEWS[city] || CITY_VIEWS["puerto-madryn"];
  return [view.lat, view.lon];
}

function zoneIcon(group) {
  const selected = group.items.some((item) => item.id === selectedId);
  const deal = group.items.find((item) => item.deal_label === "oportunidad")
    || group.items.find((item) => item.deal_label === "bueno")
    || group.items[0];
  const color = colors[deal?.deal_label] || "#7eb6d6";
  return L.divIcon({
    className: `zone-pin${selected ? " is-selected" : ""}`,
    html: `<span style="border-color:${color};color:${color}"><b>${group.items.length}</b></span>`,
    iconSize: [44, 44],
    iconAnchor: [22, 22],
    popupAnchor: [0, -18],
  });
}

function zonePopupHtml(group) {
  const rows = sorted(group.items).slice(0, 10).map((item) => `
    <button type="button" class="zone-item${item.id === selectedId ? " is-selected" : ""}" data-id="${item.id}">
      <span class="kind ${cssId(item.property_type)}">${escapeHtml(typeLabel(item))}</span>
      <span>${money(item)} · ${escapeHtml(sizeBits(item).join(" · ") || "sin m²")}</span>
    </button>
  `).join("");
  const extra = group.items.length > 10 ? `<p class="muted">+${group.items.length - 10} más en la lista de la izquierda</p>` : "";
  return `<div class="popup zone-popup">
    <strong>${escapeHtml(group.label)}</strong>
    <p>${group.items.length} aviso${group.items.length === 1 ? "" : "s"} sin dirección exacta · solo la zona</p>
    ${rows}${extra}
  </div>`;
}
function pinIcon(item) {
  const color = colors[item.deal_label] || "#7eb6d6";
  const fav = item.favorite ? " fav" : "";
  return L.divIcon({
    className: `prop-pin round${fav}`,
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

function groupExactPins(items) {
  const exact = items.filter((item) => item.has_exact_location && item.lat && item.lon);
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
  const rows = shown.map((item) => `
    <button type="button" class="zone-item${item.id === selectedId ? " is-selected" : ""}" data-id="${item.id}">
      <span class="kind ${cssId(item.property_type)}">${escapeHtml(typeLabel(item))}</span>
      <span>${money(item)} · ${escapeHtml(sizeBits(item).join(" · ") || "sin m²")}</span>
    </button>
  `).join("");
  const extra = items.length > 12 ? `<p class="muted">+${items.length - 12} más en la lista de la izquierda</p>` : "";
  return `<div class="popup zone-popup">
    <strong>${escapeHtml(title)}</strong>
    <p>${items.length} avisos · elegí uno para ver la foto y los datos</p>
    ${rows}${extra}
  </div>`;
}

function listingPopupLatLng(item) {
  if (item.has_exact_location && item.lat && item.lon) {
    return L.latLng(item.lat, item.lon);
  }
  const zone = zoneMarkersByKey[zoneKey(item)];
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

function renderBarrios(barrios) {
  barrioLayer.clearLayers();
  const zoom = map.getZoom();
  barrios.forEach((b) => {
    if (!b.ring || !b.ring.length) return;
    if (($("cityFilter")?.value || "puerto-madryn") !== (b.city || "puerto-madryn")) return;
    const cityBarrios = barrios.filter((x) => (x.city || "puerto-madryn") === (b.city || "puerto-madryn"));
    const show = cityBarrios.length <= 14 || (b.count || 0) > 0 || MAJOR_BARRIOS.has(b.name);
    if (!show) return;
    const poly = L.polygon(b.ring, {
      color: "rgba(46, 196, 182, 0.55)",
      weight: 1.2,
      fillColor: "#2ec4b6",
      fillOpacity: b.count ? 0.09 : 0.03,
      interactive: true,
    });
    const m2 = b.median_m2 ? `USD ${fmt(b.median_m2)} / m²` : "sin USD/m² todavía";
    poly.bindTooltip(
      `<strong>${escapeHtml(b.name)}</strong><br>${m2}<br>${b.count || 0} avisos`,
      { sticky: true, className: "barrio-tip" }
    );
    barrioLayer.addLayer(poly);
    if ((b.count || 0) > 0 && zoom >= 12) {
      barrioLayer.addLayer(
        L.marker([b.lat, b.lon], {
          icon: L.divIcon({
            className: "barrio-label",
            html: `<div><strong>${escapeHtml(b.name)}</strong><span>${m2}</span></div>`,
            iconSize: [140, 38],
            iconAnchor: [70, 19],
          }),
          interactive: false,
          keyboard: false,
        })
      );
    }
  });
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
    if (cmp) return cmp;
    return (b.deal_score || b.quality_score || b.score || 0) - (a.deal_score || a.quality_score || a.score || 0);
  });
  return copy;
}

function render() {
  const items = sorted(filtered());
  $("listCount").textContent = `${items.length}`;
  renderKpisFromItems(items);
  $("list").innerHTML = items.map(cardHtml).join("") || `<p class="status">Nada con esos filtros.</p>`;
  exactCluster.clearLayers();
  zoneLayer.clearLayers();
  markersById = {};
  zoneMarkersByKey = {};
  stackMarkers = [];
  const zoneGroups = {};
  groupExactPins(items).forEach((group) => {
    const marker = L.marker([group.lat, group.lon], { icon: stackIcon(group), riseOnHover: true });
    marker._stackGroup = group;
    marker.on("click", () => {
      openPickPopup(L.latLng(group.lat, group.lon), group.items, group.items.length > 1 ? "Avisos en este punto" : "Aviso");
    });
    group.items.forEach((item) => {
      markersById[item.id] = marker;
    });
    stackMarkers.push({ marker, group });
    exactCluster.addLayer(marker);
  });
  items.forEach((item) => {
    if (item.has_exact_location && item.lat && item.lon) return;
    const key = zoneKey(item);
    if (!zoneGroups[key]) {
      zoneGroups[key] = {
        key,
        city: item.city || "puerto-madryn",
        label: (item.barrio && item.barrio !== "Sin clasificar") ? item.barrio : (item.zona || "Zona sin clasificar"),
        items: [],
      };
    }
    zoneGroups[key].items.push(item);
  });
  Object.values(zoneGroups).forEach((group) => {
    const [lat, lon] = zoneCenter(group);
    const marker = L.marker([lat, lon], { icon: zoneIcon(group), riseOnHover: true, zIndexOffset: 200 });
    marker.on("click", () => {
      openPickPopup(L.latLng(lat, lon), group.items, group.label);
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
  selectedId = item.id;
  highlightSelected();
  refreshZoneIcons();
  showDetail(item);
  document.getElementById(`card-${cssId(item.id)}`)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  openListingPopup(item, at);
  if (focusMap) focusListing(item);
}

function refreshZoneIcons() {
  Object.values(zoneMarkersByKey).forEach(({ marker, group }) => {
    marker.setIcon(zoneIcon(group));
  });
  stackMarkers.forEach(({ marker, group }) => {
    marker.setIcon(stackIcon(group));
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
  const pane = $("detailPane");
  if (!pane) return;
  pane.classList.add("is-empty");
  pane.innerHTML = `<p class="muted">Elegí un aviso en la lista o en el mapa para ver la ficha.</p>`;
}

function focusListing(item) {
  if (!item) return;
  if (!item.has_exact_location) {
    const zone = zoneMarkersByKey[zoneKey(item)];
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

function sourceLabel(item) {
  return item.source_label || ({
    zonaprop: "ZonaProp",
    mercadolibre: "Mercado Libre",
    properati: "Properati",
    argenprop: "Argenprop",
    manual: "Carga manual",
  }[item.source] || item.source || "");
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
  const chips = (item.amenities || []).slice(0, 3).map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join("");
  const selected = item.id === selectedId ? " is-selected" : "";
  const contacted = item.contacted ? " is-contacted" : "";
  const approx = item.has_exact_location ? "" : " is-approx";
  return `<article class="card${item.favorite ? " is-fav" : ""}${selected}${contacted}${approx}" id="card-${cssId(item.id)}" data-id="${item.id}">
    <img src="${img}" alt="" />
    <div>
      <div class="card-top">
        <span class="kind ${cssId(item.property_type)}">${escapeHtml(kind)}</span>
        <button class="fav-btn" type="button" data-fav="${item.id}" title="Favorito">${item.favorite ? "\u2605" : "\u2606"}</button>
      </div>
      <div class="price">${money(item)}</div>
      <div class="headline">${escapeHtml(bits.join(" · ") || item.title || "")}</div>
      <div class="meta">${escapeHtml(item.barrio)} · ${escapeHtml(sourceLabel(item))}${item.has_exact_location ? "" : " · solo zona"}${item.contacted ? " · contactado" : ""}</div>
      <span class="tag ${cssId(item.deal_label)}">${escapeHtml(item.deal_label || "")}${item.vs_barrio_pct != null ? ` · ${item.vs_barrio_pct > 0 ? "-" : "+"}${Math.abs(item.vs_barrio_pct)}% vs barrio` : ""}</span>
      ${item.quality_label ? `<span class="tag quality">${escapeHtml(item.quality_label)} · ${fmt(item.quality_score)}</span>` : ""}
      ${chips ? `<div class="chips">${chips}</div>` : ""}
    </div>
  </article>`;
}

function popupHtml(item) {
  const loc = item.has_exact_location
    ? escapeHtml(item.address || item.barrio)
    : `${escapeHtml(item.barrio)} · sin dirección exacta (solo la zona)`;
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
    ${item.quality_label ? `<br/>${escapeHtml(item.quality_label)}` : ""}<br/>
    ${escapeHtml(sourceLabel(item))}
    ${blurb ? `<p class="popup-blurb">${escapeHtml(blurb)}${(item.description || "").length > 180 ? "…" : ""}</p>` : ""}
    ${item.url ? `<br/><a href="${item.url}" target="_blank" rel="noopener">Ver aviso original</a>` : ""}
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
  const res = await fetch("/api/pin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, ...payload }),
  });
  const data = await res.json();
  const item = allListings.find((x) => x.id === id);
  if (item) {
    item.favorite = data.favorite;
    item.notes = data.notes;
    if (data.contacted != null) item.contacted = data.contacted;
  }
  const keep = selectedId;
  render();
  if (keep) {
    selectedId = keep;
    highlightSelected();
    const current = allListings.find((x) => x.id === keep);
    if (current) showDetail(current);
  }
}

function showDetail(item) {
  const pane = $("detailPane");
  if (!pane) return;
  pane.classList.remove("is-empty");
  const kind = typeLabel(item);
  const chips = (item.amenities || []).map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join("");
  const reasons = (item.deal_reasons || []).map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join("");
  const fixes = (item.data_fixes || []).map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join("");
  const locLine = item.has_exact_location
    ? (item.address || item.barrio)
    : `${item.address || item.barrio} · sin dirección exacta (solo la zona)`;
  const cityName = (window.lastCities || []).find((c) => c.id === item.city)?.label
    || ({
      "puerto-madryn": "Puerto Madryn",
      "playa-union": "Playa Unión",
      trelew: "Trelew",
      rawson: "Rawson",
      gaiman: "Gaiman",
      "microcentro-caba": "Microcentro CABA",
    }[item.city] || item.city);
  const published = item.published_at
    ? new Date(item.published_at).toLocaleString("es-AR")
    : "—";
  const facts = [
    ["Dirección", item.address || "—"],
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
    ["Ubicación", item.has_exact_location ? "Dirección / punto en el mapa" : "Solo la zona"],
    ["Publicado", published],
    ["Inmobiliaria", item.publisher || "—"],
    ["Fuente", sourceLabel(item)],
  ].map(([k, v]) => `<div class="fact"><span>${escapeHtml(String(k))}</span><b>${escapeHtml(String(v))}</b></div>`).join("");
  const canEdit = Boolean(item.contacted);
  pane.innerHTML = `
    <button type="button" class="ghost detail-close" id="closeDetail">cerrar</button>
    ${item.image ? `<img class="detail-hero" src="${item.image}" alt="" />` : ""}
    <div class="detail-head">
      <span class="kind ${cssId(item.property_type)}">${escapeHtml(kind)}</span>
      <strong>${money(item)}</strong>
    </div>
    <h3>${escapeHtml(kind)} · ${escapeHtml(item.title)}</h3>
    <p class="meta">${escapeHtml(locLine)} · ${escapeHtml(sourceLabel(item))}</p>
    ${item.url ? `<a class="detail-link" href="${item.url}" target="_blank" rel="noopener">${escapeHtml(item.url)}</a>` : "<p class='muted'>Sin link al aviso original</p>"}
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
  const res = await fetch("/api/listing", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, ...payload }),
  });
  const data = await res.json();
  if (!data.listing) return;
  const idx = allListings.findIndex((x) => x.id === id);
  if (idx >= 0) allListings[idx] = data.listing;
  selectedId = id;
  render();
  showDetail(data.listing);
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
  const color = rising ? (upIsBad ? "#ff8b6b" : "#7dffb3") : (upIsBad ? "#7dffb3" : "#ff8b6b");
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
  const series = (data.series || []).map((row) => row.median_m2).filter(Boolean);
  $("marketChart").innerHTML = series.length >= 2
    ? sparkline(series)
    : `<p class="muted">Hoy es el primer punto de la curva. Abajo está el USD/m² de cada tipo; la suba/baja usa avisos nuevos vs más viejos cuando hay datos.</p>`;
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

["typeFilter", "zonaFilter", "barrioFilter", "maxPrice", "favOnly", "cityFilter", "dealBar", "sortBy"].forEach((id) => {
  if (!$(id)) return;
  $(id).addEventListener("input", () => {
    render();
    renderBarrios(allBarrios);
  });
  $(id).addEventListener("change", () => {
    render();
    renderBarrios(allBarrios);
    if (id === "typeFilter") loadMarket();
  });
});

$("cityFilter")?.addEventListener("change", () => {
  lastListingsFp = "";
  focusCity(currentCity());
  if ($("placeQuery")) {
    const opt = $("cityFilter").selectedOptions[0];
    $("placeQuery").value = opt ? opt.textContent : "";
  }
  load();
});

$("dealBar")?.addEventListener("input", () => {
  const v = Number($("dealBar").value || 0);
  const label = $("dealBarLabel");
  if (label) {
    label.textContent = v <= 0
      ? "todos los avisos"
      : v < 40
        ? `score ≥ ${v} (incluye buenos)`
        : v < 55
          ? `score ≥ ${v} (oportunidades)`
          : `score ≥ ${v} (gangas fuertes)`;
  }
});

$("refreshBtn").onclick = async () => {
  const query = ($("placeQuery")?.value || "").trim();
  const city = currentCity();
  $("refreshBtn").disabled = true;
  if ($("pauseBtn")) $("pauseBtn").disabled = false;
  hideSuggest();
  const res = await fetch("/api/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(query ? { query, city } : { city }),
  });
  const data = await res.json();
  if (data.place) applyPlace(data.place);
  else if (data.jobs) {
    const running = (data.running_cities || [])[0];
    if (running && $("cityFilter") && [...$("cityFilter").options].some((o) => o.value === running)) {
      $("cityFilter").value = running;
      focusCity(running);
    }
  }
  if (data.message) $("statusLine").textContent = data.message;
  pollStatus(true);
};

$("pauseBtn")?.addEventListener("click", async () => {
  $("pauseBtn").disabled = true;
  const city = currentCity();
  await fetch("/api/pause", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ city }),
  });
  pollStatus(true);
});

async function pollStatus(force) {
  const s = await (await fetch("/api/status")).json();
  const city = currentCity();
  const job = (s.jobs || {})[city] || {};
  const crawl = s.crawl || {};
  const cityRunning = Boolean(job.running) || (s.running_cities || []).includes(city);
  const anyRunning = Boolean(s.running) || cityRunning;
  const mode = job.mode || s.mode || "";
  const fastRunning = cityRunning && mode === "fast";
  if ($("slowCrawl") && document.activeElement !== $("slowCrawl") && crawl.enabled) {
    $("slowCrawl").checked = true;
  }
  const waitBit = crawl.wait_s ? ` · espera ${Math.round(crawl.wait_s)}s` : "";
  $("refreshBtn").disabled = fastRunning;
  if ($("pauseBtn")) $("pauseBtn").disabled = !(cityRunning || crawl.enabled);
  if (cityRunning) {
    $("statusLine").textContent = (job.message || s.message || (mode === "slow" ? "Motor de fondo…" : "Buscando avisos…")) + waitBit;
    await load({ keepStatus: true, live: true });
  } else {
    if (cityWasRunning[city] || force) await load();
    else if (s.message) $("statusLine").textContent = crawl.enabled
      ? `${s.message}${waitBit}`
      : (s.last_run
        ? `Última búsqueda: ${new Date(s.last_run).toLocaleString("es-AR")} · ${s.message}`
        : s.message);
  }
  cityWasRunning[city] = cityRunning;
  wasRunning = anyRunning;
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

$("slowCrawl")?.addEventListener("change", async () => {
  const query = ($("placeQuery")?.value || "").trim();
  const city = currentCity();
  const enabled = Boolean($("slowCrawl").checked);
  if ($("pauseBtn") && enabled) $("pauseBtn").disabled = false;
  const res = await fetch("/api/crawl", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled, city, query: query || undefined }),
  });
  const data = await res.json();
  if (data.place) applyPlace(data.place);
  if (data.message) $("statusLine").textContent = data.message;
  pollStatus(true);
});

map.on("zoomend", () => renderBarrios(allBarrios));

function focusCity(cityId) {
  const view = CITY_VIEWS[cityId] || CITY_VIEWS["puerto-madryn"];
  if (!view) return;
  map.setView([view.lat, view.lon], view.zoom || 13, { animate: true });
}

function applyPlace(place) {
  if (!place || !place.id) return;
  CITY_VIEWS[place.id] = { lat: place.lat, lon: place.lon, zoom: place.zoom || 13 };
  const select = $("cityFilter");
  if (select && ![...select.options].some((o) => o.value === place.id)) {
    const opt = document.createElement("option");
    opt.value = place.id;
    opt.textContent = place.label || place.id;
    select.appendChild(opt);
  }
  if (select) select.value = place.id;
  if ($("placeQuery")) $("placeQuery").value = place.label || "";
  window.placeBarrios = window.placeBarrios || {};
  if (place.barrios && place.barrios.length) {
    window.placeBarrios[place.id] = place.barrios.map((b) => b.name);
  }
  focusCity(place.id);
  fillZonas(cityItems());
  fillBarrios();
  renderBarrios(allBarrios);
  render();
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
  const res = await fetch("/api/place", {
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
  });
  const data = await res.json();
  applyPlace(data.city || place);
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

load();
pollStatus();
clearInterval(pollTimer);
pollTimer = setInterval(() => pollStatus(), 2500);
