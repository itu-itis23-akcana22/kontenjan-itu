import os
import json
from collections import defaultdict
import matplotlib.pyplot as plt
from termcolor import colored

SUBSCRIPTION_FILE = 'subscriptions.json'
ANALYSIS_OUTPUT_FILE = 'subscription_analysis.txt'

subscriptions = {}

def load_subscriptions():
    global subscriptions
    if os.path.exists(SUBSCRIPTION_FILE):
        try:
            with open(SUBSCRIPTION_FILE, 'r') as f:
                subscriptions_local = json.load(f)

                for user_id, lessons in subscriptions_local.items():
                    user_id = int(user_id)  # Kullanıcı ID'sini integer'a çevir
                    if user_id not in subscriptions:
                        subscriptions[user_id] = []
                    for lesson in lessons:
                        subscriptions[user_id].append(tuple(lesson))

        except json.decoder.JSONDecodeError:
            # JSON dosyası boşsa veya bozuksa, boş bir sözlük başlat
            subscriptions = {}
    else:
        subscriptions = {}

def analyze_subscriptions():
    load_subscriptions()

    # Abonelikleri ders kodu bazında grupla
    lesson_code_subscriptions = defaultdict(list)
    for user_id, subs in subscriptions.items():
        for lesson_code, crn_code in subs:
            lesson_code_subscriptions[lesson_code].append(user_id)

    # Genel istatistikler
    total_users = len(subscriptions)
    total_subscriptions = sum(len(subs) for subs in subscriptions.values())
    total_unique_lesson_codes = len(lesson_code_subscriptions)
    avg_subscriptions_per_user = total_subscriptions / total_users if total_users > 0 else 0

    # Kullanıcı bazında analiz
    user_analysis = []
    for user_id, subs in subscriptions.items():
        user_analysis.append({
            'user_id': user_id,
            'subscription_count': len(subs),
            'subscriptions': subs
        })

    # Ders kodu bazında analiz
    lesson_code_analysis = []
    for lesson_code, users in lesson_code_subscriptions.items():
        lesson_code_analysis.append({
            'lesson_code': lesson_code,
            'subscriber_count': len(users),
            'subscribers': users
        })

    # Ders kodlarını abone sayısına göre sırala
    lesson_code_analysis_sorted = sorted(lesson_code_analysis, key=lambda x: x['subscriber_count'], reverse=True)

    # Analiz sonuçlarını dosyaya kaydet
    with open(ANALYSIS_OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write("=== Kullanıcı Bazında Abonelikler ===\n")
        for user in user_analysis:
            f.write(f"Kullanıcı ID: {user['user_id']}\n")
            f.write(f"Abonelik Sayısı: {user['subscription_count']}\n")
            f.write(f"Abonelikler: {user['subscriptions']}\n")
            f.write("-" * 40 + "\n")

        f.write("\n=== Ders Kodu Bazında Abonelikler ===\n")
        for lesson in lesson_code_analysis_sorted:
            f.write(f"Ders Kodu: {lesson['lesson_code']}\n")
            f.write(f"Abone Sayısı: {lesson['subscriber_count']}\n")
            f.write(f"Abone Olan Kullanıcılar: {lesson['subscribers']}\n")
            f.write("-" * 40 + "\n")

        f.write("\n=== Genel İstatistikler ===\n")
        f.write(f"Toplam Kullanıcı Sayısı: {total_users}\n")
        f.write(f"Toplam Abonelik Sayısı: {total_subscriptions}\n")
        f.write(f"Toplam Farklı Ders Kodu Sayısı: {total_unique_lesson_codes}\n")
        f.write(f"Kullanıcı Başına Ortalama Abonelik Sayısı: {avg_subscriptions_per_user:.2f}\n")

    # Analiz sonuçlarını ekrana yazdır (renkli)
    print(colored("=== Kullanıcı Bazında Abonelikler ===", 'blue'))
    for user in user_analysis:
        print(colored(f"Kullanıcı ID: {user['user_id']}", 'green'))
        print(colored(f"Abonelik Sayısı: {user['subscription_count']}", 'yellow'))
        print(colored(f"Abonelikler: {user['subscriptions']}", 'cyan'))
        print("-" * 40)

    print(colored("\n=== Ders Kodu Bazında Abonelikler ===", 'blue'))
    for lesson in lesson_code_analysis_sorted:
        print(colored(f"Ders Kodu: {lesson['lesson_code']}", 'green'))
        print(colored(f"Abone Sayısı: {lesson['subscriber_count']}", 'yellow'))
        print(colored(f"Abone Olan Kullanıcılar: {lesson['subscribers']}", 'cyan'))
        print("-" * 40)

    print(colored("\n=== Genel İstatistikler ===", 'blue'))
    print(colored(f"Toplam Kullanıcı Sayısı: {total_users}", 'green'))
    print(colored(f"Toplam Abonelik Sayısı: {total_subscriptions}", 'yellow'))
    print(colored(f"Toplam Farklı Ders Kodu Sayısı: {total_unique_lesson_codes}", 'cyan'))
    print(colored(f"Kullanıcı Başına Ortalama Abonelik Sayısı: {avg_subscriptions_per_user:.2f}", 'magenta'))

    # Grafik oluştur (Ders kodlarına göre abone sayısı)
    if lesson_code_analysis_sorted:
        lesson_codes = [lesson['lesson_code'] for lesson in lesson_code_analysis_sorted]
        subscribers = [lesson['subscriber_count'] for lesson in lesson_code_analysis_sorted]

        plt.figure(figsize=(10, 6))
        plt.barh(lesson_codes, subscribers, color='skyblue')
        plt.xlabel('Abonelik Sayısı')
        plt.ylabel('Ders Kodları')
        plt.title('Ders Kodlarına Göre Abonelik Sayısı')
        plt.tight_layout()
        plt.savefig('subscription_analysis_by_lesson_code.png')
        plt.show()

# Analizi çalıştır
analyze_subscriptions()