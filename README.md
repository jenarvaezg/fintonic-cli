# fintonic-cli

Cliente Python y CLI **no oficiales** para [Fintonic](https://www.fintonic.com), construidos observando las llamadas que hace su web app (`www.fintonic.com/private`). No hay API pública: esto puede romperse cuando Fintonic cambie su backend.

## Instalación

```bash
uv tool install git+https://github.com/jenarvaezg/fintonic-cli
# o, desde el repo:
uv sync && uv run fintonic --help
```

## Uso

```bash
fintonic login --email tu@email.com            # PIN + SMS la primera vez en este dispositivo
fintonic login --remember                      # guarda el PIN en el Keychain → re-login automático
fintonic status

fintonic position [--history]                  # saldos por tipo de producto
fintonic banks                                 # bancos conectados y estado de sincronización
fintonic products [account|credit_card|loan|...] [--all]   # sin tipo = todos; --all incluye cancelados y ocultos
fintonic categories [--all]
fintonic transactions --from 2026-10-01 --to 2026-10-31 -c G0898 -s mercadona -n 0 -f csv
fintonic transactions --product <id> --unread  # id de cuenta o tarjeta de `fintonic products`
fintonic transactions -c G0898 -c G0501 --bank 0182 --oldest-first   # -c repetible: cualquiera de ellas
fintonic transactions --max -100               # gastos de 100 € o más (importes con signo: gastos < 0)
fintonic transactions --amount -12,50          # importe exacto; o rango: --amount -100..-50, 100.., ..-20
fintonic transactions --unclassified | --direct-debit
fintonic summary --month 2026-10 [--group parent] [--exclude-bank 2091] [--exclude-product <id>] [--local]
fintonic summary --budgets [--budgets-file budgets.toml] [--fail-over-budget]   # presupuestos locales (ver abajo)
fintonic cashflow --months 12 [--to 2026-10] [--category G0898|G0800]   # ingresos/gastos/neto por mes
fintonic transfers --month 2026-10 [--from … --to …] [--days 3] [--apply [--yes]]   # traspasos mal categorizados
fintonic mandates [<mandateId>]                # recibos domiciliados detectados por Fintonic
fintonic tx show <id>                          # detalle: fechas, producto, partes si está dividido
fintonic tx suggest <id>                       # categorías que propone Fintonic, con probabilidad

fintonic refresh [--bank 0182] [--no-wait] [--timeout 300] [--no-2fa]
fintonic bank-2fa 0182 123456                  # enviar el código 2FA que pide el banco al sincronizar
fintonic bank-2fa 0182                         # banco con aprobación en su app: confirmar que ya está aprobado

# Escritura (modifica tu cuenta). Con --dry-run muestran «antes → después» sin escribir nada
fintonic products hide <id>... | --bank 0182 [--kind credit_card]   # ocultar (como desactivar en la app)
fintonic products show <id>... | --bank 0182 [--kind credit_card]
fintonic tx set-category <id> G0898 [--similar]  # --similar crea regla para movimientos parecidos
fintonic tx split <id> G0898:85 G0101:195       # 2-3 partes en euros positivos; deben sumar el total
fintonic tx describe <id> "Cena cumple"
fintonic tx note <id> "pagó Ana la mitad"
fintonic tx mark-read <id>... | --bank 0182 | --product <id> | --all
fintonic tx mark-unread <id>...
fintonic tx set-date <id> 2026-10-01           # cambia la fecha del movimiento (verificado en #3)
fintonic tx history [<id>] [-n 20] [-f json]   # cambios hechos desde este equipo
fintonic tx undo <id> [--dry-run]              # deshace el último cambio registrado de ese movimiento

fintonic raw rest/user/profile                 # GET arbitrario (exploración)
fintonic raw user-insights --bff
fintonic logout [--forget-device]              # cierra también en el servidor
```

### Sin escritorio (Linux, CI, cron)

```bash
export FINTONIC_STORE=file                     # ~/.config/fintonic/session.json (0600); "auto" lo elige si no hay keyring
echo "$PIN" | fintonic login --email tu@email.com --pin-stdin --remember   # o FINTONIC_PIN
# primera vez en este dispositivo: el SMS se envía y el comando termina
fintonic verify 123456
```

Todas las órdenes de listado aceptan `-f table|json|csv`.

### Resumen y flujo de caja

- `summary` sin `--exclude-*` usa las estadísticas ya calculadas del BFF (`stats/range/net/categories`, una llamada); con exclusiones, o con `--local`, pagina los movimientos y calcula en local. Ambos caminos dan las mismas filas y totales.
- `cashflow` usa `stats/period/cashFlow`: una llamada cada 12 meses (el BFF no acepta más periodos por petición). Con `--category` muestra el neto y el nº de movimientos de esa categoría por mes; si es un grupo (p. ej. `G0800`) suma sus subcategorías, porque el BFF devuelve siempre 0 para los grupos.

### Presupuestos locales

Fintonic no expone los presupuestos de la app, así que se definen en un fichero TOML propio: `$XDG_CONFIG_HOME/fintonic/budgets.toml` (por defecto `~/.config/fintonic/budgets.toml`), o el que indiquen `--budgets-file` o `FINTONIC_BUDGETS`.

```toml
# Límite mensual en euros por categoría o grupo de gasto (ids de `fintonic categories --all`)
G0800 = 300      # grupo "Gastos varios": cuenta todo lo que cuelga de él
G0898 = 120.50   # solo "Restaurante"
```

- Solo ids de gasto (`G` + 4 dígitos), importes positivos con 2 decimales como mucho; cualquier error se indica con el fichero y la clave.
- `summary --budgets` añade una tabla con presupuesto, gastado, % consumido y si se ha superado (las devoluciones restan). En JSON va en la clave `budgets`; con `-f csv` la salida es la tabla de presupuestos.
- `--fail-over-budget` (implica `--budgets`) sale con **código 3** si algún presupuesto se ha superado, para la revisión diaria automatizada.

### Traspasos entre tus cuentas

`transfers` busca pares cargo/abono del mismo importe absoluto en dos productos distintos con ±`--days` días (3 por defecto) en los que al menos un lado no está en una categoría no computable `N*`, y propone pasarlos a `N0102` ("Que realices entre tus cuentas"). Cada movimiento entra en un solo par (gana la fecha más cercana) y los traspasos que ya están en `N*` en los dos lados no se proponen ni se reutilizan. No se tienen en cuenta los movimientos divididos. Sin `--apply` solo lista; con `--apply` pide confirmación (`--yes` para omitirla) y recategoriza los lados que no son `N*`.

## Como librería

```python
from fintonic import FintonicClient, KeyringSessionStore

store = KeyringSessionStore()
with FintonicClient(store.load(), store=store) as client:
    for tx in client.transactions(query="mercadona", limit=20):
        print(tx.user_date, tx.label, tx.amount, tx.category_id)
```

## Diseño

- **Reintentos por defecto**: `GET/HEAD/OPTIONS` se reintentan 4 veces con backoff exponencial + jitter ante `429/500/502/503/504`, timeouts y errores de red (respeta `Retry-After`). El login (`POST`) y el SMS (`PUT`) **nunca** se reintentan, para no gastar intentos de PIN ni cupo de SMS.
- **Sesión en el Keychain** (`keyring`) o en un fichero 0600 si no hay keyring. El access token dura 30 min y **no hay endpoint de refresh**: en la app Android (11.9.1.0, `TokensRetrofit`) el único uso del `refresh_token` es `POST /oidc/logout`, y la app renueva la sesión re-logueando con el PIN guardado. Con `--remember` hacemos lo mismo; si ese re-login falla por credenciales, el PIN se borra al instante para no bloquear la cuenta.
- **Logout en servidor**: `fintonic logout` borra primero la sesión local y después invalida el `refresh_token` con `/oidc/logout`. Es best-effort: si falla la red o Fintonic, avisa pero la sesión local ya está borrada. Las sesiones guardadas con versiones anteriores no tienen `refresh_token` y solo se cierran en local.
- **Cabeceras como la app Android** (`mc/c.java`): `finia_token` y `x-fintonic-user-code` (vienen en las cabeceras `finia_token` y `user-code` del login) en todas las peticiones, y `DEVICE-UUID` solo con el dispositivo verificado (y en `device/provisions` y `device/verification`, como la web). Timeouts de 60 s. El User-Agent sigue siendo el de Chrome: Cloudflare está delante de la API y el resto de cabeceras (`origin`, `referer`) son las de la web.
- **Movimientos divididos**: `weightedCategories` son céntimos con signo por categoría que suman el total; `summary` reparte cada parte en su categoría. Las categorías `N*` (no computables: traspasos, inversiones, liquidación de tarjeta) se excluyen.
- **Filtros en servidor**: `transactions` usa `list/pageable` (el de la app Android) y todos los filtros los aplica Fintonic. `product_id` de un movimiento es el `ccc` de la cuenta o el número de tarjeta, así que `--product` envía `ccc` o `cardNumber`; el resto de productos (préstamos, fondos…) no tienen movimientos.
- **Importes con signo**: `--min`/`--max`/`--amount` van en euros (`12,50` o `12.50`) y se envían en céntimos con signo; los gastos son negativos, así que `--max -100` son gastos de 100 € o más.
- **Dispositivo persistente**: se reenvía el `deviceUUID` verificado en cada login, así que el SMS solo se pide la primera vez.
- Importes en **céntimos** (`quantity`); los modelos exponen `amount` como `Decimal` en euros.
- **Historial y deshacer**: cada escritura de `tx` se anota, solo si la API la aceptó, en `$XDG_STATE_HOME/fintonic/changes.jsonl` (por defecto `~/.local/state/fintonic/`, directorio 0700 y fichero 0600): una línea JSON con fecha, comando, id, campo, valor anterior y nuevo. Los `mark-read` masivos consultan antes los no leídos para anotar cada uno. `tx undo` restaura el valor anterior (categoría, o las partes si estaba dividido; concepto; nota; leído; fecha) y se anota a su vez, así que un segundo `undo` rehace. Se niega si el movimiento ha cambiado desde entonces, si el cambio creó una regla (`--similar`) o si habría que volver a unir un dividido (#3).

## API observada

Base `https://api.fintonic.com/finapi`, `Accept: application/vnd.fintonic-v7+json`, cabeceras `Authorization: Bearer <jwt>`, `finia_token`, `x-fintonic-user-code` y `DEVICE-UUID` (solo con el dispositivo verificado).

| Método | Ruta | Notas |
|---|---|---|
| POST | `/oidc/token` | `{username, password, deviceUUID}` → tokens en `data`, `device-uuid`, `user-code` y `finia_token` en cabeceras |
| POST | `/oidc/logout` | `{refresh_token}` → invalida la sesión (único uso del `refresh_token`) |
| PUT | `/rest/device/provisions` | `{deviceUuid}` → envía SMS (202) |
| PUT | `/rest/device/verification` | `{deviceUuid, verificationCode}` (202) |
| GET | `/rest/user/profile` | |
| GET | `/rest/overview/position` | saldos actuales + histórico mensual |
| GET | `/rest/bankRegistries` | bancos y tipos de producto |
| GET | `/rest/{account,creditcard,deposit,fund,loan,share,pensionplan}/list*` | `{"accounts": {bankId: [...]}}`, saldos en céntimos |
| GET | `/rest/category/tree` | |
| GET | `/rest/transaction/list/pageable` | página de Spring (`content`, `last`, `totalElements`…); `page`, `size` (máx. 100), `sort=userDate,asc\|desc`, `beginDate`, `endDate`, `categoryId` (repetible, OR), `queryText`, `bankId`, `ccc`, `cardNumber`, `read`, `minAmount`/`maxAmount` (céntimos con signo), `unclassified`, `onlyDirectDebit`, `transactionIds`… |
| GET | `/rest/transaction/list` · `listByBank/{bankId}` | lo que usa la web: `pageLimit`, `pageOffset` y menos filtros (ya no se usa) |
| GET | `/rest/transaction/olderNewerTransactionUserDate` | rango de fechas disponible |
| GET | `/rest/category/probability?transactionId=…` | `data.categories: [{id, probability}]` (0..1) → `tx suggest` |
| GET | `/rest/directDebit/mandates` · `/mandates/{mandateId}` | `data`: `{mandateId, bankId, businessName, categoryId, status, userId}` → `mandates` |

Escritura (`POST`/`PUT` salvo que se indique; nunca se reintentan):

| Ruta | Body |
|---|---|
| `/rest/transaction/{id}/category` | `{categoryId}` |
| `/rest/transaction/{id}/applyUserRule` | `{categoryId}` — recategoriza y crea regla |
| `/rest/transaction/{id}/split` | `{categoryIds: [...], categoryAmounts: [...]}` céntimos con el signo del movimiento |
| `/rest/transaction/{id}/primaryDisplay` · `secondaryDisplay` | `{textFieldValue}` (concepto · nota) |
| `/rest/transaction/{id}/markAsRead` · `markAsReadByBank/{bankId}` · `markAsReadByUser` | — |
| `/rest/{account,creditcard,deposit,fund,loan,share,pensionplan}/{id}/status` | `{active: bool}` — ocultar/mostrar un producto |
| `/rest/{tipo}/all/status?bankId=…` | `{active: bool}` — todos los productos de ese tipo en un banco |
| `/rest/transaction/markAsReadByAccount/{bankId}/{ccc}` · `markAsReadByCreditCard/{bankId}/{cardNumber}` | — (app Android) |
| `/rest/transaction/{id}/userDate` | `{userDate: "<epoch ms>"}` (app Android; medianoche Europe/Madrid del día: el servidor redondea al siguiente día UTC, ver `user_date.py`) |
| `DELETE /rest/transaction/{id}/read` | — marca como no leído (app Android) |
| BFF `/openbanking/aggregations/data-actualizations` | — refresca bancos; luego sondear `GET /openbanking/aggregations/` hasta que no haya `IN_PROGRESS` |

### Sincronización de bancos (BFF `https://fintonic-pfm-bff.fintonic.com`)

| Método | Ruta | Notas |
|---|---|---|
| GET | `/openbanking/aggregations/` | una por banco: `id` (aggregationId), `entityId` (= bank_id), `status {general, detail, reason, readiness}`, `aggregationWay {mfaMessage, mfaPlaceHolder, mfaHelp}` |
| POST | `/openbanking/aggregations/data-actualizations` | refresca todos los bancos; luego sondear hasta que no haya `IN_PROGRESS` |
| POST | `/openbanking/aggregations/{aggregationId}/data-actualizations` | refresca un banco (`refresh --bank`) |
| PUT | `/openbanking/aggregations/{aggregationId}/2fa` | `{otp}` con el código; `{}` confirma una aprobación hecha en la app del banco |

Reglas de 2FA (de la app Android): espera 2FA si `status.detail == "2FA"`; `reason` `WAITING_PARAM` pide código y `BACKGROUND_WAITING_PARAM` aprobación en la app del banco; `detail == "INVALID_2FA"` o `reason` `INVALID_*`/`TIMEOUT_*`/`CANCELLED_*` indica respuesta rechazada o caducada. `refresh` pregunta el código en una terminal interactiva (hasta 3 intentos por banco); con `--no-2fa` o sin terminal solo avisa.

**Fuera de alcance a propósito**: `PUT /openbanking/aggregations/{id}/credential` (actualizar usuario/contraseña del banco). Este CLI no maneja contraseñas bancarias; cámbialas en la app.

Otros GET útiles: `/rest/transaction/listByIds?transactionIds=…`, `/rest/stats/getAllByRange?beginDate&endDate` (ingresos/gastos por categoría calculados por Fintonic; `summary` coincide al céntimo). BFF: `user-insights`, `score/history`, `insurances`.

Estadísticas del BFF (`AnalysisBffRetrofit` en la app Android; importes en céntimos con signo, sin categorías `N*` en los totales):

| Ruta | Respuesta |
|---|---|
| `/stats/range/cashFlow?startDate&endDate` | `{income, expenses, net}` |
| `/stats/period/cashFlow?pivotDate&timeUnit&numberOfPeriods&page` | `[{dateRange, cashFlow}]`, del más antiguo al más reciente |
| `/stats/range/net/categories?startDate&endDate[&categoryRegexFilter]` | `[{categoryId, net, numTransactions}]` por categoría hoja, `N*` incluidas |
| `/stats/range/net/categories/{categoryId}?startDate&endDate` | `{categoryId, net, numTransactions}` |
| `/stats/period/net/categories/{categoryId}?pivotDate&timeUnit&numberOfPeriods&page` | `[{dateRange, categoryNet}]` |

`pivotDate` es `YYYY-MM-DD` y el último periodo devuelto es el que lo contiene; `timeUnit` ∈ `WEEK`, `MONTH`, `QUARTER`, `YEAR`; `numberOfPeriods` ≤ 12 (más da 400) y `page` retrocede de `numberOfPeriods` en `numberOfPeriods`. Para un grupo (`G0800`) devuelven 0 o un error RFC 7807 con HTTP 200, y `categoryRegexFilter` no admite alternativas (`|`).

## Desarrollo

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Los tests usan `respx` con datos sintéticos; no se versiona ninguna respuesta real.
