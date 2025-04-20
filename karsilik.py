import os
from dotenv import load_dotenv
import time
import random
from datetime import datetime, timezone, timedelta
import pytz
from atproto import Client
import requests
import json
import warnings

# Pydantic uyarılarını gizle
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

# Türkiye saat dilimini ayarla
turkey_timezone = pytz.timezone('Europe/Istanbul')

# Etkileşim takibi için global değişkenler
processed_interactions = {
    'likes': set(),  # Beğenilen gönderilerin URI'leri
    'comments': set()  # Yorum yapılan gönderilerin URI'leri
}

def get_turkey_time():
    return datetime.now(turkey_timezone)

# Telegram hata yönetimi için değişkenler
telegram_error_count = 0
telegram_error_notified = False

def send_telegram_message(message):
    """Telegram kanalına mesaj gönder"""
    global telegram_error_count, telegram_error_notified
    
    try:
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {
                "chat_id": TELEGRAM_CHANNEL_ID,
                "text": message,
                "parse_mode": "HTML"
            }
            response = requests.post(url, data=data)
            
            if response.status_code == 429:  # Rate limit hatası
                telegram_error_count += 1
                
                # Eğer çok fazla hata varsa ve daha önce bildirim gönderilmediyse
                if telegram_error_count >= 5 and not telegram_error_notified:
                    emergency_data = {
                        "chat_id": TELEGRAM_CHANNEL_ID,
                        "text": "⚠️ Çok fazla sorunumuz var patron buraya bakman lazım",
                        "parse_mode": "HTML"
                    }
                    requests.post(url, data=emergency_data)
                    telegram_error_notified = True
                    print("Acil durum mesajı gönderildi!")
                    return
                
                # Eğer zaten bildirim gönderildiyse, sessizce çık
                if telegram_error_notified:
                    return
                    
                # Normal rate limit işlemi
                retry_after = response.json().get('parameters', {}).get('retry_after', 60)
                print(f"Telegram rate limit. Waiting {retry_after} seconds...")
                time.sleep(retry_after)
                response = requests.post(url, data=data)
                
            elif response.status_code == 200:
                # Başarılı gönderimde hata sayacını sıfırla
                telegram_error_count = 0
                telegram_error_notified = False
            else:
                print(f"Telegram mesajı gönderilemedi: {response.text}")
                
    except Exception as e:
        print(f"Telegram hatası: {str(e)}")

def log_error(error_type, error_message, additional_info=""):
    """Hata mesajını hem konsola yazdır hem de Telegram'a gönder"""
    current_time = get_turkey_time().strftime('%d/%m/%Y %H:%M:%S')
    error_text = f"""
⚠️ <b>Hata Bildirimi</b>
🕒 Zaman: {current_time}
📍 Konum: {error_type}
❌ Hata: {error_message}
{f"ℹ️ Ek Bilgi: {additional_info}" if additional_info else ""}
"""
    print(error_text)
    send_telegram_message(error_text)

# .env dosyasından API anahtarlarını yükle
load_dotenv()

# Telegram Bot ayarları
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID_2')

# Bluesky API bağlantısı
bluesky_client = Client()
try:
    # App Password ile kimlik doğrulama
    bluesky_client.login(os.getenv('BLUESKY_IDENTIFIER'), os.getenv('BLUESKY_APP_PASSWORD'))
    print("Bluesky bağlantısı başarılı!")
    
    # Bağlantıyı test et
    profile = bluesky_client.get_profile(os.getenv('BLUESKY_IDENTIFIER'))
    if profile:
        print(f"Bluesky profili doğrulandı: {profile.handle}")
    else:
        raise Exception("Profil bilgisi alınamadı")
        
except Exception as e:
    log_error("Bluesky Bağlantısı", str(e))
    bluesky_client = None
    print("⚠️ Bluesky bağlantısı başarısız! Bot çalışamayacak.")

# Etkileşim limitleri için değişkenler
last_like_time = None
last_reply_time = None
last_like_reset = datetime.now(turkey_timezone)
last_reply_reset = datetime.now(turkey_timezone)
liked_posts = set()     # Beğenilen gönderileri kaydet
replied_posts = set()   # Yorum yapılan gönderileri kaydet
interacted_users = set()  # Etkileşimde bulunulan kullanıcıları kaydet

def get_post_uri_from_url(url):
    """URL'den post URI'sini oluştur"""
    try:
        # URL'den kullanıcı adı ve post ID'sini çıkar
        parts = url.split('/')
        username = parts[-3]  # profile/username.bsky.social/post/ID
        post_id = parts[-1]
        
        # Kullanıcının DID'sini al
        profile = bluesky_client.get_profile(username)
        user_did = profile.did
        
        # URI'yi oluştur
        post_uri = f"at://{user_did}/app.bsky.feed.post/{post_id}"
        print(f"Post URI oluşturuldu: {post_uri}")
        return post_uri
        
    except Exception as e:
        log_error("URI Oluşturma", str(e), f"URL: {url}")
        return None

# Hedef gönderi URL'si
TARGET_POST_URL = "https://bsky.app/profile/mrmoonrose.bsky.social/post/3lna2hon6ic2r"

# Hedef gönderi URI'si
TARGET_POST_URI = get_post_uri_from_url(TARGET_POST_URL)

# Eğer URI oluşturulamadıysa, varsayılan değeri kullan
if not TARGET_POST_URI:
    TARGET_POST_URI = "at://did:plc:YOUR_DID/app.bsky.feed.post/YOUR_POST_RKEY"
    print("⚠️ URI oluşturulamadı, varsayılan değer kullanılıyor.")

# Günlük çalışma zamanları (günde 4 kez)
DAILY_RUN_TIMES = [
    "12:00",  # Öğle
    "14:00",  # Öğleden sonra
    "17:00",  # Akşam
    "19:00"   # Gece
]

# Son kontrol edilen etkileşimlerin zamanı
last_check_time = None

def can_operate():
    """Botun çalışma saatlerini kontrol et (11:00 - 20:00 arası)"""
    current_time = get_turkey_time()
    current_hour = current_time.hour
    
    # 09:00 ile 20:00 arası kontrolü
    if 11 <= current_hour < 20:
        return True
    
    print(f"Bot şu anda çalışmıyor. Çalışma saatleri: 11:00 - 20:00 (Şu anki saat: {current_time.strftime('%H:%M')})")
    return False

def is_run_time():
    """Şu anki zamanın günlük çalışma zamanlarından biri olup olmadığını kontrol et"""
    global last_check_time
    
    current_time = get_turkey_time()
    current_time_str = current_time.strftime('%H:%M')
    
    # Eğer şu anki zaman çalışma zamanlarından biriyse
    if current_time_str in DAILY_RUN_TIMES:
        # Eğer son kontrol zamanı yoksa ve son kontrolden bu yana en az 1 saat geçtiyse
        if last_check_time is None or (current_time - last_check_time).total_seconds() >= 3600:
            last_check_time = current_time
            return True
    
    return False

def can_like():
    """Beğeni yapılabilir mi kontrol et"""
    global last_like_time, last_like_reset
    current_time = get_turkey_time()
    
    # Çalışma saatleri kontrolü
    if not can_operate():
        return False
    
    # Son beğeni zamanını güncelle
    last_like_time = current_time
    
    return True

def can_reply():
    """Yorum yapılabilir mi kontrol et"""
    global last_reply_time, last_reply_reset
    current_time = get_turkey_time()
    
    # Çalışma saatleri kontrolü
    if not can_operate():
        return False
    
    # Son yorum zamanını güncelle
    last_reply_time = current_time
    
    return True

def like_post(post):
    """Gönderiyi beğen"""
    try:
        # Gönderiyi beğen
        like_data = {
            'collection': 'app.bsky.feed.like',
            'repo': bluesky_client.me.did,
            'record': {
                'subject': {
                    'uri': post.uri,
                    'cid': post.cid
                },
                'createdAt': datetime.now(timezone.utc).isoformat()
            }
        }
        
        # Beğeni işlemini gerçekleştir
        bluesky_client.com.atproto.repo.create_record(like_data)
        
        # Beğenilen gönderiler listesine ekle
        liked_posts.add(post.uri)
        
        # Telegram'a bildir
        send_telegram_message(f"✅ Gönderi beğenildi:\nKullanıcı: {post.author.handle}\nGönderi: {post.uri}")
        print(f"Gönderi beğenildi: {post.uri}")
        
    except Exception as e:
        log_error("Beğeni", str(e), f"Gönderi: {post.uri}")

def reply_to_post(post):
    """Gönderiye yorum yap"""
    try:
        # Yorum metnini oluştur
        reply_text = "Harika bir paylaşım! 👏"
        
        # Yorumu gönder
        bluesky_client.app.bsky.feed.post({
            'text': reply_text,
            'reply': {
                'root': {
                    'uri': post.uri,
                    'cid': post.cid
                },
                'parent': {
                    'uri': post.uri,
                    'cid': post.cid
                }
            }
        })
        
        # Yorum yapılan gönderiler listesine ekle
        replied_posts.add(post.uri)
        
        # Telegram'a bildir
        send_telegram_message(f"💬 Gönderiye yorum yapıldı:\nKullanıcı: {post.author.handle}\nGönderi: {post.uri}\nYorum: {reply_text}")
        print(f"Gönderiye yorum yapıldı: {post.uri}")
        
    except Exception as e:
        log_error("Yorum", str(e), f"Gönderi: {post.uri}")

def get_post_comments(post_uri):
    """Gönderiye yapılan yorumları al"""
    try:
        print(f"\nGönderi yorumları alınıyor: {post_uri}")
        
        # Gönderiye yapılan yorumları al
        response = bluesky_client.app.bsky.feed.get_post_thread({'uri': post_uri})
        
        if not response or not hasattr(response, 'thread') or not hasattr(response.thread, 'replies'):
            print("Yorum bulunamadı")
            return []
            
        comments = []
        for reply in response.thread.replies:
            if hasattr(reply, 'post') and hasattr(reply.post, 'author'):
                author = reply.post.author
                if hasattr(author, 'did'):
                    comment_data = {
                        'author': {
                            'did': author.did,
                            'handle': author.handle if hasattr(author, 'handle') else 'unknown'
                        },
                        'text': reply.post.record.text if hasattr(reply.post, 'record') and hasattr(reply.post.record, 'text') else ''
                    }
                    comments.append(comment_data)
                    print(f"Yorum bulundu - Kullanıcı: {author.did} (@{comment_data['author']['handle']})")
                    print(f"Yorum metni: {comment_data['text'][:50]}...")
        
        print(f"Toplam {len(comments)} yorum bulundu")
        return comments
        
    except Exception as e:
        print(f"Yorumlar alınırken hata: {str(e)}")
        log_error("Yorum Alma", str(e), f"Gönderi: {post_uri}")
        return []

def get_post_likes(post_uri):
    """Gönderiyi beğenenleri al"""
    try:
        print(f"\nGönderi beğenileri alınıyor: {post_uri}")
        
        # Gönderiyi beğenenleri al
        response = bluesky_client.app.bsky.feed.get_likes({'uri': post_uri})
        
        if not response or not hasattr(response, 'likes'):
            print("Beğeni bulunamadı")
            return []
            
        likes = []
        for like in response.likes:
            if hasattr(like, 'actor') and hasattr(like.actor, 'did'):
                like_data = {
                    'actor': {
                        'did': like.actor.did,
                        'handle': like.actor.handle if hasattr(like.actor, 'handle') else 'unknown'
                    }
                }
                likes.append(like_data)
                print(f"Beğeni bulundu - Kullanıcı: {like.actor.did} (@{like_data['actor']['handle']})")
        
        print(f"Toplam {len(likes)} beğeni bulundu")
        return likes
        
    except Exception as e:
        print(f"Beğeniler alınırken hata: {str(e)}")
        log_error("Beğeni Alma", str(e), f"Gönderi: {post_uri}")
        return []

def get_user_latest_post(user_did):
    """Kullanıcının en son gönderisini al (sadece kendi gönderileri, yanıtlar hariç)"""
    try:
        print(f"\nKullanıcının en son gönderisi alınıyor: {user_did}")
        
        # Kullanıcının gönderilerini al
        response = bluesky_client.app.bsky.feed.get_author_feed({
            'actor': user_did,
            'limit': 20  # Daha fazla gönderi al
        })
        
        if not response or not hasattr(response, 'feed'):
            print("Kullanıcının gönderileri bulunamadı")
            return None
            
        # En son gönderiyi bul
        for post in response.feed:
            if hasattr(post, 'post'):
                # Post detaylarını yazdır
                print(f"\nGönderi detayları:")
                print(f"URI: {post.post.uri if hasattr(post.post, 'uri') else 'URI yok'}")
                
                # Record içeriğini kontrol et
                if hasattr(post.post, 'record'):
                    record = post.post.record
                    # Reply kontrolü - record içinde reply varsa ve parent/root bilgisi varsa bu bir yanıttır
                    if hasattr(record, 'reply') and record.reply and hasattr(record.reply, 'parent'):
                        print("Bu gönderi bir yanıt (record.reply.parent var), atlanıyor...")
                        continue
                    
                # Post objesinde reply kontrolü
                if hasattr(post.post, 'reply') and post.post.reply and hasattr(post.post.reply, 'parent'):
                    print("Bu gönderi bir yanıt (post.reply.parent var), atlanıyor...")
                    continue
                
                # Eğer buraya kadar geldiyse, bu bir orijinal gönderidir
                if hasattr(post.post, 'uri'):
                    print(f"Orijinal gönderi bulundu: {post.post.uri}")
                    print(f"Gönderi metni: {post.post.record.text[:100] if hasattr(post.post, 'record') and hasattr(post.post.record, 'text') else 'Metin yok'}...")
                    return post.post.uri
                
        print("Kullanıcının orijinal gönderisi bulunamadı")
        return None
        
    except Exception as e:
        print(f"Kullanıcının gönderisi alınırken hata: {str(e)}")
        log_error("Gönderi Alma", str(e), f"Kullanıcı: {user_did}")
        return None

def uri_to_url(uri):
    """URI'yi URL'ye dönüştür"""
    try:
        # URI formatı: at://did:plc:XXXX/app.bsky.feed.post/YYYY
        parts = uri.split('/')
        if len(parts) >= 4:
            did = parts[2]
            post_id = parts[-1]
            return f"https://bsky.app/profile/{did}/post/{post_id}"
        return None
    except Exception as e:
        print(f"URI'den URL'ye dönüştürme hatası: {str(e)}")
        return None

def process_user_interaction(user_did, has_commented, has_liked):
    """Kullanıcının etkileşimlerini işle"""
    try:
        print(f"\nKullanıcı etkileşimi işleniyor: {user_did}")
        print(f"Yorum durumu: {has_commented}, Beğeni durumu: {has_liked}")
        
        # Kullanıcının en son gönderisini al
        latest_post_uri = get_user_latest_post(user_did)
        if not latest_post_uri:
            print("Kullanıcının gönderisi bulunamadı")
            return
            
        print(f"Kullanıcının en son gönderisi: {latest_post_uri}")
        
        # Kullanıcı bilgilerini al
        try:
            profile = bluesky_client.get_profile(user_did)
            username = profile.handle if profile else "Bilinmeyen Kullanıcı"
        except Exception as e:
            print(f"Kullanıcı bilgileri alınamadı: {str(e)}")
            username = "Bilinmeyen Kullanıcı"
        
        # Post URL'sini oluştur
        post_url = uri_to_url(latest_post_uri)
        if not post_url:
            post_url = latest_post_uri
        
        # Yorum yapıldıysa ve daha önce yorum yapılmamışsa
        if has_commented and latest_post_uri not in processed_interactions['comments']:
            try:
                comment_text = "Harika bir paylaşım! 👏"
                print(f"Yorum yapılıyor: {comment_text}")
                print(f"Hedef gönderi: {latest_post_uri}")
                
                # Yorumu gönder
                response = bluesky_client.app.bsky.feed.create_post({
                    'text': comment_text,
                    'reply': {
                        'root': {'uri': latest_post_uri},
                        'parent': {'uri': latest_post_uri}
                    }
                })
                
                # Yorum yapılan gönderiler listesine ekle
                processed_interactions['comments'].add(latest_post_uri)
                
                print("Yorum başarıyla yapıldı")
                send_telegram_message(f"💬 Yorum yapıldı:\n👤 Kullanıcı: @{username}\n🔗 Gönderi: {post_url}\n💭 Yorum: {comment_text}")
                time.sleep(5)  # Yorum ve beğeni arasında bekle
            except Exception as e:
                print(f"Yorum yapılırken hata: {str(e)}")
                log_error("Yorum Yapma", str(e), f"Kullanıcı: {username} (@{user_did}), Gönderi: {post_url}")
        
        # Beğeni yapıldıysa, yorum yapılmadıysa ve daha önce beğenilmemişse
        if has_liked and not has_commented and latest_post_uri not in processed_interactions['likes']:
            try:
                print(f"Beğeni yapılıyor...")
                print(f"Hedef gönderi: {latest_post_uri}")
                
                # Gönderinin detaylarını al
                post = bluesky_client.app.bsky.feed.get_posts({'uris': [latest_post_uri]})
                if not post or not post.posts:
                    print("Gönderi bulunamadı, beğeni yapılamıyor.")
                    return
                
                post_data = post.posts[0]
                
                # Beğeni yap
                like_data = {
                    'collection': 'app.bsky.feed.like',
                    'repo': bluesky_client.me.did,
                    'record': {
                        'subject': {
                            'uri': post_data.uri,
                            'cid': post_data.cid
                        },
                        'createdAt': datetime.now(timezone.utc).isoformat()
                    }
                }
                
                # Beğeni işlemini gerçekleştir
                bluesky_client.com.atproto.repo.create_record(like_data)
                
                # Beğenilen gönderiler listesine ekle
                processed_interactions['likes'].add(latest_post_uri)
                
                print("Beğeni başarıyla yapıldı")
                send_telegram_message(f"❤️ Beğeni yapıldı:\n👤 Kullanıcı: @{username}\n🔗 Gönderi: {post_url}")
            except Exception as e:
                print(f"Beğeni yapılırken hata: {str(e)}")
                log_error("Beğeni Yapma", str(e), f"Kullanıcı: {username} (@{user_did}), Gönderi: {post_url}")
                
    except Exception as e:
        print(f"Kullanıcı etkileşimi işlenirken hata: {str(e)}")
        log_error("Etkileşim İşleme", str(e), f"Kullanıcı: {user_did}")

def get_new_interactions():
    """Hedef gönderideki yeni etkileşimleri al"""
    try:
        # Hedef gönderiyi al
        post = bluesky_client.app.bsky.feed.get_posts({'uris': [TARGET_POST_URI]})
        if not post or not post.posts:
            print("Hedef gönderi bulunamadı")
            return [], []
            
        target_post = post.posts[0]
        print(f"Hedef gönderi bulundu: {target_post.record.text[:50]}...")
        
        # Yorumları al
        comments = []
        try:
            thread = bluesky_client.app.bsky.feed.get_post_thread({'uri': TARGET_POST_URI})
            if thread and hasattr(thread, 'thread') and hasattr(thread.thread, 'replies'):
                for reply in thread.thread.replies:
                    if hasattr(reply, 'post') and hasattr(reply.post, 'author'):
                        comments.append(reply.post.author.did)
        except Exception as e:
            print(f"Yorumlar alınırken hata oluştu: {str(e)}")
            log_error("Yorum Alma", str(e))
            
        # Beğenileri al
        likes = []
        try:
            likes_response = bluesky_client.app.bsky.feed.get_likes({'uri': TARGET_POST_URI})
            if likes_response and hasattr(likes_response, 'likes'):
                for like in likes_response.likes:
                    if hasattr(like, 'actor') and hasattr(like.actor, 'did'):
                        likes.append(like.actor.did)
        except Exception as e:
            print(f"Beğeniler alınırken hata oluştu: {str(e)}")
            log_error("Beğeni Alma", str(e))
            
        print(f"Toplam {len(comments)} yorum ve {len(likes)} beğeni bulundu")
        return comments, likes
        
    except Exception as e:
        print(f"Etkileşimler alınırken hata oluştu: {str(e)}")
        log_error("Etkileşim Alma", str(e))
        return [], []

def main():
    """Ana fonksiyon"""
    try:
        print("\nBot başlatılıyor...")
        print(f"Hedef gönderi URI: {TARGET_POST_URI}")
        
        # Hedef gönderiyi kontrol et
        try:
            post = bluesky_client.app.bsky.feed.get_posts({'uris': [TARGET_POST_URI]})
            if not post or not post.posts or not post.posts[0]:
                print("Hedef gönderi bulunamadı!")
                send_telegram_message("Hata: Hedef gönderi bulunamadı!")
                return
                
            post_text = post.posts[0].record.text if hasattr(post.posts[0], 'record') and hasattr(post.posts[0].record, 'text') else "Metin yok"
            print(f"Hedef gönderi bulundu: {post_text[:50]}...")
            
        except Exception as e:
            print(f"Hedef gönderi kontrol edilirken hata: {str(e)}")
            send_telegram_message(f"Hata: Hedef gönderi kontrol edilemedi: {str(e)}")
            return
            
        # Son kontrol edilen zamanı takip etmek için
        last_checked_date = None
        
        while True:
            try:
                current_time = get_turkey_time()
                current_date = current_time.date()
                current_time_str = current_time.strftime('%H:%M')
                print(f"\nŞu anki zaman: {current_time_str}")
                
                # Eğer yeni bir gün başladıysa veya ilk çalıştırmaysa
                if last_checked_date != current_date:
                    last_checked_date = current_date
                    print("Yeni gün başladı veya ilk çalıştırma")
                
                # Eğer şu anki zaman kontrol zamanlarından biriyse
                if current_time_str in DAILY_RUN_TIMES:
                    print("Kontrol zamanı geldi, etkileşimler kontrol ediliyor...")
                    
                    # Yorumları al
                    comments = get_post_comments(TARGET_POST_URI)
                    print(f"Bulunan yorum sayısı: {len(comments)}")
                    
                    # Beğenileri al
                    likes = get_post_likes(TARGET_POST_URI)
                    print(f"Bulunan beğeni sayısı: {len(likes)}")
                    
                    # Kullanıcı listelerini oluştur
                    comment_users = [comment['author']['did'] for comment in comments]
                    like_users = [like['actor']['did'] for like in likes]
                    
                    # Her iki işlemi de yapan kullanıcıları bul
                    both_users = list(set(comment_users) & set(like_users))
                    
                    # Sadece yorum yapan kullanıcıları bul
                    only_comment_users = list(set(comment_users) - set(like_users))
                    
                    # Sadece beğenen kullanıcıları bul
                    only_like_users = list(set(like_users) - set(comment_users))
                    
                    # Listeleri yazdır ve Telegram'a gönder
                    print("\n=== ETKİLEŞİM RAPORU ===")
                    print(f"Toplam yorum sayısı: {len(comments)}")
                    print(f"Toplam beğeni sayısı: {len(likes)}")
                    print(f"Her iki işlemi de yapan kullanıcı sayısı: {len(both_users)}")
                    print(f"Sadece yorum yapan kullanıcı sayısı: {len(only_comment_users)}")
                    print(f"Sadece beğenen kullanıcı sayısı: {len(only_like_users)}")
                    
                    # Telegram'a rapor gönder
                    report = f"""
📊 <b>Etkileşim Raporu</b>
🕒 Zaman: {current_time.strftime('%d/%m/%Y %H:%M')}
📝 Toplam yorum sayısı: {len(comments)}
❤️ Toplam beğeni sayısı: {len(likes)}
👥 Her iki işlemi de yapan kullanıcı sayısı: {len(both_users)}
💬 Sadece yorum yapan kullanıcı sayısı: {len(only_comment_users)}
👍 Sadece beğenen kullanıcı sayısı: {len(only_like_users)}
"""
                    send_telegram_message(report)
                    
                    # Kullanıcı listelerini detaylı olarak yazdır
                    print("\n=== KULLANICI LİSTELERİ ===")
                    
                    # Her iki işlemi de yapan kullanıcılar
                    print("\n--- Her iki işlemi de yapan kullanıcılar ---")
                    for user_did in both_users:
                        user_handle = next((comment['author']['handle'] for comment in comments if comment['author']['did'] == user_did), "Bilinmeyen")
                        print(f"- {user_handle} ({user_did})")
                    
                    # Sadece yorum yapan kullanıcılar
                    print("\n--- Sadece yorum yapan kullanıcılar ---")
                    for user_did in only_comment_users:
                        user_handle = next((comment['author']['handle'] for comment in comments if comment['author']['did'] == user_did), "Bilinmeyen")
                        print(f"- {user_handle} ({user_did})")
                    
                    # Sadece beğenen kullanıcılar
                    print("\n--- Sadece beğenen kullanıcılar ---")
                    for user_did in only_like_users:
                        user_handle = next((like['actor']['handle'] for like in likes if like['actor']['did'] == user_did), "Bilinmeyen")
                        print(f"- {user_handle} ({user_did})")
                    
                    # Kullanıcı listelerini Telegram'a gönder
                    both_users_text = "\n".join([f"- {next((comment['author']['handle'] for comment in comments if comment['author']['did'] == user_did), 'Bilinmeyen')} ({user_did})" for user_did in both_users])
                    only_comment_users_text = "\n".join([f"- {next((comment['author']['handle'] for comment in comments if comment['author']['did'] == user_did), 'Bilinmeyen')} ({user_did})" for user_did in only_comment_users])
                    only_like_users_text = "\n".join([f"- {next((like['actor']['handle'] for like in likes if like['actor']['did'] == user_did), 'Bilinmeyen')} ({user_did})" for user_did in only_like_users])
                    
                    users_report = f"""
👥 <b>Her iki işlemi de yapan kullanıcılar ({len(both_users)})</b>
{both_users_text if both_users else "Kullanıcı yok"}

💬 <b>Sadece yorum yapan kullanıcılar ({len(only_comment_users)})</b>
{only_comment_users_text if only_comment_users else "Kullanıcı yok"}

👍 <b>Sadece beğenen kullanıcılar ({len(only_like_users)})</b>
{only_like_users_text if only_like_users else "Kullanıcı yok"}
"""
                    send_telegram_message(users_report)
                    
                    # Yeni etkileşimleri işle
                    processed_users = set()
                    
                    # Önce yorum yapanları işle
                    for comment in comments:
                        user_did = comment['author']['did']
                        if user_did not in processed_users:
                            print(f"\nYorum yapan kullanıcı işleniyor: {user_did} (@{comment['author']['handle']})")
                            has_liked = user_did in like_users
                            process_user_interaction(user_did, True, has_liked)
                            processed_users.add(user_did)
                            time.sleep(10)  # Her kullanıcı arasında bekle
                    
                    # Sonra sadece beğenenleri işle
                    for like in likes:
                        user_did = like['actor']['did']
                        if user_did not in processed_users:
                            print(f"\nBeğenen kullanıcı işleniyor: {user_did} (@{like['actor']['handle']})")
                            process_user_interaction(user_did, False, True)
                            processed_users.add(user_did)
                            time.sleep(10)  # Her kullanıcı arasında bekle
                    
                    print("\nTüm etkileşimler işlendi")
                    print(f"Toplam işlenen kullanıcı sayısı: {len(processed_users)}")
                    
                    # Bir sonraki kontrol zamanına kadar bekle
                    next_check = None
                    for check_time in DAILY_RUN_TIMES:
                        hour, minute = map(int, check_time.split(':'))
                        check_datetime = current_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if check_datetime > current_time:
                            next_check = check_datetime
                            break
                    
                    if next_check is None:
                        # Eğer bugün için kontrol zamanı kalmadıysa, yarının ilk kontrol zamanını al
                        next_check = current_time.replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
                    
                    wait_seconds = (next_check - current_time).total_seconds()
                    wait_minutes = int(wait_seconds / 60)
                    print(f"Bir sonraki kontrol zamanı: {next_check.strftime('%H:%M')} ({wait_minutes} dakika sonra)")
                    
                    # Bir sonraki kontrol zamanına kadar bekle
                    time.sleep(wait_seconds)
                else:
                    # Bir sonraki kontrol zamanını hesapla
                    next_check = None
                    for check_time in DAILY_RUN_TIMES:
                        hour, minute = map(int, check_time.split(':'))
                        check_datetime = current_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if check_datetime > current_time:
                            next_check = check_datetime
                            break
                    
                    if next_check is None:
                        # Eğer bugün için kontrol zamanı kalmadıysa, yarının ilk kontrol zamanını al
                        next_check = current_time.replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
                    
                    wait_seconds = (next_check - current_time).total_seconds()
                    wait_minutes = int(wait_seconds / 60)
                    print(f"Bir sonraki kontrol zamanı: {next_check.strftime('%H:%M')} ({wait_minutes} dakika sonra)")
                    
                    # Bir sonraki kontrol zamanına kadar bekle
                    time.sleep(wait_seconds)
                
            except Exception as e:
                print(f"Döngü sırasında hata: {str(e)}")
                log_error("Ana Döngü", str(e))
                time.sleep(60)  # Hata durumunda 1 dakika bekle
                
    except Exception as e:
        print(f"Ana fonksiyonda hata: {str(e)}")
        log_error("Ana Fonksiyon", str(e))
        send_telegram_message(f"Kritik Hata: {str(e)}")

if __name__ == "__main__":
    main() 
