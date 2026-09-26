# Changelog

Todos los cambios notables de este proyecto se documentan en este archivo.

El formato está basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.0.0/),
y este proyecto adhiere a [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Agregado
- Rate limiting por dominio con SlidingWindowRateLimiter
- Circuit breaker para portales con estados closed/open/half_open
- Tests comprehensivos para rate limiting (19 tests)
- Documentación de implementación en `analysis/rate_limiting_implementation.md`

### Cambiado
- `app/http_client.py`: Integrado rate limiting y circuit breaker en `fetch_text()`
- `app/http_client.py`: Registro de éxitos en `_remember_ok()`
- `app/http_client.py`: Registro de fallos en `_mark_blocked()`
- `app/pipeline.py`: Corregido error de indentación en try-except (línea 874)

### Corregido
- SyntaxError en `app/pipeline.py` que bloqueaba la colección de tests

## [2026-09-22]

### Agregado
- Optimización de CACHE_KB de SQLite a 1024 en `app/store.py`
- Límites de recursos en Docker Compose
- Tuning de LLM

## [2026-09-20]

### Agregado
- Análisis de optimización en `analysis/optimizacion_codigo.md`

---

## Formato de entrada

Para documentar cambios, seguir este formato:

```markdown
## [YYYY-MM-DD]

### Agregado
- Nueva funcionalidad

### Cambiado
- Cambios en funcionalidad existente

### Corregido
- Bugs corregidos

### Eliminado
- Funcionalidad removida
```
