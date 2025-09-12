#TODO user_info.json program başında bir kere okunacak. save almak için bir fonksiyon yazılacak. user_info argüman olarak verilebilir.
#TODO loglar ayrı bir dosyaya kaydedilecek.
#TODO tüm admin komutlarında admin id kontrolü yapılmalı. 

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
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from datetime import datetime, timedelta
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

ADMIN_ID = int(os.getenv("ADMIN_ID"))
TOKEN = os.getenv("BOT_TOKEN")
SUBSCRIPTION_FILE = 'subscriptions.json'
USER_FILE = 'user_info.json'

subscriptions = {}

is_subs_updated = False
request_count = 0

is_branch_codes_fetched = False
branch_dict = {}

last_msg_times = {}

# Log settings
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

def save_subscriptions():
    with open(SUBSCRIPTION_FILE, 'w') as f:
        json.dump(subscriptions, f)
    logger.info("Abonelikler Kaydedildi...")

def load_subscriptions():
    global subscriptions
    subscriptions_local = {}
    if os.path.exists(SUBSCRIPTION_FILE):
        try:
            with open(SUBSCRIPTION_FILE, 'r') as f:
                subscriptions_local = json.load(f)

                for user_id, lessons in subscriptions_local.items(): 
                    if isinstance(user_id, str):
                        user_id = int(user_id)  
                    if user_id not in subscriptions:
                        subscriptions[user_id] = []
                    for lesson in lessons:
                        subscriptions[user_id].append(tuple(lesson))

        except json.decoder.JSONDecodeError:
            subscriptions_local = {}
    else:
        subscriptions_local = {}

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Merhaba! Kontenjan durumunu öğrenmek için\n/subscribe <DERS_KODU> <CRN> komutunu kullanın. (Yalnızca Lisans seviyesi dersler!)\n\nTüm komutları görmek için /help komutunu kullanın.')

async def help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('/subscribe <DERS_KODU> <CRN>  -  Bir derse abone ol.\n/unsubscribe <DERS_KODU> <CRN>  -  Abonelikten ayrıl.\n/sublist  -  Aktif tüm abonelikleri göster.\n/clearall - Aktif tüm aboneliklerden ayrıl.\n/sendmessage <MESAJ> - Admine şikayet veya önerilerinizi gönderebilirsiniz\n\nÖrnek kullanım: "/subscribe BLG 13547" \n\nBu bot abone olduğunuz derslere ait ders programı güncellendikten sonra ilgili derslerin kontenjan durumlarını kontrol eder. Eğer boş yer varsa size bildirir. Boş yer açılana kadar mesaj almazsınız.\n\nDikkat: Bu bot şu anda çalışıyor olsa bile ilerleyen zamanda bilgi işlemin sistemlerinde yapabileceği değişikliklerden etkilenebilir ve görevini yapamayabilir. Ya da ben serveri kapatabilirim :D\nServer kapandığı takdirde kullanıcılara bilgilendirme mesajı gönderilecektir.')

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

    if user_id in subscriptions:
        for subscription in subscriptions[user_id]:
            if subscription[0] == lesson_code and subscription[1] == crn_code:
                await update.message.reply_text(f'{lesson_code} {crn_code} için zaten abone oldunuz.')
                return

    # Kullanıcıyı abone et
    if user_id not in subscriptions:
        subscriptions[user_id] = []
    subscriptions[user_id].append((lesson_code, crn_code))  # Tuple olarak kaydediyoruz

    # İlgili CRN için toplam abone sayısını hesapla
    total_subscribers = 0
    for user_subs in subscriptions.values():
        for sub in user_subs:
            if sub[0] == lesson_code and sub[1] == crn_code:
                total_subscribers += 1

    await update.message.reply_text(f'{lesson_code} {crn_code} için kontenjan durumunu kontrol etmeye başladım.\nBu derse abone {total_subscribers} kişi var.')
    logger.info(f"{user_id} kullanıcısı {lesson_code} {crn_code} için kontenjan durumunu kontrol etmeye başladı.")

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
                await update.message.reply_text(f'{lesson_code} {crn_code} için aboneliğiniz iptal edilmiştir.')
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
        lessons = ", ".join([f"{sub[0]} {sub[1]}" for sub in subscriptions[user_id]])  # Tuple'dan formatla
        await update.message.reply_text(f"Abone olduğunuz dersler: {lessons}")
    else:
        await update.message.reply_text("Henüz herhangi bir derse abone olmadınız.")

async def updateallusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        for lesson_code, crn_code in user_subs:
            if lesson_code not in subscriptions_by_lesson:
                subscriptions_by_lesson[lesson_code] = []
            subscriptions_by_lesson[lesson_code].append((user_id, crn_code))

    # Her ders kodu için sadece bir istek atılacak
    for lesson_code, user_crns in subscriptions_by_lesson.items():
        lesson_id = take_option_value(lesson_code)
        if lesson_id == -1:
            logger.warning(f"Geçersiz ders kodu: {lesson_code}")
            continue

        try:
            global request_count
            request_count += 1

            response = requests.get(
                f"https://obs.itu.edu.tr/public/DersProgram/DersProgramSearch?ProgramSeviyeTipiAnahtari=LS&dersBransKoduId={lesson_id}&__RequestVerificationToken=bilgi_islem_naber"
            )
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Guncellenme saati artık gelen responseda olmadigi icin her response guncel kabul ediliyor. ayip ediyon bilgi islem. 
            
            table = soup.find('table', {'id': 'dersProgramContainer'})
            if not table:
                logger.error(f"Ders programı tablosu bulunamadı: {lesson_code}")
                continue
            
            rows = table.find('tbody').find_all('tr')
            
            valid_crns = []
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
                    
                    try:
                        kontenjan = int(kontenjan_str)
                        ogrenci_sayisi = int(yazilan_str)
                    except ValueError:
                        logger.warning(f"Kontenjan veya öğrenci sayısı dönüştürülemedi: {crn}")
                        continue
                    
                    valid_crns.append(crn)
                    dersler.append({
                        'crn': crn,
                        'dersKodu': ders_kodu,
                        'dersAdi': ders_adi,
                        'kontenjan': kontenjan,
                        'ogrenciSayisi': ogrenci_sayisi
                    })
            
            # Check for invalid CRNs and remove subscriptions
            for user_id, crn_code in user_crns:
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
                available_capacity = ders['kontenjan'] - ders['ogrenciSayisi']
                
                for user_id, crn_code in user_crns:
                    if crn_code == crn and available_capacity > 0:
                        if last_msg_times.get((user_id,crn)) is None or (datetime.now() - last_msg_times[(user_id,crn)]) > timedelta(minutes=3): # Prevent spamming
                            message = f"{ders['dersKodu']} {crn} {ders['dersAdi']} için {available_capacity} kontenjan var!"
                            last_msg_times[(user_id,crn)] = datetime.now()
                            try:
                                await context.bot.send_message(chat_id=user_id, text=message)
                                logger.info(f"ID:{user_id} Kullanısına '{message}' mesajı gönderilmiştir.")
                            except:
                                logger.info(f"ID:{user_id} Kullanısına mesaj gönderilememiştir.")

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


async def shutdown_message(update: Update, application):
    """Sunucu kapanmadan önce tüm kullanıcılara bir mesaj gönderir."""
    for user_id in subscriptions.keys():
        try:
            # await application.bot.send_message(chat_id=user_id, text="Add-Drop haftası bittiği için bot kapanacaktır. İleriki ders seçim dönemlerinde de bir aksilik olmazsa bot kullanıma açılacaktır. Botu engellemediğiniz takdirde bot yeniden aktif olduğunda bildirim alabilirsiniz.\n\nUmarım istediğiniz dersleri alabilmişsinizdir. Hepinize iyi bir dönem dilerim. Bir sonraki ders seçim haftası görüşmek üzere.\n\nNot: /clearall komutunu kullanarak aktif aboneliklerinizi tek seferde temizleyebilirsiniz.\n/sendmessage komutu ile botla ilgili sorunları ve geliştirmek için önerilerinizi iletebilirsiniz.\n\nBot bu mesajdan sonraki 1 saat içerisinde kapanacaktır ve kapalı kaldığı süre boyunca yazacağınız komutlar çalışmayacaktır.")
            await application.bot.send_message(chat_id=user_id, text="Bot bakımdadır en kısa sürede tekrar aktif olacaktır, sabrınız için teşekkürler. (Bot tekrar aktif olduğunda bildirim alacaksınız.)")
        except:
            print(f"Chat id {user_id} kullanıcısına mesaj gönderilemedi.")

def handle_shutdown(application):
    """Kapanış sinyali geldiğinde tetiklenir."""
    asyncio.create_task(shutdown_message(Update, application))
    application.stop_running()

async def main_loop(context):
    while True:
        try:
            await check_capacity_optimized(context)
        except Exception as e:
            logger.error(f"Main loop sırasında hata oluştu: {e}")
            
        await asyncio.sleep(28)

def main():
    load_subscriptions()

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

    loop = asyncio.get_event_loop()
    loop.create_task(main_loop(application))

    signal.signal(signal.SIGINT, lambda s, f: handle_shutdown(application))
    signal.signal(signal.SIGTERM, lambda s, f: handle_shutdown(application))
    
    logger.info("Bot başlatılıyor...")
    application.run_polling()

    save_subscriptions()

if __name__ == '__main__':
    main()

