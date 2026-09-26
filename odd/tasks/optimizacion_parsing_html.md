# Feature: Optimización de parsing HTML

**Fecha**: 2026-09-24
**Origen**: analysis/optimizacion_checklist.md #4, analysis/optimizacion_codigo.md §3.4
**Estado**: en progreso

## Objetivo
Reducir memoria y CPU en parsing de páginas grandes, limitar tamaño de respuesta antes de parsear, usar iterparse incremental y cachear HTML crudo.

## Contexto actual
- `app/scrapers/__init__.py::tree()` usa `lhtml.fromstring(html)` sin límite de tamaño.
- `fetch_text` trae páginas completas; parsing DOM completo en memoria.
- Sin caché de HTML crudo entre re-parseos.
- Checklist marca #4 como pendiente.

## Tareas
1. [x] Limitar tamaño de respuesta antes de parsear en `tree()`: truncar >2 MB.
2. [x] Usar `iterparse_html` para extraer elementos específicos sin construir árbol completo.
3. [x] Cachear HTML crudo en `page_cache` y reutilizar para re-parseo.
4. [x] Actualizar scrapers de Properati / MercadoLibre para usar `iterparse_html` donde aplique.
5. [ ] Medir impacto con py-spy antes/después.

## Criterios de aceptación
- `tree()` no parsea textos >2 MB; trunca con log.
- `iterparse_html` usado en al menos 2 scrapers.
- Page cache reutilizado en reintentos.
- Tests existentes pasan.

## Notas de sincronización con skill
Aplicar python-performance-optimization: profile before optimize, usar built-in, evitar copias.
