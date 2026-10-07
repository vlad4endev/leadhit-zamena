-- Фото каталога — через свой резолвер расширения (app/images.py, роут /img/<имя>).
-- Выгрузка 1С всем картинкам ставит .png, но часть файлов на static.groster.me лежит
-- как .jpg/.jpeg: проверка 2026-08-15 показала 350 битых ссылок из 1506 (23%).
-- Переводим уже загруженные строки на относительный /img/<имя>; новые импорты кладут
-- такую ссылку сами. Идемпотентно: строки, уже начинающиеся с /img/, LIKE не ловит.
UPDATE products
   SET image_url = '/img/' || substring(image_url
                                        FROM length('https://static.groster.me/images/shop/') + 1)
 WHERE image_url LIKE 'https://static.groster.me/images/shop/%'
   -- только простые имена файлов: то же ограничение, что в images._NAME_RE
   AND substring(image_url FROM length('https://static.groster.me/images/shop/') + 1)
       ~ '^[0-9A-Za-z_-]{4,64}\.(png|jpe?g)$';
