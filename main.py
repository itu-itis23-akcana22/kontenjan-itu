#TODO user_info.json program başında bir kere okunacak. save almak için bir fonksiyon yazılacak. user_info argüman olarak verilebilir.

"""
            *** GELISTIRME ONERISI***
- her bir user için dil tercihi getirilebilir. Tüm mesajların ingilizceleri de yazılır.
"""

import logging
import requests
import asyncio
import signal
import json
import os
import re
from html import escape as html_escape
from itertools import zip_longest
from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

TURKEY_TIMEZONE = ZoneInfo("Europe/Istanbul")


def turkey_now():
    """Return the current time in Türkiye, independent of the server timezone."""
    return datetime.now(TURKEY_TIMEZONE)

ADMIN_ID = int(os.getenv("ADMIN_ID"))
TOKEN = os.getenv("BOT_TOKEN")
SUBSCRIPTION_FILE = 'subscriptions.json'
USER_FILE = 'user_info.json'
BLOCKED_CRN_FILE = 'blocked_crns.json'

NOTIFY_COOLDOWN = timedelta(minutes=3)          # Aynı ders için iki "kontenjan var" mesajı arasındaki en kısa süre
DETAIL_CACHE_TTL = timedelta(minutes=2)         # Butonla abone olurken bu süreden yeni ders bilgisi tekrar çekilmez
SUBSCRIBE_FLOW_TIMEOUT = timedelta(minutes=10)  # Adım adım abonelikte cevap için beklenen en uzun süre
MAX_SECTION_BUTTONS = 60                        # Şube listesinde gösterilecek en fazla şube/buton
MAX_SUBLIST_BUTTONS = 90                        # Abonelik listesinde gösterilecek en fazla "Çık" butonu
MESSAGE_LIMIT = 4000                            # Telegram mesaj sınırı (4096 karakter) için pay bırakılmış uzunluk
OBS_UNREACHABLE_TEXT = "Şu anda OBS'ye ulaşılamıyor. Lütfen biraz sonra tekrar deneyin."

ASK_COURSE, ASK_DETAIL = range(2)  # Adım adım abonelik (ConversationHandler) durumları

subscriptions = {}
blocked_crns = set()   # Yeni aboneliğe kapatılmış CRN kodları
crn_details = {}       # crn -> son çekilen ders bilgisi (fetch_lesson_table sözlüğü). Ders programı her çekildiğinde güncellenir.

is_subs_updated = False
request_count = 0

branch_dict = {}

last_msg_times = {}    # (user_id, crn) -> son "kontenjan var" mesajının zamanı
open_notified = set()  # "Kontenjan var" mesajı gönderilmiş, henüz "kontenjan doldu" mesajı gönderilmemiş (user_id, crn) çiftleri

# Log settings
os.makedirs('logs', exist_ok=True)
log_filename = f"logs/bot_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def save_subscriptions(): # düz json.dump kullanmıyorum görsel olarak böyle daha hoş duruyor.
    with open(SUBSCRIPTION_FILE, 'w', encoding='utf-8') as f:
        f.write('{\n')

        user_ids = list(subscriptions.keys())

        for i, user_id in enumerate(user_ids):
            # Bölüm seçimi olmayan abonelikler eski formatta ([ders_kodu, crn]) yazılır
            subscription_list = [[sub[0], sub[1]] if sub[2] is None else [sub[0], sub[1], sub[2]] for sub in subscriptions[user_id]]
            subscription_json = json.dumps(subscription_list, separators=(',', ': '), ensure_ascii=False)

            if i == len(user_ids) - 1:  # Last item, no comma
                f.write(f'"{user_id}": {subscription_json}\n')
            else:  # Add comma for all except last
                f.write(f'"{user_id}": {subscription_json},\n')

        f.write('}')
    logger.info("Abonelikler Kaydedildi...")

def normalize_subscription(lesson):
    """[ders_kodu, crn] veya [ders_kodu, crn, bolum] -> (ders_kodu, crn, bolum). Bölüm seçimi yoksa bolum None olur."""
    lesson = list(lesson)
    bolum = lesson[2] if len(lesson) > 2 else None
    return (lesson[0], lesson[1], bolum)

def load_subscriptions():
    global subscriptions
    subscriptions_local = {}
    if os.path.exists(SUBSCRIPTION_FILE):
        try:
            with open(SUBSCRIPTION_FILE, 'r', encoding='utf-8') as f:
                subscriptions_local = json.load(f)

                for user_id, lessons in subscriptions_local.items():
                    if isinstance(user_id, str):
                        user_id = int(user_id)
                    if user_id not in subscriptions:
                        subscriptions[user_id] = []
                    for lesson in lessons:
                        subscriptions[user_id].append(normalize_subscription(lesson))

        except json.decoder.JSONDecodeError:
            subscriptions_local = {}
    else:
        subscriptions_local = {}

def normalize_code(text):
    """Ders kodu/CRN girdisini büyük harfe çevirir (blg -> BLG). Türkçe klavyeden gelen 'İ' de 'I' olur."""
    return text.strip().upper().replace('İ', 'I')

def tokenize_course_input(text):
    """'blg 102e', 'BLG102E', 'BLG 13547,13548' gibi girdileri ['BLG', '102E'] / ['BLG', '13547', '13548'] biçimine çevirir."""
    tokens = normalize_code(text).replace(',', ' ').split()
    if tokens:
        match = re.fullmatch(r'([A-Z]+)(\d\w*)', tokens[0])
        if match:  # Bitişik yazılmış ders kodu (BLG102E, BLG13547)
            tokens[:1] = [match.group(1), match.group(2)]
    return tokens

def unique(items):
    """Sırayı koruyarak tekrar eden elemanları atar."""
    return list(dict.fromkeys(items))

def is_valid_crn(crn_code):
    return re.fullmatch(r'[0-9]{4,5}', crn_code) is not None

def find_subscription(user_id, lesson_code, crn_code):
    """Kullanıcının ilgili derse aboneliğini döner, yoksa None."""
    for sub in subscriptions.get(user_id, []):
        if sub[0] == lesson_code and sub[1] == crn_code:
            return sub
    return None

def set_subscription_bolum(user_id, lesson_code, crn_code, bolum):
    """Mevcut aboneliğin bölüm seçimini günceller. Abonelik yoksa False döner."""
    user_subs = subscriptions.get(user_id, [])
    for i, sub in enumerate(user_subs):
        if sub[0] == lesson_code and sub[1] == crn_code:
            user_subs[i] = (lesson_code, crn_code, bolum)
            return True
    return False

def add_subscription(user_id, lesson_code, crn_code):
    """Kullanıcıyı derse abone eder. Dosyaya kaydetmek çağıranın sorumluluğundadır."""
    subscriptions.setdefault(user_id, []).append((lesson_code, crn_code, None))  # Tuple olarak kaydediyoruz, bölüm seçimi butonla yapılır

def remove_subscription(user_id, lesson_code, crn_code):
    """Aboneliği kaldırır ve kaldırılıp kaldırılmadığını döner. Dosyaya kaydetmek çağıranın sorumluluğundadır."""
    user_subs = subscriptions.get(user_id)
    if not user_subs:
        return False

    remaining = [sub for sub in user_subs if not (sub[0] == lesson_code and sub[1] == crn_code)]
    if len(remaining) == len(user_subs):
        return False

    user_subs[:] = remaining
    forget_notification_state(user_id, crn_code)
    return True

def forget_notification_state(user_id, crn_code=None):
    """Kullanıcının (crn_code verilirse yalnızca o dersin) bildirim geçmişini temizler."""
    def matches(key):
        return key[0] == user_id and (crn_code is None or key[1] == crn_code)

    for key in [key for key in last_msg_times if matches(key)]:
        del last_msg_times[key]
    open_notified.difference_update([key for key in open_notified if matches(key)])

def drop_blocked_user(user_id, error):
    """Botu engelleyen (ya da hesabını silen) kullanıcının tüm aboneliklerini düşürür."""
    removed = subscriptions.pop(user_id, None)
    forget_notification_state(user_id)
    if removed is not None:
        save_subscriptions()
        logger.info(f"{user_id} kullanıcısı botu engellediği için {len(removed)} aboneliği düşürüldü. Hata: {error}")

async def send_message_safe(bot, user_id, text, **kwargs):
    """Mesaj gönderir ve başarılı olup olmadığını döner. Kullanıcı botu engellediyse abonelikleri düşürülür."""
    try:
        await bot.send_message(chat_id=user_id, text=text, **kwargs)
        return True
    except Forbidden as e:
        drop_blocked_user(user_id, e)
    except Exception as e:
        logger.error(f"{user_id} kullanıcısına mesaj gönderilemedi. Hata: {e}")
    return False

def save_blocked_crns():
    with open(BLOCKED_CRN_FILE, 'w', encoding='utf-8') as f:
        json.dump(sorted(blocked_crns), f, indent=4, ensure_ascii=False)
    logger.info("Aboneliğe kapalı CRN listesi kaydedildi...")

def load_blocked_crns():
    global blocked_crns
    if os.path.exists(BLOCKED_CRN_FILE):
        try:
            with open(BLOCKED_CRN_FILE, 'r', encoding='utf-8') as f:
                blocked_crns = {str(crn) for crn in json.load(f)}
        except (json.decoder.JSONDecodeError, TypeError):
            blocked_crns = set()
    else:
        blocked_crns = set()

def count_crn_subscribers(crn_code):
    """Verilen CRN koduna abone olan kullanıcı sayısını döner."""
    total = 0
    for user_subs in subscriptions.values():
        for sub in user_subs:
            if sub[1] == crn_code:
                total += 1
    return total

def log_user_info(user_id, user_info):
    if not os.path.exists(USER_FILE):
        with open(USER_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)

    with open(USER_FILE, "r", encoding="utf-8") as f:
        try:
            users = json.load(f)
            if not isinstance(users, dict): 
                users = {}
        except json.JSONDecodeError: 
            users = {}

    user_id = str(user_id)  # JSON'dan okunan anahtarlar string olduğu için karşılaştırma string ile yapılır
    if user_id not in users:
        users[user_id] = user_info 

        with open(USER_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=4, ensure_ascii=False)

def remember_user(update: Update):
    """Botu kullanan kişinin bilgilerini user_info.json dosyasına kaydeder."""
    user = update.effective_user
    if user is None or update.effective_chat is None:
        return

    log_user_info(update.effective_chat.id, {
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    })

def fetch_branch_codes():
    url = "https://obs.itu.edu.tr/public/DersProgram/SearchBransKoduByProgramSeviye?programSeviyeTipiAnahtari=LS"
    
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        return response.json()  
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"Branş kodları alınamadı: {e}")
        return []

async def take_option_value(branch_code):
    """Verilen branş kodunun opsiyon değerini döner. Geçersiz kodda -1, branş listesi alınamazsa None döner.
    Liste ilk kullanımda arka planda çekilir; alınamazsa sonraki çağrıda tekrar denenir."""
    global branch_dict

    if not branch_dict:
        branch_codes = await asyncio.to_thread(fetch_branch_codes)
        branch_dict = {branch['dersBransKodu']: branch['bransKoduId'] for branch in branch_codes}
        if not branch_dict:
            return None
    
    return branch_dict.get(normalize_code(branch_code), -1)

def parse_rezervasyon(text):
    """Rezervasyon sütununu çözer: 'YZVE_LS/10/7 | Diğer/70/70' ->
    [{'bolum': 'YZVE_LS', 'kontenjan': 10, 'yazilan': 7}, {'bolum': 'Diğer', 'kontenjan': 70, 'yazilan': 70}]
    Rezervasyon yoksa ('-') boş liste döner. Bölüm sayısı sınırlı değildir."""
    rezervasyonlar = []
    if not text or text.strip() == '-':
        return rezervasyonlar

    for part in text.split('|'):
        part = part.strip()
        if not part:
            continue
        pieces = part.rsplit('/', 2)  # bölüm adı / kontenjan / yazılan
        if len(pieces) != 3:
            logger.warning(f"Rezervasyon bilgisi çözümlenemedi: {part}")
            continue
        try:
            rezervasyonlar.append({'bolum': pieces[0].strip(), 'kontenjan': int(pieces[1]), 'yazilan': int(pieces[2])})
        except ValueError:
            logger.warning(f"Rezervasyon bilgisi çözümlenemedi: {part}")

    return rezervasyonlar

def format_rezervasyon(rezervasyonlar):
    """Bölüm listesini 'YZVE_LS: 7/10 | Diğer: 70/70' (yazılan/kontenjan) şeklinde metne çevirir."""
    return " | ".join(f"{rez['bolum']}: {rez['yazilan']}/{rez['kontenjan']}" for rez in rezervasyonlar)

def capacity_for(ders, bolum):
    """Kullanıcının bölüm seçimine göre (yazılan, kontenjan, baz alınan bölüm) döner.
    Bölüm seçilmemişse (None) ya da seçilen bölüm artık rezervasyonda yoksa toplam kontenjana bakılır."""
    if bolum is not None:
        for rez in ders['rezervasyonlar']:
            if rez['bolum'] == bolum:
                return rez['yazilan'], rez['kontenjan'], bolum
    return ders['ogrenciSayisi'], ders['kontenjan'], None

def available_capacity_for(ders, bolum):
    """Kullanıcının bölüm seçimine göre boş kontenjanı ve baz alınan bölümü döner."""
    yazilan, kontenjan, secilen_bolum = capacity_for(ders, bolum)
    return kontenjan - yazilan, secilen_bolum

def cell_strings(cell):
    """Hücrenin kendi metin parçalarını döner (<br> ile ayrılan değerler ayrı parça olur).
    OBS bazen bir hücrenin </td> etiketini unutuyor, bu durumda sonraki hücreler bu hücrenin içine geçiyor; onların metni alınmaz."""
    return [text.strip() for text in cell.find_all(string=True) if text.strip() and text.find_parent('td') is cell]

def cell_text(cell):
    return " ".join(cell_strings(cell))

def cell_values(cell):
    """Çok oturumlu derslerde <br> ile ayrılmış değerleri döner. '-', '/', '-/-' gibi boş değerler atlanır."""
    return [text for text in cell_strings(cell) if text.strip('-/ ')]

def parse_egitmenler(cell):
    """Eğitmen hücresini isim listesine çevirir ('Ali Veli, Ayşe Kaya' ya da <br> ile ayrılmış isimler)."""
    egitmenler = []
    for text in cell_values(cell):
        for name in text.split(','):
            name = name.strip(' /')
            if name.strip('-'):
                egitmenler.append(name)
    return egitmenler

def parse_program(gun_cell, saat_cell):
    """Gün ve saat hücrelerini [('Pazartesi', '08:30-11:29'), ('Çarşamba', '13:30-15:29')] biçiminde oturum listesine çevirir."""
    gunler = cell_values(gun_cell)
    saatler = [saat.replace('/', '-') for saat in cell_values(saat_cell)]
    return list(zip_longest(gunler, saatler, fillvalue=''))

def fetch_lesson_table(lesson_code, lesson_id):
    """Ders programı tablosunu çeker ve satırları sözlük listesi olarak döner. İstek/parse hatalarında exception fırlatır.
    İstek ve HTML parse işlemi uzun sürebildiği için asenkron kodda doğrudan değil fetch_lessons üzerinden çağrılır."""
    global request_count
    request_count += 1

    response = requests.get(
        f"https://obs.itu.edu.tr/public/DersProgram/DersProgramSearch?ProgramSeviyeTipiAnahtari=LS&dersBransKoduId={lesson_id}&__RequestVerificationToken=bilgi_islem_naber",
        timeout=20
    )
    response.raise_for_status()
    fetched_at = turkey_now()

    soup = BeautifulSoup(response.text, 'html.parser')

    # Guncellenme saati artık gelen responseda olmadigi icin her response guncel kabul ediliyor. ayip ediyon bilgi islem.

    table = soup.find('table', {'id': 'dersProgramContainer'})
    if not table:
        raise ValueError(f"Ders programı tablosu bulunamadı: {lesson_code}")

    rows = table.find('tbody').find_all('tr')

    dersler = []
    for row in rows:
        cols = row.find_all('td')
        if len(cols) >= 11:
            crn = cell_text(cols[0])
            ders_kodu_element = cols[1].find('a')
            ders_kodu = ders_kodu_element.text.strip() if ders_kodu_element else cell_text(cols[1])
            ders_adi = cell_text(cols[2])
            kontenjan_str = cell_text(cols[9])
            yazilan_str = cell_text(cols[10])
            rezervasyon_str = cell_text(cols[11]) if len(cols) > 11 else '-'  # Reservasyon Böl./Kont./Yaz. sütunu

            try:
                kontenjan = int(kontenjan_str)
                ogrenci_sayisi = int(yazilan_str)
            except ValueError:
                logger.warning(f"Kontenjan veya öğrenci sayısı dönüştürülemedi: {crn}")
                continue

            dersler.append({
                'crn': crn,
                'dersKodu': ders_kodu,
                'dersAdi': ders_adi,
                'egitmenler': parse_egitmenler(cols[4]),      # Birden fazla eğitmen olabilir
                'program': parse_program(cols[6], cols[7]),  # [(gün, saat), ...] Çok oturumlu derslerde birden fazla
                'kontenjan': kontenjan,
                'ogrenciSayisi': ogrenci_sayisi,
                'rezervasyonlar': parse_rezervasyon(rezervasyon_str),  # Bölüm bazlı kontenjan, yoksa []
                'guncelleme': fetched_at
            })

    return dersler

async def fetch_lessons(lesson_code, lesson_id):
    """fetch_lesson_table'ı event loop'u bloklamadan arka planda çalıştırır ve gelen ders bilgilerini saklar."""
    dersler = await asyncio.to_thread(fetch_lesson_table, lesson_code, lesson_id)
    for ders in dersler:
        crn_details[ders['crn']] = ders
    return dersler

def find_sections(dersler, course_code):
    """Ders koduna (örn. 'BLG 102E') ait şubeleri döner. Boşluk ve büyük/küçük harf farkı gözetilmez."""
    target = normalize_code(course_code).replace(' ', '')
    return [ders for ders in dersler if normalize_code(ders['dersKodu']).replace(' ', '') == target]

def short_egitmenler(egitmenler, limit=3):
    """Uzun eğitmen listelerini ilk birkaç isim ve '+N kişi daha' şeklinde kısaltır."""
    if len(egitmenler) <= limit:
        return egitmenler
    return egitmenler[:limit] + [f"+{len(egitmenler) - limit} kişi daha"]

def format_program(program):
    """Oturumları gün adları hizalı satırlara çevirir: ['Pazartesi 08:30-11:29', 'Çarşamba  13:30-15:29']"""
    width = max((len(gun) for gun, _ in program), default=0)
    return [f"{gun.ljust(width)} {saat}".strip() for gun, saat in program]

def format_ders_table(ders):
    """Eğitmen, gün/saat, doluluk ve bölüm kontenjanlarını hizalı bir tablo (<pre>) olarak döner."""
    rows = [
        ('Eğitmen', short_egitmenler(ders['egitmenler']) or ['-']),
        ('Gün/Saat', format_program(ders['program']) or ['-']),
        ('Doluluk', [f"{ders['ogrenciSayisi']}/{ders['kontenjan']}"]),
    ]
    if ders['rezervasyonlar']:
        width = max(len(rez['bolum']) for rez in ders['rezervasyonlar'])
        rows.append(('Bölümler', [f"{rez['bolum'].ljust(width)} {rez['yazilan']}/{rez['kontenjan']}" for rez in ders['rezervasyonlar']]))

    label_width = max(len(label) for label, _ in rows) + 2
    lines = []
    for label, values in rows:
        for i, value in enumerate(values):
            lines.append((label if i == 0 else '').ljust(label_width) + value)

    # Avoid Telegram's copy-code button, which appears for <pre> blocks.
    return "\n".join(html_escape(line) for line in lines)

def capacity_status(yazilan, kontenjan):
    """Doluluk durumunu emoji ile döner: '🟢 47/50 · 3 boş yer' ya da '🔴 50/50 · dolu'"""
    bos = kontenjan - yazilan
    if bos > 0:
        return f"🟢 {yazilan}/{kontenjan} · {bos} boş yer"
    return f"🔴 {yazilan}/{kontenjan} · dolu"

def build_open_message(ders, available_capacity, secilen_bolum, others):
    """Kontenjan açıldı bildirimi: ders bilgileri tablo halinde ve bildirimin kaç kişiye daha gönderildiği bilgisiyle."""
    bolum_str = f"{html_escape(secilen_bolum)} bölümünde " if secilen_bolum else ""
    if others > 0:
        others_str = f"Bu bildirim {others} kişiye daha gönderildi."
    else:
        others_str = "Bu bildirim yalnızca size gönderildi."

    return (f"🔔 <b>{html_escape(ders['dersKodu'])} {html_escape(ders['crn'])}</b> için {bolum_str}<b>{available_capacity}</b> kontenjan var!\n"
            f"<i>{html_escape(ders['dersAdi'])}</i>\n"
            f"{format_ders_table(ders)}\n"
            f"{others_str}")

def build_open_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "📖 Ders kayıt sayfasını aç",
        url="https://obs.itu.edu.tr/ogrenci/DersKayitIslemleri/DersKayit",
    )]])

def build_full_message(ders, secilen_bolum):
    """Daha önce kontenjan var bildirimi gönderilen dersin tekrar dolduğunu bildirir."""
    yazilan, kontenjan, _ = capacity_for(ders, secilen_bolum)
    bolum_str = f" ({html_escape(secilen_bolum)} bölümü)" if secilen_bolum else ""
    return (f"🔴 <b>{html_escape(ders['dersKodu'])} {html_escape(ders['crn'])}</b>{bolum_str} için kontenjan doldu ({yazilan}/{kontenjan}).\n"
            f"Yer açılırsa tekrar bildirim alacaksınız.")

def format_check_block(ders):
    """/check sonucundaki tek bir şubenin bilgileri."""
    return (f"<b>{html_escape(ders['dersKodu'])} · {html_escape(ders['crn'])}</b>\n"
            f"<i>{html_escape(ders['dersAdi'])}</i>\n"
            f"{capacity_status(ders['ogrenciSayisi'], ders['kontenjan'])}\n"
            f"{format_ders_table(ders)}")

def button_rows(buttons, per_row=2):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]

def build_bolum_keyboard(lesson_code, crn_code, rezervasyonlar):
    """Bölüm seçimi butonları. callback_data: bolum|<ders_kodu>|<crn>|<bolum>  ('*' = toplam kontenjan)"""
    buttons = [
        InlineKeyboardButton(f"{rez['bolum']} ({rez['yazilan']}/{rez['kontenjan']})", callback_data=f"bolum|{lesson_code}|{crn_code}|{rez['bolum']}")
        for rez in rezervasyonlar
    ]
    rows = button_rows(buttons)  # Satır başına 2 buton
    rows.append([InlineKeyboardButton("Toplam kontenjan (bölüm seçme)", callback_data=f"bolum|{lesson_code}|{crn_code}|*")])
    return InlineKeyboardMarkup(rows)

def bolum_prompt_text(ders, existing_subscription, with_title=False):
    """Kontenjanı bölümlere ayrılmış derslerde bölüm seçimi açıklaması."""
    subject = f"{ders['dersKodu']} {ders['crn']} dersinin" if with_title else "Bu dersin"
    text = f"{subject} kontenjanı bölümlere ayrılmış (yazılan/kontenjan):\n{format_rezervasyon(ders['rezervasyonlar'])}\n"
    if existing_subscription is not None:
        return text + f"Mevcut seçiminiz: {existing_subscription[2] or 'Toplam kontenjan'}\nSeçiminizi değiştirmek için bölümünüzü seçin:"
    return text + ("Yalnızca kendi bölümünüzün kontenjanı açıldığında bildirim almak için bölümünüzü seçin. "
                   "Seçim yapmazsanız toplam kontenjana göre bildirim alırsınız.")

def sub_button(lesson_code, ders, subscribed):
    """Şube listesi ve /check sonuçlarındaki abone ol butonu. 🔔 abone olunabilecek, ✅ abone olunmuş şubeyi gösterir."""
    icon = "✅" if subscribed else "🔔"
    return InlineKeyboardButton(f"{icon} {ders['crn']} · {ders['ogrenciSayisi']}/{ders['kontenjan']}", callback_data=f"sub|{lesson_code}|{ders['crn']}")

async def send_blocks(bot, chat_id, blocks, reply_markup=None, parse_mode=ParseMode.HTML, separator="\n\n"):
    """Metin bloklarını Telegram mesaj sınırını aşmayacak şekilde birleştirerek gönderir. Butonlar son mesaja eklenir."""
    chunks = []
    for block in blocks:
        if chunks and len(chunks[-1]) + len(separator) + len(block) <= MESSAGE_LIMIT:
            chunks[-1] += separator + block
        else:
            chunks.append(block)

    for i, chunk in enumerate(chunks):
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode, reply_markup=reply_markup if i == len(chunks) - 1 else None)

async def send_section_list(context, user_id, lesson_code, sections):
    """Bir dersin şubelerini doluluk, eğitmen ve gün/saat bilgisiyle listeler. Butonlara dokunarak abone olunur."""
    shown = sections[:MAX_SECTION_BUTTONS]
    blocks = [f"<b>{html_escape(sections[0]['dersKodu'])}</b> · {html_escape(sections[0]['dersAdi'])}\n"
              f"{len(sections)} şube bulundu. Abone olmak istediğiniz şubelerin butonlarına dokunun (birden fazla seçebilirsiniz)."]
    buttons = []
    for ders in shown:
        egitmen = ", ".join(short_egitmenler(ders['egitmenler'], limit=2)) or "-"
        program = ", ".join(f"{gun} {saat}".strip() for gun, saat in ders['program']) or "-"
        blocks.append(f"<b>{html_escape(ders['crn'])}</b> · {capacity_status(ders['ogrenciSayisi'], ders['kontenjan'])}\n"
                      f"👤 {html_escape(egitmen)}\n"
                      f"🕒 {html_escape(program)}")
        buttons.append(sub_button(lesson_code, ders, find_subscription(user_id, lesson_code, ders['crn']) is not None))

    if len(sections) > len(shown):
        blocks.append(f"… ve {len(sections) - len(shown)} şube daha. Listede olmayan şubelere CRN yazarak abone olabilirsiniz: /subscribe {lesson_code} CRN")
    blocks.append("🔔 abone olabileceğiniz, ✅ abone olduğunuz şubeleri gösterir.")

    await send_blocks(context.bot, user_id, blocks, InlineKeyboardMarkup(button_rows(buttons)))

async def subscribe_crns(context, user_id, lesson_code, crn_codes, dersler):
    """Kullanıcıyı bir ders kodundaki bir veya birden fazla CRN'e abone eder ve sonucu bildirir.
    dersler None ise (ders programı çekilemediyse) CRN'ler doğrulanmadan abone olunur, geçersiz olanlar kontrol döngüsünde temizlenir."""
    dersler_by_crn = {ders['crn']: ders for ders in dersler} if dersler is not None else None
    results = []
    bolum_prompts = []  # Kontenjanı bölümlere ayrılmış dersler: (ders, mevcut abonelik)
    subscribed_any = False

    for crn_code in unique(crn_codes):
        if not is_valid_crn(crn_code):
            results.append(f'❌ {crn_code}: CRN kodu 4 veya 5 haneli bir sayı olmalıdır.')
            continue

        ders = dersler_by_crn.get(crn_code) if dersler_by_crn is not None else None
        if dersler_by_crn is not None and ders is None:
            results.append(f"❌ {crn_code}: {lesson_code} dersleri arasında bulunamadı. Ders kodunu ve CRN'i kontrol edin.")
            continue

        existing_subscription = find_subscription(user_id, lesson_code, crn_code)
        if existing_subscription is None and crn_code in blocked_crns:
            results.append(f'⛔ {crn_code}: Bu derse geçici olarak abone olamazsınız.')
            logger.info(f"{user_id} kullanıcısı aboneliğe kapalı {lesson_code} {crn_code} dersine abone olmak istedi.")
            continue

        label = f"{ders['dersKodu']} {crn_code} ({ders['dersAdi']})" if ders else f"{lesson_code} {crn_code}"
        if existing_subscription is not None:
            results.append(f'ℹ️ {label} için zaten abonesiniz.')
        else:
            add_subscription(user_id, lesson_code, crn_code)
            subscribed_any = True
            results.append(f'✅ {label} için kontenjan durumunu kontrol etmeye başladım. Bu derse abone {count_crn_subscribers(crn_code)} kişi var.')
            logger.info(f"{user_id} kullanıcısı {lesson_code} {crn_code} için kontenjan durumunu kontrol etmeye başladı.")

        if ders and ders['rezervasyonlar']:
            bolum_prompts.append((ders, existing_subscription))

    if subscribed_any:
        save_subscriptions()  # Abonelikleri kaydet

    if len(results) == 1 and bolum_prompts:  # Tek ders: sonuç ve bölüm seçimi aynı mesajda
        ders, existing_subscription = bolum_prompts[0]
        await context.bot.send_message(
            chat_id=user_id,
            text=f"{results[0]}\n\n{bolum_prompt_text(ders, existing_subscription)}",
            reply_markup=build_bolum_keyboard(lesson_code, ders['crn'], ders['rezervasyonlar'])
        )
        return

    await send_blocks(context.bot, user_id, results, parse_mode=None, separator="\n")
    for ders, existing_subscription in bolum_prompts:
        await context.bot.send_message(
            chat_id=user_id,
            text=bolum_prompt_text(ders, existing_subscription, with_title=True),
            reply_markup=build_bolum_keyboard(lesson_code, ders['crn'], ders['rezervasyonlar'])
        )

def set_flow_state(context, state, branch=None):
    """Adım adım abonelik bilgisini sonraki duruma göre günceller ve durumu döner. Akış bittiğinde bilgi silinir."""
    if state == ConversationHandler.END:
        context.user_data.pop('subscribe_flow', None)
    else:
        flow = context.user_data.setdefault('subscribe_flow', {})
        flow['time'] = turkey_now()
        if branch is not None:
            flow['branch'] = branch
    return state

def subscribe_flow_expired(context):
    flow = context.user_data.get('subscribe_flow')
    return flow is None or turkey_now() - flow['time'] > SUBSCRIBE_FLOW_TIMEOUT

async def handle_course_input(context, user_id, tokens, retry_state=None):
    """/subscribe girdisini işler ve konuşmanın sonraki durumunu döner:
    - yalnızca ders kodu (BLG): ders numarası ya da CRN sorulur
    - ders kodu + ders numarası (BLG 102E): şubeler butonlarla listelenir
    - ders kodu + CRN'ler (BLG 13547 13548): doğrudan abone olunur
    retry_state verilmişse (adım adım akış) hatalı girdide aynı soruda kalınır, verilmemişse (tek komut) akış biter."""
    fail_state = ConversationHandler.END if retry_state is None else retry_state
    if not tokens:
        await context.bot.send_message(chat_id=user_id, text='Lütfen ders kodunu yazın (örn. BLG 102E ya da BLG 13547).')
        return set_flow_state(context, fail_state)

    lesson_code, rest = tokens[0], tokens[1:]
    lesson_id = await take_option_value(lesson_code)
    if lesson_id is None:
        await context.bot.send_message(chat_id=user_id, text=OBS_UNREACHABLE_TEXT)
        return set_flow_state(context, fail_state)
    if lesson_id == -1:
        await context.bot.send_message(chat_id=user_id, text=f'"{lesson_code}" geçerli bir ders kodu değil. Ders kodunu kontrol edip tekrar yazın (örn. BLG 102E ya da BLG 13547).')
        return set_flow_state(context, fail_state)

    if not rest:
        await context.bot.send_message(chat_id=user_id, text=f"{lesson_code} dersinin numarasını (örn. 102E) ya da abone olmak istediğiniz CRN kodlarını yazın.\nİptal etmek için /cancel")
        return set_flow_state(context, ASK_DETAIL, branch=lesson_code)

    # Ders programını çek: CRN'ler doğrulanır, ders numarası yazıldıysa şubeler bulunur
    try:
        dersler = await fetch_lessons(lesson_code, lesson_id)
    except Exception as e:
        # Tablo çekilemezse eski davranış: doğrulama yapılmadan abone edilir, geçersiz CRN kontrol döngüsünde temizlenir
        logger.warning(f"{lesson_code} için ders programı çekilemedi, CRN'ler doğrulanmadan devam ediliyor: {e}")
        dersler = None

    if len(rest) == 1:
        course_code = f"{lesson_code} {rest[0]}"
        if dersler is not None and not any(ders['crn'] == rest[0] for ders in dersler):
            sections = find_sections(dersler, course_code)
            if sections:
                await send_section_list(context, user_id, lesson_code, sections)
                return set_flow_state(context, ConversationHandler.END)

        if not is_valid_crn(rest[0]):  # CRN değil, ders numarası yazılmış
            text = OBS_UNREACHABLE_TEXT if dersler is None else f"{course_code} için açılmış şube bulunamadı. Ders numarasını kontrol edin ya da CRN kodunu yazın."
            await context.bot.send_message(chat_id=user_id, text=text)
            return set_flow_state(context, fail_state)

    await subscribe_crns(context, user_id, lesson_code, rest, dersler)
    return set_flow_state(context, ConversationHandler.END)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Merhaba! Kontenjan durumunu takip etmek istediğiniz derse abone olmak için /subscribe komutunu kullanın, ders kodunu ve şubeyi adım adım soracağım.\nCRN kodunu biliyorsanız doğrudan /subscribe <DERS_KODU> <CRN> yazabilirsiniz.\n(Yalnızca Lisans seviyesi dersler!)\n\nAbone olmadan anlık kontenjan sorgulamak için /check <DERS_KODU> <CRN>\nTüm komutları görmek için /help komutunu kullanın.')

async def help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('/subscribe  -  Adım adım abone ol (ders kodunu sorar, şubeleri listeler).\n/subscribe <DERS_KODU> <CRN> [CRN ...]  -  Bir veya birden fazla şubeye abone ol.\n/subscribe <DERS_KODU> <DERS_NO>  -  Dersin şubelerini listele, butonla abone ol.\n/check <DERS_KODU> <CRN> [CRN ...]  -  Abone olmadan anlık kontenjan sorgula.\n/sublist  -  Aboneliklerini ve güncel doluluklarını göster, butonla çık.\n/unsubscribe <DERS_KODU> <CRN> [CRN ...]  -  Abonelikten ayrıl.\n/clearall - Aktif tüm aboneliklerden ayrıl.\n/cancel - Adım adım abonelik işlemini iptal et.\n/sendmessage <MESAJ> - Admine şikayet veya önerilerinizi gönderebilirsiniz\n\nÖrnek kullanım: "/subscribe BLG 13547 13548", "/subscribe BLG 102E", "/check BLG 13547" \nDers kodlarını büyük ya da küçük harfle yazabilirsiniz.\n\nBu bot abone olduğunuz derslerin kontenjan durumlarını belirli aralıklarla kontrol eder. Eğer boş yer varsa size bildirir. Boş yer açılana kadar mesaj almazsınız. Kontenjan tekrar dolduğunda bir kez "kontenjan doldu" mesajı alırsınız.\nNOT: Bir ders için kontenjan var mesajı aldıktan sonra spama düşmemek amacıyla aynı ders için sonraki 3 dakika boyunca mesaj almazsınız, diğer derslerin kontrolü devam eder. \nKontenjanı bölümlere ayrılmış derslerde (örn. YZVE_LS/10/7 | Diğer/70/70) abone olurken bölümünüzü seçebilirsiniz; böylece yalnızca kendi bölümünüzün kontenjanı açıldığında bildirim alırsınız. \n\nDikkat: Bu bot şu anda çalışıyor olsa bile ilerleyen zamanda bilgi işlemin yapabileceği değişikliklerden etkilenebilir ve görevini yapamayabilir. Ya da ben serveri kapatabilirim :D\nServer admin tarafından kapatıldığı durumda kullanıcılara bilgilendirme mesajı gönderilecektir.')

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    remember_user(update)
    tokens = tokenize_course_input(" ".join(context.args))

    if not tokens:  # Argümansız kullanım: adım adım abonelik
        await update.message.reply_text('Hangi dersi takip etmek istiyorsunuz? Ders kodunu yazın.\n'
                                        '"BLG 102E" yazarsanız şubeleri listelerim, "BLG 13547" yazarsanız doğrudan abone olursunuz.\n'
                                        '(Yalnızca Lisans seviyesi dersler!)\n\nİptal etmek için /cancel')
        return set_flow_state(context, ASK_COURSE)

    return await handle_course_input(context, update.effective_chat.id, tokens)

async def subscribe_course_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Adım adım abonelik: ders kodu cevabı."""
    if subscribe_flow_expired(context):  # Uzun süre cevap verilmediyse mesaj abonelik cevabı sayılmaz
        return set_flow_state(context, ConversationHandler.END)

    remember_user(update)
    tokens = tokenize_course_input(update.message.text)
    return await handle_course_input(context, update.effective_chat.id, tokens, retry_state=ASK_COURSE)

async def subscribe_detail_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Adım adım abonelik: ders numarası / CRN cevabı. Ders kodu yazılmadıysa önceki adımdaki ders kodu kullanılır."""
    if subscribe_flow_expired(context):  # Uzun süre cevap verilmediyse mesaj abonelik cevabı sayılmaz
        return set_flow_state(context, ConversationHandler.END)

    remember_user(update)
    tokens = tokenize_course_input(update.message.text)
    branch = context.user_data['subscribe_flow'].get('branch')
    if tokens and branch and not tokens[0].isalpha():  # "102E" ya da "13547 13548" yazıldı
        tokens.insert(0, branch)
    return await handle_course_input(context, update.effective_chat.id, tokens, retry_state=ASK_DETAIL)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('Abonelik işlemi iptal edildi.')
    return set_flow_state(context, ConversationHandler.END)

async def cancel_idle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('İptal edilecek bir işlem yok.')

async def subscribe_busy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Önceki abonelik isteği (OBS sorgusu) sürerken aynı kullanıcıdan gelen abonelik mesajları."""
    await update.message.reply_text('⏳ Önceki isteğiniz işleniyor, lütfen birkaç saniye sonra tekrar deneyin.')

async def subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Şube listesindeki ya da /check sonucundaki abone ol butonuna basıldığında abone eder. callback_data: sub|<ders_kodu>|<crn>"""
    query = update.callback_query
    parts = query.data.split('|', 2)
    if len(parts) != 3:
        await query.answer()
        return
    _, lesson_code, crn_code = parts
    user_id = update.effective_chat.id

    if find_subscription(user_id, lesson_code, crn_code) is not None:
        await query.answer('Bu şubeye zaten abonesiniz. Aboneliklerinizi /sublist ile yönetebilirsiniz.')
        await mark_subscribed_button(query)
        return
    await query.answer()

    lesson_id = await take_option_value(lesson_code)
    if lesson_id is None or lesson_id == -1:
        await context.bot.send_message(chat_id=user_id, text=OBS_UNREACHABLE_TEXT if lesson_id is None else f'"{lesson_code}" geçerli bir ders kodu değil.')
        return

    ders = crn_details.get(crn_code)
    if ders is not None and turkey_now() - ders['guncelleme'] <= DETAIL_CACHE_TTL:
        dersler = [ders]  # Liste az önce çekildi, tekrar istek atılmaz
    else:
        try:
            dersler = await fetch_lessons(lesson_code, lesson_id)
        except Exception as e:
            logger.warning(f"{lesson_code} için ders programı çekilemedi, {crn_code} doğrulanmadan devam ediliyor: {e}")
            dersler = None

    remember_user(update)
    await subscribe_crns(context, user_id, lesson_code, [crn_code], dersler)
    if find_subscription(user_id, lesson_code, crn_code) is not None:
        await mark_subscribed_button(query)

async def mark_subscribed_button(query):
    """Basılan abone ol butonunu ✅ ile işaretler."""
    markup = getattr(query.message, 'reply_markup', None)
    if markup is None:
        return

    changed = False
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for button in row:
            if button.callback_data == query.data and button.text.startswith('🔔'):
                button = InlineKeyboardButton(button.text.replace('🔔', '✅', 1), callback_data=button.callback_data)
                changed = True
            new_row.append(button)
        rows.append(new_row)

    if changed:
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
        except BadRequest as e:
            logger.warning(f"Abone ol butonu güncellenemedi: {e}")

async def bolum_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bölüm seçimi butonuna basıldığında aboneliğin bölüm bilgisini günceller."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split('|', 3)  # bolum|<ders_kodu>|<crn>|<bolum>
    if len(parts) != 4:
        return
    _, lesson_code, crn_code, bolum = parts
    if bolum == '*':
        bolum = None

    user_id = query.message.chat_id if query.message else query.from_user.id

    if set_subscription_bolum(user_id, lesson_code, crn_code, bolum):
        save_subscriptions()
        logger.info(f"{user_id} kullanıcısı {lesson_code} {crn_code} için bölüm seçti: {bolum or 'Toplam kontenjan'}")
        if bolum is None:
            text = f'{lesson_code} {crn_code} için toplam kontenjana göre bildirim alacaksınız.'
        else:
            text = f'{lesson_code} {crn_code} için yalnızca {bolum} bölümünün kontenjanı açıldığında bildirim alacaksınız.'
    else:
        text = f'{lesson_code} {crn_code} için aktif aboneliğiniz bulunmuyor. Abone olmak için: /subscribe {lesson_code} {crn_code}'

    try:
        await query.edit_message_text(text)  # Butonları kaldırıp sonucu yazar
    except Exception as e:  # Mesaj çok eskiyse düzenlenemez, yeni mesaj gönder
        logger.warning(f"{user_id} için bölüm seçimi mesajı düzenlenemedi: {e}")
        await context.bot.send_message(chat_id=user_id, text=text)

async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abone olmadan anlık kontenjan sorgusu: /check <DERS_KODU> <CRN> [CRN ...] ya da /check <DERS_KODU> <DERS_NO>"""
    remember_user(update)
    user_id = update.effective_chat.id
    tokens = tokenize_course_input(" ".join(context.args))
    if len(tokens) < 2:
        await update.message.reply_text('Lütfen geçerli formatta giriş yapın: /check <DERS_KODU> <CRN> [CRN ...]\nÖrnek: "/check BLG 13547" ya da bir dersin tüm şubeleri için "/check BLG 102E"')
        return

    lesson_code, crn_codes = tokens[0], unique(tokens[1:])
    lesson_id = await take_option_value(lesson_code)
    if lesson_id is None:
        await update.message.reply_text(OBS_UNREACHABLE_TEXT)
        return
    if lesson_id == -1:
        await update.message.reply_text(f'"{lesson_code}" geçerli bir ders kodu değil: /check <DERS_KODU> <CRN>')
        return

    try:
        dersler = await fetch_lessons(lesson_code, lesson_id)
    except Exception as e:
        logger.warning(f"/check sırasında {lesson_code} ders programı çekilemedi: {e}")
        await update.message.reply_text(OBS_UNREACHABLE_TEXT)
        return
    
    dersler_by_crn = {ders['crn']: ders for ders in dersler}
    if len(crn_codes) == 1 and crn_codes[0] not in dersler_by_crn:  # Ders numarası yazıldıysa tüm şubeleri listele
        sections = find_sections(dersler, f"{lesson_code} {crn_codes[0]}")
        if sections:
            await send_section_list(context, user_id, lesson_code, sections)
            return

    blocks = []
    buttons = []
    for crn_code in crn_codes:
        ders = dersler_by_crn.get(crn_code)
        if ders is None:
            blocks.append(f"❌ {html_escape(crn_code)}: {lesson_code} dersleri arasında bulunamadı.")
            continue
        blocks.append(format_check_block(ders))
        buttons.append(sub_button(lesson_code, ders, find_subscription(user_id, lesson_code, crn_code) is not None))

    if any(button.text.startswith('🔔') for button in buttons):
        blocks.append("🔔 butonlarına dokunarak abone olabilirsiniz.")
    reply_markup = InlineKeyboardMarkup(button_rows(buttons)) if buttons else None
    await send_blocks(context.bot, user_id, blocks, reply_markup)

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tokens = tokenize_course_input(" ".join(context.args))
    if len(tokens) < 2:
        await update.message.reply_text('Lütfen geçerli formatta giriş yapın: /unsubscribe <DERS_KODU> <CRN>\nAboneliklerinizi görüp butonla çıkmak için /sublist komutunu kullanabilirsiniz.')
        return

    lesson_code, crn_codes = tokens[0], unique(tokens[1:])
    user_id = update.effective_chat.id

    remember_user(update)

    results = []
    sub_cancelled = False
    for crn_code in crn_codes:
        if remove_subscription(user_id, lesson_code, crn_code):
            sub_cancelled = True
            results.append(f'{lesson_code} {crn_code} için aboneliğiniz iptal edilmiştir.')
            logger.info(f"{user_id} kullanıcısı {lesson_code} {crn_code} aboneliğinden ayrıldı.")
        else:
            results.append(f'{lesson_code} {crn_code} için aboneliğiniz yok veya yanlış CRN kodu girdiniz.')
                
    if sub_cancelled:
        save_subscriptions()  # Abonelikleri kaydet

    await update.message.reply_text("\n".join(results))

async def clear_all_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.chat_id 

    remember_user(update)

    # Kullanıcının aboneliklerini kontrol et
    if user_id in subscriptions and subscriptions[user_id]:
        # Tüm abonelikleri sil
        subscriptions[user_id].clear()
        forget_notification_state(user_id)
        await update.message.reply_text('Tüm abonelikleriniz başarıyla temizlendi.')
        save_subscriptions()  # Abonelikleri kaydet
    else:
        await update.message.reply_text('Zaten herhangi bir aboneliğiniz bulunmuyor.')

def build_sublist(user_id):
    """/sublist mesajının metnini (HTML) ve butonlarını hazırlar. Doluluk bilgileri kontrol döngüsünün son verisinden gelir."""
    user_subs = subscriptions.get(user_id, [])
    if not user_subs:
        return 'Henüz herhangi bir derse abone olmadınız.\nAbone olmak için /subscribe komutunu kullanın.', None

    full_entries = []   # Ders adı ve bölüm bilgisiyle ayrıntılı liste
    short_entries = []  # Çok sayıda abonelikte kullanılan tek satırlık liste
    update_times = []
    for index, (lesson_code, crn_code, bolum) in enumerate(user_subs, 1):
        bolum_note = f" · {html_escape(bolum)}" if bolum else ""
        ders = crn_details.get(crn_code)
        if ders is None:
            full_entries.append(f"<b>{index}. {html_escape(lesson_code)}</b> · CRN {html_escape(crn_code)}{bolum_note}\n⏳ Kontenjan bilgisi henüz alınmadı")
            short_entries.append(f"{index}. {html_escape(lesson_code)} {html_escape(crn_code)} · ⏳{bolum_note}")
            continue

        update_times.append(ders['guncelleme'])
        yazilan, kontenjan, secilen_bolum = capacity_for(ders, bolum)
        status = capacity_status(yazilan, kontenjan)
        if secilen_bolum:
            status += f" ({html_escape(secilen_bolum)} kontenjanı)"
        elif bolum:
            status += f" (seçtiğiniz {html_escape(bolum)} bölümünün ayrı kontenjanı kalmadı, toplam kontenjan takip ediliyor)"

        full_entries.append(f"<b>{index}. {html_escape(ders['dersKodu'])}</b> · CRN {html_escape(crn_code)}\n"
                            f"<i>{html_escape(ders['dersAdi'])}</i>\n"
                            f"{status}")
        short_entries.append(f"{index}. {html_escape(ders['dersKodu'])} {html_escape(crn_code)} · {capacity_status(yazilan, kontenjan)}{bolum_note}")

    header = f"📋 <b>Abonelikleriniz ({len(user_subs)})</b>"
    footer = []
    if update_times:
        footer.append(f"🕒 Son kontrol: {min(update_times).strftime('%H:%M')}")
    footer.append("Bir abonelikten çıkmak için ilgili ❌ butonuna dokunun.")

    text = "\n\n".join([header, *full_entries, "\n".join(footer)])
    if len(text) > MESSAGE_LIMIT:  # Çok sayıda abonelik: tek satırlık liste, sığmayanlar özetlenir
        lines = [header, ""]
        length = sum(len(line) + 1 for line in lines + footer) + 50
        for i, entry in enumerate(short_entries):
            if length + len(entry) + 1 > MESSAGE_LIMIT:
                lines.append(f"… ve {len(short_entries) - i} abonelik daha")
                break
            lines.append(entry)
            length += len(entry) + 1
        text = "\n".join(lines + [""] + footer)

    buttons = [InlineKeyboardButton(f"❌ Çık: {crn_code}", callback_data=f"unsub|{lesson_code}|{crn_code}")
               for lesson_code, crn_code, _ in user_subs[:MAX_SUBLIST_BUTTONS]]
    rows = button_rows(buttons)
    rows.append([InlineKeyboardButton("🔄 Yenile", callback_data="sublist|refresh")])
    return text, InlineKeyboardMarkup(rows)

async def sublist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)

    text, reply_markup = build_sublist(update.message.chat_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    
async def sublist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abonelik listesindeki butonlar. callback_data: unsub|<ders_kodu>|<crn> ya da sublist|refresh"""
    query = update.callback_query
    parts = query.data.split('|', 2)
    user_id = update.effective_chat.id

    if parts[0] == 'unsub' and len(parts) == 3:
        _, lesson_code, crn_code = parts
        if remove_subscription(user_id, lesson_code, crn_code):
            save_subscriptions()
            logger.info(f"{user_id} kullanıcısı {lesson_code} {crn_code} aboneliğinden ayrıldı.")
            await query.answer(f'{lesson_code} {crn_code} için aboneliğiniz iptal edildi.')
        else:
            await query.answer('Bu aboneliğiniz zaten bulunmuyor.')
    else:
        await query.answer('Liste güncellendi.')

    text, reply_markup = build_sublist(user_id)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if 'not modified' in str(e).lower():  # Liste değişmediyse Telegram düzenlemeyi reddeder
            return
        logger.warning(f"{user_id} için abonelik listesi düzenlenemedi, yeni mesaj gönderiliyor: {e}")
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def updateallusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Only for admin
    admin_id = ADMIN_ID
    if update.message.chat_id != admin_id:
        await update.message.reply_text('Bu komutu kullanma yetkiniz yok.')
        return

    save_subscriptions()
    global is_subs_updated
    if not is_subs_updated:
        is_subs_updated = True
        for user_id in subscriptions.keys():
            try:    
                message = "Bot tekrar çalışır durumdadır. Aktif ders aboneliklerinizi /sublist komutu ile görüntüleyebilirsiniz."
                # await context.bot.send_message(chat_id=user_id, text=message)
            except:
                logger.info(f"Kullanıcı {user_id} ye mesaj gönderilemedi.")
    else:
        await update.message.reply_text("Abonelikler Güncel.")

    total_sub = 0
    for i in range(len(subscriptions.keys())):

        key = list(subscriptions.keys())[i]
        subs = subscriptions[key]
        sub_num = len(subs)
        total_sub += sub_num
    await update.message.reply_text(f"User Number: {len(subscriptions.keys())}\nTotal Subscription: {total_sub}\n")
    logger.info(f"Request Count: {request_count}")

async def check_capacity_optimized(context):
    lesson_codes = {lesson_code for user_subs in subscriptions.values() for lesson_code, _, _ in user_subs}

    # Her ders kodu için sadece bir istek atılacak
    for lesson_code in lesson_codes:
        try:
            await check_lesson(context, lesson_code)
        except Exception as e:
            logger.error(f"{lesson_code} kontrol edilirken hata oluştu: {e}")

    await asyncio.sleep(5)

async def check_lesson(context, lesson_code):
    """Bir ders kodunun ders programını tek istekle çeker, geçersiz CRN'leri temizler ve abonelere bildirim gönderir."""
    lesson_id = await take_option_value(lesson_code)
    if lesson_id is None:
        logger.warning(f"Branş kodları alınamadığı için {lesson_code} kontrol edilemedi.")
        return
    if lesson_id == -1:
        logger.warning(f"Geçersiz ders kodu: {lesson_code}")
        return

    try:
        dersler = await fetch_lessons(lesson_code, lesson_id)
    except requests.exceptions.RequestException as e:
        logger.error(f"Ders kodu {lesson_code} için istek atılırken hata oluştu: {e}")
        return
    except Exception as e:
        logger.error(f"HTML parse edilirken hata oluştu {lesson_code}: {e}")
        return

    dersler_by_crn = {ders['crn']: ders for ders in dersler}

    # Aboneler istekten sonra alınır, istek sürerken abone olan/ayrılan kullanıcılar da hesaba katılır
    subscribers_by_crn = {}  # crn -> {user_id: bolum}
    for user_id, user_subs in subscriptions.items():
        for sub_lesson, crn_code, bolum in user_subs:
            if sub_lesson == lesson_code:
                subscribers_by_crn.setdefault(crn_code, {}).setdefault(user_id, bolum)

    # Check for invalid CRNs and remove subscriptions. Boş tablo geçici bir OBS hatası olabileceği için abonelikler silinmez.
    invalid_crns = {crn_code: subscribers for crn_code, subscribers in subscribers_by_crn.items() if crn_code not in dersler_by_crn}
    if invalid_crns and dersler:
        await remove_invalid_subscriptions(context, lesson_code, invalid_crns)

    # Check capacity for each course
    for crn_code, subscribers in subscribers_by_crn.items():
        if crn_code in dersler_by_crn:
            await notify_subscribers(context, dersler_by_crn[crn_code], subscribers)

async def remove_invalid_subscriptions(context, lesson_code, invalid_crns):
    """Ders programında bulunmayan CRN'lere ait abonelikleri kaldırır ve kullanıcıları bilgilendirir. invalid_crns: {crn: {user_id: bolum}}"""
    removed = []
    for crn_code, subscribers in invalid_crns.items():
        for user_id in subscribers:
            if remove_subscription(user_id, lesson_code, crn_code):
                removed.append((user_id, crn_code))
                logger.info(f"Geçersiz CRN: {crn_code} için {user_id} kullanıcısının aboneliği kaldırıldı.")

    if not removed:
        return
    save_subscriptions()

    for user_id, crn_code in removed:
        message = f"{lesson_code} {crn_code} geçersiz bir CRN kodu olduğu için aboneliğiniz iptal edilmiştir."
        await send_message_safe(context.bot, user_id, message)

async def notify_subscribers(context, ders, subscribers):
    """Bir şubenin abonelerine kontenjan var / kontenjan doldu mesajlarını gönderir. subscribers: {user_id: bolum}"""
    crn = ders['crn']
    now = turkey_now()
    open_targets = []  # (user_id, boş kontenjan, baz alınan bölüm)
    full_targets = []  # (user_id, baz alınan bölüm)

    for user_id, bolum in subscribers.items():
        # Bölüm seçen kullanıcı için o bölümün, diğerleri için toplam kontenjana bakılır
        available_capacity, secilen_bolum = available_capacity_for(ders, bolum)
        key = (user_id, crn)
        if available_capacity > 0:
            if last_msg_times.get(key) is None or (now - last_msg_times[key]) > NOTIFY_COOLDOWN:  # Prevent spamming
                open_targets.append((user_id, available_capacity, secilen_bolum))
        elif key in open_notified:  # Kontenjan var mesajı almıştı, doldu bilgisi yalnızca bir kez gönderilir
            full_targets.append((user_id, secilen_bolum))

    for user_id, available_capacity, secilen_bolum in open_targets:
        key = (user_id, crn)
        last_msg_times[key] = now
        message = build_open_message(ders, available_capacity, secilen_bolum, others=len(open_targets) - 1)
        if await send_message_safe(context.bot, user_id, message, parse_mode=ParseMode.HTML, reply_markup=build_open_keyboard()):
            open_notified.add(key)
            logger.info(f"ID:{user_id} Kullanıcısına {ders['dersKodu']} {crn} için {available_capacity} kontenjan var mesajı gönderilmiştir.")

    for user_id, secilen_bolum in full_targets:
        open_notified.discard((user_id, crn))
        if await send_message_safe(context.bot, user_id, build_full_message(ders, secilen_bolum), parse_mode=ParseMode.HTML):
            logger.info(f"ID:{user_id} Kullanıcısına {ders['dersKodu']} {crn} için kontenjan doldu mesajı gönderilmiştir.")

async def send_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text('Lütfen bir mesaj yazın: /sendmessage <mesaj>')
        return

    user_message = " ".join(context.args) 
    user_id = update.message.chat_id 
    timestamp = turkey_now().strftime("%Y-%m-%d %H:%M:%S") 

    remember_user(update)

    with open("messages.txt", "a", encoding="utf-8") as f:
        f.write(f"{timestamp} - Kullanıcı ID: {user_id} - Mesaj: {user_message}\n")

    await update.message.reply_text('Mesajınız admin\'e iletildi. Teşekkür ederiz!')

    # admin bilgilendirmesi
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"Yeni mesaj var!\n\nKullanıcı ID: {user_id}\nMesaj: {user_message}")

async def read_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Only for admin
    admin_id = ADMIN_ID 
    if update.message.chat_id != admin_id:
        await update.message.reply_text('Bu komutu kullanma yetkiniz yok.')
        return

    try:
        with open("messages.txt", "r", encoding="utf-8") as f:
            messages = f.read()
            if not messages:
                await update.message.reply_text('Henüz hiç mesaj yok.')
            else:
                await send_long_message(update, f"Mesajlar:\n\n{messages}")
    except FileNotFoundError:
        await update.message.reply_text('Henüz hiç mesaj yok.')

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Only for admin
    admin_id = ADMIN_ID 
    if update.message.chat_id != admin_id:
        await update.message.reply_text('Bu komutu kullanma yetkiniz yok.')
        return

    if not context.args:
        await update.message.reply_text('Lütfen bir mesaj yazın: /broadcast <mesaj>')
        return

    broadcast_message = " ".join(context.args)  

    user_ids = list(subscriptions.keys())  # Gönderim sırasında botu engelleyen kullanıcılar sözlükten silinebilir
    sent_count = 0
    for user_id in user_ids:
        if await send_message_safe(context.bot, user_id, broadcast_message):
            sent_count += 1

    await update.message.reply_text(f'Mesaj {sent_count}/{len(user_ids)} kullanıcıya gönderildi.')

async def send_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Only for admin
    admin_id = ADMIN_ID 
    if update.message.chat_id != admin_id:
        await update.message.reply_text('Bu komutu kullanma yetkiniz yok.')
        return

    if len(context.args) < 2:
        await update.message.reply_text('Lütfen geçerli formatta giriş yapın: /sendto <user_id> <mesaj>')
        return

    try:
        user_id = int(context.args[0]) 
    except ValueError:
        await update.message.reply_text('Geçersiz kullanıcı ID\'si. Lütfen bir sayı girin.')
        return

    user_message = " ".join(context.args[1:])  

    try:
        await context.bot.send_message(chat_id=user_id, text=user_message)
        await update.message.reply_text(f'Mesaj, kullanıcı {user_id} ye gönderildi.')
    except Forbidden as e:
        drop_blocked_user(user_id, e)
        await update.message.reply_text(f'Kullanıcı {user_id} botu engellemiş, abonelikleri düşürüldü.')
    except Exception as e:
        logger.error(f"Kullanıcı {user_id} ye mesaj gönderilemedi: {e}")
        await update.message.reply_text(f'Kullanıcı {user_id} ye mesaj gönderilemedi. Hata: {e}')

async def send_long_message(update, text):
    """Telegram mesaj sınırını aşan metinleri satır sonlarından bölerek gönderir."""
    chunk_limit = 4000
    chunk = ''
    for line in text.split('\n'):
        while len(line) > chunk_limit:  # Tek başına sınırı aşan satır
            if chunk:
                await update.message.reply_text(chunk)
                chunk = ''
            await update.message.reply_text(line[:chunk_limit])
            line = line[chunk_limit:]

        if len(chunk) + len(line) + 1 > chunk_limit:
            await update.message.reply_text(chunk)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line

    if chunk:
        await update.message.reply_text(chunk)

async def block_crn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bir CRN kodunu yeni aboneliklere kapatır/açar. Mevcut abonelikler etkilenmez."""
    # Only for admin
    admin_id = ADMIN_ID
    if update.message.chat_id != admin_id:
        await update.message.reply_text('Bu komutu kullanma yetkiniz yok.')
        return

    if not context.args:  # Argümansız kullanım kapalı CRN listesini gösterir
        if blocked_crns:
            listed = "\n".join(f"{crn} - {count_crn_subscribers(crn)} mevcut abone" for crn in sorted(blocked_crns))
            await update.message.reply_text(f'Aboneliğe kapalı CRN kodları:\n{listed}\n\nDurumu değiştirmek için: /blockcrn <CRN>')
        else:
            await update.message.reply_text('Aboneliğe kapalı CRN kodu yok.\nBir CRN kodunu kapatmak için: /blockcrn <CRN>')
        return

    if len(context.args) != 1:
        await update.message.reply_text('Lütfen geçerli formatta giriş yapın: /blockcrn <CRN>')
        return

    crn_code = context.args[0]

    if not crn_code.isdigit() or len(crn_code) < 4 or len(crn_code) > 5:
        await update.message.reply_text('CRN kodu 4 veya 5 haneli olmalıdır: /blockcrn <CRN>')
        return

    current_subscribers = count_crn_subscribers(crn_code)

    if crn_code in blocked_crns:
        blocked_crns.remove(crn_code)
        logger.info(f"{crn_code} CRN kodu tekrar aboneliğe açıldı.")
        message_lines = [f'{crn_code} tekrar aboneliğe açıldı.']
    else:
        blocked_crns.add(crn_code)
        logger.info(f"{crn_code} CRN kodu yeni aboneliklere kapatıldı.")
        message_lines = [f'{crn_code} yeni aboneliklere kapatıldı. Mevcut abonelikler devam ediyor.']

    save_blocked_crns()

    message_lines.append(f'Bu CRN için mevcut abone sayısı: {current_subscribers}')
    if crn_code in crn_details:
        ders = crn_details[crn_code]
        message_lines.append(f"Ders: {ders['dersKodu']} {ders['dersAdi']}")
    if blocked_crns:
        message_lines.append('')
        message_lines.append('Aboneliğe kapalı tüm CRN kodları: ' + ", ".join(sorted(blocked_crns)))

    await update.message.reply_text("\n".join(message_lines))

async def subscription_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abonelikleri ders kodu ve CRN bazında detaylı olarak raporlar."""
    # Only for admin
    admin_id = ADMIN_ID
    if update.message.chat_id != admin_id:
        await update.message.reply_text('Bu komutu kullanma yetkiniz yok.')
        return

    crn_stats = {}     # (ders_kodu, crn) -> {'subs': abone sayısı, 'bolumler': {bölüm: abone sayısı}}
    lesson_stats = {}  # ders_kodu -> {'subs': abonelik sayısı, 'users': kullanıcılar, 'crns': crn kodları}
    active_users = 0
    total_subscriptions = 0

    for user_id, user_subs in subscriptions.items():
        if not user_subs:
            continue

        active_users += 1
        total_subscriptions += len(user_subs)

        for lesson_code, crn_code, bolum in user_subs:
            crn = crn_stats.setdefault((lesson_code, crn_code), {'subs': 0, 'bolumler': {}})
            crn['subs'] += 1
            if bolum is not None:
                crn['bolumler'][bolum] = crn['bolumler'].get(bolum, 0) + 1

            lesson = lesson_stats.setdefault(lesson_code, {'subs': 0, 'users': set(), 'crns': set()})
            lesson['subs'] += 1
            lesson['users'].add(user_id)
            lesson['crns'].add(crn_code)

    avg_per_user = total_subscriptions / active_users if active_users > 0 else 0

    lines = []
    lines.append('=== Genel İstatistikler ===')
    lines.append(f'Kayıtlı kullanıcı: {len(subscriptions)}')
    lines.append(f'Aktif aboneliği olan kullanıcı: {active_users}')
    lines.append(f'Toplam abonelik: {total_subscriptions}')
    lines.append(f'Farklı ders kodu: {len(lesson_stats)}')
    lines.append(f'Farklı CRN: {len(crn_stats)}')
    lines.append(f'Aktif kullanıcı başına ortalama abonelik: {avg_per_user:.2f}')

    lines.append('')
    lines.append('=== Ders Kodu Bazında ===')
    if lesson_stats:
        for lesson_code, data in sorted(lesson_stats.items(), key=lambda item: item[1]['subs'], reverse=True):
            lines.append(f"{lesson_code}: {data['subs']} abonelik | {len(data['users'])} kullanıcı | {len(data['crns'])} CRN")
    else:
        lines.append('Aktif abonelik yok.')

    lines.append('')
    lines.append('=== CRN Bazında ===')
    if crn_stats:
        for (lesson_code, crn_code), data in sorted(crn_stats.items(), key=lambda item: item[1]['subs'], reverse=True):
            if crn_code in crn_details:
                ders = crn_details[crn_code]
                label = f"{crn_code} - {ders['dersKodu']} {ders['dersAdi']}"
            else:
                label = f"{crn_code} - {lesson_code} (ders adı henüz alınmadı)"

            bolum_note = ''
            if data['bolumler']:  # Bölüm seçen abone varsa dağılımı göster
                parcalar = [f"{bolum}: {count}" for bolum, count in sorted(data['bolumler'].items(), key=lambda item: item[1], reverse=True)]
                toplam_secenler = data['subs'] - sum(data['bolumler'].values())
                if toplam_secenler > 0:
                    parcalar.append(f"Toplam kontenjan: {toplam_secenler}")
                bolum_note = ' (' + ', '.join(parcalar) + ')'

            blocked_note = ' [ABONELİĞE KAPALI]' if crn_code in blocked_crns else ''
            lines.append(f"{label}: {data['subs']} abone{bolum_note}{blocked_note}")
    else:
        lines.append('Aktif abonelik yok.')

    top_users = sorted(
        ((user_id, len(user_subs)) for user_id, user_subs in subscriptions.items() if user_subs),
        key=lambda item: item[1],
        reverse=True
    )[:5]
    if top_users:
        lines.append('')
        lines.append('=== En Çok Aboneliği Olan Kullanıcılar ===')
        for user_id, sub_count in top_users:
            lines.append(f"{user_id}: {sub_count} abonelik")

    lines.append('')
    lines.append('=== Aboneliğe Kapalı CRN Kodları ===')
    if blocked_crns:
        for crn_code in sorted(blocked_crns):
            lines.append(f"{crn_code}: {count_crn_subscribers(crn_code)} mevcut abone")
    else:
        lines.append('Yok')

    await send_long_message(update, "\n".join(lines))


async def shutdown_message(update: Update, application):
    """Sunucu kapanmadan önce tüm kullanıcılara bir mesaj gönderir."""
    # Only for admin
    admin_id = ADMIN_ID
    if update.message.chat_id != admin_id:
        await update.message.reply_text('Bu komutu kullanma yetkiniz yok.')
        return

    for user_id in list(subscriptions.keys()):  # Gönderim sırasında botu engelleyen kullanıcılar sözlükten silinebilir
        # await send_message_safe(application.bot, user_id, "Add-Drop haftası bittiği için bot kapanacaktır. İleriki ders seçim dönemlerinde de bir aksilik olmazsa bot kullanıma açılacaktır. Botu engellemediğiniz takdirde bot yeniden aktif olduğunda bildirim alabilirsiniz.\n\nUmarım istediğiniz dersleri alabilmişsinizdir. Hepinize iyi bir dönem dilerim. Bir sonraki ders seçim haftası görüşmek üzere.\n\nNot: /clearall komutunu kullanarak aktif aboneliklerinizi tek seferde temizleyebilirsiniz.\n/sendmessage komutu ile botla ilgili sorunları ve geliştirmek için önerilerinizi iletebilirsiniz.\n\nBot bu mesajdan sonraki 1 saat içerisinde kapanacaktır ve kapalı kaldığı süre boyunca yazacağınız komutlar çalışmayacaktır.")
        await send_message_safe(application.bot, user_id, "Bot bakımdadır en kısa sürede tekrar aktif olacaktır, sabrınız için teşekkürler. (Bot tekrar aktif olduğunda bildirim alacaksınız.)")

def handle_shutdown(application):
    """Kapanış sinyali geldiğinde tetiklenir."""
    # asyncio.create_task(shutdown_message(Update, application))
    application.stop_running()

async def main_loop(context):
    while True:
        try:
            await check_capacity_optimized(context)
        except Exception as e:
            logger.error(f"Main loop sırasında hata oluştu: {e}")
            
        await asyncio.sleep(58)

def add_handlers(application):
    """Komut ve buton işleyicilerini uygulamaya ekler."""
    # Tek komutla (/subscribe BLG 13547) ya da adım adım (/subscribe) abonelik.
    # block=False: OBS sorgusu sürerken diğer kullanıcıların komutları bekletilmez.
    subscribe_conversation = ConversationHandler(
        entry_points=[CommandHandler("subscribe", subscribe)],
        states={
            ASK_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, subscribe_course_input)],
            ASK_DETAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, subscribe_detail_input)],
            ConversationHandler.WAITING: [  # Önceki istek bitmeden gelen abonelik mesajları sessizce kaybolmasın
                CommandHandler(["subscribe", "cancel"], subscribe_busy),
                MessageHandler(filters.TEXT & ~filters.COMMAND, subscribe_busy),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        block=False,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(subscribe_conversation)
    application.add_handler(CommandHandler("cancel", cancel_idle))  # Adım adım işlem yokken /cancel
    application.add_handler(CommandHandler("check", check, block=False))  # Anlık kontenjan sorgusu
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))
    application.add_handler(CommandHandler("sublist", sublist))
    application.add_handler(CommandHandler("help", help))
    application.add_handler(CommandHandler("updateallusers", updateallusers))
    application.add_handler(CommandHandler("stopmsgadmincmd", shutdown_message))
    application.add_handler(CommandHandler("sendmessage", send_message))  # Mesaj gönderme komutu
    application.add_handler(CommandHandler("readmessages", read_messages))  # Mesajları okuma komutu
    application.add_handler(CommandHandler("broadcast", broadcast_message))  # Tüm kullanıcılara mesaj gönderme komutu
    application.add_handler(CommandHandler("clearall", clear_all_subscriptions))  # Tüm abonelikleri temizleme komutu
    application.add_handler(CommandHandler("sendto", send_to_user))  # Belirli bir kullanıcıya mesaj gönderme komutu
    application.add_handler(CommandHandler("blockcrn", block_crn))  # Bir CRN'i yeni aboneliklere kapatma/açma komutu
    application.add_handler(CommandHandler("substats", subscription_stats))  # Detaylı abonelik analizi komutu
    application.add_handler(CallbackQueryHandler(bolum_callback, pattern=r"^bolum\|"))  # Bölüm seçimi butonları
    application.add_handler(CallbackQueryHandler(subscribe_callback, pattern=r"^sub\|", block=False))  # Şube listesi / check abone ol butonları
    application.add_handler(CallbackQueryHandler(sublist_callback, pattern=r"^(unsub|sublist)\|"))  # Abonelik listesi Çık / Yenile butonları

# Sohbetteki "/" komut menüsünde görünen kullanıcı komutları. Admin komutları bilerek eklenmedi.
USER_COMMANDS = [
    BotCommand("subscribe", "Derse abone ol (adım adım ya da DERS_KODU CRN)"),
    BotCommand("check", "Abone olmadan anlık kontenjan sorgula"),
    BotCommand("sublist", "Aboneliklerini görüntüle"),
    BotCommand("unsubscribe", "Abonelikten ayrıl (DERS_KODU CRN)"),
    BotCommand("clearall", "Tüm aboneliklerden ayrıl"),
    BotCommand("cancel", "Adım adım abonelik işlemini iptal et"),
    BotCommand("sendmessage", "Admine şikayet veya öneri gönder"),
    BotCommand("help", "Tüm komutlar ve kullanım bilgisi"),
    BotCommand("start", "Botu başlat"),
]

async def set_bot_commands(application):
    """Bot açılırken komut menüsünü Telegram'a kaydeder."""
    try:
        await application.bot.set_my_commands(USER_COMMANDS)
        logger.info("Komut listesi güncellendi.")
    except Exception as e:
        logger.error(f"Komut listesi güncellenemedi: {e}")

def main():
    load_subscriptions()
    load_blocked_crns()

    application = ApplicationBuilder().token(TOKEN).post_init(set_bot_commands).build()
    add_handlers(application)

    loop = asyncio.get_event_loop()
    loop.create_task(main_loop(application))

    signal.signal(signal.SIGINT, lambda s, f: handle_shutdown(application))
    signal.signal(signal.SIGTERM, lambda s, f: handle_shutdown(application))
    
    logger.info("Bot başlatılıyor...")
    application.run_polling()

    save_subscriptions()

if __name__ == '__main__':
    main()

