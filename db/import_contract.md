# Контракт файла импорта GrosterHit (XML)

> **YML принимается тем же импортом.** Если у магазина уже есть выгрузка для Яндекс.Маркета
> (`<yml_catalog>`), грузите её как есть — формат распознаётся по корню файла, писать выгрузку
> под нас не нужно. Из YML берём `offer id` → `product_id`, `available` → наличие, `name`
> (или `vendor`+`model`), `price`, `categoryId`, `url`, первую `picture`. Топ-5 и подписчиков
> YML не несёт — их присылайте форматом ниже. Дерево категорий схлопывается в плоский список.

Справочные данные (**каталог, топ-5, подписчики**) грузятся в сервис **файлом** через
админку (`/admin` → раздел «Товарные рекомендации» → кнопка «Импорт из файла (XML)»).
Заказы этот файл **не** несёт — они остаются на живой 1С (`/orders/exists`). Состав корзины
берётся из пинга сниппета. Пример готового файла: [`import_sample.xml`](import_sample.xml).

## Общее
- Формат: XML, корень `<grosterhit-import>`.
- Кодировка: `utf-8` или `windows-1251` — **обязательно** указать в декларации
  `<?xml version="1.0" encoding="..."?>` (парсер берёт кодировку оттуда).
- Три секции — `catalog`, `top5`, `subscribers` — **независимы и необязательны**: можно
  прислать любое подмножество (например, только каталог). Отсутствующая секция не трогается.
- Импорт **атомарный**: если файл кривой или в нём ошибка — не применяется ничего.
- Размер: до 50 МБ.

## Секция `catalog`
Заменяет живой pull каталога из 1С. Апсерт по `product_id`/`category_id` (существующие
обновляются, новые добавляются; товары НЕ удаляются автоматически).

```xml
<catalog>
  <categories>
    <category id="salfetki-bumajnye" sort="1">Бумажные салфетки</category>
  </categories>
  <products>
    <product>
      <id>0057412</id>                              <!-- обязателен, PK -->
      <name>Салфетка …</name>
      <category>salfetki-bumajnye</category>         <!-- обязателен -->
      <price>71.60</price>                            <!-- число с точкой, за базовую единицу -->
      <in_stock>true</in_stock>                       <!-- true/false/1/0/да/нет; по умолч. true -->
      <image_url>https://…</image_url>                <!-- необязателен -->
      <product_url>https://…/0057412/</product_url>
    </product>
  </products>
</catalog>
```
- `<categories>` необязателен: если категория товара в нём не указана, она заводится
  автоматически (имя = id). Атрибут `sort` необязателен — порядок можно не задавать.
- `category_id` **должен совпадать** с id, который витрина шлёт в `groster.cart(...)`
  (иначе подбор товаров в письме промахнётся).

## Секция `top5`
**Полная замена** среза топ-5 (старые позиции очищаются целиком). `position` — 1..5.
`product_id` (текст элемента) и `category id` должны существовать в каталоге.

```xml
<top5>
  <category id="salfetki-bumajnye">
    <product position="1">0057412</product>
    <product position="2">0057999</product>
  </category>
</top5>
```

## Секция `subscribers`
Апсерт по `user_id`. Даты — ISO 8601 с зоной.

```xml
<subscribers>
  <subscriber>
    <user_id>42817</user_id>                          <!-- обязателен, PK -->
    <email>m.orlova@mail.ru</email>
    <is_unsubscribed>false</is_unsubscribed>
    <consent_at>2026-07-20T10:00:00+03:00</consent_at> <!-- 152-ФЗ: дата согласия -->
    <last_purchase_at>2026-07-15T12:00:00+03:00</last_purchase_at>
    <last_purchase_category_id>kasaletki</last_purchase_category_id>
  </subscriber>
</subscribers>
```

## Ответ импорта
```json
{ "ok": true, "imported": { "categories": 2, "products": 2, "top5": 1, "subscribers": 1 } }
```
При ошибке: `{ "ok": false, "reason": "product 0057412: цена не число ('—')" }` — ничего не применено.

## Проверка
`python -m app.import_xml` — self-check парсера на встроенном примере.
