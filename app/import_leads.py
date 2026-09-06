"""Импорт лидов старой базы: CSV (гугл-таблица) и XLSX (выгрузка LeadHit/Numbers).

Формат заказчику не диктуем: колонки ищем по названию (email/Почта, отписан,
дата), разделитель CSV — по первой строке (Google даёт запятую, Excel в русской
локали точку с запятой), кодировку — utf-8 с откатом на cp1251.

XLSX читаем целиком, всеми листами: в выгрузке LeadHit статус живёт не в колонке,
а в НАЗВАНИИ листа («Отписанные»), а адреса дублируются между листами-сегментами.
Импорт одного листа «Общая база» отправил бы письма всем, кто отписался, — поэтому
книга разбирается за один проход, а отписка «липкая» и побеждает в любом порядке.

Лиду без своего user_id даём cart.anon_user_id(email) — тот же ключ, что у лида
с колеса. Один и тот же адрес из старой базы и с витрины сходится в одну строку
subscribers, а не в две.

Из названий листов достаём «теплоту» (engagement) — она нужна для постепенного
запуска: сразу писать на всю четырёхлетнюю базу означает жалобы и спам-фолдер для
домена. Порог отправки живёт в params сервиса best_offer (min_engagement).
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from dataclasses import dataclass
from typing import Optional

from app.cart import anon_user_id, valid_email


@dataclass
class Lead:
    """Строка импорта. Отдельно от feeds.Subscriber: у того нет engagement, и добавлять
    его туда — значит завести поле, которое push-фид принимает, а upsert молча теряет."""
    user_id: str
    email: str
    is_unsubscribed: bool = False
    consent_at: Optional[str] = None
    engagement: int = 0

MAX_BYTES = 10 * 1024 * 1024        # ~200к строк адресов; trust-boundary, как в XML/JSON-импорте
MAX_UNPACKED = 100 * 1024 * 1024    # xlsx — это zip: ограничиваем и распакованный размер
MAX_SKIPS = 50                      # сколько отбракованных строк показываем в отчёте
HEADER_SCAN = 5                     # в скольких верхних строках листа ищем заголовок


def _norm(s: str) -> str:
    """Заголовок/значение к сравнимому виду: «Эл. почта» → «элпочта»."""
    return re.sub(r"[^0-9a-zа-яё]+", "", (s or "").lower())


_EMAIL_H = {"email", "emailaddress", "mail", "почта", "элпочта", "электроннаяпочта",
            "адресэлектроннойпочты", "мейл", "имейл", "адрес"}
_UID_H = {"userid", "id", "clientid", "клиент", "кодклиента", "идентификатор", "код"}
_UNSUB_H = {"unsubscribed", "isunsubscribed", "отписан", "отписался", "отписка",
            "статус", "status", "статусподписки"}
_CONSENT_H = {"consentat", "consent", "согласие", "датасогласия", "subscribedat",
              "датаподписки", "датапоявления", "датарегистрации", "дата"}

# Значения колонки «отписан/статус», которые считаем отказом от рассылки.
# ponytail: список подогнан под типовые выгрузки; свои формулировки старой базы
# («жалоба», «bounce», «чёрный список») дописывать сюда — одно слово в строку.
_UNSUB_VALUES = {"1", "true", "yes", "y", "да", "отписан", "отписался", "отписка",
                 "unsubscribed", "unsub", "отказ", "неподписан", "заблокирован"}

# Лист книги, чьё имя само по себе означает отписку (в выгрузке LeadHit статуса-колонки нет).
_UNSUB_SHEETS = {"отписанные", "отписавшиеся", "отписки", "отписан", "unsubscribed"}

# Лист-сегмент → теплота лида. Порядок именно почтовый: для репутации домена клик по
# письму весит больше покупки, потому что покупатель, который писем не открывает, — это
# будущая жалоба на спам. Лид из нескольких листов получает максимум (см. _ingest).
# Незнакомый лист и CSV без сегментов → 0: холодный, отсекается порогом. Безопасная сторона.
SEGMENTS = {"переходятпоссылкам": 3, "читаютписьма": 2, "совершилизаказ": 1, "общаябаза": 0}

_DATE_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
                 "%d/%m/%Y", "%Y/%m/%d")


def _parse_date(v: str):
    """Дата в ISO или None. Пустая/нечитаемая — None: дату согласия не выдумываем."""
    v = (v or "").strip()
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(v, f).isoformat()
        except ValueError:
            continue
    # Excel/Numbers иногда отдают дату числом (дни от 30.12.1899). Нижняя граница 20000
    # (1954 г.) отсекает счётчики визитов и флаги 0/1, случайно попавшие в колонку даты.
    try:
        n = float(v)
    except ValueError:
        return None
    return (datetime(1899, 12, 30) + timedelta(days=n)).isoformat() if 20000 <= n <= 80000 else None


def _decode(data: bytes) -> str:
    """utf-8 (с BOM) → cp1251. Excel в русской локали сохраняет CSV в cp1251."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1251")
        except UnicodeDecodeError:
            raise ValueError("не читается кодировка файла: сохраните CSV в UTF-8")


def _delimiter(line: str) -> str:
    """Разделитель — самый частый из , ; \\t. Проще и предсказуемее csv.Sniffer."""
    counts = {d: line.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _columns(header: list[str]) -> dict[str, int]:
    """Строка заголовка → {роль: индекс}. Пусто, если ни одна колонка не опознана."""
    cols: dict[str, int] = {}
    for i, cell in enumerate(header):
        n = _norm(cell)
        for role, names in (("email", _EMAIL_H), ("user_id", _UID_H),
                            ("unsub", _UNSUB_H), ("consent", _CONSENT_H)):
            if n in names and role not in cols:
                cols[role] = i
    return cols


def _find_header(rows: list[list[str]]):
    """(индекс строки заголовка, колонки) или (None, None). Выгрузка из Numbers может
    начинаться с титульной строки, поэтому смотрим не только первую."""
    for i, r in enumerate(rows[:HEADER_SCAN]):
        cols = _columns(r)
        if "email" in cols:
            return i, cols
    return None, None


# --- Разбор XLSX без зависимостей: это zip с XML. --------------------------------

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_index(ref: str) -> int:
    """«C7» → 2. Позиция важна: у пустых ячеек в XML нет тега, строки «сползли» бы."""
    n = 0
    for ch in re.match(r"[A-Z]*", ref or "").group(0):
        n = n * 26 + (ord(ch) - 64)
    return max(n - 1, 0)


def _read_xlsx(data: bytes) -> list[tuple[str, list[list[str]]]]:
    """XLSX bytes → [(имя листа, строки)]. Значения как текст, порядок листов книги."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("битый XLSX")
    if sum(i.file_size for i in z.infolist()) > MAX_UNPACKED:
        raise ValueError("слишком большой XLSX в распакованном виде")
    try:
        wb = z.read("xl/workbook.xml").decode("utf-8")
        rels = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"',
                               z.read("xl/_rels/workbook.xml.rels").decode("utf-8")))
    except KeyError:
        raise ValueError("это не книга XLSX")
    shared = ["".join(t.text or "" for t in si.iter(_NS + "t"))
              for si in ET.fromstring(z.read("xl/sharedStrings.xml"))] \
        if "xl/sharedStrings.xml" in z.namelist() else []

    sheets = []
    for name, rid in re.findall(r'<sheet[^>]*name="([^"]*)"[^>]*r:id="([^"]*)"', wb):
        target = rels.get(rid, "").lstrip("/")
        if not target:
            continue
        if not target.startswith("xl/"):
            target = "xl/" + target
        rows: list[list[str]] = []
        for row in ET.fromstring(z.read(target)).iter(_NS + "row"):
            cells: dict[int, str] = {}
            for c in row.iter(_NS + "c"):
                v, t = c.find(_NS + "v"), c.get("t")
                if t == "s" and v is not None and v.text is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    node = c.find(_NS + "is")
                    val = "".join(x.text or "" for x in node.iter(_NS + "t")) if node is not None else ""
                else:
                    val = (v.text or "") if v is not None else ""
                cells[_col_index(c.get("r"))] = val
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
        sheets.append((name, rows))
    return sheets


# --- Общий сбор строк -------------------------------------------------------------

def _ingest(acc: dict, rows: list[list[str]], cols: dict, first_line: int, *,
            sheet: str = "", force_unsub: bool = False, consent_now: bool = False,
            engagement: int = 0, now: str) -> None:
    """Строки одного листа/файла → аккумулятор. Дедуп по user_id, отписка липкая."""
    out, taken = acc["rows"], 0
    for n, r in enumerate(rows, start=first_line):
        get = lambda role: (r[cols[role]].strip()                    # noqa: E731
                            if cols.get(role) is not None and cols[role] < len(r) else "")
        acc["total"] += 1
        email = get("email")
        if not valid_email(email):
            acc["skipped_count"] += 1
            if not email:
                # Строка без адреса — анонимный визит из старой системы. Их тысячи, и
                # перечислять их построчно бессмысленно: в отчёт идёт только счётчик.
                acc["blank_count"] += 1
            elif len(acc["skipped"]) < MAX_SKIPS:
                where = f"{sheet}, стр. {n}" if sheet else f"стр. {n}"
                acc["skipped"].append({"line": where, "reason": f"не email: {email[:60]}"})
            continue
        taken += 1
        uid = get("user_id") or anon_user_id(email)
        unsub = force_unsub or _norm(get("unsub")) in _UNSUB_VALUES
        # Дату согласия пишем только по явному подтверждению админа; берём реальную дату
        # из файла, а где её нет — дату импорта (152-ФЗ: даты не выдумываем без подтверждения).
        consent = ((_parse_date(get("consent")) or now) if consent_now else None)
        prev = out.get(uid)
        out[uid] = Lead(
            user_id=uid, email=email,
            # Отписка липкая: дубль адреса с отпиской в любой строке любого листа
            # означает отписку, каким бы ни был порядок листов в книге.
            is_unsubscribed=unsub or bool(prev and prev.is_unsubscribed),
            consent_at=(prev.consent_at if prev and prev.consent_at else consent),
            # Теплота — максимум по листам: адрес из «Общей базы», который есть и в
            # «Читают письма», остаётся тёплым независимо от порядка листов.
            engagement=max(engagement, prev.engagement if prev else 0),
        )
    acc["columns"].update(cols)
    if sheet:
        acc["sheets"].append({"name": sheet, "rows": len(rows), "emails": taken,
                              "unsubscribed": force_unsub, "engagement": engagement})


def parse(data: bytes, consent_now: bool = False) -> dict:
    """CSV/XLSX bytes → {'rows':[Subscriber], 'total', 'skipped', 'skipped_count',
    'columns', 'sheets'}.

    consent_now — админ подтвердил, что согласие на рассылку в старой базе собрано.
    Без подтверждения consent_at остаётся пустым.
    """
    if not data:
        raise ValueError("пустой файл")
    if len(data) > MAX_BYTES:
        raise ValueError(f"файл больше {MAX_BYTES // (1024 * 1024)} МБ")

    now = datetime.now(timezone.utc).isoformat()
    acc = {"rows": {}, "total": 0, "skipped": [], "skipped_count": 0,
           "blank_count": 0, "columns": {}, "sheets": []}

    if data[:4] == b"PK\x03\x04":                       # xlsx — это zip-архив
        sheets = _read_xlsx(data)
        for name, rows in sheets:
            i, cols = _find_header(rows)
            if cols is None:                            # служебный лист («Обзор экспорта») — мимо
                continue
            _ingest(acc, rows[i + 1:], cols, i + 2, sheet=name,
                    force_unsub=_norm(name) in _UNSUB_SHEETS,
                    engagement=SEGMENTS.get(_norm(name), 0),
                    consent_now=consent_now, now=now)
        if not acc["sheets"]:
            raise ValueError("в книге нет листа с колонкой email "
                             f"(листы: {', '.join(n for n, _ in sheets) or 'нет'})")
    else:
        text = _decode(data)
        reader = csv.reader(io.StringIO(text), delimiter=_delimiter(text.split("\n", 1)[0]))
        try:
            rows = [r for r in reader if any((c or "").strip() for c in r)]
        except csv.Error as e:
            raise ValueError(f"битый CSV: {e}")
        if not rows:
            raise ValueError("в файле нет строк")
        i, cols = _find_header(rows)
        if cols is not None:
            _ingest(acc, rows[i + 1:], cols, i + 2, consent_now=consent_now, now=now)
        elif valid_email((rows[0][0] or "").strip()):   # без заголовка: колонка адресов
            _ingest(acc, rows, {"email": 0}, 1, consent_now=consent_now, now=now)
        else:
            raise ValueError("не нашли колонку с email — назовите её «email» или «Почта»")

    return {"rows": list(acc["rows"].values()), "total": acc["total"],
            "skipped": acc["skipped"], "skipped_count": acc["skipped_count"],
            "blank_count": acc["blank_count"], "columns": sorted(acc["columns"]),
            "sheets": acc["sheets"]}


async def import_rows(con, rows: list[Lead]) -> dict:
    """Атомарный upsert лидов. Возвращает {'new':n,'updated':n}.

    Семантика бережная, в отличие от feeds.upsert_subscribers_rows: импорт старой
    выгрузки НЕ воскрешает отписавшихся и не затирает уже зафиксированное согласие.
    Одна инструкция с unnest вместо executemany — иначе RETURNING не собрать.
    """
    if not rows:
        return {"new": 0, "updated": 0}
    async with con.transaction():
        res = await con.fetch(
            """INSERT INTO subscribers(user_id, email, is_unsubscribed, consent_at, engagement)
               SELECT * FROM unnest($1::text[], $2::text[], $3::bool[],
                                    $4::text[]::timestamptz[], $5::smallint[])
               ON CONFLICT (user_id) DO UPDATE SET
                 email = COALESCE(EXCLUDED.email, subscribers.email),
                 is_unsubscribed = subscribers.is_unsubscribed OR EXCLUDED.is_unsubscribed,
                 consent_at = COALESCE(subscribers.consent_at, EXCLUDED.consent_at),
                 -- NULL = лид добыт этой системой (колесо/корзина): он самый тёплый,
                 -- импорт старой базы его в «холодные» не переводит. Уже импортированному
                 -- лиду теплота только растёт (он мог найтись в более активном листе).
                 engagement = CASE WHEN subscribers.engagement IS NULL THEN NULL
                                   ELSE GREATEST(subscribers.engagement, EXCLUDED.engagement) END
               RETURNING (xmax = 0) AS inserted""",
            [r.user_id for r in rows], [r.email for r in rows],
            [r.is_unsubscribed for r in rows], [r.consent_at for r in rows],
            [r.engagement for r in rows],
        )
    new = sum(1 for r in res if r["inserted"])
    return {"new": new, "updated": len(res) - new}


_SAMPLE = (
    "Почта;Статус;Дата согласия\n"
    "m.orlova@mail.ru;активен;12.03.2024\n"
    "M.Orlova@mail.ru;отписан;\n"          # дубль в другом регистре + отписка
    "ivan@example.com;активен;2024-05-01\n"
    ";активен;01.01.2024\n"                # пустой email → в skipped
    "не-адрес;активен;\n"                  # мусор → в skipped
).encode("utf-8")


def _xlsx_sample() -> bytes:
    """Мини-книга «как из LeadHit»: сегментные листы + «Отписанные» + служебный лист."""
    def sheet(rows):
        cells = "".join(
            "<row>" + "".join(f'<c t="inlineStr" r="{chr(65+i)}{n}"><is><t>{v}</t></is></c>'
                              for i, v in enumerate(r) if v) + "</row>"
            for n, r in enumerate(rows, 1))
        return ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<sheetData>{cells}</sheetData></worksheet>").encode()

    hdr = ["Имя", "Почта", "Визитов", "Дата появления"]
    books = [
        ("Обзор экспорта", [["Документ экспортирован из Numbers"], ["Лист", "Таблица"]]),
        ("Общая база", [hdr, ["Елена", "e@mail.ru", "1", "27.04.2022 15:24"],
                        ["", "", "1", "27.04.2022 10:13"],          # аноним без почты
                        ["Пётр", "p@mail.ru", "2", "01.05.2022 10:00"]]),
        ("Читают письма", [hdr, ["Елена", "E@Mail.ru", "1", "12.05.2022 14:16"]]),
        ("Отписанные", [hdr, ["Пётр", "p@mail.ru", "9", "26.08.2026 14:14"]]),
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml",
                   '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>'
                             for i, (n, _) in enumerate(books, 1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", "<Relationships>" + "".join(
            f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(books) + 1)) + "</Relationships>")
        for i, (_, rows) in enumerate(books, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", sheet(rows))
    return buf.getvalue()


def _demo() -> None:
    """Self-check парсеров на встроенных примерах (без сети/БД)."""
    r = parse(_SAMPLE, consent_now=True)
    # Пустой адрес идёт в счётчик, мусорный — ещё и в построчный отчёт.
    assert r["total"] == 5 and r["skipped_count"] == 2, r
    assert r["blank_count"] == 1 and len(r["skipped"]) == 1, r["skipped"]
    by = {s.user_id: s for s in r["rows"]}
    assert set(by) == {"anon:m.orlova@mail.ru", "anon:ivan@example.com"}, by
    o = by["anon:m.orlova@mail.ru"]
    assert o.is_unsubscribed is True and o.consent_at.startswith("2024-03-12"), o
    assert by["anon:ivan@example.com"].consent_at.startswith("2024-05-01")
    # Без подтверждения — согласие пустое, дату не выдумываем даже из колонки файла.
    assert all(s.consent_at is None for s in parse(_SAMPLE)["rows"])

    # Запятая, английские заголовки, свой user_id.
    r2 = parse(b"user_id,email,unsubscribed\nu-1,a@b.ru,true\n")
    assert r2["rows"][0].user_id == "u-1" and r2["rows"][0].is_unsubscribed is True
    # Файл без заголовка — просто колонка адресов.
    assert len(parse(b"a@b.ru\nc@d.ru\n")["rows"]) == 2

    # XLSX: служебный лист пропущен, дубль между листами схлопнут, лист «Отписанные»
    # ставит отписку адресу, который в «Общей базе» выглядит активным.
    x = parse(_xlsx_sample(), consent_now=True)
    xb = {s.user_id: s for s in x["rows"]}
    assert set(xb) == {"anon:e@mail.ru", "anon:p@mail.ru"}, xb
    assert xb["anon:p@mail.ru"].is_unsubscribed is True, "лист «Отписанные» не сработал"
    assert xb["anon:e@mail.ru"].is_unsubscribed is False
    assert xb["anon:e@mail.ru"].consent_at.startswith("2022-04-27"), "дата из файла"
    assert [s["name"] for s in x["sheets"]] == ["Общая база", "Читают письма", "Отписанные"]
    # Теплота = максимум по листам: адрес из «Общей базы» (0), найденный в «Читают
    # письма» (2), остаётся тёплым. Лист «Отписанные» сегментом не считается → 0.
    assert xb["anon:e@mail.ru"].engagement == 2, xb["anon:e@mail.ru"]
    assert xb["anon:p@mail.ru"].engagement == 0, xb["anon:p@mail.ru"]
    assert SEGMENTS["переходятпоссылкам"] > SEGMENTS["читаютписьма"] > SEGMENTS["совершилизаказ"]
    # CSV без листов — холодный импорт: попадёт под порог, а не в рассылку по умолчанию.
    assert parse(b"email\nx@y.ru\n")["rows"][0].engagement == 0
    assert x["skipped_count"] == 1 and x["blank_count"] == 1 and x["skipped"] == []

    for bad, why in [(b"", "пустой"), (b"\n\n", "нет строк"),
                     ("имя;телефон\nвася;+7\n".encode(), "нет колонки email"),
                     (b"PK\x03\x04broken", "битый xlsx")]:
        try:
            parse(bad); assert False, f"должно упасть: {why}"
        except ValueError:
            pass
    print("import_leads._demo OK")


if __name__ == "__main__":
    _demo()
