# Profiling producción

## Comandos recomendados
py-spy record -o profile.svg -- python run.py
py-spy dump --pid $(pgrep -f run.py)

## Métricas a loggear
- fetch_text: tiempo por portal y carril
- parse: tiempo de tree() e iterparse
- upsert_many: tiempo medio SQLite
- llm_call: tiempo con y sin backoff

## Logs existentes
Logging en tree() truncado añadido.
Medición RAM en public_meta().

Próximo: añadir decorador @timed a fetch_text y upsert_listings.
