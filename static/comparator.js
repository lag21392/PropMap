/* v1790354913 */
const CMP_KEY = "propmap.compare.ids";
export function getCompareIds(){ try{ return JSON.parse(localStorage.getItem(CMP_KEY)||"[]"); }catch{return [];} }
export function toggleCompare(id){
  const ids = getCompareIds();
  const i = ids.indexOf(id);
  if (i>=0) ids.splice(i,1); else { if (ids.length>=4) ids.shift(); ids.push(id); }
  localStorage.setItem(CMP_KEY, JSON.stringify(ids));
  return ids;
}
export function openComparator(){
  const ids = getCompareIds(); if (ids.length<2) return;
  const items = allListings.filter(l=>ids.includes(l.id));
  const html = items.map(it=>`
    <div style=\"border:1px solid var(--line);border-radius:10px;padding:8px\">
      <b>${escapeHtml(it.title||'')}</b><br>
      ${money(it)} · ${listingM2(it)} m² · ${it.deal_label||''}
    </div>`).join('');
  const dlg = document.createElement('dialog');
  dlg.innerHTML = `<form method=\"dialog\" style=\"padding:12px;max-width:800px\">
    <h3>Comparador</h3><div style=\"display:grid;grid-template-columns:repeat(${items.length},1fr);gap:8px\">${html}</div>
    <button value=\"close\" style=\"margin-top:10px\">Cerrar</button>
  </form>`;
  document.body.appendChild(dlg); dlg.showModal();
}
