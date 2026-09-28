# PropMap · Puerto Madryn

App local para juntar casas, PH y departamentos **en venta** en Puerto Madryn, verlos en un mapa y comparar el precio con el promedio del barrio.

No hay que crear cuentas ni pegar API keys. Corre en Linux (Docker o Python) y lee los avisos públicos de ZonaProp, Argenprop, Properati y Mercado Libre. Facebook Marketplace pide login, así que queda como atajo + carga manual.

## Cómo usarla

### Docker (recomendado)

En Linux, con Docker Engine y Compose v2:

```bash
./run.sh
```

o:

```bash
docker compose up --build
```

Abrí [http://127.0.0.1:8010](http://127.0.0.1:8010) y tocá **Buscar avisos ahora**. El 8000 queda para producción.

Los avisos se guardan en `./data/` (SQLite). Para dejarla en segundo plano:

```bash
docker compose up --build -d
```

Parar: `docker compose down`.

### Linux, sin Docker

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

## Qué muestra

- Mapa con cada aviso (OpenStreetMap, sin clave)
- Precio, m², tipo y portal de origen
- Mediana de USD y USD/m² por barrio y por zona
- Marca de **oportunidad** si el USD/m² queda ~12% o más debajo de la mediana comparable
- Carga a mano para avisos de Facebook

Los portales cambian el HTML seguido: si uno falla, los otros siguen. Volvé a buscar de vez en cuando para refrescar.
