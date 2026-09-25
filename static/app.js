const map = L.map("map", {
  zoomControl: true,
  dragging: true,
  scrollWheelZoom: true,
  doubleClickZoom: true,
  boxZoom: true,
  inertia: true,
}).setView([-38.4161, -63.6167], 4);
window.map = map;

L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19,
  updateWhenIdle: true,
  keepBuffer: 2,
}).addTo(map);
const poiLayer = L.layerGroup().addTo(map);

const exactCluster = L.markerClusterGroup({
  showCoverageOnHover: false,
  maxClusterRadius: 42,
  spiderfyOnMaxZoom: true,
  zoomToBoundsOnClick: true,
  singleMarkerMode: false,
});
map.addLayer(exactCluster);

const zoneLayer = L.layerGroup().addTo(map);
let zoneMarkersByKey = {};
let stackMarkers = [];
let approxBox = null;

let allListings = [];
let listWindowItems = [];
let listWinRange = "";
let mapPaintGen = 0;
let facebook = [];
let markersById = {};
let pollTimer = null;
let pollBusy = false;
let wasRunning = false;
let cityWasRunning = {};
let noteTimers = {};
let selectedId = null;
let loadInFlight = false;
let lastListingsFp = "";
let lastListingsFetchAt = 0;
let listingsReady = false;
let scrapeRunning = false;
let viewSeq = 0;
let listingsRev = 0;
let listingsRevByCity = {};
let usdArs = 0;
let loadRetries = 0;
let lastStatus = {};
const isAdmin = location.pathname.replace(/\/+$/, "") === "/admin";
document.body.classList.toggle("is-admin", isAdmin);
const clientPerf = {};
let lastMarketKey = "";
let marketInFlight = "";

function markClient(name, ms, extra) {
  const row = clientPerf[name] || (clientPerf[name] = { n: 0, last: 0, max: 0, samples: [] });
  const value = Math.round(Number(ms) || 0);
  row.n += 1;
  row.last = value;
  row.max = Math.max(row.max, value);
  row.samples.push(value);
  if (row.samples.length > 20) row.samples.shift();
  if (extra) row.extra = extra;
}

function clientPct(samples, q) {
  if (!samples.length) return 0;
  const ordered = [...samples].sort((a, b) => a - b);
  return ordered[Math.min(ordered.length - 1, Math.floor((ordered.length - 1) * q))];
}

function clientPerfPayload() {
  const out = {};
  Object.entries(clientPerf).forEach(([name, row]) => {
    out[name] = {
      n: row.n,
      last_ms: row.last,
      max_ms: row.max,
      p50_ms: clientPct(row.samples, 0.5),
      p95_ms: clientPct(row.samples, 0.95),
    };
  });
  return out;
}

async function timedFetch(url, name, opts) {
  const t0 = performance.now();
  try {
    const res = await fetch(url, opts);
    markClient(name, performance.now() - t0, { ok: res.ok, server: res.headers.get("Server-Timing") || "" });
    return res;
  } catch (err) {
    markClient(name, performance.now() - t0, { ok: false });
    throw err;
  }
}

function waitCopy(kind) {
  if (kind === "scrape") return emptyPlaceCopy();
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

function clearLoadedListings() {
  viewSeq += 1;
  allListings = [];
  listWindowItems = [];
  listWinRange = "";
  lastListingsFp = "";
  listingsRev = 0;
  selectedId = null;
  mapPaintGen += 1;
  exactCluster.clearLayers();
  zoneLayer.clearLayers();
  markersById = {};
  zoneMarkersByKey = {};
  stackMarkers = [];
  if (approxBox) {
    map.removeLayer(approxBox);
    approxBox = null;
  }
  map.closePopup();
  poiLayer.clearLayers();
  const pane = $("detailPane");
  if (pane) {
    pane.classList.add("is-empty");
    pane.innerHTML = `<p class="muted">Elegí un aviso en la lista o en el mapa para ver la ficha.</p>`;
  }
  showListingsWait("city");
}

const emptyPlaces = new Set();
try {
  JSON.parse(localStorage.getItem("propmap.emptyPlaces") || "[]").forEach((id) => {
    if (id) emptyPlaces.add(String(id));
  });
} catch (err) {}

function rememberEmptyPlace(cityId) {
  const id = String(cityId || "");
  if (!id || emptyPlaces.has(id)) return;
  emptyPlaces.add(id);
  localStorage.setItem("propmap.emptyPlaces", JSON.stringify([...emptyPlaces]));
}

function forgetEmptyPlace(cityId) {
  const id = String(cityId || "");
  if (!emptyPlaces.has(id)) return;
  emptyPlaces.delete(id);
  localStorage.setItem("propmap.emptyPlaces", JSON.stringify([...emptyPlaces]));
}

function forgetCityWithoutListings(cityId) {
  rememberEmptyPlace(cityId);
  const rows = (window.lastCities || []).filter((c) => String(c.id) !== String(cityId));
  window.lastCities = rows;
  window.knownCities = rows;
  const select = $("cityFilter");
  if (select) {
    [...select.options].filter((o) => o.value === cityId).forEach((o) => o.remove());
  }
  if (!rows.length) {
    if (select) select.innerHTML = "";
    localStorage.removeItem("propmap.city");
    showListingsWait("city");
    if ($("statusLine")) $("statusLine").textContent = "No hay ciudades con avisos.";
    return;
  }
  fillCities(rows, { source: "catalog" });
  const next = select?.value;
  if (!next || next === cityId) return;
  localStorage.setItem("propmap.city", next);
  if ($("placeQuery")) {
    const opt = select.selectedOptions[0];
    $("placeQuery").value = opt ? opt.textContent : "";
  }
  pickedPlace = placeFromCityFilter();
  rememberCityView(pickedPlace);
  focusedCity = next;
  focusCity(next);
  clearLoadedListings();
  load({ waitKind: "city" });
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

function hasRealIntersection(item) {
  const inter = String(item?.intersection || "").trim();
  if (!inter) return false;
  const parts = inter.split(/\s+y\s+/i).map((part) => part.trim().toLowerCase()).filter(Boolean);
  if (parts.length < 2) return true;
  return parts[0] !== parts[1];
}

function isExactPin(item) {
  if (!item || item.lat == null || item.lon == null) return false;
  if (item.location_approx) return false;
  if (item.location_real) return true;
  if (hasStreetNumber(item) && item.has_exact_location) return true;
  if (hasRealIntersection(item) && item.has_exact_location) return true;
  return Boolean(item.portal_exact && !item.portal_approx);
}

function isPlotLabel(text) {
  return /\b(?:lotes?|parcela|manzana|mz\.?|fracci[oó]n(?:es)?|loteo)\s*(?:n(?:ro|umero|úmero)?\.?\s*)?\d{1,5}\b/i.test(String(text || ""));
}

function streetAddress(item) {
  if (!item) return "";
  const street = String(item.street || "").trim();
  const rawNumber = item.street_number;
  const hasNumber = rawNumber != null && String(rawNumber).trim() !== "" && Number(rawNumber) !== 0;
  if (street && hasNumber && !isPlotLabel(`${street} ${rawNumber}`)) return `${street} ${rawNumber}`.trim();
  const addr = String(item.address || "").trim();
  const barrio = String(item.barrio || "").trim().toLowerCase();
  const city = String(item.city_label || item.city || "").replace(/-/g, " ").trim().toLowerCase();
  const folded = addr.toLowerCase();
  if (!addr) return street;
  if (folded === barrio || folded === city) return street;
  if (isPlotLabel(addr)) return street && hasNumber ? `${street} ${rawNumber}`.trim() : "";
  if (addr.length >= 3) return addr;
  return street;
}

function hasStreetNumber(item) {
  if (!item) return false;
  if (isPlotLabel(item.address) || isPlotLabel(`${item.street || ""} ${item.street_number || ""}`)) return false;
  const n = item.street_number;
  if (n != null && String(n).trim() !== "" && Number(n) !== 0) return true;
  return /\d{2,5}/.test(String(item.address || ""));
}

function hasInterseccion(item) {
  return hasRealIntersection(item);
}

function hasScrapedApprox(item) {
  if (!item) return false;
  if (item.portal_lat != null && item.portal_lon != null) return true;
  if (String(item.approx_address || "").trim()) return true;
  const source = String(item.source || "").toLowerCase();
  if (source === "properati" && item.lat != null && item.lon != null) return true;
  return false;
}

function isLocationMissing(item) {
  if (!item) return true;
  if (typeof item.location_missing === "boolean") return item.location_missing;
  if (isExactPin(item)) return false;
  return !streetAddress(item) && !hasInterseccion(item) && !hasScrapedApprox(item);
}

function prefetchPins(city, seq, waitKind) {
  const q = new URLSearchParams({ city, pins: "1" });
  const ac = new AbortController();
  const kill = setTimeout(() => ac.abort(), 8000);
  timedFetch("/api/listings?" + q.toString(), "listings.pins", { signal: ac.signal })
    .then((res) => (res.ok ? res.json() : null))
    .then((data) => {
      if (!data || seq !== viewSeq || currentCity() !== city) return;
      if (!(data.listings || []).length) return;
      applyListingsPayload(data, city, seq, { keepStatus: true });
      if ($("statusLine") && String(lastListingsFp).endsWith(":p")) {
        $("statusLine").textContent = `Mostrando ${allListings.length} avisos. Cargando fichas…`;
      }
    })
    .catch(() => {})
    .finally(() => clearTimeout(kill));
}

async function load(opts = {}) {
  const city = currentCity();
  if (loadInFlight && opts.live) return;
  if (!opts.live) viewSeq += 1;
  const seq = viewSeq;
    loadInFlight = true;
  const shouldWait = !listingsReady || opts.waitKind === "city";
  if (shouldWait && !allListings.length) showListingsWait(opts.waitKind || "init");
  if (!allListings.length && !opts.live) prefetchPins(city, seq, opts.waitKind);
  let data;
  try {
    const q = new URLSearchParams();
    if (opts.live) q.set("live", "1");
    q.set("city", city);
    const knownRev = listingsRevByCity[city];
    if (allListings.length && knownRev && !String(lastListingsFp).endsWith(":p")) {
      q.set("since", String(knownRev));
    }
    const ac = new AbortController();
    const kill = setTimeout(() => ac.abort(), 25000);
    try {
      const res = await timedFetch("/api/listings?" + q.toString(), "listings", { signal: ac.signal });
      if (!res.ok) throw new Error(String(res.status));
      data = await res.json();
    } finally {
      clearTimeout(kill);
    }
    if (!(data.warming && !(data.listings || []).length && !allListings.length)) loadRetries = 0;
  } catch {
    if (seq !== viewSeq || currentCity() !== city) return;
    if (listingsReady && opts.live && allListings.length) return;
    loadRetries += 1;
    if (!allListings.length) showListingsWait(opts.waitKind || "init");
    const delay = Math.min(3000, 350 * loadRetries);
    setTimeout(() => load({ ...opts, live: Boolean(opts.live || allListings.length) }), delay);
    return;
  } finally {
    loadInFlight = false;
  }
  if (!data) return;
  if (seq !== viewSeq || currentCity() !== city) return;
  if (data.rev != null) {
    listingsRev = data.rev;
    listingsRevByCity[city] = data.rev;
  }
  if (data.warming) {
    if (!data.unchanged && (data.listings || []).length) {
      applyListingsPayload(data, city, seq, { keepStatus: true });
      if (seq !== viewSeq || currentCity() !== city) return;
      if ($("statusLine")) {
        $("statusLine").textContent = `Mostrando ${allListings.length} avisos. Sigue cargando…`;
      }
    } else if (!allListings.length) {
      loadRetries += 1;
      showListingsWait(opts.waitKind || "init");
    }
    const delay = allListings.length ? 500 : Math.min(2000, 300 + loadRetries * 40);
    setTimeout(() => load({ waitKind: opts.waitKind || "init", live: true }), delay);
    return;
  }
  if (data.unchanged) {
    lastListingsFetchAt = Date.now();
    if (!allListings.length) {
      listingsRev = 0;
      listingsRevByCity[city] = 0;
      hideListingsWait();
      load({ waitKind: "city" });
      return;
    }
    hideListingsWait();
    return;
  }
  if (!data.warming && data.layer !== "pins" && !(data.listings || []).length) {
    if (city !== "caba" && city !== "capital-federal") {
      forgetCityWithoutListings(city);
      return;
    }
  }
  if ((data.listings || []).length) forgetEmptyPlace(city);
  applyListingsPayload(data, city, seq, { keepStatus: opts.keepStatus, live: opts.live });
}

function applyListingsPayload(data, city, seq, opts = {}) {
  const fp = `${data.rev}:${(data.listings || []).length}:${data.warming ? "w" : (data.layer === "pins" ? "p" : "f")}`;
  if (opts.live && fp === lastListingsFp && listingsReady) return;
  if (lastListingsFp.endsWith(":f") && data.layer === "pins") return;
  lastListingsFp = fp;
  lastListingsFetchAt = Date.now();
  allListings = applyLocalPins(data.listings || []).map(withCachedFicha);
  facebook = data.facebook || [];
  if (data.usd_ars) usdArs = Number(data.usd_ars) || usdArs;
  window.lastStats = data.stats || {};
  hideListingsWait();
  renderKpis(data.stats || {});
  fillCities(data.cities || [], { source: "listings" });
  if (currentCity() !== city) {
    listingsRev = 0;
    lastListingsFp = "";
    load({ waitKind: "city" });
    return;
  }
  if (seq !== viewSeq) return;
  fillZonas(cityItems());
  fillBarrios();
  render();
  renderStats();
  if (data.layer !== "pins") loadMarket();
  else if (!lastMarketKey) loadMarket();
  if (!opts.keepStatus) {
    $("statusLine").textContent = data.last_run && allListings.length
      ? `Última búsqueda: ${new Date(data.last_run).toLocaleString("es-AR")}`
      : publicStatusLine();
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
  if (pickedPlace?.id && placeIsPicked()) return pickedPlace.id;
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

function placeNameToken(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/-/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function cityQueryTokens(city) {
  const tokens = new Set([placeNameToken(city)]);
  const row = (window.lastCities || []).find((c) => c.id === city) || {};
  if (row.label) tokens.add(placeNameToken(row.label));
  return tokens;
}

function belongsToCity(item, city) {
  if (!city) return true;
  const tags = (item.place_tags || []).map(placeNameToken).filter(Boolean);
  const wanted = cityQueryTokens(city);
  if (tags.length && [...wanted].some((view) => tags.includes(view))) return true;
  if (tags.length && [...wanted].some((view) => tags.some((tag) => view !== tag && view.startsWith(`${tag} `)))) {
    return false;
  }
  const ids = placeIdsFor(city);
  const tagged = ids.has(item.city || "") || (item.city || "") === city
    || ids.has(item.search_city || "") || (item.search_city || "") === city;
  const view = CITY_VIEWS[city];
  if (item.lat != null && item.lon != null && view && view.lat != null && view.lon != null) {
    return inCityView(item, view);
  }
  return tagged;
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

function isCabaPlace(c) {
  const id = String(c?.id || "");
  return id === "caba" || id === "capital-federal";
}

function foldPlaceToken(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/-/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function isLocatableCity(c) {
  if (!c || !c.id) return false;
  if (isCabaPlace(c)) return true;
  const label = String(c.label || "").toLowerCase().trim();
  if (label.startsWith("barrio ") || label.startsWith("departamento ")) return false;
  return true;
}

function placeCaption(c) {
  if (!c) return "";
  const hint = String(c.hint || "").trim();
  if (hint) return hint;
  return String(c.label || c.id || "");
}

function placeSuggestSub(p) {
  const label = String(p?.label || "").trim();
  const named = String(p?.province_label || "").trim();
  if (named) return named;
  const hint = String(p?.hint || "").trim();
  if (hint && label && hint.toLowerCase().startsWith(label.toLowerCase() + ",")) {
    return hint.slice(label.length + 1).trim();
  }
  if (hint && hint !== label) return hint;
  return String(p?.province || "").replace(/-/g, " ").trim();
}

function placeMatchesQuery(p, q) {
  const needle = foldPlaceToken(q);
  if (needle.length < 2 || !p) return false;
  const label = foldPlaceToken(p.label || "");
  const prov = foldPlaceToken(p.province_label || p.province || "");
  if (label.startsWith(needle)) return true;
  if (needle.length >= 3 && label.includes(needle)) return true;
  if (prov && `${label} ${prov}`.startsWith(needle)) return true;
  return false;
}

function placeSuggestKey(p) {
  const label = foldPlaceToken(p?.label || p?.id || "");
  const prov = foldPlaceToken(p?.province_label || p?.province || "");
  return prov ? `${label}|${prov}` : label;
}

function mergePlaceHits(...lists) {
  const byKey = new Map();
  lists.flat().forEach((p) => {
    if (!p || !p.id) return;
    const key = placeSuggestKey(p);
    const prev = byKey.get(key);
    if (!prev || String(p.id).length > String(prev.id).length) byKey.set(key, p);
  });
  return [...byKey.values()].slice(0, 8);
}

function localPlaceHits(q) {
  const needle = foldPlaceToken(q);
  if (needle.length < 2) return [];
  const rows = [];
  const seen = new Set();
  for (const c of [...(window.lastCities || []), ...(window.knownCities || [])]) {
    if (!c || !c.id || seen.has(c.id) || !isLocatableCity(c)) continue;
    const label = foldPlaceToken(c.label || "");
    const id = foldPlaceToken(c.id);
    const hint = foldPlaceToken(c.hint || "");
    const prov = foldPlaceToken(c.province_label || c.province || "");
    if (
      label.startsWith(needle)
      || id.startsWith(needle)
      || hint.startsWith(needle)
      || (prov && `${label} ${prov}`.startsWith(needle))
      || (label && needle.length >= 3 && label.includes(needle))
    ) {
      seen.add(c.id);
      rows.push(c);
    }
  }
  return rows.slice(0, 8);
}

function placeCatalogKey(c) {
  const label = foldPlaceToken(c.label || c.id);
  const prov = foldPlaceToken(c.province);
  if (isCabaPlace(c) || !prov) return label;
  return `${label}|${prov}`;
}

function cityReadyForCatalog(c) {
  if (!c || !c.id) return false;
  const id = String(c.id);
  if (emptyPlaces.has(id)) return false;
  if (id.endsWith("-pins") || id.includes("-pins-") || id.includes(".pins")) return false;
  if (!isLocatableCity(c)) return false;
  const n = Number(c.n);
  return Number.isFinite(n) && n >= 8;
}

function fillCities(cities, opts = {}) {
  const select = $("cityFilter");
  const manual = document.querySelector("#manualForm [name=city]");
  if (!select) return;
  const saved = localStorage.getItem("propmap.city") || "";
  const current = select.value || saved || "caba";
  const byId = new Map();
  const previous = window.lastCities || window.knownCities || [];
  const incoming = cities || [];
  const source = opts.source || "listings";
  const take = (c) => {
    if (!c || !c.id) return;
    const id = String(c.id);
    if (id.endsWith("-pins") || id.includes("-pins-") || id.includes(".pins")) return;
    const label = String(c.label || "").toLowerCase().trim();
    if (label.startsWith("barrio ") || label.startsWith("departamento ")) return;
    const merged = { ...(byId.get(id) || {}), ...c };
    if (!cityReadyForCatalog(merged)) {
      byId.delete(id);
      return;
    }
    byId.set(id, merged);
  };
  if (source === "catalog" && incoming.length) {
    incoming.forEach((c) => take(c));
  } else if (!previous.length) {
    incoming.forEach((c) => take(c));
  } else {
    previous.forEach((c) => take(c));
    incoming.forEach((c) => {
      if (byId.has(String(c.id)) || !cityReadyForCatalog(c)) take(c);
    });
  }
  if (
    pickedPlace?.id
    && pickedPlace.lat != null
    && pickedPlace.lon != null
    && !byId.has(pickedPlace.id)
    && cityReadyForCatalog(pickedPlace)
  ) {
    byId.set(pickedPlace.id, { ...(byId.get(pickedPlace.id) || {}), ...pickedPlace });
  }
  const rows = [...byId.values()].sort((a, b) => {
    const byLabel = String(a.label || a.id).localeCompare(String(b.label || b.id), "es");
    if (byLabel) return byLabel;
    return String(a.province || "").localeCompare(String(b.province || ""), "es");
  });
  window.knownCities = rows;
  window.lastCities = rows;
  if (!rows.length) {
    select.innerHTML = "";
    if (manual) manual.innerHTML = "";
    return;
  }
  const options = rows.map((c) => `<option value="${c.id}">${placeCaption(c)}</option>`).join("");
  select.innerHTML = options;
  if (manual) manual.innerHTML = options;
  const ids = rows.map((c) => c.id);
  select.value = ids.includes(current) ? current : (ids.includes("caba") ? "caba" : ids[0]);
  localStorage.setItem("propmap.city", select.value);
  rows.forEach((c) => {
    rememberCityView(c);
    if (c.barrios && c.barrios.length) {
      window.placeBarrios = window.placeBarrios || {};
      window.placeBarrios[c.id] = c.barrios.map((b) => b.name);
    }
  });
  const view = CITY_VIEWS[select.value];
  if (view && $("placeQuery") && !$("placeQuery").value) {
    $("placeQuery").placeholder = `ej. ${rows.find((c) => c.id === select.value)?.label || "una ciudad"}`;
  }
  if (!focusedCity && select.value) {
    focusCity(select.value);
    focusedCity = select.value;
  }
  if (document.activeElement !== $("placeQuery") && !pickedPlace) {
    pickedPlace = placeFromCityFilter();
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
  const fromItems = cityItems().map((x) => x.barrio).filter((n) => n && n !== "Sin clasificar");
  const fromKnown = (window.placeBarrios && window.placeBarrios[city]) || [];
  const names = [...new Set([...fromItems, ...fromKnown])].sort((a, b) => a.localeCompare(b, "es"));
  select.innerHTML = `<option value="">Todos</option>` + names.map((n) => `<option>${escapeHtml(n)}</option>`).join("");
  let next = names.includes(current) ? current : "";
  if (landingBarrio && names.includes(landingBarrio)) {
    next = landingBarrio;
    landingBarrio = "";
  }
  select.value = next;
}

function ambientesOf(item) {
  const type = item?.property_type || "";
  if (type === "terreno" || type === "local" || type === "oficina" || type === "galpon") return null;
  const rooms = Number(item?.rooms);
  if (Number.isFinite(rooms) && rooms > 0) return rooms;
  const beds = Number(item?.bedrooms);
  if (Number.isFinite(beds) && beds > 0) return beds + 1;
  return null;
}

function bedroomsOf(item) {
  const type = item?.property_type || "";
  if (type === "terreno" || type === "local" || type === "oficina" || type === "galpon") return null;
  const beds = Number(item?.bedrooms);
  if (Number.isFinite(beds) && beds >= 0) return beds;
  const rooms = Number(item?.rooms);
  if (Number.isFinite(rooms) && rooms > 0) return Math.max(0, rooms - 1);
  return null;
}

function bathroomsOf(item) {
  if ((item?.property_type || "") === "terreno") return null;
  const baths = Number(item?.bathrooms);
  return Number.isFinite(baths) && baths >= 0 ? baths : null;
}

function listingM2(item) {
  if (!item) return 0;
  const lot = Number(item.lot_m2 || item.total_m2 || 0) || 0;
  const covered = Number(item.covered_m2 || 0) || 0;
  if (item.property_type === "terreno") return lot || covered;
  return covered || lot;
}

function minChosen(id) {
  const raw = $(id)?.value;
  if (raw == null || raw === "") return 0;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

function passesSize(item) {
  const minM2 = minChosen("minM2");
  const maxM2 = minChosen("maxM2");
  if (minM2 || maxM2) {
    const m2 = listingM2(item);
    if (!m2) return false;
    if (minM2 && m2 < minM2) return false;
    if (maxM2 && m2 > maxM2) return false;
  }
  if ($("typeFilter")?.value === "terreno") return true;
  const minBeds = minChosen("minBeds");
  const minRooms = minChosen("minRooms");
  const minBaths = minChosen("minBaths");
  if (minBeds && !(bedroomsOf(item) >= minBeds)) return false;
  if (minRooms && !(ambientesOf(item) >= minRooms)) return false;
  if (minBaths && !(bathroomsOf(item) >= minBaths)) return false;
  return true;
}

function syncRoomFilters() {
  const lot = $("typeFilter")?.value === "terreno";
  ["minBeds", "minRooms", "minBaths"].forEach((id) => {
    const el = $(id);
    if (el) el.disabled = lot;
  });
}

function cityDealScore(item) {
  const raw = item?.deal_score;
  if (raw != null && raw !== "" && Number.isFinite(Number(raw))) return Number(raw);
  if (item?.vs_barrio_pct != null && Number.isFinite(Number(item.vs_barrio_pct))) {
    return Math.max(0, Math.min(100, Math.round((38 + Number(item.vs_barrio_pct)) * 10) / 10));
  }
  if (item?.deal_label === "oportunidad") return 70;
  if (item?.deal_label === "bueno") return 48;
  return 0;
}

function selectedTraits() {
  return new Set(
    Array.from(document.querySelectorAll('input[name="listingTrait"]:checked')).map((el) => el.value)
  );
}

function passesTraits(item, traits) {
  if (!traits || !traits.size) return true;
  if (traits.has("credit") && item.mortgage_credit !== true) return false;
  if (traits.has("owner") && item.owner_direct !== true) return false;
  if (traits.has("expenses") && item.low_expenses !== true) return false;
  if (traits.has("urgent") && item.urgent_sale !== true) return false;
  if (traits.has("quiet") && item.environment !== "quiet") return false;
  if (traits.has("good") && !isGoodCondition(item)) return false;
  if (traits.has("balcony") && item.has_balcony !== true) return false;
  if (traits.has("bright") && item.bright !== true) return false;
  if (traits.has("growing") && item.growing_area !== true) return false;
  if (traits.has("view") && item.open_view !== true) return false;
  if (traits.has("patio") && item.has_patio !== true) return false;
  if (traits.has("garage") && item.has_garage !== true) return false;
  if (traits.has("terrace") && item.has_terrace !== true) return false;
  return true;
}

function filtered() {
  const city = currentCity();
  const type = $("typeFilter").value;
  const zona = $("zonaFilter").value;
  const barrio = $("barrioFilter")?.value || "";
  const max = Number($("maxPrice").value || 0);
  const minDeal = Number($("dealBar")?.value || 0);
  const favs = $("favOnly").checked;
  const traits = selectedTraits();

  return allListings.filter((item) => {
    if (city && !belongsToCity(item, city)) return false;
    if (type && item.property_type !== type) return false;
    if (zona && item.zona !== zona) return false;
    if (barrio && item.barrio !== barrio) return false;
    if (max && (!item.price_usd || item.price_usd > max)) return false;
    if (minDeal > 0 && cityDealScore(item) < minDeal) return false;
    if (minDeal >= 40 && item.is_outlier) return false;
    if (favs && !item.favorite) return false;
    if (!passesTraits(item, traits)) return false;
    if (!passesSize(item)) return false;
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
  const lost = group.items.every(isLocationMissing);
  const deal = group.items.find((item) => item.deal_label === "oportunidad")
    || group.items.find((item) => item.deal_label === "bueno")
    || group.items[0];
  const color = colors[deal?.deal_label] || "#7eb6d6";
  return L.divIcon({
    className: `zone-pin approx${lost ? " is-lost" : ""}${selected ? " is-selected" : ""}`,
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

function pinIcon(item) {
  const color = colors[item.deal_label] || "#7eb6d6";
  const fav = item.favorite ? " fav" : "";
  const shape = isExactPin(item) ? "round" : isLocationMissing(item) ? "lost" : "square";
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

function distKm(a, b) {
  return distM(a, b) / 1000;
}

async function loadNear(item) {
  if (!item || !item.city || item.lat == null || item.lon == null) return null;
  const q = new URLSearchParams({
    city: item.city,
    lat: String(item.lat),
    lon: String(item.lon),
  });
  if (item.id) q.set("listing_id", item.id);
  const res = await fetch(`/api/near?${q}`);
  if (!res.ok) return null;
  return res.json();
}

function groupNearby(rows) {
  return [...(rows || [])].sort((a, b) => (a.km || 0) - (b.km || 0));
}

function walkMeters(item, data) {
  const km = Number(
    data?.walk_km
    || item?.access?.walk_km
    || item?.profile?.access?.walk_km
    || 0.6
  );
  if (!Number.isFinite(km) || km <= 0) return 600;
  return Math.round(km * 1000);
}

function applyNearToProfile(item, data) {
  if (!item || !data) return;
  if (!item.profile) item.profile = { axes: {} };
  if (!item.profile.axes) item.profile.axes = {};
  if (item.property_type === "terreno") item.profile.order = ["price_m2", "zona", "servicios"];
  if (!item.access) item.access = {};
  if (data.walk_km) item.access.walk_km = data.walk_km;
  const score = data.pending ? null : data.score;
  item.profile.axes.servicios = {
    score: score == null ? null : Number(score),
    confidence: score == null ? (data.pending ? "low" : "none") : "high",
    note: data.pending
      ? (data.reason || "calculando POIs cercanos…")
      : (data.reason || ""),
  };
  stampProfile(item.profile);
  redrawPentagon(item);
}

function redrawPentagon(item) {
  if (!item?.profile) return;
  const card = document.querySelector(".ficha-bundle > .radar-card");
  if (card) {
    const wrap = document.createElement("div");
    wrap.innerHTML = pentagonChart(item.profile, { kind: item.property_type });
    const next = wrap.firstElementChild;
    if (next) card.replaceWith(next);
  }
  const mini = document.querySelector(`#card-${cssId(item.id)} .radar-card`);
  if (mini) {
    const wrap = document.createElement("div");
    wrap.innerHTML = pentagonChart(item.profile, { mini: true, kind: item.property_type });
    const next = wrap.firstElementChild;
    if (next) mini.replaceWith(next);
  }
}

function formatKm(km) {
  if (km < 1) return `${Math.round(km * 1000)} m`;
  return `${String(km.toFixed(1)).replace(".", ",")} km`;
}

function nearbyHtml(item, rows, pending) {
  const meters = walkMeters(item);
  if (!isExactPin(item)) {
    return `<div class="near-list"><p class="muted">Con ubicación exacta se lista lo que hay a pie (hasta ${meters} m): súper, paradas, subte, tren, salud, plaza, escuela…</p></div>`;
  }
  if (pending && !(rows || []).length) {
    return `<div class="near-list"><p class="muted">Buscando en el mapa lo que está a menos de ${meters} m…</p></div>`;
  }
  if (!(rows || []).length) {
    return `<div class="near-list"><p class="muted">Nada de esto etiquetado a menos de ${meters} m de este pin.</p></div>`;
  }
  const lis = rows.map((row, i) => {
    const title = row.name || row.kind || row.label;
    const sub = [];
    if (row.kind && row.kind !== title) sub.push(row.kind);
    else if (row.label && row.label !== title) sub.push(row.label);
    sub.push(formatKm(row.km));
    return `<li><button type="button" class="near-item is-${escapeHtml(row.category || "")}" data-near="${i}"><i class="near-dot" aria-hidden="true"></i><span><b>${escapeHtml(title)}</b><small>${escapeHtml(sub.join(" · "))}</small></span></button></li>`;
  }).join("");
  return `<div class="near-list"><p class="muted">Hasta ${meters} m, de más cerca a más lejos.</p><ul>${lis}</ul></div>`;
}

function paintNearby(rows) {
  poiLayer.clearLayers();
  const colors = { health: "#1f7a4a", police: "#9a5a28", transport: "#3d6f8a", subway: "#7a3d6a", train: "#4a4a4a", beach: "#2a6f8a", plaza: "#5a7a38", shop: "#b5812a", school: "#6b4c9a" };
  (rows || []).forEach((row) => {
    if (row.lat == null || row.lon == null) return;
    L.circleMarker([row.lat, row.lon], {
      radius: 7,
      color: colors[row.category] || "#2a4a3c",
      weight: 2,
      fillColor: "#fbfcf9",
      fillOpacity: 1,
    }).bindTooltip(`${row.name || row.kind || ""} · ${row.label || ""}`).addTo(poiLayer);
  });
}

function showNearbyOnMap(rows) {
  if (document.querySelector(".near-fold")?.open) paintNearby(rows);
  else poiLayer.clearLayers();
}

function bindNearby(item, rows) {
  document.querySelectorAll(".near-item").forEach((btn) => {
    btn.onclick = (ev) => {
      ev.preventDefault();
      const row = rows[Number(btn.dataset.near)];
      if (!row || row.lat == null || row.lon == null) return;
      map.setView([row.lat, row.lon], 17, { animate: true });
    };
  });
}

async function fillNearby(item) {
  const box = $("nearBox");
  if (!box || !item) return;
  const seed = groupNearby(item.nearby || []);
  if (seed.length) {
    box.innerHTML = nearbyHtml(item, seed, false);
    bindNearby(item, seed);
    showNearbyOnMap(seed);
  } else if (isExactPin(item)) {
    box.innerHTML = nearbyHtml(item, [], true);
    poiLayer.clearLayers();
  } else {
    box.innerHTML = nearbyHtml(item, [], false);
    poiLayer.clearLayers();
    return;
  }
  if (!item.city || item.lat == null || item.lon == null || !isExactPin(item)) return;
  try {
    const data = await loadNear(item);
    if (!data || selectedId !== item.id) return;
    const rows = groupNearby(data.nearby || []);
    applyNearToProfile(item, data);
    if (rows.length) {
      box.innerHTML = nearbyHtml(item, rows, data.pending);
      bindNearby(item, rows);
      showNearbyOnMap(rows);
    } else {
      const meters = walkMeters(item, data);
      const msg = data.pending
        ? `Buscando en el mapa lo que está a menos de ${meters} m…`
        : (data.reason || `Nada de esto etiquetado a menos de ${meters} m de este pin.`);
      box.innerHTML = `<div class="near-list"><p class="muted">${escapeHtml(msg)}</p></div>`;
      poiLayer.clearLayers();
    }
    if (data.pending) {
      setTimeout(() => {
        if (selectedId === item.id) fillNearby(item);
      }, 4000);
    }
  } catch (_) {}
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
    <p>${items.length} avisos · elegí uno</p>
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

function bindPickRows(latlng) {
  document.querySelectorAll(".zone-popup .zone-item").forEach((btn) => {
    btn.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const current = allListings.find((x) => x.id === btn.dataset.id);
      if (!current) return;
      selectListing(current, { focusMap: false, at: latlng, keepPopup: true });
      document.querySelectorAll(".zone-popup .zone-item").forEach((el) => {
        el.classList.toggle("is-selected", el.dataset.id === current.id);
      });
    };
  });
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
  copy.sort((a, b) => {
    if (a.favorite !== b.favorite) return a.favorite ? -1 : 1;
    let cmp = 0;
    if (key === "price-asc") cmp = (a.price_usd || 1e15) - (b.price_usd || 1e15);
    else if (key === "price-desc") cmp = (b.price_usd || 0) - (a.price_usd || 0);
    else if (key === "m2") cmp = listingM2(b) - listingM2(a);
    else if (key === "m2price") cmp = (a.price_m2 || 1e15) - (b.price_m2 || 1e15);
    else if (key === "type") cmp = typeLabel(a).localeCompare(typeLabel(b), "es");
    else if (key === "barrio") cmp = (a.barrio || "").localeCompare(b.barrio || "", "es");
    else if (key === "contacted") cmp = Number(Boolean(b.contacted)) - Number(Boolean(a.contacted));
    else if (key === "yield-month") cmp = (b.monthly_yield_pct || 0) - (a.monthly_yield_pct || 0);
    else if (key === "yield-temp") cmp = (b.temporal_yield_pct || 0) - (a.temporal_yield_pct || 0);
    if (cmp) return cmp;
    return (cityDealScore(b) || b.quality_score || b.score || 0) - (cityDealScore(a) || a.quality_score || a.score || 0);
  });
  return copy;
}

function listCardHeight(list) {
  const card = list.querySelector(".card");
  return card ? Math.round(card.getBoundingClientRect().height) + 8 : 118;
}

function ensureListVirtual() {
  const list = $("list");
  if (!list || list._virt) return;
  list._virt = true;
  list.addEventListener("scroll", () => {
    if (list._raf) return;
    list._raf = requestAnimationFrame(() => {
      list._raf = 0;
      paintListWindow();
    });
  }, { passive: true });
}

function paintListWindow(force = false) {
  const list = $("list");
  const items = listWindowItems;
  if (!listingsReady || !list || !items.length) return;
  const h = listCardHeight(list);
  const view = list.clientHeight || 640;
  const start = Math.max(0, Math.floor(list.scrollTop / h) - 6);
  const end = Math.min(items.length, start + Math.ceil(view / h) + 12);
  const key = `${start}:${end}:${items.length}:${scrapeRunning}:${selectedId}`;
  if (!force && key === listWinRange && list.querySelector(".card")) return;
  listWinRange = key;
  const padTop = start * h;
  const padBot = Math.max(0, (items.length - end) * h);
  const banner = scrapeRunning
    ? `<p class="list-banner" role="status">Se actualizan los avisos. Podés filtrar y cambiar de lugar.</p>`
    : "";
  list.innerHTML = `${banner}<div class="list-pad" style="height:${padTop}px" aria-hidden="true"></div>${items.slice(start, end).map(cardHtml).join("")}<div class="list-pad" style="height:${padBot}px" aria-hidden="true"></div>`;
  highlightSelected();
}

function paintList(items) {
  const list = $("list");
  const jumped = listWindowItems[0]?.id !== items[0]?.id || listWindowItems.length !== items.length;
  listWindowItems = items;
  updateSearchingBanner();
  if (!items.length) {
    listWinRange = "";
    if (scrapeRunning || placeIsQueued()) {
      list.innerHTML = listingsWaitHtml(waitCopy("scrape"));
    } else if (allListings.length) {
      list.innerHTML = `<p class="status">Nada con esos filtros.</p>`;
    } else {
      list.innerHTML = `<p class="status">${emptyPlaceCopy()}</p>`;
    }
    return;
  }
  ensureListVirtual();
  if (jumped) list.scrollTop = 0;
  paintListWindow(true);
  if (list.querySelector(".card") && Math.abs(listCardHeight(list) - 118) > 8) {
    paintListWindow(true);
  }
}

function paintMapMarkers(items) {
  const t0 = performance.now();
  const gen = ++mapPaintGen;
  exactCluster.clearLayers();
  zoneLayer.clearLayers();
  markersById = {};
  zoneMarkersByKey = {};
  stackMarkers = [];
  const zoneGroups = {};
  const exactMarkers = [];
  groupExactPins(items).forEach((group) => {
    if (gen !== mapPaintGen) return;
    const n = group.items.length;
    const lat = group.lat;
    const lon = group.lon;
    const marker = L.marker([lat, lon], {
      icon: n === 1 ? pinIcon(group.items[0]) : stackIcon(group),
      riseOnHover: true,
    });
    if (n === 1) {
      marker._listing = group.items[0];
      marker.on("click", () => selectListing(group.items[0], { focusMap: false, at: L.latLng(lat, lon) }));
      markersById[group.items[0].id] = marker;
    } else {
      marker._stackGroup = group;
      group.items.forEach((item) => {
        markersById[item.id] = marker;
      });
      marker.on("click", () => openPickPopup(L.latLng(lat, lon), group.items, "Avisos en este punto"));
    }
    stackMarkers.push({ marker, group });
    exactMarkers.push(marker);
  });
  if (gen !== mapPaintGen) return;
  let i = 0;
  const step = 250;
  const addChunk = () => {
    if (gen !== mapPaintGen) return;
    exactCluster.addLayers(exactMarkers.slice(i, i + step));
    i += step;
    if (i < exactMarkers.length) requestAnimationFrame(addChunk);
  };
  addChunk();
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
  const zoneList = Object.values(zoneGroups);
  let z = 0;
  const addZones = () => {
    if (gen !== mapPaintGen) return;
    const slice = zoneList.slice(z, z + 80);
    slice.forEach((group) => {
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
    z += 80;
    if (z < zoneList.length) requestAnimationFrame(addZones);
  };
  addZones();
  markClient("paintMap", performance.now() - t0, { n: items.length });
}

function render() {
  if (!listingsReady) {
    if ($("listCount")) $("listCount").textContent = "…";
    return;
  }
  const items = sorted(filtered());
  $("listCount").textContent = `${items.length}`;
  renderKpisFromItems(items);
  paintList(items);
  const overlay = $("mapLoading");
  if (overlay) {
    const keep = (scrapeRunning || placeIsQueued()) && !items.length;
    overlay.hidden = !keep;
    if (keep) {
      const line = overlay.querySelector("[data-loading-copy]");
      if (line) line.textContent = waitCopy("scrape");
    }
  }
  bindListEvents();
  highlightSelected();
  if (selectedId) {
    const current = allListings.find((x) => x.id === selectedId);
    if (current) {
      showDetail(withCachedFicha(current));
      fillListingFicha(current);
    }
  }
  requestAnimationFrame(() => paintMapMarkers(items));
  renderStats();
}

function selectListing(item, { focusMap = true, at = null, keepPopup = false } = {}) {
  if (!item) return;
  if (window.innerWidth <= 980) setTab("detail");
  track("listing", { listing: item.id });
  selectedId = item.id;
  const list = $("list");
  const idx = listWindowItems.findIndex((x) => x.id === item.id);
  if (idx >= 0 && list) {
    list.scrollTop = Math.max(0, idx * listCardHeight(list) - 48);
    paintListWindow(true);
  }
  highlightSelected();
  refreshZoneIcons();
  showDetail(item);
  fillListingFicha(item);
  if (!keepPopup) map.closePopup();
  showApproxBox(item, at || listingPopupLatLng(item));
  if (focusMap) focusListing(item);

  // Highlight the clicked marker with pulse effect
  highlightMarkerPulse(item);
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
  map.closePopup();
  if (approxBox) {
    map.removeLayer(approxBox);
    approxBox = null;
  }
  poiLayer.clearLayers();
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
  if (isLocationMissing(item)) return "Sin ubicación";
  if (isExactPin(item)) {
    if (item.location_kind === "intersection" || (hasRealIntersection(item) && !hasStreetNumber(item))) {
      return "Ubicación real · intersección";
    }
    if (hasStreetNumber(item)) return "Ubicación real · calle y altura";
    if (streetAddress(item)) return "Ubicación real · calle";
    return "Ubicación real";
  }
  if (item.location_kind === "intersection" || (hasRealIntersection(item) && !item.street_number)) {
    return "Intersección aproximada";
  }
  if (item.lat == null || item.lon == null) return "Sin punto en el mapa";
  return "Aproximada (manzana del aviso)";
}

function locationLine(item) {
  const address = streetAddress(item);
  const parts = [];
  if (address) parts.push(address);
  else if (item.approx_address) parts.push(item.approx_address);
  if (item.intersection && hasRealIntersection(item)) parts.push(`Intersección ${item.intersection}`);
  if (item.between) parts.push(`Entre ${item.between}`);
  if (isLocationMissing(item)) parts.push("sin ubicación");
  else if (!isExactPin(item) && item.lat != null) parts.push("ubicación aprox.");
  return parts.join(" · ") || locationKindLabel(item);
}

function isGoodCondition(item) {
  const cond = String(item.condition || "").trim().toLowerCase();
  return cond === "a estrenar" || cond === "reciclado";
}

function listingKindFlags(item) {
  const flags = [];
  if (item.mortgage_credit === true) flags.push(["credit", "Apto crédito"]);
  if (item.owner_direct === true) flags.push(["owner", "Dueño directo"]);
  if (item.urgent_sale === true) flags.push(["urgent", "Venta urgente"]);
  if (item.has_balcony === true) flags.push(["", "Balcón"]);
  if (item.bright === true) flags.push(["", "Luminoso"]);
  if (item.growing_area === true) flags.push(["", "En crecimiento"]);
  if (item.open_view === true) flags.push(["", "Vista abierta"]);
  if (item.has_patio === true) flags.push(["", "Patio"]);
  if (item.has_garage === true) flags.push(["", "Cochera"]);
  if (item.has_terrace === true) flags.push(["", "Terraza"]);
  if (isLocationMissing(item)) flags.push(["lost", "Sin ubicación"]);
  else if (isExactPin(item)) flags.push(["real", "Ubicación real"]);
  return flags;
}

function listingTraits(item) {
  const out = [];
  if (item.mortgage_credit === true) out.push("apto crédito");
  if (item.owner_direct === true) out.push("dueño directo");
  if (item.low_expenses === true) out.push("expensas bajas");
  if (item.urgent_sale === true) out.push("venta urgente");
  if (item.environment === "quiet") out.push("tranquilo");
  if (item.has_balcony === true) out.push("balcón");
  if (item.bright === true) out.push("luminoso");
  if (item.growing_area === true) out.push("en crecimiento");
  if (item.open_view === true) out.push("vista abierta");
  if (item.has_patio === true) out.push("patio");
  if (item.has_garage === true) out.push("cochera");
  if (item.has_terrace === true) out.push("terraza");
  const cond = String(item.condition || "").trim().toLowerCase();
  if (cond === "a estrenar" || cond === "reciclado" || cond === "en pozo" || cond === "a reciclar") {
    out.push(cond);
  }
  return out;
}

function listingTags(item) {
  const seen = new Set();
  const out = [];
  for (const raw of [...listingTraits(item), ...(item.place_tags || []), ...(item.tags || []), ...(item.amenities || [])]) {
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

function rentMoney(usd, suffix) {
  const dollars = `USD ${fmt(usd)}${suffix}`;
  if (!usdArs || usdArs < 100) return dollars;
  return `${dollars} · ~$${fmt(usd * usdArs)}`;
}

function rentStrip(item) {
  if (item.monthly_yield_pct == null && item.temporal_yield_pct == null) return "";
  const rows = [];
  if (item.monthly_rent_usd) {
    rows.push(`<span>Contrato <b>${pct(item.monthly_yield_pct)}</b> · ${rentMoney(item.monthly_rent_usd, "/mes")}</span>`);
  }
  if (item.nightly_usd) {
    rows.push(`<span>Temporal <b>${pct(item.temporal_yield_pct)}</b> · ${rentMoney(item.nightly_usd, "/noche")}</span>`);
  }
  return rows.length ? `<div class="rent-strip">${rows.join("")}</div>` : "";
}

function rentEstimate(item) {
  if (item.monthly_yield_pct == null && item.temporal_yield_pct == null) return "";
  const range = item.monthly_rent_lo && item.monthly_rent_hi
    ? `<small>rango USD ${fmt(item.monthly_rent_lo)}–${fmt(item.monthly_rent_hi)}/mes${item.rent_confidence ? ` · confianza ${escapeHtml(item.rent_confidence)}` : ""}</small>`
    : "";
  const month = item.monthly_rent_usd
    ? `<div class="rent-row"><span>Con contrato</span><b>${rentMoney(item.monthly_rent_usd, "/mes")} · ${pct(item.monthly_yield_pct)} anual</b>${range}<small>${escapeHtml(item.rental_month_scope || "según la ciudad")}${item.rental_month_n ? ` · ${item.rental_month_n} avisos` : ""}</small></div>`
    : `<div class="rent-row"><span>Con contrato</span><b>sin comps aún</b></div>`;
  const night = item.nightly_usd
    ? `<div class="rent-row"><span>Temporal</span><b>${rentMoney(item.nightly_usd, "/noche")} · ${pct(item.temporal_yield_pct)} anual</b><small>${escapeHtml(item.rental_night_scope || "según la ciudad")}${item.occupancy_pct ? ` · ocupación ${item.occupancy_pct}%` : ""}${item.rental_night_n ? ` · ${item.rental_night_n} avisos` : ""}</small></div>`
    : `<div class="rent-row"><span>Temporal</span><b>sin comps aún</b></div>`;
  return `<div class="rent-estimate">
    ${month}
    ${night}
    <p class="muted">Estimación por comparables de alquiler de la zona, mismo tipo y tamaño. El % es neto. Los pesos usan el dólar blue del momento.</p>
  </div>`;
}

const CHART_AXES = ["price_m2", "zona", "ambientes", "alquiler", "servicios"];
const LOT_AXES = ["price_m2", "zona", "servicios"];
const CHART_LABELS = {
  price_m2: "USD/m²", zona: "Zona", ambientes: "Ambientes", alquiler: "Alquiler", servicios: "POIs cercanos",
};

function chartOrder(profile) {
  const order = profile?.order;
  if (Array.isArray(order) && order.length >= 3) return order;
  return CHART_AXES;
}

function stampProfile(profile) {
  if (!profile) return profile;
  const order = chartOrder(profile);
  profile.order = order.slice();
  profile.labels = Object.fromEntries(order.map((key) => [key, CHART_LABELS[key] || key]));
  const scores = [];
  const pending = [];
  order.forEach((key) => {
    const axis = profile.axes?.[key] || {};
    if (axis.score == null) {
      const note = String(axis.note || "todavía no se calculó");
      const blocked = axis.confidence === "none" || /faltan m²|ubicación exacta|pin aproximado/i.test(note);
      pending.push({
        key,
        label: profile.labels[key] || key,
        note,
        state: blocked ? "blocked" : "pending",
      });
    } else {
      scores.push(Number(axis.score));
    }
  });
  profile.total = scores.length ? Math.round((scores.reduce((a, n) => a + n, 0) / scores.length) * 10) / 10 : null;
  profile.total_n = scores.length;
  profile.pending = pending;
  return profile;
}

function pentagonChart(profile, { mini = false, kind = "" } = {}) {
  profile = profile && typeof profile === "object" ? profile : {};
  if (!profile.axes) profile.axes = {};
  if (kind === "terreno") profile.order = LOT_AXES.slice();
  else if (kind) profile.order = CHART_AXES.slice();
  stampProfile(profile);
  const order = chartOrder(profile);
  const labels = profile.labels || CHART_LABELS;
  const n = order.length;
  const w = mini ? 84 : (n === 3 ? 320 : 280);
  const h = mini ? 84 : (n === 3 ? 248 : 268);
  const cx = w / 2;
  const cy = mini ? h / 2 : (n === 3 ? 112 : 128);
  const r = mini ? 30 : (n === 3 ? 70 : 78);
  const labelR = n === 3 ? r + 24 : r + 28;
  const angle = (i) => -Math.PI / 2 + (i * 2 * Math.PI) / n;
  const xy = (i, rr) => [cx + Math.cos(angle(i)) * rr, cy + Math.sin(angle(i)) * rr];
  const ring = (frac) => order.map((_, i) => xy(i, r * frac).map((n) => n.toFixed(1)).join(",")).join(" ");
  const valuePts = [];
  const spokes = [];
  const caps = [];
  const dots = [];
  order.forEach((key, i) => {
    const axis = profile.axes[key] || {};
    const missing = axis.score == null;
    const score = missing ? 0 : Number(axis.score);
    const [x, y] = xy(i, r * (score / 100));
    valuePts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
    const [ex, ey] = xy(i, r);
    spokes.push(`<line class="${missing ? "is-missing" : ""}" x1="${cx}" y1="${cy}" x2="${ex.toFixed(1)}" y2="${ey.toFixed(1)}" />`);
    if (!missing) {
      dots.push(`<circle class="radar-dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${mini ? 2 : 3.2}" />`);
    } else if (!mini) {
      dots.push(`<circle class="radar-miss" cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="3" />`);
    }
    if (!mini) {
      const [lx, ly] = xy(i, labelR);
      const muted = missing || axis.confidence === "none" ? " is-missing" : "";
      const anchor = Math.abs(lx - cx) < 14 ? "middle" : (lx < cx ? "end" : "start");
      const words = String(labels[key] || key).split(/\s+/);
      if (words.length > 1) {
        caps.push(
          `<text class="radar-cap${muted}" x="${lx.toFixed(1)}" y="${(ly - 4).toFixed(1)}" text-anchor="${anchor}">`
          + `<tspan x="${lx.toFixed(1)}" dy="0">${escapeHtml(words[0])}</tspan>`
          + `<tspan x="${lx.toFixed(1)}" dy="10">${escapeHtml(words.slice(1).join(" "))}</tspan>`
          + `</text>`
        );
      } else {
        caps.push(`<text class="radar-cap${muted}" x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="${anchor}">${escapeHtml(labels[key] || key)}</text>`);
      }
    }
  });
  const total = profile.total;
  const totalLabel = total == null ? "—" : String(Math.round(total));
  const collapsed = valuePts.every((pt) => {
    const [px, py] = pt.split(",").map(Number);
    return Math.hypot(px - cx, py - cy) < 4;
  });
  const fill = collapsed ? "" : `<polygon class="radar-fill" points="${valuePts.join(" ")}" />`;
  const pendingLine = mini || !(profile.pending || []).length
    ? ""
    : `<p class="radar-pending">${(profile.pending || []).map((row) => {
      const tag = row.state === "pending" ? "calculando" : "falta";
      return `${escapeHtml(row.label)}: ${tag}${row.note ? ` · ${escapeHtml(row.note)}` : ""}`;
    }).join(" · ")}</p>`;
  const notes = mini ? "" : `<ul class="radar-notes">${order.map((key) => {
    const axis = profile.axes[key] || {};
    const missing = axis.score == null;
    const val = missing ? "—" : Math.round(axis.score);
    const hint = missing
      ? (axis.note || "todavía no se calculó")
      : (axis.note || axis.confidence || "");
    return `<li class="${missing ? "is-missing" : ""}"><b>${escapeHtml(labels[key] || key)}</b> ${val}<small>${escapeHtml(hint)}</small></li>`;
  }).join("")}</ul>`;
  const pin = profile.pin_grade === "exact" || profile.pin_grade === "intersection"
    ? "ubicación exacta"
    : profile.pin_grade === "approx" ? "pin aproximado · zona y POIs cercanos en pausa" : "sin pin";
  return `<div class="radar-card${mini ? " is-mini" : ""}">
    ${mini ? "" : `<h4>Perfil del aviso</h4><p class="muted">${escapeHtml(pin)}</p>`}
    <div class="radar-plot">
    <svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${n === 3 ? "Triángulo" : "Pentágono"} de scores, total ${totalLabel}">
      <polygon class="radar-grid" points="${ring(1)}" />
      <polygon class="radar-grid" points="${ring(0.7)}" />
      <polygon class="radar-grid" points="${ring(0.4)}" />
      <g class="radar-spokes">${spokes.join("")}</g>
      ${fill}
      ${dots.join("")}
      ${caps.join("")}
    </svg>
    <span class="radar-total">${totalLabel}</span>
    </div>
    ${pendingLine}
    ${notes}
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
  const amb = ambientesOf(item);
  if (amb) bits.unshift(`${amb} amb`);
  if (item.price_m2) bits.push(`USD ${fmt(item.price_m2)}/m²`);
  return bits;
}
function cardHtml(item) {
  const img = listingImage(item.image, "thumb");
  const thumb = img
    ? `<img src="${img}" alt="${escapeHtml(item.title || "Aviso en venta")}" width="86" height="74" loading="lazy" decoding="async" referrerpolicy="no-referrer" />`
    : `<span class="card-ph" aria-hidden="true"></span>`;
  const kind = typeLabel(item);
  const bits = sizeBits(item);
  const chips = listingTags(item).slice(0, 4).map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join("");
  const selected = item.id === selectedId ? " is-selected" : "";
  const contacted = item.contacted ? " is-contacted" : "";
  const approx = isExactPin(item) ? "" : isLocationMissing(item) ? " is-noloc" : " is-approx";
  const place = streetAddress(item) || (isLocationMissing(item) ? "Sin ubicación" : (item.approx_address || item.barrio || ""));
  const crossing = item.intersection ? ` · intersección ${item.intersection}` : "";
  const between = item.between ? ` · entre ${item.between}` : "";
  const locBit = isExactPin(item) ? " · ubicación real" : isLocationMissing(item) ? " · sin ubicación" : " · ubicación aprox.";
  const dealNote = (item.deal_label || "").startsWith("revisar") ? ` · ${escapeHtml(item.deal_label)}` : "";
  return `<article class="card${item.favorite ? " is-fav" : ""}${selected}${contacted}${approx}" id="card-${cssId(item.id)}" data-id="${item.id}">
    <div class="card-left">
      ${thumb}
      ${pentagonChart(item.profile, { mini: true, kind: item.property_type })}
      ${chips ? `<div class="chips card-chips">${chips}</div>` : ""}
    </div>
    <div class="card-body">
      <div class="card-top">
        <span class="kind ${cssId(item.property_type)}">${escapeHtml(kind)}</span>
        <div class="price">${money(item)}</div>
        <button class="fav-btn" type="button" data-fav="${item.id}" title="Favorito">${item.favorite ? "\u2605" : "\u2606"}</button>
      </div>
      <div class="headline">${escapeHtml(bits.join(" · ") || item.title || "")}</div>
      <div class="meta">${escapeHtml(place)}${escapeHtml(crossing)}${escapeHtml(between)} · ${escapeHtml(sourceSummary(item))}${locBit}${item.mortgage_credit === true ? " · apto crédito" : ""}${item.owner_direct === true ? " · dueño" : ""}${item.contacted ? " · contactado" : ""}${dealNote}</div>
      ${rentStrip(item)}
    </div>
  </article>`;
}

function bindListEvents() {
  const list = $("list");
  if (!list || list._bound) return;
  list._bound = true;
  list.addEventListener("click", (ev) => {
    const fav = ev.target.closest(".fav-btn");
    if (fav) {
      ev.preventDefault();
      ev.stopPropagation();
      const id = fav.dataset.fav;
      const listing = allListings.find((x) => x.id === id);
      if (listing) savePin(id, { favorite: !listing.favorite });
      return;
    }
    if (ev.target.closest(".note-box")) return;
    const node = ev.target.closest(".card");
    if (!node) return;
    const item = allListings.find((x) => x.id === node.dataset.id);
    if (item) selectListing(item);
  });
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
    if (current) {
      showDetail(withCachedFicha(current));
      fillListingFicha(current);
    }
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

const fichaById = {};
const fichaWait = {};
const fichaMiss = {};

function isFichaReady(item) {
  return Boolean(
    item
    && Object.prototype.hasOwnProperty.call(item, "description")
    && Object.prototype.hasOwnProperty.call(item, "monthly_yield_pct")
  );
}

function withCachedFicha(item) {
  if (!item) return item;
  if (isFichaReady(item)) {
    fichaById[item.id] = item;
    return item;
  }
  const extra = fichaById[item.id];
  return extra ? { ...item, ...extra } : item;
}

function mergeFicha(item, full) {
  if (!item || !full) return item;
  fichaById[item.id] = full;
  const idx = allListings.findIndex((row) => row.id === item.id);
  if (idx >= 0) {
    allListings[idx] = { ...allListings[idx], ...full };
    return allListings[idx];
  }
  return { ...item, ...full };
}

async function hydrateFicha(item) {
  if (!item || !item.id) return item;
  if (isFichaReady(item)) return item;
  if (fichaMiss[item.id]) return item;
  if (fichaById[item.id]) return mergeFicha(item, fichaById[item.id]);
  if (!fichaWait[item.id]) {
    fichaWait[item.id] = fetch(`/api/listing?id=${encodeURIComponent(item.id)}`)
      .then((res) => {
        if (res.status === 404) {
          fichaMiss[item.id] = true;
          return null;
        }
        return res.ok ? res.json() : null;
      })
      .then((data) => data && data.listing)
      .catch(() => null);
  }
  const full = await fichaWait[item.id];
  delete fichaWait[item.id];
  if (!full) return item;
  return mergeFicha(item, full);
}

function fillListingFicha(item) {
  if (!item || isFichaReady(item)) return;
  const id = item.id;
  hydrateFicha(item).then((full) => {
    if (selectedId !== id || !full) return;
    showDetail(full);
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
  const direccion = streetAddress(item) || "—";
  const cityName = (window.lastCities || []).find((c) => c.id === item.city)?.label
    || (item.city || "").replace(/-/g, " ");
  const published = item.published_at
    ? new Date(item.published_at).toLocaleString("es-AR")
    : "—";
  const approxAddr = item.approx_address && item.approx_address !== direccion ? item.approx_address : "";
  const facts = [
    ["Dirección", direccion],
    ["Intersección", (item.intersection || "").trim() || "—"],
    ["Entre calles", item.between || "—"],
    ["Ubicación aproximada", approxAddr || "—"],
    ["Punto en el mapa", locationKindLabel(item)],
    ["Barrio", item.barrio || "—"],
    ["Zona", item.zona || "—"],
    ["Ciudad", cityName],
    ["Tipo", kind],
    ["Precio", money(item)],
    ["Cubiertos", item.covered_m2 ? `${fmt(item.covered_m2)} m²` : "—"],
    ["Terreno / lote", (item.lot_m2 || item.total_m2) ? `${fmt(item.lot_m2 || item.total_m2)} m²` : "—"],
    ["Ambientes", ambientesOf(item) ?? "—"],
    ["Dormitorios", item.bedrooms ?? "—"],
    ["Baños", item.bathrooms ?? "—"],
    ["Antigüedad", item.age_years != null ? `${item.age_years} años` : "—"],
    ["Expensas", item.expenses ? `USD ${fmt(item.expenses)}` : "—"],
    ["USD/m²", item.price_m2 ? `USD ${fmt(item.price_m2)}` : "—"],
    ["Score ganga", cityDealScore(item) ? fmt(cityDealScore(item)) : "—"],
    ["Calidad", item.quality_score != null ? `${fmt(item.quality_score)} · ${item.quality_label || ""}` : "—"],
    ["Apto crédito", item.mortgage_credit === true ? "Sí" : item.mortgage_credit === false ? "No" : "—"],
    ["Dueño directo", item.owner_direct === true ? "Sí" : "—"],
    ["Expensas bajas", item.low_expenses === true ? "Sí" : "—"],
    ["Venta urgente", item.urgent_sale === true ? "Sí" : "—"],
    ["Entorno", item.environment === "quiet" ? "Tranquilo (lo dice el aviso)" : "—"],
    ["Balcón", item.has_balcony === true ? "Sí" : "—"],
    ["Luminoso", item.bright === true ? "Sí" : "—"],
    ["Barrio en crecimiento", item.growing_area === true ? "Sí" : "—"],
    ["Vista abierta", item.open_view === true ? "Sí" : "—"],
    ["Patio", item.has_patio === true ? "Sí" : "—"],
    ["Cochera", item.has_garage === true || item.parking ? "Sí" : "—"],
    ["Terraza", item.has_terrace === true ? "Sí" : "—"],
    ["Piso", item.floor ?? "—"],
    ["Orientación", item.orientation || "—"],
    ["Estado", item.condition || "—"],
    ["Publicado", published],
    ["Inmobiliaria", item.publisher || "—"],
    ["Fuente", sourceSummary(item)],
  ].filter(([k, v]) => {
    if (item.property_type === "terreno" && ["Ambientes", "Dormitorios", "Baños"].includes(k)) return false;
    if (["Ubicación aproximada", "Entre calles", "Dueño directo", "Expensas bajas", "Venta urgente", "Entorno", "Balcón", "Luminoso", "Barrio en crecimiento", "Vista abierta", "Patio", "Cochera", "Terraza"].includes(k) && (v === "—" || !v)) return false;
    return true;
  }).map(([k, v]) => `<div class="fact"><span>${escapeHtml(String(k))}</span><b>${escapeHtml(String(v))}</b></div>`).join("");
  const photos = photoSet(item);
  const hero = listingImage(photos[0], "hero");
  const film = photos.length > 1
    ? `<div class="detail-film">${photos.map((url, i) => {
        const thumb = listingImage(url, "thumb");
        const full = listingImage(url, "hero");
        return `<button type="button" class="${i === 0 ? "is-on" : ""}" data-photo="${full}" aria-label="Foto ${i + 1}"><img src="${thumb}" alt="" width="64" height="48" loading="lazy" decoding="async" referrerpolicy="no-referrer" /></button>`;
      }).join("")}</div>`
    : "";
  const missingMark = isLocationMissing(item)
    ? `<p class="loc-missing">Sin dirección, intersección ni ubicación aproximada del aviso.</p>`
    : "";
  const flags = listingKindFlags(item).map(([cls, label]) => (
    `<span class="kind ${cls}">${escapeHtml(label)}</span>`
  )).join("");
  pane.innerHTML = `
    <button type="button" class="ghost detail-close" id="closeDetail">cerrar</button>
    ${hero ? `<figure class="detail-photos"><img class="detail-hero" src="${hero}" alt="${escapeHtml(item.title || "Foto del aviso")}" width="640" height="420" decoding="async" referrerpolicy="no-referrer" />${film}</figure>` : ""}
    <div class="detail-head">
      <div class="detail-kicker">
        <span class="kind ${cssId(item.property_type)}">${escapeHtml(kind)}</span>
        <strong class="detail-price">${money(item)}</strong>
      </div>
      ${flags ? `<div class="detail-flags">${flags}</div>` : ""}
    </div>
    <h3>${escapeHtml(kind)} · ${escapeHtml(item.title)}</h3>
    <p class="meta">${escapeHtml(locLine)} · ${escapeHtml(sourceSummary(item))}</p>
    ${missingMark}
    ${sourceLinkHtml(item, "detail-link") || "<p class='muted'>Sin link al aviso original</p>"}
    <div class="ficha-bundle">
      ${pentagonChart(item.profile, { kind: item.property_type })}
      <details class="fold ficha-fold" open>
        <summary>Ficha</summary>
        <div class="facts">${facts}</div>
        <div class="price-track" id="priceTrack"><p class="muted">Cargando historial de precio…</p></div>
        <p>${escapeHtml(item.deal_label || "")}${item.vs_barrio_pct != null ? ` · ${item.vs_barrio_pct > 0 ? "-" : "+"}${Math.abs(item.vs_barrio_pct)}% vs barrio` : ""}</p>
        ${reasons ? `<div class="chips">${reasons}</div>` : ""}
        ${fixes ? `<div class="chips">${fixes}</div>` : ""}
        ${chips ? `<div class="chips">${chips}</div>` : ""}
      </details>
    </div>
    ${item.property_type === "terreno" ? "" : `<details class="fold" open>
      <summary>Cuánto podría alquilar</summary>
      ${rentEstimate(item) || "<p class='muted'>Sin estimación de alquiler para este aviso.</p>"}
    </details>`}
    <details class="fold near-fold">
      <summary>Cerca en el mapa</summary>
      <div id="nearBox">${nearbyHtml(item, groupNearby(item.nearby || []), isExactPin(item) && !(item.nearby || []).length)}</div>
    </details>
    <details class="fold" open>
      <summary>Descripción</summary>
      <p class="desc">${escapeHtml(item.description || "Sin descripción todavía. Tocá buscar avisos para leer la ficha completa.")}</p>
    </details>
    ${currentUser ? `
    <details class="fold" open>
      <summary>Notas y correcciones</summary>
      <form class="edit-form" id="editForm">
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
    </details>
    ` : ""}
  `;
  $("closeDetail")?.addEventListener("click", closeDetail);
  pane.querySelectorAll(".detail-film button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const heroEl = pane.querySelector(".detail-hero");
      if (!heroEl || !btn.dataset.photo) return;
      heroEl.hidden = false;
      heroEl.src = btn.dataset.photo;
      pane.querySelectorAll(".detail-film button").forEach((el) => el.classList.toggle("is-on", el === btn));
    });
  });
  document.querySelector(".near-fold")?.addEventListener("toggle", () => {
    if (selectedId !== item.id) return;
    if (document.querySelector(".near-fold")?.open) fillNearby(item);
    else poiLayer.clearLayers();
  });
  fillNearby(item);
  $("saveEdits")?.addEventListener("click", async () => {
    const payload = {
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

function quantile(sorted, p) {
  const i = (sorted.length - 1) * p;
  const lo = Math.floor(i);
  const hi = Math.ceil(i);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
}

function medianOf(values) {
  let nums = values.filter((n) => Number.isFinite(n) && n > 0).sort((a, b) => a - b);
  if (nums.length >= 6) {
    const q1 = quantile(nums, 0.25);
    const q3 = quantile(nums, 0.75);
    const iqr = q3 - q1;
    if (iqr > 0) {
      const lo = q1 - 1.5 * iqr;
      const hi = q3 + 1.5 * iqr;
      const trimmed = nums.filter((n) => n >= lo && n <= hi);
      if (trimmed.length >= 3) nums = trimmed;
    }
  }
  if (nums.length >= 8) {
    const lo = Math.floor(nums.length * 0.15);
    const hi = Math.floor(nums.length * 0.85) || nums.length;
    const cut = nums.slice(lo, hi);
    if (cut.length) nums = cut;
  }
  if (!nums.length) return 0;
  const mid = Math.floor(nums.length / 2);
  return nums.length % 2 ? nums[mid] : (nums[mid - 1] + nums[mid]) / 2;
}

function averageTitle() {
  const names = {
    casa: "casas",
    departamento: "departamentos",
    ph: "PH",
    terreno: "terrenos",
    local: "locales",
    oficina: "oficinas",
    galpon: "galpones",
  };
  const type = names[$("typeFilter")?.value || ""];
  return type ? `Promedios por barrio · ${type}` : "Promedios por barrio";
}

function forBarrioAverage(item, traits) {
  // Precio máximo, ganga y favoritos recortan la muestra que el promedio está midiendo.
  if (item.is_outlier || item.exclude_from_comps) return false;
  const city = currentCity();
  const type = $("typeFilter")?.value || "";
  const zona = $("zonaFilter")?.value || "";
  const barrio = $("barrioFilter")?.value || "";
  if (city && !belongsToCity(item, city)) return false;
  if (type && item.property_type !== type) return false;
  if (zona && item.zona !== zona) return false;
  if (barrio && item.barrio !== barrio) return false;
  if (!passesTraits(item, traits)) return false;
  if (!passesSize(item)) return false;
  return true;
}

function barrioAverages() {
  const traits = selectedTraits();
  const groups = new Map();
  for (const item of allListings) {
    if (!forBarrioAverage(item, traits)) continue;
    const name = item.barrio || "Sin clasificar";
    let group = groups.get(name);
    if (!group) {
      group = { name, count: 0, usd: [], m2: [] };
      groups.set(name, group);
    }
    group.count += 1;
    const usd = Number(item.price_usd);
    const m2 = Number(item.price_m2);
    if (usd > 0) group.usd.push(usd);
    if (m2 > 0) group.m2.push(m2);
  }
  return [...groups.values()]
    .filter((group) => group.count)
    .map((group) => ({
      name: group.name,
      count: group.count,
      median_usd: Math.round(medianOf(group.usd) || 0),
      median_m2: Math.round(medianOf(group.m2) || 0),
    }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name, "es"));
}

function renderStats(stats) {
  const body = $("statsBody");
  if (!body) return;
  const head = document.querySelector("#statsCard .fold-head");
  if (head) head.textContent = averageTitle();
  let rows;
  if (listingsReady && allListings.length) {
    rows = barrioAverages();
  } else {
    const city = currentCity();
    rows = ((stats || window.lastStats || {}).by_barrio || []).filter((r) => r.count && (!r.city || r.city === city));
  }
  body.innerHTML = rows.map((r) => `<tr>
    <td>${escapeHtml(r.name)}</td>
    <td>${r.median_usd ? fmt(r.median_usd) : "—"}</td>
    <td>${r.median_m2 ? fmt(r.median_m2) : "—"}</td>
  </tr>`).join("");
}

async function loadMarket(force) {
  const city = currentCity();
  const type = $("typeFilter")?.value || "";
  const key = `${city}|${type}`;
  if (!force && key === lastMarketKey) return;
  if (marketInFlight === key) return;
  marketInFlight = key;
  let data;
  try {
    data = await (await timedFetch(`/api/market?city=${encodeURIComponent(city)}&type=${encodeURIComponent(type)}`, "market")).json();
  } catch {
    if (marketInFlight === key) marketInFlight = "";
    return;
  }
  lastMarketKey = key;
  if (marketInFlight === key) marketInFlight = "";
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
  const yOf = (v) => (max === min ? h / 2 : h - 18 - ((v - min) / span) * (h - 28));
  const pts = nums.map((v, i) => {
    const x = nums.length < 2 ? w / 2 : 8 + (i / Math.max(nums.length - 1, 1)) * (w - 16);
    return `${x.toFixed(1)},${yOf(v).toFixed(1)}`;
  });
  const last = nums[nums.length - 1];
  const first = nums[0];
  const rising = last > first * 1.008;
  const color = rising ? (upIsBad ? "#c45c3a" : "#1f7a4a") : (upIsBad ? "#1f7a4a" : "#c45c3a");
  const firstPt = pts[0].split(",");
  const lastPt = pts[pts.length - 1].split(",");
  const labelY = (y) => Math.min(h - 8, Math.max(12, Number(y) - 8));
  const same = nums.length < 2 || Math.abs(last - first) < 1;
  const startLabel = `<text class="axis" x="${same ? w / 2 : 8}" y="${labelY(firstPt[1])}"${same ? ' text-anchor="middle"' : ""}>USD ${fmt(first)}</text>`;
  const endLabel = same
    ? ""
    : `<text class="axis" x="${w - 8}" y="${labelY(lastPt[1])}" text-anchor="end">USD ${fmt(last)}</text>`;
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Evolución del precio en USD">
    <polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round" />
    <circle cx="${firstPt[0]}" cy="${firstPt[1]}" r="2.4" fill="${color}" />
    <circle cx="${lastPt[0]}" cy="${lastPt[1]}" r="3.2" fill="${color}" />
    ${startLabel}${endLabel}
  </svg>`;
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
    : `<p class="muted">${data.history_n
      ? "Todavía no vimos un recorte de precio en esta vista. El pulso guarda cada baja desde que hay dos lecturas del mismo aviso."
      : "Cuando un aviso baje de precio, aparece acá."}</p>`;
  const deals = data.new_deals || [];
  $("marketDeals").innerHTML = deals.length
    ? deals.map((item) => marketRow(item, `${item.vs_barrio_pct != null ? `-${Math.abs(item.vs_barrio_pct)}% vs zona` : item.deal_label} · ${item.barrio || ""}`)).join("")
    : `<p class="muted">${(data.mix && data.mix.oportunidad)
      ? "Hay oportunidades en el mix, pero ninguna entra en esta lista todavía."
      : "No hay avisos claramente más baratos que la zona en esta vista."}</p>`;
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
  const usable = points.filter((p) => p.price_usd && !p.outlier);
  const chart = sparkline((usable.length ? usable : points).map((p) => p.price_usd), { upIsBad: true });
  const rows = points.map((p) => {
    const when = p.seen_at ? new Date(p.seen_at).toLocaleString("es-AR", { day: "numeric", month: "short" }) : "";
    const m2 = p.price_m2 && !p.outlier ? ` · USD ${fmt(p.price_m2)}/m²` : "";
    const odd = p.outlier ? ` class="is-outlier"` : "";
    const note = p.outlier ? ` <em>dato raro</em>` : "";
    return `<li${odd}>${when} · USD ${fmt(p.price_usd || 0)}${m2}${note}</li>`;
  }).join("");
  const change = data.change_pct == null
    ? (data.outlier_n
      ? "Hay un precio disparatado en el historial; no lo usamos para la variación."
      : "Un solo registro por ahora.")
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

function photoSet(item) {
  const urls = [];
  const push = (url) => {
    const href = String(url || "").trim();
    if (href && !urls.includes(href)) urls.push(href);
  };
  push(item?.image);
  for (const url of item?.photos || []) push(url);
  return urls.slice(0, 4);
}

function sizedPhoto(url, role) {
  if (!url) return "";
  let href = String(url).trim().replace(/^http:\/\//i, "https://");
  const thumb = role === "thumb";
  if (/zonapropcdn\.com/i.test(href)) {
    const size = thumb ? "360x266" : "720x532";
    return href.replace(/\/\d{2,4}x\d{2,4}\//, `/${size}/`);
  }
  if (/mlstatic\.com/i.test(href)) {
    const next = thumb ? "-I.webp" : "-O.jpg";
    return href.replace(/-[A-Z](\.[a-z0-9]+)(?=$|\?)/i, next);
  }
  if (/img\.properati\.com\//i.test(href)) {
    return properatiSize(href, thumb ? [160, 120] : [640, 480]);
  }
  if (/argenprop\.com/i.test(href) && !thumb) {
    return href.replace("_u_small", "_u_medium");
  }
  return href;
}

function properatiSize(href, size) {
  try {
    const token = href.split("/").pop().split("?")[0];
    const pad = token + "=".repeat((4 - (token.length % 4)) % 4);
    const payload = JSON.parse(atob(pad.replace(/-/g, "+").replace(/_/g, "/")));
    payload.edits = payload.edits || {};
    payload.edits.resize = { width: size[0], height: size[1], fit: "cover" };
    const encoded = btoa(JSON.stringify(payload)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    return `https://img.properati.com/${encoded}`;
  } catch {
    return href;
  }
}

function listingImage(url, role) {
  const href = sizedPhoto(url, role || "hero");
  return href ? escapeHtml(href) : "";
}

document.addEventListener("error", (ev) => {
  const img = ev.target;
  if (!(img instanceof HTMLImageElement)) return;
  if (img.classList.contains("detail-hero")) {
    img.closest(".detail-photos")?.remove();
    return;
  }
  if (img.closest(".detail-film")) {
    img.closest("button")?.remove();
    return;
  }
  if (img.closest(".card")) {
    const ph = document.createElement("span");
    ph.className = "card-ph";
    ph.setAttribute("aria-hidden", "true");
    img.replaceWith(ph);
  }
}, true);

function cssId(value) {
  return String(value || "").replace(/[^a-z0-9]+/gi, "-");
}

["typeFilter", "zonaFilter", "barrioFilter", "maxPrice", "minM2", "maxM2", "minBeds", "minRooms", "minBaths", "favOnly", "dealBar", "sortBy"].forEach((id) => {
  if (!$(id)) return;
  $(id).addEventListener("input", () => {
    render();
  });
  $(id).addEventListener("change", () => {
    if (id === "typeFilter") syncRoomFilters();
    render();
    if (id === "typeFilter") loadMarket();
  });
});
syncRoomFilters();

document.querySelectorAll('input[name="listingTrait"]').forEach((el) => {
  el.addEventListener("change", () => {
    render();
  });
});

let ignoreCityChange = false;

$("cityFilter")?.addEventListener("change", () => {
  if (ignoreCityChange) return;
  const next = $("cityFilter").value;
  localStorage.setItem("propmap.city", next);
  if ($("placeQuery")) {
    const opt = $("cityFilter").selectedOptions[0];
    $("placeQuery").value = opt ? opt.textContent : "";
  }
  pickedPlace = placeFromCityFilter();
  rememberCityView(pickedPlace);
  focusedCity = next;
  focusCity(next);
  clearLoadedListings();
  load({ waitKind: "city" });
});

$("dealBar")?.addEventListener("input", () => {
  const bar = $("dealBar");
  const v = Number(bar?.value || 0);
  if (!bar) return;
  bar.title = v <= 0 ? "Sin filtro de ganga" : `Ganga en esta ciudad · score ≥ ${v}`;
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
  if (query && !placeIsPicked()) {
    $("statusLine").textContent = "Elegí una ciudad de las sugerencias. No se busca texto libre.";
    if (placeHits.length) showSuggest(placeHits);
    else $("placeQuery")?.focus();
    return;
  }
  const city = pickedPlace?.id || currentCity();
  $("refreshBtn").disabled = true;
  if ($("pauseBtn")) $("pauseBtn").disabled = false;
  hideSuggest();
  const body = pickedPlace
    ? {
        city: pickedPlace.id,
        query: pickedPlace.label || pickedPlace.id,
        label: pickedPlace.label,
        lat: pickedPlace.lat,
        lon: pickedPlace.lon,
        province: pickedPlace.province,
        password,
      }
    : { city, password };
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
  if (res.status === 400) {
    $("refreshBtn").disabled = false;
    const detail = typeof data.detail === "string" ? data.detail : "Elegí una ciudad de las sugerencias.";
    $("statusLine").textContent = detail.charAt(0).toUpperCase() + detail.slice(1);
    return;
  }
  if (data.place) rememberPlace(data.place);
  const label = data.place?.label || data.city || "ese lugar";
  $("statusLine").textContent = emptyPlaceCopy(label, data.eta_min);
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

function emptyPlaceCopy(label) {
  const name = (label || $("cityFilter")?.selectedOptions?.[0]?.textContent || "este lugar").trim();
  if (scrapeRunning || placeIsQueued()) {
    return `Todavía no hay avisos en ${name}. Se van a ir actualizando más adelante.`;
  }
  return `Todavía no hay avisos en ${name}.`;
}

function publicStatusLine(s) {
  const label = $("cityFilter")?.selectedOptions?.[0]?.textContent || "";
  if (!allListings.length) return emptyPlaceCopy(label);
  const when = s?.last_run || lastStatus.last_run;
  if (when) return `Última búsqueda: ${new Date(when).toLocaleString("es-AR")}`;
  return "Los avisos se van a ir actualizando.";
}

function backgroundStatus(s) {
  return publicStatusLine(s);
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
  box.textContent = emptyPlaceCopy(label);
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
  clearTimeout(pollTimer);
  const hurry = scrapeRunning || !allListings.length;
  pollTimer = setTimeout(() => pollStatus(), hurry ? 8000 : 10000);
}

async function pollStatus(force) {
  if (pollBusy && !force) return;
  pollBusy = true;
  let s;
  try {
    const ac = new AbortController();
    const kill = setTimeout(() => ac.abort(), 8000);
    try {
      const res = await timedFetch("/api/status", "status", { signal: ac.signal });
      if (!res.ok) throw new Error(String(res.status));
      s = await res.json();
    } finally {
      clearTimeout(kill);
    }
  } catch {
    pollBusy = false;
    armPoll();
    return;
  }
  if (s.busy && lastStatus) {
    pollBusy = false;
    armPoll();
    return;
  }
  lastStatus = s;
  if (s.city_catalog && s.city_catalog.length) fillCities(s.city_catalog, { source: "catalog" });
  const city = currentCity();
  const fastJobs = fastJobIds(s);
  const running = s.running_cities || [];
  const viewingThis = running.includes(city);
  const scrapeWas = scrapeRunning;
  scrapeRunning = viewingThis || placeIsQueued();
  if (listingsReady && scrapeWas !== scrapeRunning) render();
  if ($("refreshBtn")) $("refreshBtn").disabled = fastJobs.includes(city);
  if ($("pauseBtn")) $("pauseBtn").disabled = !fastJobs.length;
  updateSearchingBanner();
  updatePipelineLine(s);
  const now = Date.now();
  const due = !lastListingsFetchAt || now - lastListingsFetchAt > 8000;
  try {
    if (viewingThis) {
      if (due) await load({ live: true });
      else $("statusLine").textContent = publicStatusLine(s);
    } else if (cityWasRunning[city] || force) {
      await load();
    } else if (due && (s.listings_rev || 0) !== listingsRev) {
      await load({ keepStatus: true, live: true });
    } else {
      $("statusLine").textContent = publicStatusLine(s);
    }
    cityWasRunning[city] = viewingThis;
    wasRunning = viewingThis;
  } finally {
    pollBusy = false;
    armPoll();
  }
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
  if (!view || view.lat == null || view.lon == null || Number.isNaN(Number(view.lat))) return;
  if (Math.abs(Number(view.lat) + 38.4161) < 0.05 && Math.abs(Number(view.lon) + 63.6167) < 0.05) return;
  const run = () => {
    const bounds = viewBounds(view);
    if (bounds) {
      map.fitBounds(bounds, { padding: [28, 28], maxZoom: 14, animate: true });
      return;
    }
    map.setView([view.lat, view.lon], view.zoom || 13, { animate: true });
  };
  if (window.innerWidth <= 980) {
    setTab("map");
    setTimeout(() => {
      map.invalidateSize();
      run();
    }, 80);
    return;
  }
  run();
}

function ensureCityView(cityId) {
  if (!cityId || focusedCity === cityId) return;
  focusedCity = cityId;
  focusCity(cityId);
}

function rememberPlace(place) {
  if (!place || !place.id || !isLocatableCity(place)) return;
  rememberCityView(place);
  const select = $("cityFilter");
  if (!select || [...select.options].some((o) => o.value === place.id)) return;
  if (!cityReadyForCatalog(place)) return;
  const opt = document.createElement("option");
  opt.value = place.id;
  opt.textContent = placeCaption(place);
  select.appendChild(opt);
  window.placeBarrios = window.placeBarrios || {};
  if (place.barrios && place.barrios.length) {
    window.placeBarrios[place.id] = place.barrios.map((b) => b.name);
  }
}

function applyPlace(place) {
  if (!place || !place.id || !isLocatableCity(place)) return;
  pickedPlace = { ...(pickedPlace || {}), ...place };
  rememberPlace(place);
  const select = $("cityFilter");
  if (select) {
    ignoreCityChange = true;
    if ([...select.options].some((o) => o.value === place.id)) select.value = place.id;
    ignoreCityChange = false;
  }
  if ([...($("cityFilter")?.options || [])].some((o) => o.value === place.id)) {
    localStorage.setItem("propmap.city", place.id);
  }
  focusedCity = place.id;
  if ($("placeQuery")) $("placeQuery").value = placeCaption(place);
  focusCity(place.id);
  fillZonas(cityItems());
  fillBarrios();
  if (listingsReady) render();
  else renderStats(window.lastStats || {});
}

let placeTimer = null;
let placeHits = [];
let placeActive = -1;
let pickedPlace = null;
let placeFetchCtrl = null;
let lastRemotePlaces = [];

function placeFromCityFilter() {
  const select = $("cityFilter");
  const id = select?.value;
  if (!id) return null;
  const opt = select.selectedOptions[0];
  const row = (window.lastCities || window.knownCities || []).find((c) => c.id === id) || {};
  return {
    id,
    label: row.label || opt?.textContent || id,
    lat: row.lat,
    lon: row.lon,
    province: row.province,
  };
}

function placeIsPicked() {
  const q = ($("placeQuery")?.value || "").trim();
  if (!q) return true;
  if (!pickedPlace) return false;
  const label = String(pickedPlace.label || "").trim();
  const id = String(pickedPlace.id || "").trim();
  const hint = String(pickedPlace.hint || placeCaption(pickedPlace)).trim();
  return q === label || q === id || q === hint;
}

function revertPlaceQuery() {
  const place = pickedPlace || placeFromCityFilter();
  pickedPlace = place;
  if ($("placeQuery") && place) $("placeQuery").value = placeCaption(place) || place.label || place.id || "";
  hideSuggest();
}

function hideSuggest() {
  const box = $("placeSuggest");
  if (box) {
    box.hidden = true;
    box.innerHTML = "";
  }
  placeHits = [];
  placeActive = -1;
}

function showSuggest(places, opts = {}) {
  const box = $("placeSuggest");
  if (!box) return;
  placeHits = places || [];
  placeActive = placeHits.length ? 0 : -1;
  box.hidden = false;
  if (!placeHits.length) {
    const typed = ($("placeQuery")?.value || "").trim();
    const msg = opts.waiting || typed.length < 4
      ? "Seguí escribiendo para ver ciudad y provincia."
      : "No hay una ciudad con ese nombre. Elegí una sugerencia.";
    box.innerHTML = `<div class="place-suggest-empty">${msg}</div>`;
    return;
  }
  box.innerHTML = placeHits.map((p, i) => `
    <button type="button" data-idx="${i}" class="${i === placeActive ? "is-active" : ""}">
      ${escapeHtml(p.label || p.id)}
      <small>${escapeHtml(placeSuggestSub(p) || placeCaption(p))}</small>
    </button>
  `).join("");
  box.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => choosePlace(placeHits[Number(btn.dataset.idx)]);
  });
}

async function choosePlace(place) {
  if (!place || !place.id) return;
  forgetEmptyPlace(place.id);
  pickedPlace = place;
  hideSuggest();
  lastListingsFp = "";
  allListings = [];
  listingsRev = 0;
  applyPlace(place);
  track("place", { city: place.id });
  if (window.innerWidth <= 980) setTab("map");
  showListingsWait("scrape");
  scrapeRunning = true;
  if ($("statusLine")) {
    $("statusLine").textContent = emptyPlaceCopy(place.label || place.id, 4);
  }
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
      if (data && data.city) {
        pickedPlace = { ...place, ...data.city };
        applyPlace(data.city);
      }
      if ($("statusLine")) {
        $("statusLine").textContent = emptyPlaceCopy(data.city?.label || place.label || place.id, data.eta_min);
      }
      pollStatus(true);
    })
    .catch(() => {});
  await loading;
}

$("placeQuery")?.addEventListener("input", () => {
  const q = ($("placeQuery").value || "").trim();
  if (
    pickedPlace
    && q !== String(pickedPlace.label || "").trim()
    && q !== String(pickedPlace.id || "").trim()
    && q !== String(pickedPlace.hint || placeCaption(pickedPlace)).trim()
  ) {
    pickedPlace = null;
  }
  clearTimeout(placeTimer);
  if (q.length < 2) {
    hideSuggest();
    return;
  }
  const local = localPlaceHits(q);
  const remembered = (lastRemotePlaces || []).filter((p) => placeMatchesQuery(p, q));
  const instant = mergePlaceHits(local, remembered);
  if (instant.length) showSuggest(instant);
  else showSuggest([], { waiting: true });
  placeTimer = setTimeout(async () => {
    placeFetchCtrl?.abort();
    const ac = new AbortController();
    placeFetchCtrl = ac;
    try {
      const res = await fetch(`/api/places?q=${encodeURIComponent(q)}`, { signal: ac.signal });
      if (!res.ok) return;
      const data = await res.json();
      if (($("placeQuery").value || "").trim() !== q) return;
      const remote = data.places || [];
      if (remote.length) lastRemotePlaces = mergePlaceHits(remote, lastRemotePlaces);
      showSuggest(mergePlaceHits(remote, localPlaceHits(q), lastRemotePlaces.filter((p) => placeMatchesQuery(p, q))));
    } catch (err) {
      if (err && err.name === "AbortError") return;
      const fallback = mergePlaceHits(localPlaceHits(q), (lastRemotePlaces || []).filter((p) => placeMatchesQuery(p, q)));
      if (fallback.length) showSuggest(fallback);
    }
  }, 140);
});

$("placeQuery")?.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") {
    revertPlaceQuery();
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
  if (ev.key === "Enter") {
    ev.preventDefault();
    if (placeHits.length && placeActive >= 0) {
      choosePlace(placeHits[placeActive]);
      return;
    }
    if (!placeIsPicked()) {
      $("statusLine").textContent = "Elegí una ciudad de las sugerencias.";
    }
  }
});

$("placeQuery")?.addEventListener("blur", () => {
  setTimeout(() => {
    if (document.activeElement && $("placeSuggest")?.contains(document.activeElement)) return;
    if (!placeIsPicked()) revertPlaceQuery();
  }, 180);
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
    body: JSON.stringify({ password, days: 14, client: clientPerfPayload() }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return;
  $("analyticsBox").hidden = false;
  const days = (data.days || []).map((row) => `${row.day.slice(8)}:${row.visitors}`).join(" · ");
  const places = (data.places || []).slice(0, 8).map((row) => `<div class="stat-row"><span>${row.name}</span><b>${row.count}</b></div>`).join("");
  const refs = (data.referrers || []).slice(0, 8).map((row) => `<div class="stat-row"><span>${labelSource(row.name)}</span><b>${row.count}</b></div>`).join("");
  const devices = (data.devices || []).map((row) => `<div class="stat-row"><span>${row.name}</span><b>${row.count}</b></div>`).join("");
  const nav = performance.getEntriesByType("navigation")[0];
  if (nav) {
    markClient("html", nav.responseEnd);
    markClient("dom", nav.domContentLoadedEventEnd);
    markClient("load", nav.loadEventEnd);
  }
  $("analyticsReadout").innerHTML = `
    <div class="kpis">
      <div class="kpi"><b>${data.visitors || 0}</b><span>visitantes</span></div>
      <div class="kpi"><b>${data.pageviews || 0}</b><span>visitas</span></div>
    </div>
    <p class="muted">${days || "todavía no hay días"} · ${data.events || 0} eventos en 14 días</p>
    <h3>Cuellos de botella</h3>
    ${renderPerf(data.perf, data.client_perf || clientPerfPayload())}
    <h3>De dónde entran</h3>${refs || "<p class='muted'>Todavía no hay orígenes.</p>"}
    <h3>Lugares</h3>${places || "<p class='muted'>Nadie eligió un lugar todavía.</p>"}
    <h3>Dispositivo</h3>${devices || "<p class='muted'>—</p>"}
  `;
}

function renderPerf(server, client) {
  const rows = [];
  const add = (src, bag) => {
    Object.entries(bag || {})
      .sort((a, b) => (b[1].last_ms || b[1].last || 0) - (a[1].last_ms || a[1].last || 0))
      .forEach(([name, row]) => {
        const last = row.last_ms ?? row.last ?? 0;
        const p50 = row.p50_ms ?? 0;
        rows.push(`<div class="stat-row"><span>${src} ${name}</span><b>${last} ms · p50 ${p50} · n ${row.n || 0}</b></div>`);
      });
  };
  add("srv", server);
  add("nav", client);
  if (!rows.length) return "<p class='muted'>Todavía no hay mediciones. Recorré el mapa y actualizá.</p>";
  return `<p class="muted">Última, mediana y cantidad. srv = servidor, nav = este navegador.</p>${rows.join("")}`;
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
  const title = currentUser ? "Menú de la cuenta" : "Entrar o crear cuenta";
  ["accountBtn", "mapAccountBtn"].forEach((id) => {
    const btn = $(id);
    if (!btn) return;
    btn.classList.toggle("is-on", Boolean(currentUser));
    btn.textContent = label;
    btn.title = title;
    if (!currentUser) btn.setAttribute("aria-expanded", "false");
  });
  if (!currentUser) closeAccountMenu();
}

function fillSettings() {
  if (!currentUser) return;
  if ($("setUser")) $("setUser").textContent = currentUser.username;
  if ($("setEmail")) $("setEmail").textContent = currentUser.email_masked;
  if ($("setVerified")) {
    $("setVerified").textContent = currentUser.email_verified ? "Mail validado" : "Pendiente de validación";
  }
  if ($("resendVerify")) $("resendVerify").hidden = Boolean(currentUser.email_verified);
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
      ? "Usuario, mail y contraseña. El mail y los favoritos se cifran. La cuenta no se activa hasta que valides el correo."
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
  closeAccountMenu();
  if (!currentUser) {
    openAuth("login");
    return;
  }
  fillSettings();
  if ($("settingsMsg")) $("settingsMsg").hidden = true;
  $("settingsModal")?.showModal();
}

function closeAccountMenu() {
  const menu = $("accountMenu");
  if (menu) {
    menu.hidden = true;
    delete menu.dataset.for;
  }
  ["accountBtn", "mapAccountBtn"].forEach((id) => {
    $(id)?.setAttribute("aria-expanded", "false");
  });
}

function toggleAccountMenu(btn) {
  if (!currentUser) {
    closeAccountMenu();
    openAuth("login");
    return;
  }
  const menu = $("accountMenu");
  if (!menu || !btn) return;
  const already = !menu.hidden && menu.dataset.for === btn.id;
  closeAccountMenu();
  if (already) return;
  menu.hidden = false;
  menu.dataset.for = btn.id;
  btn.setAttribute("aria-expanded", "true");
  const rect = btn.getBoundingClientRect();
  const width = menu.offsetWidth || 248;
  const left = Math.min(Math.max(8, rect.right - width), window.innerWidth - width - 8);
  menu.style.top = `${Math.round(rect.bottom + 6)}px`;
  menu.style.left = `${Math.round(left)}px`;
}

function showVerifyLink(box, message, url) {
  if (!box) return;
  box.hidden = false;
  if (!url) {
    box.textContent = message;
    return;
  }
  box.innerHTML = `${escapeHtml(message)} <a class="verify-go" href="${escapeHtml(url)}">Activar cuenta</a>`;
}

async function downloadFavReport() {
  closeAccountMenu();
  if (!currentUser) {
    openAuth("login");
    flashAuth("Entrá con tu cuenta para bajar el reporte.");
    return;
  }
  if (!currentUser.email_verified) {
    openAuth("login");
    flashAuth("Validá tu mail para bajar el reporte de favoritos.");
    return;
  }
  const btn = $("favReportMenuBtn");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/favorites-report");
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      flashAuth(typeof data.detail === "string" ? data.detail : "No se pudo armar el PDF.");
      if (res.status === 401 || res.status === 403) openAuth("login");
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "favoritos-propmap.pdf";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch {
    flashAuth("No se pudo bajar el reporte.");
  } finally {
    if (btn) btn.disabled = false;
  }
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

$("accountBtn")?.addEventListener("click", (ev) => {
  ev.stopPropagation();
  toggleAccountMenu($("accountBtn"));
});
$("mapAccountBtn")?.addEventListener("click", (ev) => {
  ev.stopPropagation();
  toggleAccountMenu($("mapAccountBtn"));
});
$("accountSettingsBtn")?.addEventListener("click", openSettings);
$("favReportMenuBtn")?.addEventListener("click", downloadFavReport);
document.addEventListener("click", (ev) => {
  if (ev.target.closest("#accountMenu, #accountBtn, #mapAccountBtn")) return;
  closeAccountMenu();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeAccountMenu();
});
window.addEventListener("resize", closeAccountMenu);
document.querySelectorAll("[data-auth-tab]").forEach((btn) => {
  btn.addEventListener("click", () => setAuthTab(btn.dataset.authTab));
});
$("authCancel")?.addEventListener("click", () => $("authModal")?.close());
$("authSoonClose")?.addEventListener("click", () => $("authModal")?.close());
$("authResend")?.addEventListener("click", async () => {
  const err = $("authError");
  const ok = $("authOk");
  if (err) err.hidden = true;
  if (ok) ok.hidden = true;
  const res = await fetch("/api/auth/resend", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: $("authUser")?.value || "",
      password: $("authPass")?.value || "",
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (err) {
      err.hidden = false;
      err.textContent = apiDetail(data, "No se pudo reenviar.");
    }
    return;
  }
  showVerifyLink(ok, data.message || "Mail reenviado.", data.verify_url);
});
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
  const resendBtn = $("authResend");
  if (resendBtn) resendBtn.hidden = true;
  if (!res.ok) {
    if (err) {
      err.hidden = false;
      err.textContent = apiDetail(data, "No se pudo completar.");
    }
    if (res.status === 403 && !register && resendBtn) resendBtn.hidden = false;
    return;
  }
  if (register) {
    currentUser = null;
    paintAccountBtn();
    showVerifyLink(ok, data.message || "Revisá tu mail para activar la cuenta.", data.verify_url);
    if (resendBtn) resendBtn.hidden = false;
    return;
  }
  currentUser = data.user;
  paintAccountBtn();
  await importLocalPins();
  $("authModal")?.close();
  load({ live: false });
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
  showVerifyLink(msg, data.message || "Mail reenviado.", data.verify_url);
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
    let open = box.dataset.foldDefault !== "closed";
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
  const amateur = /\.local$|localhost/i.test(addr);
  const inbox = mail.inbox || "";
  document.querySelectorAll("[data-site-mail]").forEach((el) => {
    if (!addr || amateur) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    if (el.tagName === "A") {
      el.href = `mailto:${addr}`;
      el.textContent = addr;
    } else {
      el.textContent = addr;
    }
  });
  document.querySelectorAll("[data-mail-inbox]").forEach((el) => {
    if (!inbox) {
      el.hidden = true;
      return;
    }
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
      // Las visitas las cuenta el servidor al servir la página. No se carga
      // matomo.js: uBlock / EasyPrivacy lo bloquean aunque sea first-party.
      if (matomo.siteId) return;
    } catch (_) {}
    await new Promise((resolve) => setTimeout(resolve, 2500));
  }
}

function whenIdle(fn) {
  if (window.requestIdleCallback) {
    requestIdleCallback(fn, { timeout: 4000 });
    return;
  }
  setTimeout(fn, 1200);
}

const landingQuery = new URLSearchParams(location.search);
let landingBarrio = landingQuery.get("barrio") || "";
const landingCity = landingQuery.get("ciudad") || "";
const landingType = landingQuery.get("tipo") || "";
const landingText = landingQuery.get("q") || "";
if (landingCity) {
  try { localStorage.setItem("propmap.city", landingCity); } catch (_) {}
}
if (landingType && $("typeFilter") && [...$("typeFilter").options].some((opt) => opt.value === landingType)) {
  $("typeFilter").value = landingType;
  syncRoomFilters();
}
if (landingText && $("placeQuery")) $("placeQuery").value = landingText;

bindFolds();
hydrateAuth().finally(() => {
  load();
  pollStatus();
  armPoll();
  loadTrackers();
});
