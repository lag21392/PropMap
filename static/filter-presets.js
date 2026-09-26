/* v1790354913 */
const PRESET_KEY = "propmap.presets.v1";

window.loadPresets = function loadPresets() {
  try {
    return JSON.parse(localStorage.getItem(PRESET_KEY) || "[]");
  } catch { return []; }
}
window.savePresets = function savePresets(list) {
  localStorage.setItem(PRESET_KEY, JSON.stringify(list.slice(0,20)));
}
window.currentFilters = function currentFilters() {
  const ids = ["cityFilter","typeFilter","barrioFilter","zonaFilter","maxPrice","minM2","maxM2","minBeds","minRooms","minBaths","sortBy","dealBar"];
  const out = {};
  ids.forEach(id => { const el = document.getElementById(id); if (el) out[id] = el.value; });
  const traits = [...document.querySelectorAll('input[name="listingTrait"]:checked')].map(cb=>cb.value);
  out.traits = traits;
  out.favOnly = document.getElementById("favOnly")?.checked || false;
  return out;
}
window.applyFilters = function applyFilters(state) {
  Object.entries(state).forEach(([k,v]) => {
    if (k === "traits") {
      document.querySelectorAll('input[name="listingTrait"]').forEach(cb => cb.checked = v.includes(cb.value));
    } else if (k === "favOnly") {
      const el = document.getElementById("favOnly"); if (el) el.checked = v;
    } else {
      const el = document.getElementById(k); if (el) el.value = v ?? "";
    }
  });
  if (typeof render === "function") render();
}
window.saveCurrent = function saveCurrent(name) {
  const list = loadPresets();
  list.unshift({ name, state: currentFilters(), ts: Date.now() });
  savePresets(list);
}
window.deletePreset = function deletePreset(idx) {
  const list = loadPresets(); list.splice(idx,1); savePresets(list);
}
