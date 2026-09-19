#TODO user_info.json program başında bir kere okunacak. save almak için bir fonksiyon yazılacak. user_info argüman olarak verilebilir.

"""
            *** GELISTIRME ONERISI***
- her bir user için dil tercihi getirilebilir. Tüm mesajların ingilizceleri de yazılır.
- kontenjan mesajı atılırken bu mesajın kaç kişiye daha atıldığı bilgisi eklenebilir.
"""

import logging
import requests
import asyncio
import signal
import json
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes
from datetime import datetime, timedelta
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

ADMIN_ID = int(os.getenv("ADMIN_ID"))
TOKEN = os.getenv("BOT_TOKEN")
SUBSCRIPTION_FILE = 'subscriptions.json'
USER_FILE = 'user_info.json'
BLOCKED_CRN_FILE = 'blocked_crns.json'

subscriptions = {}
blocked_crns = set()   # Yeni aboneliğe kapatılmış CRN kodları
crn_details = {}       # crn -> (ders_kodu, ders_adi). Kontenjan kontrolü sırasında doldurulur.

is_subs_updated = False
request_count = 0

is_branch_codes_fetched = False
branch_dict = {}

last_msg_times = {}

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

    if user_id not in users:
        users[user_id] = user_info 

        with open(USER_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=4, ensure_ascii=False)

def fetch_branch_codes():
    url = "https://obs.itu.edu.tr/public/DersProgram/SearchBransKoduByProgramSeviye?programSeviyeTipiAnahtari=LS"
    
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()  
    else:
        print("Veri alınamadı:", response.status_code)
        return []

def take_option_value(branch_code):
    """Verilen branş kodunun opsiyon değerini döner."""
    global is_branch_codes_fetched
    global branch_dict

    if not is_branch_codes_fetched:
        branch_codes = fetch_branch_codes()
        branch_dict = {branch['dersBransKodu']: branch['bransKoduId'] for branch in branch_codes}
        is_branch_codes_fetched = True
    
    return branch_dict.get(branch_code, -1)

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

def available_capacity_for(ders, bolum):
    """Kullanıcının bölüm seçimine göre boş kontenjanı ve baz alınan bölümü döner.
    Bölüm seçilmemişse (None) ya da seçilen bölüm artık rezervasyonda yoksa toplam kontenjana bakılır."""
    if bolum is not None:
        for rez in ders['rezervasyonlar']:
            if rez['bolum'] == bolum:
                return rez['kontenjan'] - rez['yazilan'], bolum
    return ders['kontenjan'] - ders['ogrenciSayisi'], None

def fetch_lesson_table(lesson_code, lesson_id):
    """Ders programı tablosunu çeker ve satırları sözlük listesi olarak döner. İstek/parse hatalarında exception fırlatır."""
    global request_count
    request_count += 1

    response = requests.get(
        f"https://obs.itu.edu.tr/public/DersProgram/DersProgramSearch?ProgramSeviyeTipiAnahtari=LS&dersBransKoduId={lesson_id}&__RequestVerificationToken=bilgi_islem_naber",
        timeout=20
    )
    response.raise_for_status()

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
            crn = cols[0].text.strip()
            ders_kodu_element = cols[1].find('a')
            ders_kodu = ders_kodu_element.text.strip() if ders_kodu_element else cols[1].text.strip()
            ders_adi = cols[2].text.strip()
            kontenjan_str = cols[9].text.strip()
            yazilan_str = cols[10].text.strip()
            rezervasyon_str = cols[11].text.strip() if len(cols) > 11 else '-'  # Reservasyon Böl./Kont./Yaz. sütunu

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
                'kontenjan': kontenjan,
                'ogrenciSayisi': ogrenci_sayisi,
                'rezervasyonlar': parse_rezervasyon(rezervasyon_str)  # Bölüm bazlı kontenjan, yoksa []
            })

    return dersler

def build_bolum_keyboard(lesson_code, crn_code, rezervasyonlar):
    """Bölüm seçimi butonları. callback_data: bolum|<ders_kodu>|<crn>|<bolum>  ('*' = toplam kontenjan)"""
    buttons = [
        InlineKeyboardButton(f"{rez['bolum']} ({rez['yazilan']}/{rez['kontenjan']})", callback_data=f"bolum|{lesson_code}|{crn_code}|{rez['bolum']}")
        for rez in rezervasyonlar
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]  # Satır başına 2 buton
    rows.append([InlineKeyboardButton("Toplam kontenjan (bölüm seçme)", callback_data=f"bolum|{lesson_code}|{crn_code}|*")])
    return InlineKeyboardMarkup(rows)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Merhaba! Kontenjan durumunu öğrenmek için\n/subscribe <DERS_KODU> <CRN> komutunu kullanın.\n(Yalnızca Lisans seviyesi dersler!)\n\nTüm komutları görmek için /help komutunu kullanın.')

async def help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('/subscribe <DERS_KODU> <CRN>  -  Bir derse abone ol.\n/unsubscribe <DERS_KODU> <CRN>  -  Abonelikten ayrıl.\n/sublist  -  Aktif tüm abonelikleri göster.\n/clearall - Aktif tüm aboneliklerden ayrıl.\n/sendmessage <MESAJ> - Admine şikayet veya önerilerinizi gönderebilirsiniz\n\nÖrnek kullanım: "/subscribe BLG 13547" \n\nBu bot abone olduğunuz derslerin kontenjan durumlarını belirli aralıklarla kontrol eder. Eğer boş yer varsa size bildirir. Boş yer açılana kadar mesaj almazsınız.\nNOT: Bir ders için kontenjan var mesajı aldıktan sonra spama düşmemek amacıyla aynı ders için sonraki 3 dakika boyunca mesaj almazsınız, diğer derslerin kontrolü devam eder. \nKontenjanı bölümlere ayrılmış derslerde (örn. YZVE_LS/10/7 | Diğer/70/70) abone olurken bölümünüzü seçebilirsiniz; böylece yalnızca kendi bölümünüzün kontenjanı açıldığında bildirim alırsınız. \n\nDikkat: Bu bot şu anda çalışıyor olsa bile ilerleyen zamanda bilgi işlemin yapabileceği değişikliklerden etkilenebilir ve görevini yapamayabilir. Ya da ben serveri kapatabilirim :D\nServer admin tarafından kapatıldığı durumda kullanıcılara bilgilendirme mesajı gönderilecektir.')

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2:
        await update.message.reply_text('Lütfen geçerli formatta giriş yapın: /subscribe <DERS_KODU> <CRN>')
        return

    lesson_code = context.args[0]
    lesson_id = take_option_value(lesson_code)
    crn_code = context.args[1]
    user_id = update.message.chat_id

    user_info = {
        "username": update.message.from_user.username,
        "first_name": update.message.from_user.first_name,
        "last_name": update.message.from_user.last_name,
    }

    log_user_info(user_id, user_info)

    if lesson_id == -1:
        await update.message.reply_text('Lütfen geçerli bir ders kodu girin: /subscribe <DERS_KODU> <CRN>')
        return

    if len(crn_code) < 4 or len(crn_code) > 5:
        await update.message.reply_text('CRN kodu 4 veya 5 haneli olmalıdır: /subscribe <DERS_KODU> <CRN>')
        return

    existing_subscription = find_subscription(user_id, lesson_code, crn_code)

    if existing_subscription is None and crn_code in blocked_crns:
        await update.message.reply_text('Bu derse geçici olarak abone olamazsınız.')
        logger.info(f"{user_id} kullanıcısı aboneliğe kapalı {lesson_code} {crn_code} dersine abone olmak istedi.")
        return

    # Ders programını çek: CRN doğrulanır ve bölüm bazlı kontenjan (rezervasyon) bilgisi alınır
    ders = None
    try:
        dersler = fetch_lesson_table(lesson_code, lesson_id)
        ders = next((d for d in dersler if d['crn'] == crn_code), None)
        if ders is None:
            await update.message.reply_text(f'{lesson_code} dersleri arasında {crn_code} CRN kodu bulunamadı. Ders kodunu ve CRN\'i kontrol edin: /subscribe <DERS_KODU> <CRN>')
            return
        crn_details[crn_code] = (ders['dersKodu'], ders['dersAdi'])
    except Exception as e:
        # Tablo çekilemezse eski davranış: doğrulama yapılmadan abone edilir, geçersiz CRN kontrol döngüsünde temizlenir
        logger.warning(f"{lesson_code} için ders programı çekilemedi, {crn_code} doğrulanmadan devam ediliyor: {e}")

    rezervasyonlar = ders['rezervasyonlar'] if ders else []

    if existing_subscription is not None:
        if rezervasyonlar:  # Zaten abone; bölüm seçimini değiştirme imkanı ver
            mevcut_secim = existing_subscription[2] or 'Toplam kontenjan'
            await update.message.reply_text(
                f'{lesson_code} {crn_code} için zaten abone oldunuz. Mevcut seçiminiz: {mevcut_secim}\n\n'
                f'Bu dersin kontenjanı bölümlere ayrılmış (yazılan/kontenjan):\n{format_rezervasyon(rezervasyonlar)}\n'
                f'Seçiminizi değiştirmek için bölümünüzü seçin:',
                reply_markup=build_bolum_keyboard(lesson_code, crn_code, rezervasyonlar)
            )
        else:
            await update.message.reply_text(f'{lesson_code} {crn_code} için zaten abone oldunuz.')
        return

    # Kullanıcıyı abone et
    if user_id not in subscriptions:
        subscriptions[user_id] = []
    subscriptions[user_id].append((lesson_code, crn_code, None))  # Tuple olarak kaydediyoruz, bölüm seçimi butonla yapılır

    # İlgili CRN için toplam abone sayısını hesapla
    total_subscribers = 0
    for user_subs in subscriptions.values():
        for sub in user_subs:
            if sub[0] == lesson_code and sub[1] == crn_code:
                total_subscribers += 1

    message = f'{lesson_code} {crn_code} için kontenjan durumunu kontrol etmeye başladım.\nBu derse abone {total_subscribers} kişi var.'
    reply_markup = None
    if rezervasyonlar:
        message += (f'\n\nBu dersin kontenjanı bölümlere ayrılmış (yazılan/kontenjan):\n{format_rezervasyon(rezervasyonlar)}\n'
                    f'Yalnızca kendi bölümünüzün kontenjanı açıldığında bildirim almak için bölümünüzü seçin. '
                    f'Seçim yapmazsanız toplam kontenjana göre bildirim alırsınız.')
        reply_markup = build_bolum_keyboard(lesson_code, crn_code, rezervasyonlar)

    try:
        await update.message.reply_text(message, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"{user_id} kullanıcısına abonelik mesajı gönderilemedi. Hata: {e}")

    save_subscriptions()  # Abonelikleri kaydet
    logger.info(f"{user_id} kullanıcısı {lesson_code} {crn_code} için kontenjan durumunu kontrol etmeye başladı.")

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

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2:
        await update.message.reply_text('Lütfen geçerli bir CRN kodu girin: /unsubscribe <DERS_KODU> <CRN>')
        return

    lesson_code = context.args[0]
    crn_code = context.args[1]
    user_id = update.message.chat_id

    user_info = {
        "username": update.message.from_user.username,
        "first_name": update.message.from_user.first_name,
        "last_name": update.message.from_user.last_name,
    }
    
    log_user_info(user_id, user_info)

    sub_cancelled = False
    if user_id in subscriptions:
        for subscription in subscriptions[user_id]:
            if subscription[0] == lesson_code and subscription[1] == crn_code:
                subscriptions[user_id].remove(subscription)
                try:
                    await update.message.reply_text(f'{lesson_code} {crn_code} için aboneliğiniz iptal edilmiştir.')
                except:
                    logger.error(f"{user_id} kullanıcısına abonelik iptal mesajı gönderilemedi.")
                sub_cancelled = True
                
    if not sub_cancelled:
        await update.message.reply_text(f'{lesson_code} {crn_code} için aboneliğiniz yok veya yanlış CRN kodu girdiniz.')

async def clear_all_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.chat_id 

    user_info = {
        "username": update.message.from_user.username,
        "first_name": update.message.from_user.first_name,
        "last_name": update.message.from_user.last_name,
    }

    log_user_info(user_id, user_info)

    # Kullanıcının aboneliklerini kontrol et
    if user_id in subscriptions and subscriptions[user_id]:
        # Tüm abonelikleri sil
        subscriptions[user_id].clear()
        await update.message.reply_text('Tüm abonelikleriniz başarıyla temizlendi.')
        save_subscriptions()  # Abonelikleri kaydet
    else:
        await update.message.reply_text('Zaten herhangi bir aboneliğiniz bulunmuyor.')

async def sublist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.chat_id

    user_info = {
        "username": update.message.from_user.username,
        "first_name": update.message.from_user.first_name,
        "last_name": update.message.from_user.last_name,
    }
    
    log_user_info(user_id, user_info)

    # Kullanıcının abone olduğu dersleri kontrol et
    if user_id in subscriptions and subscriptions[user_id]:
        lessons = ", ".join([f"{sub[0]} {sub[1]}" + (f" ({sub[2]})" if sub[2] else "") for sub in subscriptions[user_id]])  # Tuple'dan formatla, bölüm seçimi varsa parantezde
        await update.message.reply_text(f"Abone olduğunuz dersler: {lessons}")
    else:
        await update.message.reply_text("Henüz herhangi bir derse abone olmadınız.")

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
    subscriptions_by_lesson = {}
    for user_id, user_subs in subscriptions.items():
        for lesson_code, crn_code, bolum in user_subs:
            if lesson_code not in subscriptions_by_lesson:
                subscriptions_by_lesson[lesson_code] = []
            subscriptions_by_lesson[lesson_code].append((user_id, crn_code, bolum))

    # Her ders kodu için sadece bir istek atılacak
    for lesson_code, user_crns in subscriptions_by_lesson.items():
        lesson_id = take_option_value(lesson_code)
        if lesson_id == -1:
            logger.warning(f"Geçersiz ders kodu: {lesson_code}")
            continue

        subscribed_crns = {crn_code for _, crn_code, _ in user_crns}

        try:
            dersler = fetch_lesson_table(lesson_code, lesson_id)
            valid_crns = [ders['crn'] for ders in dersler]

            for ders in dersler:
                if ders['crn'] in subscribed_crns:  # Abone olunan derslerin adını istatistikler için sakla
                    crn_details[ders['crn']] = (ders['dersKodu'], ders['dersAdi'])

            # Check for invalid CRNs and remove subscriptions
            for user_id, crn_code, _ in user_crns:
                if crn_code not in valid_crns:
                    if user_id in subscriptions:
                        for subscription in subscriptions[user_id]:
                            if subscription[0] == lesson_code and subscription[1] == crn_code:
                                subscriptions[user_id].remove(subscription)
                                logger.info(f"Geçersiz CRN: {crn_code} için {user_id} kullanıcısının aboneliği kaldırıldı.")
                                message = f"{lesson_code} {crn_code} geçersiz bir CRN kodu olduğu için aboneliğiniz iptal edilmiştir."
                                await context.bot.send_message(chat_id=user_id, text=message)
                                break

            # Check capacity for each course
            for ders in dersler:
                crn = ders['crn']
                rezervasyon_str = f" ({format_rezervasyon(ders['rezervasyonlar'])})" if ders['rezervasyonlar'] else ""

                for user_id, crn_code, bolum in user_crns:
                    if crn_code != crn:
                        continue

                    # Bölüm seçen kullanıcı için o bölümün, diğerleri için toplam kontenjana bakılır
                    available_capacity, secilen_bolum = available_capacity_for(ders, bolum)
                    if available_capacity > 0:
                        if last_msg_times.get((user_id,crn)) is None or (datetime.now() - last_msg_times[(user_id,crn)]) > timedelta(minutes=3): # Prevent spamming
                            bolum_str = f"{secilen_bolum} bölümünde " if secilen_bolum else ""
                            message = f"{ders['dersKodu']} {crn} {ders['dersAdi']} için {bolum_str}{available_capacity} kontenjan var!{rezervasyon_str}"
                            last_msg_times[(user_id,crn)] = datetime.now()
                            try:
                                await context.bot.send_message(chat_id=user_id, text=message)
                                logger.info(f"ID:{user_id} Kullanısına '{message}' mesajı gönderilmiştir.")
                            except Exception as e:
                                logger.info(f"ID:{user_id} Kullanısına mesaj gönderilememiştir: {e}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Ders kodu {lesson_code} için istek atılırken hata oluştu: {e}")
            continue
        except Exception as e:
            logger.error(f"HTML parse edilirken hata oluştu {lesson_code}: {e}")
            continue

    await asyncio.sleep(5)

async def send_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text('Lütfen bir mesaj yazın: /sendmessage <mesaj>')
        return

    user_message = " ".join(context.args) 
    user_id = update.message.chat_id 
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S") 

    user_info = {
        "username": update.message.from_user.username,
        "first_name": update.message.from_user.first_name,
        "last_name": update.message.from_user.last_name,
    }
    
    log_user_info(user_id, user_info)

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
                await update.message.reply_text(f"Mesajlar:\n\n{messages}")
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

    for user_id in subscriptions.keys():
        try:
            await context.bot.send_message(chat_id=user_id, text=broadcast_message)
        except Exception as e:
            logger.error(f"Kullanıcı {user_id} ye mesaj gönderilemedi: {e}")

    await update.message.reply_text('Mesaj tüm kullanıcılara gönderildi.')

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
        ders_kodu, ders_adi = crn_details[crn_code]
        message_lines.append(f'Ders: {ders_kodu} {ders_adi}')
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
                ders_kodu, ders_adi = crn_details[crn_code]
                label = f"{crn_code} - {ders_kodu} {ders_adi}"
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

    for user_id in subscriptions.keys():
        try:
            # await application.bot.send_message(chat_id=user_id, text="Add-Drop haftası bittiği için bot kapanacaktır. İleriki ders seçim dönemlerinde de bir aksilik olmazsa bot kullanıma açılacaktır. Botu engellemediğiniz takdirde bot yeniden aktif olduğunda bildirim alabilirsiniz.\n\nUmarım istediğiniz dersleri alabilmişsinizdir. Hepinize iyi bir dönem dilerim. Bir sonraki ders seçim haftası görüşmek üzere.\n\nNot: /clearall komutunu kullanarak aktif aboneliklerinizi tek seferde temizleyebilirsiniz.\n/sendmessage komutu ile botla ilgili sorunları ve geliştirmek için önerilerinizi iletebilirsiniz.\n\nBot bu mesajdan sonraki 1 saat içerisinde kapanacaktır ve kapalı kaldığı süre boyunca yazacağınız komutlar çalışmayacaktır.")
            await application.bot.send_message(chat_id=user_id, text="Bot bakımdadır en kısa sürede tekrar aktif olacaktır, sabrınız için teşekkürler. (Bot tekrar aktif olduğunda bildirim alacaksınız.)")
        except:
            print(f"Chat id {user_id} kullanıcısına mesaj gönderilemedi.")

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

def main():
    load_subscriptions()
    load_blocked_crns()

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("subscribe", subscribe))
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

    loop = asyncio.get_event_loop()
    loop.create_task(main_loop(application))

    signal.signal(signal.SIGINT, lambda s, f: handle_shutdown(application))
    signal.signal(signal.SIGTERM, lambda s, f: handle_shutdown(application))
    
    logger.info("Bot başlatılıyor...")
    application.run_polling()

    save_subscriptions()

if __name__ == '__main__':
    main()

