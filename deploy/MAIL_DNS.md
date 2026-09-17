# Почта: sendmail и DNS для домена отправки

По умолчанию в проде без ESP письма уходят через **локальный sendmail**
(`postfix` / `exim` / `ssmtp` — бинарь `/usr/sbin/sendmail -t`).

SMTP-релей ESP — опционально (`MAIL_TRANSPORT=smtp`).

Домен API (`PUBLIC_BASE_URL`) и **домен From** могут различаться.

## Настройка sendmail (рекомендуемый путь без ESP)

В `.env` приложения (или mailer-service):

```bash
# Без mailer-service — приложение шлёт само:
MAILER_SERVICE_URL=

MAIL_TRANSPORT=sendmail
SENDMAIL_PATH=/usr/sbin/sendmail
MAIL_FROM=noreply@ваш-домен.ru
MAIL_FROM_NAME=Название
```

На сервере должен быть рабочий MTA:

```bash
# пример: postfix
sudo apt install postfix   # или уже установлен
echo 'test' | sendmail -v ваш@email.ru
which sendmail             # обычно /usr/sbin/sendmail
```

Проверка из админки: **Настройки → Почта → Отправить тест**.

Массовая постановка: `POST /admin/mail/send-batch` или `get_mailer().send_batch()`.

### Docker

Контейнер `mailer` по умолчанию **не содержит** системный sendmail.
Варианты:

1. **Systemd / хост** — API и workers на хосте с postfix, `MAIL_TRANSPORT=sendmail`, без `MAILER_SERVICE_URL`.
2. **mailer-service в Docker** — пробросить/установить MTA в образ или смонтировать sendmail + очередь (сложнее); проще SMTP или хостовый sendmail.

## SMTP (если всё же ESP)

| Параметр | Пример |
|----------|--------|
| `MAIL_TRANSPORT` | `smtp` |
| `SMTP_HOST` / `PORT` / `USER` / `PASSWORD` | от ESP |
| `MAIL_FROM` | ящик, разрешённый релеем |

## Данные для привязки почты к домену

Админу DNS домена From нужны:

### 1. Ящик From + MTA или SMTP

- Адрес `MAIL_FROM` (создать ящик или alias на сервере)
- Для sendmail: postfix/exim настроен на исходящую с этого домена
- Для SMTP: credentials ESP

### 2. SPF (TXT)

Кто имеет право слать от имени домена. Для **своего IP сервера**:

```
v=spf1 ip4:А.Б.В.Г ~all
```

Для ESP — `include:` от провайдера. Один SPF на имя, не два TXT.

### 3. DKIM (TXT)

- **sendmail/postfix**: настроить OpenDKIM (селектор + ключ) → TXT `selector._domainkey`
- **ESP**: взять готовые записи из панели

### 4. DMARC (TXT `_dmarc.`)

Старт:

```
v=DMARC1; p=none; rua=mailto:dmarc@ваш-домен.ru
```

Позже `quarantine` / `reject`.

### 5. MX

Нужен, если принимаете входящие / Reply-To на том же домене.

### 6. PTR (обратная DNS IP)

Для своего VPS — PTR у хостера на IP, с которого шлёт postfix. Без PTR часто спам.

## Почему «✓ Отправлено», а письма нет

Админка показывает успех, когда **sendmail принял** письмо (exit 0) и выдал Message-ID.
Это **не** «доставлено во входящие».

Типичная причина на Mac/VPS:

```text
postqueue: fatal: Queue report unavailable - mail system is down
```

Бинарник `/usr/sbin/sendmail` есть, а **postfix не запущен** — письмо уходит в никуда,
но sendmail всё равно отвечает «ок».

### Что сделать на сервере, где крутится API

```bash
# статус
sudo postfix status          # или: systemctl status postfix
mailq                        # очередь; «mail system is down» = MTA мёртв

# запуск (Debian/Ubuntu)
sudo systemctl enable --now postfix

# ручной тест
echo "Subject: test
From: noreply@mail.groster.me
To: ваш@email.ru

hello" | /usr/sbin/sendmail -t -oi -v -f noreply@mail.groster.me

# смотреть доставку
sudo tail -f /var/log/mail.log   # или maillog
mailq
```

Дополнительно:

1. **Исходящий порт 25** у многих VPS/хостеров закрыт — без SMTP-релея внешняя
   доставка не взлетит. Тогда нужен relay (SendGrid/Unisender/Yandex) или разблокировка 25.
2. **Spam** — проверьте папку «Спам».
3. **SPF/DKIM/PTR** для IP сервера — иначе Gmail/Mail.ru молча режут.
4. В Docker-контейнере sendmail обычно **не умеет** слать наружу — MTA должен быть на хосте.

## Куда что писать

| Где | Что |
|-----|-----|
| `.env` | `MAIL_TRANSPORT=sendmail`, `SENDMAIL_PATH`, `MAIL_FROM*` |
| DNS домена From | SPF, DKIM, DMARC, MX |
| Хостер VPS | PTR для IP |
| Система | пакет postfix/exim + OpenDKIM |
