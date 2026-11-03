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
fintonic products [account|credit_card|loan|...] [--all]   # sin tipo = todos; --all incluye cancelados
fintonic categories [--all]
fintonic transactions --from 2026-10-01 --to 2026-10-31 -c G0898 -s mercadona -n 0 -f csv
fintonic transactions --product <id> --unread  # id de `fintonic products`
fintonic summary --month 2026-10 [--group parent] [--exclude-bank 2091] [--exclude-product <id>]
fintonic refresh [--no-wait] [--timeout 300]  # sincronizar bancos y esperar (avisa de 2FA pendiente)

# Escritura (modifica tu cuenta). Con --dry-run muestran «antes → después» sin escribir nada
fintonic tx show <id>                          # detalle: fechas, producto, partes si está dividido
fintonic tx set-category <id> G0898 [--similar]  # --similar crea regla para movimientos parecidos
fintonic tx split <id> G0898:85 G0101:195       # 2-3 partes en euros positivos; deben sumar el total
fintonic tx describe <id> "Cena cumple"
fintonic tx note <id> "pagó Ana la mitad"
fintonic tx mark-read <id>... | --bank 0182 | --product <id> | --all
fintonic tx mark-unread <id>...
fintonic tx set-date <id> 2026-10-01           # EXPERIMENTAL: sin verificar en una cuenta real (#3)
fintonic tx history [<id>] [-n 20] [-f json]   # cambios hechos desde este equipo
fintonic tx undo <id> [--dry-run]              # deshace el último cambio registrado de ese movimiento

fintonic raw rest/user/profile                 # GET arbitrario (exploración)
fintonic raw user-insights --bff
fintonic logout [--forget-device]
```

### Sin escritorio (Linux, CI, cron)

```bash
export FINTONIC_STORE=file                     # ~/.config/fintonic/session.json (0600); "auto" lo elige si no hay keyring
echo "$PIN" | fintonic login --email tu@email.com --pin-stdin --remember   # o FINTONIC_PIN
# primera vez en este dispositivo: el SMS se envía y el comando termina
fintonic verify 123456
```

Todas las órdenes de listado aceptan `-f table|json|csv`.

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
- **Sesión en el Keychain** (`keyring`) o en un fichero 0600 si no hay keyring. El access token dura 30 min y **no hay endpoint de refresh** (el `refresh_token` que devuelve el login no se puede canjear: finapi no expone ruta y el Keycloak no es público), así que con `--remember` se renueva re-logueando con el PIN guardado. Si ese re-login falla por credenciales, el PIN se borra al instante para no bloquear la cuenta.
- **Movimientos divididos**: `weightedCategories` son céntimos con signo por categoría que suman el total; `summary` reparte cada parte en su categoría. Las categorías `N*` (no computables: traspasos, inversiones, liquidación de tarjeta) se excluyen.
- **Producto de un movimiento**: `product_id` es el `ccc` de la cuenta o el número de tarjeta; `--product` filtra en cliente dentro del banco.
- **Dispositivo persistente**: se reenvía el `deviceUUID` verificado en cada login, así que el SMS solo se pide la primera vez.
- Importes en **céntimos** (`quantity`); los modelos exponen `amount` como `Decimal` en euros.
- **Historial y deshacer**: cada escritura de `tx` se anota, solo si la API la aceptó, en `$XDG_STATE_HOME/fintonic/changes.jsonl` (por defecto `~/.local/state/fintonic/`, directorio 0700 y fichero 0600): una línea JSON con fecha, comando, id, campo, valor anterior y nuevo. Los `mark-read` masivos consultan antes los no leídos para anotar cada uno. `tx undo` restaura el valor anterior (categoría, o las partes si estaba dividido; concepto; nota; leído; fecha) y se anota a su vez, así que un segundo `undo` rehace. Se niega si el movimiento ha cambiado desde entonces, si el cambio creó una regla (`--similar`) o si habría que volver a unir un dividido (#3).

## API observada

Base `https://api.fintonic.com/finapi`, `Accept: application/vnd.fintonic-v7+json`, cabeceras `Authorization: Bearer <jwt>` y `DEVICE-UUID`.

| Método | Ruta | Notas |
|---|---|---|
| POST | `/oidc/token` | `{username, password, deviceUUID}` → tokens en `data`, `device-uuid` en cabecera |
| PUT | `/rest/device/provisions` | `{deviceUuid}` → envía SMS (202) |
| PUT | `/rest/device/verification` | `{deviceUuid, verificationCode}` (202) |
| GET | `/rest/user/profile` | |
| GET | `/rest/overview/position` | saldos actuales + histórico mensual |
| GET | `/rest/bankRegistries` | bancos y tipos de producto |
| GET | `/rest/{account,creditcard,deposit,fund,loan,share,pensionplan}/list*` | `{"accounts": {bankId: [...]}}`, saldos en céntimos |
| GET | `/rest/category/tree` | |
| GET | `/rest/transaction/list` | `pageLimit`, `pageOffset`, `beginDate`, `endDate`, `categoryId`, `queryText`, `read` |
| GET | `/rest/transaction/listByBank/{bankId}` | mismos filtros |
| GET | `/rest/transaction/olderNewerTransactionUserDate` | rango de fechas disponible |

Escritura (`POST` salvo que se indique; nunca se reintentan):

| Ruta | Body |
|---|---|
| `/rest/transaction/{id}/category` | `{categoryId}` |
| `/rest/transaction/{id}/applyUserRule` | `{categoryId}` — recategoriza y crea regla |
| `/rest/transaction/{id}/split` | `{categoryIds: [...], categoryAmounts: [...]}` céntimos con el signo del movimiento |
| `/rest/transaction/{id}/primaryDisplay` · `secondaryDisplay` | `{textFieldValue}` (concepto · nota) |
| `/rest/transaction/{id}/markAsRead` · `markAsReadByBank/{bankId}` · `markAsReadByUser` | — |
| `/rest/transaction/markAsReadByAccount/{bankId}/{ccc}` · `markAsReadByCreditCard/{bankId}/{cardNumber}` | — (app Android) |
| `/rest/transaction/{id}/userDate` | `{userDate: "<epoch ms>"}` (app Android; enviamos las 12:00 Europe/Madrid del día, ver `user_date.py`) |
| `DELETE /rest/transaction/{id}/read` | — marca como no leído (app Android) |
| BFF `/openbanking/aggregations/data-actualizations` | — refresca bancos; luego sondear `GET /openbanking/aggregations/` hasta que no haya `IN_PROGRESS` |

Otros GET útiles: `/rest/transaction/listByIds?transactionIds=…`, `/rest/stats/getAllByRange?beginDate&endDate` (ingresos/gastos por categoría calculados por Fintonic; `summary` coincide al céntimo), `/rest/category/probability?transactionId=…`.

BFF `https://fintonic-pfm-bff.fintonic.com`: `user-insights`, `score/history`, `insurances`, `openbanking/aggregations/`.

## Desarrollo

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Los tests usan `respx` con datos sintéticos; no se versiona ninguna respuesta real.
