# Notas de performance - skill python-performance-optimization

## Aplicado
- Profile before optimizing: logging de truncado en tree() para medir.
- Hot paths: parsing HTML en fetch_text y tree(), paginate().
- Usar built-in: logging.warning en vez de print.
- Evitar copias: truncar slice referencia, no duplicar texto entero.
- Batch I/O: page_cache reutiliza lectura de disco.

## Pendiente para medir
- Añadir py-spy en producción 10 min para validar iterparse vs fromstring.
- Medir tamaño real _by_id con sys.getsizeof + tracemalloc.
- Loggear tiempos fetch_text, parse, upsert_many.

## Recomendaciones skill
1. Perfilado CPU con cProfile en run.py.
2. Reducir CACHE_KB a 1024 ya hecho.
3. Reutilizar ThreadPoolExecutor global ya hecho.
4. Evitar DOM completo: iterparse_html ahora disponible.
