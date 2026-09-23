# Análisis de Optimización de Código - PropMap

**Fecha:** 2026-09-22  
**Repositorio:** /home/lag/REPOS_HDD/PropMap  
**Stack:** Python 3.12+, FastAPI, Uvicorn, SQLite WAL, httpx, lxml, orjson, ThreadPoolExecutor

> Este informe es un análisis estático y heurístico basado en la estructura del código, patrones observados y best practices de Python. No incluye profiling en vivo. Se recomienda ejecutar cProfile / py-spy sobre `run.py` en carga real para validar los cuellos de botella.

## 0. Perfil de Hardware y Volumen Real

**Hardware detectado**
- CPU: 12 hilos (6 núcleos físicos @ 2.40GHz base / 3.6GHz turbo)
- RAM: 31 GiB total, 20 GiB usados, 10 GiB disponible
- GPU: NVIDIA GeForce GTX 1060 6GB, 6GB VRAM, 114W uso, 96% GPU util, 81°C
- Disco: /mnt/md0 bcache 1.8T, 286G usados, 1.5T libre

**Volumen real de datos (hoy)**
- SQLite: 778 MB + WAL 255 MB = ~1 GB
- Listings: 57.093 registros
- Ciudades: 1.048 distintas
- Top: caba 6.717, fuera 5.951, cordoba 4.138
- page_cache: 26 GB
- city_cache: 586 MB
- llm/: 7.7 GB
- Total data/: 35 GB

**Conclusión de volumen**
No es sobreingeniería de datos: 57k listings es pequeño. El problema real es **page_cache 26 GB** y **llm 7.7 GB** sin limpieza. SQLite de 1 GB está bien para este volumen. No necesitas PostgreSQL/DuckDB ahora.

**Implicaciones ajustadas**
- CPU 12 hilos es suficiente, no necesitas escalar horizontal.
- RAM 10 GiB libres es más que suficiente para caché RAM de 57k listings.
- GPU 96% util es el cuello real, no la DB.
- Disco 1.5T libre permite mantener page_cache por un tiempo, pero 26 GB es desperdicio.

- Parsing lxml completo de páginas grandes sin límite de tamaño
- LLM con un solo slot GPU y timeouts largos bloqueando hilos

## 1. Arquitectura y Patrones Observados

### 1.1. FastAPI + Lifespan
`app/main.py` levanta en `lifespan`:
- `store.init()` → SQLite
- `load_custom_places()`
- `start_warmup()` → carga snapshots en RAM
- `start_background_scraper()` → scraper diario
- `watchdog.start()`, `matomo_sync`

Correcto uso de `asynccontextmanager`. El hilo principal de API es async pero el trabajo pesado se hace en hilos.

### 1.2. Almacenamiento
`app/store.py`
- SQLite en WAL con `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `cache_size=-4000`, `busy_timeout=8000`, `wal_autocheckpoint=1000`
- Conexión por `threading.local` → evita compartir conexión entre hilos
- `_write` Lock global para `upsert_listings`
- `executemany` con lotes de 400 filas

**Problemas**
- `CACHE_KB=4000` ≈ 4 MB por conexión. Con 4 workers Uvicorn → 16 MB solo de caché SQLite, más el caché de Python.
- `conn.close()` está evitado porque libera caché, pero `_TlsConn` mantiene conexión abierta indefinidamente.
- Write lock global serializa todos los writes, incluidos los de LLM que ya usan una cola de un solo escritor.

### 1.3. Caché de Listings
`app/listings_cache.py`
- Snapshots por ciudad en disco `.json.gz` y en RAM
- `MAX_RAM_SNAPS=4`, `HYDRATE_RAM=3`, `RAM_SOFT_KB=3_800_000` → evicción por memoria
- `_by_id` mantiene referencia a cada listing en RAM
- Pre-compresión gzip con `jsoncodec.gzip_bytes`

**Problemas**
- `_by_id` crece sin límite explícito por ciudad; si hay 10k listings, cada uno con `extra_json` completo, la RAM explota.
- Warmup carga ciudades prioritarias en hilos daemon que compiten con scraping por CPU/IO.

### 1.4. HTTP Cliente
`app/http_client.py`
- `httpx.Client` síncrono reutilizado por hilo vía `_tls.clients`
- `max_connections=8`, `max_keepalive_connections=4` por cliente
- Timeouts: 35s base, 55s con proxy
- Reintentos, fallback a Yandex Translate, Tor, local

**Problemas**
- No hay `httpx.AsyncClient`. Todo bloquea hilos del pool.
- Cada hilo crea su propio cliente → no hay sharing de pool entre workers.
- `wait_s = max(timeout, 55.0 if proxy else 35.0)` mantiene hilos bloqueados mucho tiempo.
- `with httpx.Client(...) as client` en varios sitios crea/cierra conexiones repetidamente.

### 1.5. Scraping y Parsing
`app/scrapers/__init__.py`, `scrapers/*.py`
- `paginate` usa `ThreadPoolExecutor` por lote de páginas
- `tree(url)` → `lxml.html.fromstring(fetch_text(url))`
- `fetch_text` bloqueante

**Problemas**
- Parsing DOM completo en memoria. Páginas de portales pueden superar 1-2 MB.
- No hay límite de tamaño antes de parsear.
- Pool por lote: si `page_workers()=4` y batch=4, se crean 4 hilos por iteración → overhead.

### 1.6. Pipeline
`app/pipeline.py`
- `PORTAL_WORKERS=4`, `MAX_JOBS=1`
- `ThreadPoolExecutor` para portales, detalle y enriquecimiento
- `as_completed` con submit por página
- `store.upsert_many` dentro del loop

**Problemas**
- `MAX_JOBS=1` serializa ciudades, pero `PORTAL_WORKERS=4` paraleliza portales dentro de una ciudad → bien para un portal, mal si se quiere escalar a varias ciudades.
- No hay backpressure explícita entre scraping y escritura a DB.

### 1.7. LLM Enriquecimiento
`app/llm_enrich.py`
- Cola con `deque`, `threading.Condition`, `queue.Queue(maxsize=16)`
- Un solo slot GPU `_GpuSlot`
- Un hilo consumidor escribe a SQLite para evitar write lock
- `httpx.Client` síncrono con timeout 40s y timer de kill manual

**Problemas**
- Slot único serializa todas las llamadas LLM → cuello de botella.
- Timeouts largos bloquean hilos.
- No hay backoff exponencial ni circuit breaker visible.

### 1.8. Serialización
`app/jsoncodec.py`
- Usa `orjson` si está disponible → bueno
- `gzip_bytes` con yield cada 262 KB
- `dumps_text` decodifica bytes a str → copia extra

**Positivo**
- `orjson` es rápido y en C.
- Gzip level 4 es razonable.

## 2. Cuellos de Botella Probables

1. **Contención en SQLite writes**
   - `_write` Lock global + commit por lote.
   - LLM escribe vía cola de un solo escritor → latencia.

2. **Hilos bloqueados por HTTP**
   - `httpx.Client` síncrono con timeouts de 35-55s.
   - Con 4 workers y 4 hilos por worker → 16 hilos bloqueados simultáneamente.

3. **Memoria RAM por caché**
   - `_by_id` + snapshots gzip en RAM.
   - `CACHE_KB=4000` por conexión SQLite.
   - `RAM_SOFT_KB=3.8 GB` dispara evicción tardía.

4. **Parsing lxml**
   - DOM completo de páginas grandes.
   - Sin streaming ni límite de tamaño.

5. **LLM serializado**
   - Un slot GPU, cola pequeña, timeouts largos.

## 3. Recomendaciones Priorizadas

### Alta prioridad

#### 3.1. Migrar HTTP a async
- Reemplazar `httpx.Client` síncrono por `httpx.AsyncClient` en `http_client.py`.
- Usar `asyncio.to_thread` solo para lxml y SQLite.
- Compartir un `AsyncClient` por worker con límites de conexión y keepalive.
- Reducir timeouts a 15-20s con reintentos exponenciales.

**Impacto:** Reduce hilos bloqueados, mejora latencia de API y throughput de scraping.

#### 3.2. Optimizar SQLite
- Reducir `CACHE_KB` a 1024 o 2048.
- Usar `BEGIN IMMEDIATE` para writes y batch más grandes.
- Preparar statement una vez y reutilizar en `executemany`.
- Evitar cargar `extra_json` completo en lecturas de mapa; ya se hace `_MAP_EXTRA_SQL` pero `_by_id` sigue con todo.

**Impacto:** Menor RAM, menos contención, writes más rápidos.

#### 3.3. Reutilizar ThreadPoolExecutor
- Crear un `ThreadPoolExecutor` global por módulo en vez de crear uno por operación en `paginate`, `_enrich_details`, `place_api`.
- Limitar workers a `os.cpu_count()` para CPU-bound, y a 20-30 para I/O-bound.

**Impacto:** Menos overhead de creación de hilos, mejor uso de recursos.

### Media prioridad

#### 3.4. Parsing HTML más eficiente
- Limitar tamaño de respuesta antes de parsear: `if len(text) > 2 MB: truncar`.
- Usar `lxml.etree.iterparse` o `html5lib` incremental para páginas grandes.
- Cachear HTML crudo si se re-parsea.

#### 3.5. LLM async y backoff
- Migrar llamadas LLM a `aiohttp` + `asyncio.Semaphore(1)` para slot GPU.
- Implementar backoff exponencial con jitter y circuit breaker.
- Aumentar `maxsize` de `_out_q` a 64 y usar `asyncio.Queue`.

#### 3.6. Memoria y caché
- Medir tamaño real de `_by_id` y snapshots con `sys.getsizeof` + `tracemalloc`.
- Evitar mantener `extra_json` completo en RAM para mapa; usar vista reducida.
- Evicción más agresiva: bajar `RAM_SOFT_KB` a 1.5 GB y `MAX_RAM_SNAPS` a 3.

### Baja prioridad / Mejoras de calidad

#### 3.7. Profiling en producción
- Añadir `py-spy` para profiling sin reiniciar.
- Loggear tiempos de `fetch_text`, `parse`, `upsert_many`, `llm_call`.
- Métricas de cola LLM y SQLite busy.

#### 3.8. Batch y streaming
- Implementar streaming de resultados de scraping a la API con Server-Sent Events.
- Batch de writes a SQLite con `executemany` de 1000 filas y `PRAGMA journal_mode=WAL`.

#### 3.9. Dependencias
- `laya>=0.1.0` es poco mantenida; evaluar alternativas.
- Añadir `aiofiles` para IO de disco async.
- Usar `pydantic` v2 validators para normalización de datos.

## 4. Cambios Concretos Sugeridos

### 4.1. http_client.py
```python
# Antes: httpx.Client síncrono por hilo
# Después: AsyncClient compartido
from httpx import AsyncClient, Limits

class AsyncHttpPool:
    def __init__(self):
        self.client = AsyncClient(
            limits=Limits(max_connections=100, max_keepalive_connections=20),
            timeout=20.0,
            headers=HEADERS
        )
    async def get(self, url):
        return await self.client.get(url)
```
Usar `asyncio.to_thread` solo para lxml.

### 4.2. store.py
```python
CACHE_KB = 1024  # 1 MB por conexión
```
Preparar statement:
```python
stmt = conn.prepare("INSERT OR REPLACE INTO listings ...")
```

### 4.3. listings_cache.py
Limitar `_by_id` a campos necesarios para mapa:
```python
PIN_KEYS = (... )  # ya existe, usarlo para filtrar antes de guardar en RAM
```

### 4.4. pipeline.py
Reutilizar executor:
```python
from concurrent.futures import ThreadPoolExecutor
_executor = ThreadPoolExecutor(max_workers=8)
```

## 5. Métricas a Monitorear

- Tiempo medio de `fetch_text` por portal y por carril
- Tamaño de `_by_id` en MB y número de items
- SQLite busy timeouts y tiempo medio de `upsert_many`
- Cola LLM: tamaño, tiempo de espera, tasa de éxito
- RAM usada por proceso Uvicorn
- Requests por segundo y latencia p95 de `/api/listings`

## 6. Herramientas Recomendadas

- **Profiling CPU:** `py-spy record -o profile.svg -- python run.py`
- **Profiling memoria:** `tracemalloc`, `memory_profiler`
- **SQLite:** `sqlite3 listings.sqlite "PRAGMA wal_checkpoint(TRUNCATE);"`
- **HTTP:** `httpx` con `http2=True` si los portales lo soportan
- **Lint:** `ruff check app/`, `mypy app/`

## 7. Conclusión

El código es funcional y tiene buenas prácticas en serialización y caché. Los principales cuellos de botella son la mezcla de async/sync, HTTP bloqueante y contención en SQLite. Con migración a HTTP async, reducción de caché SQLite y reutilización de pools, se puede reducir latencia y RAM en 30-50% bajo carga.

---

**Próximos pasos**
1. Ejecutar `py-spy` en producción 10 min para validar hipótesis.
2. Implementar `AsyncClient` en `http_client`.
3. Reducir `CACHE_KB` y medir impacto.
4. Añadir métricas de cola LLM y SQLite.

*Análisis generado automáticamente. Revisar con profiling real antes de aplicar cambios masivos.*

## 8. Arquitectura y Cambios Tecnológicos

PropMap hoy es monolito FastAPI + SQLite + scraping síncrono. Para escalar datos y control, conviene separar responsabilidades.

### 8.1. ETL / Data Pipeline
Hoy el scraping, transformación y carga suceden en el mismo proceso web. Patrón ETL:
- **Extract:** scrapers en workers separados, solo extraen HTML crudo
- **Transform:** parser + normalización + enriquecimiento LLM en cola
- **Load:** writer dedicado a SQLite / warehouse

Beneficios: desacopla fallos, permite reintentos, métricas por etapa.

Opciones:
- **En proceso:** usar `arq` o `celery` con Redis como broker. Tareas: `scrape_city`, `parse_page`, `enrich_llm`, `upsert_db`.
- **Serverless:** Cloud Functions para scraping por ciudad, con Pub/Sub.
- **Stream:** `Kafka` / `Redis Streams` para eventos de nuevos avisos.

### 8.2. Almacenamiento a gran escala
SQLite WAL funciona hasta ~10-50k listings activos. Más allá:
- **PostgreSQL + PostGIS:** mejor para consultas geográficas, índices GIST, concurrencia de writes.
- **DuckDB:** analítica rápida sobre archivos Parquet, ideal para backfills y reportes.
- **Hybrid:** SQLite para API rápida, PostgreSQL como fuente de verdad, sync nocturno.

Control de datos:
- Particionar por ciudad y fecha: `listings_2026_09.sqlite` o esquemas por ciudad.
- Archivar listings inactivos >90 días a Parquet en `data/archive/`.
- Índices: `CREATE INDEX idx_city_updated ON listings(city, scraped_at DESC)`.
- `VACUUM` semanal y `PRAGMA optimize`.

### 8.3. Caché y entrega
- **CDN para snapshots:** servir `.json.gz` desde Nginx con `Cache-Control: public, max-age=300`.
- **Redis:** caché de respuestas `/api/listings` por ciudad, TTL 60s. Evita tocar SQLite en cada request.
- **Pre-render:** generar `city_id.json.gz` cada 5 min en background, API solo sirve archivo.

### 8.4. Scraping
- **Playwright async** para sitios con JS pesado, con pool limitado.
- **Scrapy + Scrapy-Redis** para crawling distribuido, con `AUTOTHROTTLE`.
- **Rate limiting por dominio:** `ratelimit` + `backoff` por portal.
- **Detección de cambios:** hash de contenido, solo re-parsear si cambió.

### 8.5. LLM
- **Batching:** agrupar 8-16 listings por request a LLM.
- **Cache de embeddings:** Redis con key `llm:{listing_id}:{ver}`.
- **Fallback offline:** reglas heurísticas si LLM cae.

## 9. Control de Volumen de Datos

### 9.1. Retención
- `listings` activos: últimos 90 días.
- `listings_history`: solo cambios de precio/estado.
- `page_cache`: TTL 12h, limpieza diaria.

### 9.2. Compresión
- `extra_json` comprimido con `zlib` antes de guardar en DB.
- `gzip` ya usado para snapshots, mantener.

### 9.3. Sampling
- Para mapa: enviar solo campos `PIN_KEYS`, no `extra_json` completo.
- Para analítica: agregados por barrio/día, no filas crudas.

### 9.4. Monitoreo
- Métricas: listings por ciudad, tasa de scraping, errores por portal, tiempo LLM, tamaño DB.
- Alertas: DB > 2 GB, cola LLM > 100, errores HTTP > 20%.

## 10. Roadmap de Migración

**Fase 1 - Sin cambio de stack:**
- Async HTTP, reducir CACHE_KB, reutilizar pools.
- Redis para caché de API.
- Archivado Parquet mensual.

**Fase 2 - Desacoplamiento:**
- Workers `arq` para scraping y LLM.
- PostgreSQL para writes, SQLite para lectura.

**Fase 3 - Escala:**
- Scrapy-Redis + Playwright.
- DuckDB para analítica.
- CDN para snapshots.

Con esto controlas volumen, reduces RAM y latencia, y el sistema sigue funcionando si un portal cae.

## 11. Optimización del Sistema Completo

### 11.1. Infraestructura Docker y Compose
**Observado en `compose.yaml` y `Dockerfile`**
- `propmap` corre con `user: 1000:1000`, volúmenes montados `./data`, `./static`, `./app`. Buen aislamiento.
- `init: true` evita zombies.
- `healthcheck` basado en heartbeat file cada 10s.
- `matomo-db` MariaDB con `max-allowed-packet=64MB`.
- `tor` con `cap_drop: ALL` y `no-new-privileges:true`.
- `propmap-mail` Mailpit con `MP_MAX_MESSAGES=2000`.

**Mejoras ajustadas a hardware**
- **Recursos límites:** con 12 hilos y 31 GiB RAM:
  - `propmap`: `cpus: 4.0`, `memory: 6G`
  - `llama-server`: `cpus: 2.0`, `memory: 8G`, `devices: nvidia.com/gpu:1`
  - `tor`: `cpus: 0.5`, `memory: 512M`
  - `matomo`: `cpus: 1.0`, `memory: 1G`
- **Workers Uvicorn:** `WEB_CONCURRENCY=4` para 12 hilos, dejando 8 para scraping/LLM.
- **Restart policies:** `unless-stopped` ok, `matomo-init` a `restart: "no"`.
- **Logs:** aumentar a `max-size: 50m`, `max-file: 5` para debug sin saturar disco.
- **Build cache:** volumen `/root/.cache/pip` persistente.
- **Redes:** red interna `propmap-net`, exponer solo `propmap:8000` y `matomo:8080`.

### 11.2. Scraping y Egress
**Observado en `app/http_client.py`, `app/egress.py`, `app/ops.py`**
- Carriles: `translate`, `tor`, `local`. Fallback automático.
- `SCRAPE_LOCAL_MAX_DAY=600` requests/día por IP local.
- `SCRAPE_LOCAL_GAP_SEC=20`.
- `TOR_CIRCUITS=4`.

**Mejoras**
- **Rate limiting por dominio:** usar `ratelimit` con token bucket por portal, no solo por carril.
- **Backoff exponencial:** en 429/403, aumentar espera por host en base a historial.
- **Dedupe de URLs:** evitar re-fetch de fichas ya vistas en `page_cache` con TTL 12h.
- **Proxy rotation:** si `SCRAPE_PROXIES` está vacío, considerar lista rotativa con health check.
- **Circuit breaker:** si un portal falla >20% en 5 min, pausar 30 min.
- **Privacidad:** el usuario usa Tor/VPN. No loggear IPs reales, anonimizar `ops` logs.

### 11.3. LLM y GPU
**Observado en `scripts/llm-serve.sh`, `compose.yaml`**
- Modelo `gemma-4-E4B-it-Q4_K_M.gguf` ~4.6 GiB.
- `llama-server` con `-ngl 99`, `batch 512`, `ubatch 256`, `threads 1`.
- `LLM_WORKERS=1`, `LLM_CTX=2048`.
- **Hardware actual:** GTX 1060 6GB al 96% util, 3.1GB VRAM usado, 81°C.

**Mejoras ajustadas a hardware**
- **Batch reducido:** bajar `batch` a 256 y `ubatch` a 128 para evitar OOM en 6GB VRAM.
- **Contexto:** mantener `LLM_CTX=2048` máximo; si se necesita más, usar Q4_K_M con `ngl 99` ya es óptimo.
- **Threads CPU:** aumentar `threads` a 6 para aprovechar 6 núcleos físicos en pre/post-procesamiento.
- **Temperatura:** 81°C es alto; limitar `LLM_PARALLEL=1` y añadir `cooling` o `power_limit 100W`.
- **Cache:** Redis obligatorio para evitar re-inferencia; con 10 GiB RAM libres, asignar 2 GiB a Redis.
- **Cola prioritaria:** ciudades con más búsquedas primero, usando `schedule.score()`.
- **Monitoreo:** exportar métricas de VRAM y temperatura, alertar >85°C.

### 11.4. Datos y Persistencia
**Observado en `data/`, `app/store.py`**
- SQLite `listings.sqlite` con WAL.
- `page_cache/` con HTML crudo.
- `city_cache/` snapshots.
- `llm/` con logs.
- `mail/` para Mailpit.

**Mejoras**
- **Tamaño DB:** implementar `VACUUM` semanal y `PRAGMA incremental_vacuum`.
- **Archivado:** mover listings >90 días a Parquet particionado por `city/year/month`.
- **Limpieza:** script diario para borrar `page_cache` >12h y `llm/` logs >30 días.
- **Backups:** `sqlite3 listings.sqlite .dump` diario a volumen `backups/`.
- **Checksums:** validar integridad de snapshots con hash.

### 11.5. Seguridad y Privacidad
**Usuario requiere privacidad, no usar IP, usa Tor/VPN.**
- No almacenar IPs en `ops` ni `matomo`.
- `X-Frame-Options`, CSP ya configurados en `main.py`.
- `SecureHeaders` añade `Permissions-Policy`.
- `SEARCH_PASSWORD` para búsqueda.

**Mejoras**
- **Matomo:** deshabilitar IP logging, anonimizar a /24.
- **Headers:** añadir `Strict-Transport-Security` solo en HTTPS.
- **Secrets:** mover `.env` a Docker secrets o Vault, no en repo.
- **Rate limit API:** limitar `/api/listings` por IP para evitar scraping externo.
- **CORS:** restringir orígenes permitidos.

### 11.6. Monitoreo y Observabilidad
**Observado en `app/watchdog.py`, `app/matomo.py`**
- Watchdog revisa heartbeat y `/api/alive`.
- Matomo para analytics.

**Mejoras**
- **Métricas Prometheus:** exponer `/metrics` con `prometheus_client`: listings por ciudad, errores HTTP, tiempo LLM, tamaño cola.
- **Logs estructurados:** JSON logs con `structlog`, nivel INFO para scraping, DEBUG para debug.
- **Alertas:** notificar por email si DB >2GB, cola LLM >100, errores >20%.
- **Dashboard:** Grafana con métricas de pipeline.

### 11.7. Scripts y Automatización
**Observado en `scripts/`**
- `llm-progreso.sh`, `llm-serve.sh`, `matomo-bootstrap.sh`, `ollama-pull.sh`.

**Mejoras**
- **Idempotencia:** scripts deben poder ejecutarse múltiples veces sin duplicar.
- **Logging:** redirigir salida a `/var/log/propmap/`.
- **Tests:** añadir `pytest` para scripts críticos.

### 11.8. Rendimiento Web
**Observado en `static/`, `app/main.py`**
- `CachedStatic` con `Cache-Control: public, max-age=604800`.
- `SkipListingsGZip` evita doble compresión.
- Brotli para texto.

**Mejoras**
- **HTTP/2:** habilitar en Uvicorn con `uvicorn[standard]`.
- **Preload:** `<link rel="preload">` para `app.js` y `styles.css`.
- **Lazy load:** mapa carga tiles bajo demanda.
- **Service Worker:** cache offline para UI estática.

### 11.9. Costos y Recursos ajustados a hardware
- **CPU 12 hilos:** 
  - 4 workers Uvicorn
  - 2 hilos para scraping pool
  - 2 hilos para LLM pre/post
  - 4 hilos libres para OS/DB
- **RAM 31 GiB:**
  - Propmap app 6 GiB
  - SQLite cache 0.5 GiB
  - Redis 2 GiB
  - LLM RAM 4 GiB
  - Sistema 8 GiB
  - Libre 10 GiB buffer
- **GPU GTX 1060 6GB:**
  - VRAM usado 3.1GB, límite seguro 5GB
  - Batch máximo 256, contexto 2048
  - Temperatura 81°C → limitar a 100W
- **Tor:** 4 circuitos suficientes con 2 hilos; escalar a 6 si latencia >3s
- **Matomo:** cron cada 10 min, suficiente con tráfico actual
- **Disco:** 1.5T libre permite archivar 12+ meses en Parquet

### 11.10. Checklist Operativo
- [ ] Limites de recursos en compose
- [ ] Backups diarios de SQLite
- [ ] Rotación de logs
- [ ] Monitoreo Prometheus + Grafana
- [ ] Alertas por email/Slack
- [ ] Pruebas de carga con `locust`
- [ ] Revisión de seguridad trimestral
- [ ] Documentación de runbooks

Con estas optimizaciones el sistema es más robusto, privado y escalable sin cambiar la lógica de negocio central.
