this is a modiefied version of (https://github.com/azavaxhuman/Nodex) with openai codex
# Nodex - Unofficial version of the 3X-ui Node

<p align="center">
  <b>همگام‌سازی کامل اینباندها و کلاینت‌ها + همگام‌سازی ترافیک بین سرورها</b><br>
  سبک، ایمن، داکرایز شده، و قابل‌تنظیم با فایل <code>.env</code>
</p>

## ⚡ نــــــودکس

<div dir="rtl">

نودکس یک سرویس سبک و داکر‌شده است که به‌عنوان «نود» برای پنل مرکزی 3X-UI عمل می‌کند. این سرویس:

- اینباندها و کلاینت‌ها را از **سرور مرکزی** به **نودها** سینک می‌کند.
- مصرف ترافیک کلاینت‌ها را بین چند نود **تجمیع و همگام** می‌کند.
- با API رسمی پنل (مسیرهای `/panel/api/...`) کار می‌کند.
- ذخیره‌سازی حالت/ترافیک را داخل SQLite (با WAL) انجام می‌دهد.

</div>

## 🍀 ویژگی‌ها

<div dir="rtl">

- **سینک کامل اینباندها** (ایجاد/به‌روزرسانی در نودها بر اساس پنل مرکزی)
- **سینک انتخابی اینباندها بر اساس پورت** (مثلاً پورت مرکزی `8080` به پورت نود `8081`)
- **سینک کلاینت‌ها** (حذف/افزودن/آپدیت مطابق با مرکزی)
- **همگام‌سازی ترافیک**: جمع‌زدن مصرف از نودها و اعمال شمارندهٔ یکپارچه
- **داکرایز کامل**: ایمیج کم‌حجم Python 3.12 + `tini` + اجرا به‌صورت غیر‌روت
- **ایمن به‌صورت پیش‌فرض**: `read_only`، `no-new-privileges`، `cap_drop: ALL`، `tmpfs` برای `/tmp`
- **Healthcheck داخلی**: مبتنی بر فایل `.heartbeat`
- **تنظیمات ساده**: از طریق `config.json` و متغیرهای `.env`
- **لاگ‌گیری استاندارد** (Stdout) + **لاگ فایل** (اختیاری با `ENABLE_FILE_LOG=1`)
- **موازی‌سازی درخواست‌ها** به نودها (قابل کنترل با `NET_*`)

</div>

## 📁 ساختار و مسیرهای پیش‌فرض

<div dir="rtl">

- داده‌ها و دیتابیس: `/var/lib/dds-nodex/data`
- پیکربندی فقط‌خواندنی: `/var/lib/dds-nodex/config`
- فایل پایگاه‌داده: `/var/lib/dds-nodex/data/traffic_state.db`
- فایل کانفینگ: `/var/lib/dds-nodex/config/config.json`

درون کانتینر:

- `DATA_DIR=/app/data`
- `DB_FILE=/app/data/traffic_state.db`
- `CONFIG_FILE=/app/config/config.json`

</div>

## 🧩 فایل‌های نمونه مهم

### 1) نمونهٔ کانفیگ (`config.sample.json`)

<div dir="rtl">

یک کپی با نام `config.json` داخل مسیر پیکربندی بسازید:

</div>

```json
{
  "central_server": {
    "url": "http://host.docker.internal:PORT/WEBPATH",
    "api_port": 2053,
    "username": "central_username",
    "password": "central_password"
  },
  "nodes": [
    {
      "url": "http://IP:PORT/WEBPATH",
      "api_port": 2054,
      "username": "node_username",
      "password": "node_password",
      "inbound_mappings": [
        {
          "central_port": 8080,
          "node_port": 8081
        }
      ]
    }
  ]
}
```

</div>

**نکته**: حتما برای نود های خود از SSL استفاده کنید . عدم استفاده از HTTPS امنیت شما را به خطر می اندازد. مسیر پنل معمولاً شبیه `http://IP:PORT` یا `https://IP:PORT/panel` است. `WEBPATH` را مطابق پنل خود بگذارید.

`api_port` اختیاری است. اگر مقدار بدهید، پورت داخل `url` با آن جایگزین می‌شود. اگر برای یک نود `inbound_mappings` تعریف نکنید، رفتار قبلی حفظ می‌شود و همهٔ اینباندهای مرکزی با همان `id` روی نود سینک می‌شوند. اگر `inbound_mappings` تعریف شود، فقط همان پورت‌های مشخص‌شده سینک می‌شوند و اینباندهای دیگر نود حذف یا تغییر داده نمی‌شوند.

فرم‌های معتبر برای `inbound_mappings`:

```json
"inbound_mappings": [
  { "central_port": 8080, "node_port": 8081 },
  { "source_port": 8443, "target_port": 9443 },
  "10000"
]
```

مورد `"10000"` یعنی پورت مرکزی و پورت نود هر دو `10000` باشند.

### 2) متغیرهای محیطی (`.env`)

<div dir="rtl">

</div>

```env
SYNC_INTERVAL_MINUTES=1
# فاصله بین سیکل‌های سینک (دقیقه)

# تنظیم سرورها از طریق .env (اختیاری)
# اگر این مقادیر را بگذارید، config.json را override می‌کنند.
# CENTRAL_URL=http://host.docker.internal:2053/WEBPATH
# CENTRAL_API_PORT=2053
# CENTRAL_USERNAME=central_username
# CENTRAL_PASSWORD=central_password

# NODE_COUNT=1
# NODE_1_URL=http://NODE_IP:2054/WEBPATH
# NODE_1_API_PORT=2054
# NODE_1_USERNAME=node_username
# NODE_1_PASSWORD=node_password
# فقط اینباندهای مشخص‌شده سینک شوند: central_port:node_port
# NODE_1_INBOUND_MAPPINGS=8080:8081,8443:8444


# شبکه/پرفورمنس
NET_PARALLEL_NODE_CALLS=true
NET_MAX_WORKERS=12
NET_REQUEST_TIMEOUT=15
NET_CONNECT_POOL_SIZE=100
NET_VALIDATE_TTL_SECONDS=180

# تنظیمات SQLite
DB_WAL=1
DB_SYNCHRONOUS=NORMAL   # FULL | NORMAL | OFF
DB_CACHE_SIZE_MB=64

# لاگ فایل (غیرفعال = 0، فعال = 1)
ENABLE_FILE_LOG=0

# سطح لاگ
LOG_LEVEL=INFO

# سلامت سرویس (ثانیه): حداکثر سن .heartbeat
HEALTH_MAX_AGE=180
```

</div>

### 3) داکر-کمپوز

<div dir="rtl">

م.دکس با این Compose اجرا می‌شود (خلاصهٔ کانفیگ):

</div>

```yaml
services:
  dds-nodex:
    build: .
    image: dds-nodex:prod
    container_name: dds-nodex
    restart: unless-stopped
    environment:
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      SYNC_INTERVAL_MINUTES: ${SYNC_INTERVAL_MINUTES:-1}
      REQUEST_TIMEOUT: ${REQUEST_TIMEOUT:-10}
      DATA_DIR: /app/data
      DB_FILE: /app/data/traffic_state.db
      ENABLE_FILE_LOG: ${ENABLE_FILE_LOG:-0}
      CONFIG_FILE: /app/config/config.json
      HEALTH_MAX_AGE: ${HEALTH_MAX_AGE:-180}
    volumes:
      - /var/lib/dds-nodex/data:/app/data
      - /var/lib/dds-nodex/config:/app/config:ro
    read_only: true
    tmpfs: ["/tmp"]
    security_opt: ["no-new-privileges:true"]
    cap_drop: ["ALL"]
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

</div>

## 🔌 شروع به کار

<div dir="rtl">

### برای نصب نسخه های مختلف از این اسکریپت استفاده کنید

</div>

```bash
curl -sSL https://raw.githubusercontent.com/ppsimn/nodex-inboundmappedports/main/install.sh -o install.sh && chmod +x install.sh && ./install.sh
```

#### پس از اجرا شما هر بار میتوانید با دستور dds-nodex منو را فراخوانی کنید.

## ❤️ حمایت مالی (Donate)

اگر Nodex برای شما مفید بود، می‌توانید از پروژه حمایت کنید:
https://github.com/azavaxhuman/Nodex

## 🧠 نحوهٔ کار (High-level)

<div dir="rtl">

1. **لوگین به پنل مرکزی و نودها** (Sessionهای پایدار با TTL)
2. **دریافت لیست اینباندها از مرکزی** → اعمال همهٔ اینباندها یا فقط پورت‌های map شده روی نودها
3. **سینک کلاینت‌ها داخل هر اینباند هدف** (ایجاد/حذف/به‌روزرسانی)
4. **ترافیک**: Nodex مجموع مصرف کلاینت را از نودهای مرتبط جمع می‌کند و به‌صورت کل واحد برای کلاینت نگه می‌دارد.
5. **SQLite + WAL**: ذخیرهٔ حالت/مصرف با قفل‌گذاری Thread-safe
6. **Healthcheck** بر پایهٔ تازه بودن فایل `.heartbeat` نسبت به `HEALTH_MAX_AGE`

</div>

## 🛡️ نکات امنیتی

<div dir="rtl">

- `read_only: true` + `no-new-privileges` + `cap_drop: ALL`
- کانفیگ به‌صورت فقط‌خواندنی در کانتینر (`/app/config:ro`)

**پیشنهاد**: Nodex را پشت یک شبکهٔ داخلی Docker یا سرور داخل همان شبکه/دیتاسنتر با پنل‌ها قرار دهید.

</div>

## 🔍 عیب‌یابی و لاگ‌ها

<div dir="rtl">

### مشاهدهٔ لاگ‌ها:

</div>

```bash
dds-nodex --logs
```

یا از طریق اجرای دستور dds-nodex و انتخاب مشاهده ی لاگ ها از منو میتوانید لاگ هارا به صورت زنده مشاهده کنید.

<div dir="rtl">

### فعال کردن لاگ فایل:

داخل `.env` مقدار `ENABLE_FILE_LOG=1`  
مسیر لاگ: `/var/lib/dds-nodex/data/sync.log`

### مشکلات رایج:

- **401/403**: نام کاربری/رمز یا مسیر `WEBPATH` اشتباه است.
- **Timeout**: `NET_REQUEST_TIMEOUT` را بالا ببرید یا اتصال بین Nodex و سرورها را بررسی کنید.
- **Healthcheck Fail**: مقدار `HEALTH_MAX_AGE` کم است یا سینک پی‌درپی شکست می‌خورد (لاگ را چک کنید).
- **DB Lock**: فضای دیسک/مجوز پوشهٔ `data` را بررسی کنید.

</div>

## ❓ پرسش‌های متداول

### آیا Nodex دادهٔ اصلی پنل را تغییر می‌دهد؟

خیر؛ Nodex با API کار می‌کند و تغییرات را در سطح اینباند/کلاینت‌های نودها اعمال می‌کند.

### چند نود همزمان پشتیبانی می‌شود؟

به‌صورت پیش‌فرض موازی‌سازی فعال است؛ با `NET_MAX_WORKERS` می‌توانید متناسب با منابع افزایش/کاهش دهید.

### اگر DB قدیمی دارم؟

Nodex در شروع، در صورت وجود DB قدیمی در مسیرهای قدیمی، آن‌را به مسیر جدید مهاجرت می‌دهد (به‌همراه wal/shm).

## 📜 لایسنس

این پروژه تحت مجوزی منتشر شده که در فایل `LICENSE` آمده است (در صورت عدم وجود، لطفاً مجوز مدنظرتان را اضافه کنید).

## 🧾 تغییرات (Changelog)

### v1.3

- بهینه‌سازی موازی‌سازی درخواست‌ها
- Healthcheck مبتنی بر `.heartbeat`
- PRAGMAهای SQLite قابل تنظیم
- بهبود لاگینگ
