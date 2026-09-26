/* v1790354913 */
/**
 * parse-free-text.js
 * Parser ligero de búsqueda libre para PropMap.
 * Convierte texto natural en filtros existentes.
 * Estratégia: reglas con regex, sin dependencias externas.
 */

const TYPE_MAP = {
  casa: "casa",
  casas: "casa",
  departamento: "departamento",
  departamentos: "departamento",
  depto: "departamento",
  deptos: "departamento",
  ph: "ph",
  duplex: "ph",
  dúplex: "ph",
  terreno: "terreno",
  lote: "terreno",
  lotes: "terreno",
  local: "local",
  locales: "local",
  oficina: "oficina",
  oficinas: "oficina",
  galpon: "galpon",
  galpón: "galpon",
  galpones: "galpon",
  galpones: "galpon"
};

const TRAIT_MAP = [
  { keys: ["credito","apto credito","hipotecable","hipoteca"], value: "credit" },
  { keys: ["dueno","dueño directo","dueño","owner"], value: "owner" },
  { keys: ["expensas bajas","bajas expensas","expensas baja"], value: "expenses" },
  { keys: ["urgente","venta urgente","rapido"], value: "urgent" },
  { keys: ["tranquilo","silencioso","quiet"], value: "quiet" },
  { keys: ["buen estado","bueno estado","renovado","impecable"], value: "good" },
  { keys: ["balcon","balcón"], value: "balcony" },
  { keys: ["luminoso","luz","luminoso"], value: "bright" },
  { keys: ["crecimiento","crece","en crecimiento"], value: "growing" },
  { keys: ["vista","vista abierta","vista panoramica"], value: "view" },
  { keys: ["patio","jardin"], value: "patio" },
  { keys: ["cochera","garage","garaje","parking"], value: "garage" },
  { keys: ["terraza","terraza"], value: "terrace" },
];

function normalize(str) {
  return String(str || "")
    .normalize("NFD").replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/-/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function parseNumber(str) {
  if (!str) return null;
  const cleaned = str.replace(/[^0-9.,kK]/g, "").replace(",", ".");
  if (/k$/i.test(str)) {
    const n = parseFloat(cleaned.replace(/k/i, ""));
    return isFinite(n) ? n * 1000 : null;
  }
  const n = parseFloat(cleaned);
  return isFinite(n) ? n : null;
}

function findType(text) {
  for (const [k, v] of Object.entries(TYPE_MAP)) {
    const re = new RegExp(`\\b${k}\\b`);
    if (re.test(text)) return v;
  }
  return null;
}

function findTrait(text) {
  const found = new Set();
  for (const t of TRAIT_MAP) {
    for (const k of t.keys) {
      const re = new RegExp(`\\b${k}\\b`);
      if (re.test(text)) { found.add(t.value); break; }
    }
  }
  return [...found];
}

function parsePrice(text) {
  // hasta 200k USD, max 200000, 200 mil
  const re = /(?:hasta|max|menos de|up to)\s*([0-9]+(?:[.,][0-9]+)?\s*[kK]?)\s*(usd|ars|pesos)?/i;
  const m = text.match(re);
  if (!m) return null;
  let num = parseNumber(m[1]);
  if (!num) return null;
  // Detectar "200 mil"
  if (/mil/.test(text)) num = num * 1000;
  return { maxPrice: num, currency: m[2] ? m[2].toUpperCase() : null };
}

function parseM2(text) {
  const reMin = /(?:min|minimo|desde)\s*([0-9]+)\s*m2?/i;
  const reMax = /(?:max|maximo|hasta)\s*([0-9]+)\s*m2?/i;
  const reRange = /entre\s*([0-9]+)\s*y\s*([0-9]+)\s*m2?/i;
  const range = text.match(reRange);
  if (range) {
    return { minM2: parseInt(range[1]), maxM2: parseInt(range[2]) };
  }
  const min = text.match(reMin);
  const max = text.match(reMax);
  return {
    minM2: min ? parseInt(min[1]) : null,
    maxM2: max ? parseInt(max[1]) : null
  };
}

function parseRooms(text) {
  const re = /(\d+)\s*(dorm|dormitorios|habitaciones|amb|ambientes|baños?|bath)/gi;
  const beds = [];
  const rooms = [];
  const baths = [];
  let m;
  while ((m = re.exec(text)) !== null) {
    const num = parseInt(m[1]);
    const key = (m[2] || "").toLowerCase();
    if (/dorm|habit/.test(key)) beds.push(num);
    else if (/amb/.test(key)) rooms.push(num);
    else if (/baño|bath/.test(key)) baths.push(num);
  }
  return {
    minBeds: beds.length ? Math.max(...beds) : null,
    minRooms: rooms.length ? Math.max(...rooms) : null,
    minBaths: baths.length ? Math.max(...baths) : null
  };
}

function parsePlace(text) {
  // Heurística simple: buscar último nombre propio que coincida con ciudades/barrios conocidos
  const known = [];
  try {
    const cities = window.lastCities || [];
    cities.forEach(c => {
      known.push({ id: c.id, label: normalize(c.label || c.id), type: "city" });
    });
    const placeBarrios = window.placeBarrios || {};
    const city = document.getElementById("cityFilter")?.value;
    if (city && placeBarrios[city]) {
      placeBarrios[city].forEach(b => known.push({ id: b, label: normalize(b), type: "barrio" }));
    }
  } catch {}
  const words = normalize(text).split(" ");
  for (let i = words.length - 1; i >= 0; i--) {
    const candidate = words.slice(i).join(" ");
    const hit = known.find(k => k.label === candidate || candidate.startsWith(k.label + " "));
    if (hit) {
      return { place: hit.id, type: hit.type };
    }
  }
  return null;
}

window.parseFreeText = function parseFreeText(query) {
  const text = normalize(query);
  if (!text) return null;
  const out = {
    type: findType(text),
    city: null,
    barrio: null,
    zona: null,
    minBeds: null,
    minRooms: null,
    minBaths: null,
    minM2: null,
    maxM2: null,
    maxPrice: null,
    priceCurrency: null,
    traits: []
  };
  const price = parsePrice(text);
  if (price) { out.maxPrice = price.maxPrice; out.priceCurrency = price.currency; }
  const m2 = parseM2(text);
  if (m2) { out.minM2 = m2.minM2; out.maxM2 = m2.maxM2; }
  const rooms = parseRooms(text);
  if (rooms) {
    out.minBeds = rooms.minBeds;
    out.minRooms = rooms.minRooms;
    out.minBaths = rooms.minBaths;
  }
  out.traits = findTrait(text);
  const place = parsePlace(text);
  if (place) {
    if (place.type === "city") out.city = place.place;
    if (place.type === "barrio") out.barrio = place.place;
  }
  return out;
}

window.applyParsedFilters = function applyParsedFilters(parsed) {
  if (!parsed) return;
  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (el && val != null && val !== "") el.value = String(val);
  };
  if (parsed.type) setVal("typeFilter", parsed.type);
  if (parsed.city) setVal("cityFilter", parsed.city);
  if (parsed.barrio) setVal("barrioFilter", parsed.barrio);
  if (parsed.minBeds != null) setVal("minBeds", parsed.minBeds);
  if (parsed.minRooms != null) setVal("minRooms", parsed.minRooms);
  if (parsed.minBaths != null) setVal("minBaths", parsed.minBaths);
  if (parsed.minM2 != null) setVal("minM2", parsed.minM2);
  if (parsed.maxM2 != null) setVal("maxM2", parsed.maxM2);
  if (parsed.maxPrice != null) setVal("maxPrice", parsed.maxPrice);

  // traits checkboxes
  if (parsed.traits && parsed.traits.length) {
    document.querySelectorAll('input[name="listingTrait"]').forEach(cb => {
      cb.checked = parsed.traits.includes(cb.value);
    });
  }
  // Trigger render
  if (typeof render === "function") render();
}
