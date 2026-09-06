-- До 30 товаров в одном письме (было 5).
-- Таблицу top5_by_category не переименовываем: имя торчит в фидах, 1С-контракте и API —
-- переименование стоит дороже, чем неточное название. Меняем только границу позиции.
-- ponytail: верхняя граница 30 сидит и в app/svc_config.MAX_ITEMS_PER_EMAIL — менять парой.
ALTER TABLE top5_by_category DROP CONSTRAINT IF EXISTS top5_by_category_position_check;
ALTER TABLE top5_by_category
    ADD CONSTRAINT top5_by_category_position_check CHECK (position BETWEEN 1 AND 30);
