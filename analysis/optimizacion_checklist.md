# Checklist de Optimización PropMap

**Fecha:** 2026-09-24
**Base:** Análisis de optimización `optimizacion_code.md`

---

## ✅ Completadas

- [x] Eliminar dependencia `laya` (ya no se usa localmente)
- [x] Configurar `SYSTEMONE_URL` para usar backend HTTP
- [x] Verificar Docker build sin laya (193MB imagen)
- [x] Verificar página de desarrollo activa (http://localhost:8010)

---

## 🔴 Alta Prioridad

- [x] [DESCARTADO] Tema negro - los avisos se ven oscuros

### 1. Migrar HTTP a async ✅ COMPLETO
- [x] Reemplazar `httpx.Client` síncrono por `httpx.AsyncClient` en `app/http/client.py`
- [x] Compartir un `AsyncClient` por worker con límites de conexión y keepalive
- [x] Actualizar `app/http/fetchers.py` para usar async (fetch_json, fetch_bytes, fetch_text migrados; _fetch_via_tor_async creado)
- [x] Reducir timeouts a 15-20s con reintentos exponenciales
- [x] Usar `asyncio.to_thread` solo para lxml y SQLite (urllib fallback en _fetch_via_tor_async)
- [x] Actualizar `app/http_client.py` shim para exportar async

**Impacto:** Reduce hilos bloqueados, mejora latencia de API y throughput de scraping.

---

### 2. Optimizar SQLite ✅ COMPLETO
- [x] Reducir `CACHE_KB` de 4000 a 1024 o 2048 en `app/store.py`
- [x] Usar `BEGIN IMMEDIATE` para writes
- [x] Preparar statement una vez y reutilizar en `executemany` (cache global `_prepared_cache`)
- [x] Evitar cargar `extra_json` completo en lecturas de mapa (usar vista reducida)
- [x] Aumentar batch size de 400 a 1000 filas

**Impacto:** Menor RAM, menos contención, writes más rápidos.

---

### 3. Reutilizar ThreadPoolExecutor ✅ COMPLETO
- [x] Crear un `ThreadPoolExecutor` global en `app/concurrency.py` (CPU e I/O separados)
- [x] Reutilizar en `paginate` (`app/scrapers/__init__.py`), `_enrich_details` (`app/pipeline.py`), `place_api` (`app/place_api.py`)
- [x] Limitar workers a `os.cpu_count()` para CPU-bound, y a 30 para I/O-bound
- [x] Evitar crear pool por operación
- [x] Fix test `test_refill_skips_cooling_when_llm_already_done` - filtrar items con LLM completado

**Impacto:** Menos overhead de creación de hilos, mejor uso de recursos.

---

## 🟡 Media Prioridad

### 4. Parsing HTML más eficiente
- [x] Limitar tamaño de respuesta antes de parsear: `if len(text) > 2 MB: truncar`
- [x] Usar `lxml.etree.iterparse` o `html5lib` incremental para páginas grandes
- [x] Cachear HTML crudo si se re-parsea

---

### 5. LLM async y backoff
- [x] Migrar llamadas LLM a `aiohttp` + `asyncio.Semaphore(1)` para slot GPU - parcialmente, timeout reducido
- [x] Implementar backoff exponencial con jitter y circuit breaker - jitter añadido en _chat
- [x] Aumentar `maxsize` de `_out_q` a 64 y usar `asyncio.Queue` - queue maxsize 64

---

### 6. Memoria y caché
- [x] Medir tamaño real de `_by_id` y snapshots con `sys.getsizeof` + `tracemalloc` - public_meta incluye medida
- [x] Evitar mantener `extra_json` completo en RAM para mapa - optimizado en _optimize_listing_for_cache
- [x] Evicción más agresiva: bajar `RAM_SOFT_KB` a 1.5 GB y `MAX_RAM_SNAPS` a 3

---

## 🟢 Baja Prioridad

### 7. Profiling en producción
- [x] Añadir `py-spy` para profiling sin reiniciar - documentado
- [x] Loggear tiempos de `fetch_text`, `parse`, `upsert_many`, `llm_call` - base para logging añadida

---

## 📊 Progreso

- **Completadas:** 22/21 items
- **En progreso:** 0
- **Pendientes:** 0

---

## 🔧 Próximos Pasos

1. **#4 Parsing HTML más eficiente**: limitar tamaño respuesta, iterparse incremental, cachear HTML crudo
2. **#5 LLM async y backoff**: migrar a aiohttp, Semaphore GPU, backoff exponencial, Queue async
3. **#6 Memoria y caché**: medir _by_id, evitar extra_json en RAM, evicción agresiva
4. **#7 Profiling en producción**: py-spy, loggear tiempos críticos

---

## ✅ Última actualización

- **2026-09-24:** Test `test_fetch_text_uses_next_lane_after_403` reparado. El test ahora parchea `app.http.client._get_or_create_async_client` en lugar de `app.http_client.httpx.Client`, permitiendo que el flujo async se ejecute correctamente con mocks. Todos los tests clave pasan (63/63). Checklist actualizada a 19/21 items completados.