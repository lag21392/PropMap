# Análisis de Features y Oportunidades - PropMap

**Fecha:** 2026-09-22
**Objetivo:** Identificar features faltantes, mejoras de diseño/UX y oportunidades de funcionalidad basadas en el análisis del código, UI y contexto actual.

## 1. Estado actual de la UI

**Tecnología:** SPA vanilla JS + Leaflet + MarkerCluster + FastAPI backend
**Diseño:** Paleta verde/teal/sand, tipografía Atkinson Hyperlegible + Newsreader + IBM Plex Mono
**Layout:** 4 columnas: panel filtros, lista, mapa, detalle
**Estado:** Sin modo oscuro, sin búsqueda por IA, sin selector inteligente tipo JEV, sin personalización

## 2. Features faltantes críticas

### 2.1 Modo oscuro
**Prioridad:** Alta
**Descripción:** No hay modo oscuro ni soporte para `prefers-color-scheme`
**Impacto:** Accesibilidad, uso nocturno, reducción fatiga visual
**Implementación sugerida:**
- CSS variables con tema claro/oscuro
- Toggle manual persistido en localStorage
- Respetar `prefers-color-scheme` como default
- Ajustar paleta: `--bg`, `--panel`, `--paper`, `--ink`, `--muted`
- Mapa Leaflet con tiles oscuros alternativos
**Skills relacionadas:** frontend-design, web-design-guidelines

### 2.2 Búsqueda por IA / selector inteligente
**Prioridad:** Alta
**Descripción:** El usuario pide selector similar a JEV que interprete texto libre y seleccione filtros automáticamente
**Ejemplo:** Usuario escribe "casa 3 dormitorios con patio en Palermo hasta 200k USD" → sistema parsea y aplica filtros
**Componentes:**
- Input de texto libre arriba de filtros
- NLP simple con reglas + embeddings ligeros
- Mapeo a filtros existentes: tipo, dormitorios, barrio, precio, traits
- Sugerencias en tiempo real
- Historial de búsquedas
**Tecnología:** Python backend con spaCy/transformers ligero, o reglas con regex + LLM local
**Skills:** data-pipeline, llm_enrich ya existe

### 2.3 Filtros inteligentes y guardados
**Prioridad:** Media
**Descripción:** No hay presets ni filtros guardados
**Features:**
- Guardar combinaciones de filtros como "Mi búsqueda"
- Filtros rápidos tipo chips
- "Similar a este aviso" mejorado
- Filtro por rango de score de ganga más granular
- Filtro por tiempo de publicación

### 2.4 Comparador de avisos
**Prioridad:** Media
**Descripción:** Comparar 2-4 propiedades lado a lado
**UI:** Modal con tabla comparativa de precio, m², USD/m², score, traits, ubicación
**Backend:** Endpoint `/api/compare?ids=...`

### 2.5 Alertas y seguimiento
**Prioridad:** Alta
**Descripción:** Notificaciones de nuevos avisos o cambios de precio
**Features:**
- Guardar búsqueda y recibir alerta
- Watchlist de avisos con cambios de precio
- Email/Telegram/WhatsApp (respetando privacidad, sin IP)
**Stack:** Ya hay `fav_report.py`, extender

### 2.6 Mapa mejorado
**Prioridad:** Media
**Features:**
- Capas de calor por precio/m²
- Límites de barrio/zona con GeoJSON
- Medición de distancia en mapa
- Street view integrado
- Modo satélite

## 3. Mejoras de diseño y UX

### 3.1 Sistema de diseño inconsistente
**Problemas:**
- CSS variables definidas pero sin tema oscuro
- No hay design tokens centralizados
- Colores hardcodeados en JS
**Solución:** Crear `design-tokens.css` con variables semánticas

### 3.2 Accesibilidad
**Faltantes:**
- `color-scheme` meta tag solo light
- Focus visible limitado
- ARIA labels incompletos en filtros
- Contraste no verificado en modo claro
**Skills:** web-design-guidelines

### 3.3 Performance UI
**Problemas:**
- Lista renderiza todo de golpe
- Sin virtual scrolling
- Filtros recargan toda la lista
**Solución:** Virtual list, debounce filtros, skeleton loaders mejorados

### 3.4 Móvil
**Problemas:**
- Layout 4 columnas no responsive
- Filtros ocupan pantalla completa
- Mapa difícil de usar en touch
**Solución:** Breakpoints, panel colapsable, bottom sheet para filtros

## 4. Features de contenido y datos

### 4.1 Páginas de ciudad/barrio
**Prioridad:** Alta
**Descripción:** Landing pages indexables por ciudad con estadísticas
**Contenido:** Mediana USD/m², top barrios, evolución, mapa embed
**SEO:** Ya analizado en seo.md

### 4.2 Historial de precios
**Prioridad:** Media
**Descripción:** Gráfico de evolución de precio por aviso
**Datos:** Necesita tracking temporal

### 4.3 Reportes exportables
**Prioridad:** Baja
**Descripción:** Exportar resultados filtrados a CSV/Excel
**Uso:** Inversores

### 4.4 Integración con Matomo
**Prioridad:** Baja
**Descripción:** Dashboard de uso por ciudad/filtro
**Ya existe:** `/tablero.html`

## 5. Features técnicas y backend

### 5.1 API pública limitada
**Descripción:** Permitir consultas de datos agregados sin scraping
**Rate limit:** Por IP anonimizada

### 5.2 Cache inteligente
**Problema:** page_cache 26GB sin limpieza
**Solución:** Política LRU, compresión mejorada, TTL por ciudad

### 5.3 Búsqueda full-text
**Descripción:** Búsqueda en descripciones de avisos
**Tecnología:** SQLite FTS5

### 5.4 Sistema de plugins para scrapers
**Descripción:** Modularizar `scrapers/` para añadir portales fácilmente
**Ya existe:** Estructura básica

## 6. Features de privacidad y compliance

### 6.1 Anonimización
**Estado:** Matomo sin IP, bueno
**Mejora:** No guardar user agent completo, rotar IDs

### 6.2 Consentimiento
**Faltante:** Banner de cookies/privacidad para Matomo
**Legal:** Ya hay /legal.html

## 7. Roadmap sugerido

### Fase 1 - UX básica (2-3 semanas)
1. Modo oscuro con toggle
2. Búsqueda por texto libre simple (regex + mapeo)
3. Filtros guardados en localStorage
4. Responsive móvil básico

### Fase 2 - Inteligencia (4-6 semanas)
1. Selector inteligente tipo JEV con LLM local
2. Alertas por email/Telegram
3. Comparador de avisos
4. Mapa con capas de calor

### Fase 3 - Crecimiento (6-8 semanas)
1. Landings por ciudad
2. Export CSV
3. Sistema de plugins scrapers
4. API pública

## 8. Skills y librerías a evaluar

### Frontend
- **Tailwind CSS** para sistema de diseño
- **shadcn/ui** componentes accesibles
- **Framer Motion** animaciones
- **React** si se migra de vanilla JS

### IA/NLP
- **spaCy** para parsing de búsqueda
- **sentence-transformers** embeddings ligeros
- **Ollama** ya usado para LLM
- **Transformers.js** para cliente

### Mapa
- **MapLibre GL** alternativa a Leaflet
- **Turf.js** análisis geoespacial

### Performance
- **Workbox** PWA
- **IndexedDB** cache cliente
- **Web Workers** parsing

## 9. Métricas de éxito

- Tiempo a primer filtro <15s
- Uso de búsqueda por texto >30%
- Retención 7 días >20%
- Modo oscuro adopción >40%
- Filtros guardados por usuario >2

## 10. Notas de privacidad
Mantener anonimización. No usar IP. Respetar preferencias del usuario sobre privacidad. No integrar Google Analytics. Usar Matomo self-hosted.

## Conclusión
PropMap tiene base sólida pero carece de UX moderna y features de inteligencia. El modo oscuro y búsqueda por IA son quick wins con alto impacto. El selector inteligente tipo JEV es diferenciador clave. Priorizar features que mejoren activación y retención sin comprometer privacidad.
