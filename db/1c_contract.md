# HTTP-сервис 1С для триггерных рассылок

Сервис рассылок берёт из 1С **статусы заказов** (живой HTTP, ниже). Каталог, топ-5 и
подписчики импортируются **файлом** (CommerceML, загрузка в админке — отдельный контракт).
Состав корзины из 1С **не запрашивается** — истина берётся из пинга сниппета (см. п.2 ниже).
Магазин оптовый (HoReCa/расходники), цены за базовую единицу с копейками.
Формальная спецификация: `1c_api.openapi.json` (открывается в Swagger/Postman).

## Общее

* База: `https://1c.groster.me/base/hs/grosterhit/v1` (путь уточнить)
* Авторизация: `Authorization: Bearer <token>` (можно Basic)
* `Content-Type: application/json; charset=utf-8`
* Даты: ISO 8601 с зоной, `2026-07-23T09:05:00+03:00`
* Цена: за базовую единицу (`unit`), число с точкой, `71.60`

Ошибка (код 4xx/5xx и тело):

```json
{ "error": { "code": "not_found", "message": "Товар не найден" } }
```

## Товар

`GET /catalog?changed_since=2026-07-22T00:00:00+03:00&page=1&page_size=1000`

`changed_since` необязателен: если задан, отдаём только изменённые позиции. Пагинация нужна.

```json
{
  "items": [
    {
      "product_id": "0057412",
      "name": "Салфетка бумажная 1-слойная 24*24 \"Лилия\" белая, 250 лист",
      "category_id": "salfetki-bumajnye",
      "category_name": "Бумажные салфетки",
      "price": 71.60,
      "unit": "пач",
      "currency": "RUB",
      "min_order_qty": 1,
      "packaging": [ { "level": "упак", "qty": 21 } ],
      "in_stock": true,
      "stock_level": "много",
      "attributes": [],
      "tags": ["хит"],
      "image_url": "https://groster.me/upload/iblock/0057412.jpg",
      "product_url": "https://groster.me/catalog/salfetki-bumajnye/0057412/",
      "updated_at": "2026-07-23T09:00:00+03:00"
    },
    {
      "product_id": "0038921",
      "name": "Касалетка 900 мл 222*160*38 алюминиевая без крышки",
      "category_id": "kasaletki",
      "category_name": "Касалетки",
      "price": 11.80,
      "unit": "шт",
      "currency": "RUB",
      "min_order_qty": 100,
      "packaging": [ { "level": "упак", "qty": 100 }, { "level": "кор", "qty": 600 } ],
      "in_stock": true,
      "stock_level": "много",
      "attributes": [],
      "tags": [],
      "image_url": "https://groster.me/upload/iblock/0038921.jpg",
      "product_url": "https://groster.me/catalog/kasaletki/0038921/",
      "updated_at": "2026-07-23T09:00:00+03:00"
    }
  ],
  "page": 1,
  "page_size": 1000,
  "total": 5230
}
```

Поля товара:

* `product_id` код/артикул в 1С
* `price` за базовую единицу `unit` (шт, упак, пач)
* `packaging` фасовка по уровням (упак/кор/меш), qty = сколько базовых единиц; пусто если не применимо
* `min_order_qty` минимум к заказу в базовых единицах
* `stock_level` много / достаточно / мало / нет
* `attributes` варианты (Вкус/Цвет/Размер/Запах), пусто если товар без вариантов
* `tags` например хит, новинка

Товары по списку id:

```
POST /products
{ "ids": ["0057412", "0038921"] }
```

В ответе те же поля товара и массив `missing` (id, которых нет в базе).

## Корзина — из 1С НЕ запрашивается (решение п.2)

Состав корзины на момент отправки письма берётся из последнего `cart-ping` сниппета
(`cart_sessions.cart_items`), а не живым запросом в 1С. Поэтому прокидывать `session_id`
в 1С не нужно (см. закрытый вопрос №1). Пустую/оформленную корзину закрывают gate
`has_items` и проверка заказа (`/orders/exists`, ниже).

## Заказы

Статусы по списку:

```
POST /orders/status
{ "order_ids": ["ГР-184213", "ГР-184001"] }
```

```json
{
  "items": [
    { "order_id": "ГР-184213", "status": "paid", "order_date": "2026-07-23T09:10:00+03:00", "total": 1323.20 },
    { "order_id": "ГР-184001", "status": "cancelled", "order_date": "2026-07-22T14:00:00+03:00", "total": 0 }
  ]
}
```

Проверка заказа после времени (для отмены письма о брошенной корзине):

```
GET /orders/exists?user_id=42817&since=2026-07-23T09:00:00+03:00
```

```json
{ "exists": true, "order_id": "ГР-184213", "order_date": "2026-07-23T09:10:00+03:00" }
```

Статусы: `new`, `paid`, `shipped`, `cancelled`, `returned`, `refunded`.
Заказ считается оформленным при любом статусе, кроме `cancelled`, `returned`, `refunded`.

## Вопросы по реализации

1. ~~Прокидывается ли `session_id` из cookie сайта в 1С?~~ **Закрыт (п.2):** корзина в 1С не запрашивается, истина — из пинга сниппета. `session_id` в 1С не нужен.
2. Финальный перечень статусов заказа.
3. Как узнавать о новых заказах для сценария «постпродажа»: поллинг по `changed_since` или отдельный метод.
