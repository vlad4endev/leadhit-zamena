-- Чистка ссылок на фото, собранных из артикула, — на любом хосте.
-- 005 сняла такие ссылки только для static.groster.me. Но 1С-контракт показывал пример
-- `https://groster.me/upload/iblock/<артикул>.jpg`, и выгрузки его повторяли: проверка
-- 2026-09-06 — 12 из 12 таких ссылок отдают 404. Имя файла картинки — GUID из CMS,
-- из артикула не выводится, починить на нашей стороне нельзя.
-- Обнуляем: товар покажет «нет фото» вместо битой картинки в письме, а ближайший импорт
-- нормальной выгрузки поставит рабочую ссылку (затереть её NULL'ом уже нельзя — COALESCE
-- в app/feeds.py и app/onec.py). Идемпотентно.
UPDATE products
   SET image_url = NULL
 WHERE image_url IS NOT NULL
   AND lower(regexp_replace(split_part(split_part(image_url, '?', 1), '#', 1), '^.*/', ''))
       IN (lower(product_id) || '.png', lower(product_id) || '.jpg', lower(product_id) || '.jpeg');
