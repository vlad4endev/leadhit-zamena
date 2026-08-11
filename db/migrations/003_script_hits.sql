-- Пульс сайта для индикатора связи: сколько раз витрина загрузила наши скрипты.
-- Только агрегат по дням: ни IP, ни URL, ни User-Agent (152-ФЗ — поведение не собираем).
-- Нужен, чтобы честно различать «тег сняли с сайта» и «тег стоит, но корзины пустые»:
-- cart-ping идёт лишь при непустой корзине, поэтому его молчание само по себе ничего
-- не доказывает.
CREATE TABLE IF NOT EXISTS script_hits (
    day     DATE NOT NULL,
    path    TEXT NOT NULL,
    hits    BIGINT NOT NULL DEFAULT 0,
    last_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, path)
);
