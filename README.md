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

# Escritura (modifica tu cuenta)
fintonic tx show <id>                          # detalle: fechas, producto, partes si está dividido
fintonic tx set-category <id> G0898 [--similar]  # --similar crea regla para movimientos parecidos
fintonic tx split <id> G0898:85 G0101:195       # 2-3 partes en euros positivos; deben sumar el total
fintonic tx describe <id> "Cena cumple"
fintonic tx note <id> "pagó Ana la mitad"
fintonic tx mark-read <id>... | --bank 0182 | --all

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
- **Producto de un movimiento**: `product_id` es el `ccc` de la cuenta o el número de tarjeta; `--product` filtra en cliente dentro del banco.
- **Dispositivo persistente**: se reenvía el `deviceUUID` verificado en cada login, así que el SMS solo se pide la primera vez.
- Importes en **céntimos** (`quantity`); los modelos exponen `amount` como `Decimal` en euros.

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
| GET | `/rest/transaction/list` | `pageLimit`, `pageOffset`, `beginDate`, `endDate`, `categoryId`, `queryText`, `read` |
| GET | `/rest/transaction/listByBank/{bankId}` | mismos filtros |
| GET | `/rest/transaction/olderNewerTransactionUserDate` | rango de fechas disponible |

Escritura (todas `POST`, nunca se reintentan):

| Ruta | Body |
|---|---|
| `/rest/transaction/{id}/category` | `{categoryId}` |
| `/rest/transaction/{id}/applyUserRule` | `{categoryId}` — recategoriza y crea regla |
| `/rest/transaction/{id}/split` | `{categoryIds: [...], categoryAmounts: [...]}` céntimos con el signo del movimiento |
| `/rest/transaction/{id}/primaryDisplay` · `secondaryDisplay` | `{textFieldValue}` (concepto · nota) |
| `/rest/transaction/{id}/markAsRead` · `markAsReadByBank/{bankId}` · `markAsReadByUser` | — |
| BFF `/openbanking/aggregations/data-actualizations` | — refresca bancos; luego sondear `GET /openbanking/aggregations/` hasta que no haya `IN_PROGRESS` |

Otros GET útiles: `/rest/transaction/listByIds?transactionIds=…`, `/rest/stats/getAllByRange?beginDate&endDate` (ingresos/gastos por categoría calculados por Fintonic; `summary` coincide al céntimo), `/rest/category/probability?transactionId=…`.

BFF `https://fintonic-pfm-bff.fintonic.com`: `user-insights`, `score/history`, `insurances`, `openbanking/aggregations/`.

## Desarrollo

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Los tests usan `respx` con datos sintéticos; no se versiona ninguna respuesta real.
