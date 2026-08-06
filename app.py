import os
import sqlite3
import asyncio
import io
import re
import queue
import threading
import difflib
import webbrowser
from flask import Flask, request, jsonify, Response, render_template, send_from_directory

from dotenv import load_dotenv
from google import genai
from google.genai import types
import edge_tts

load_dotenv()
app = Flask(__name__)

# ==========================================
# 🔑 ENTERPRISE API KEY ROTATION
# ==========================================
keys_env = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY")
if not keys_env:
    raise ValueError("Missing GEMINI_API_KEYS in the .env file!")

API_KEYS = [k.strip() for k in keys_env.split(',')]
current_key_index = 0

CACHE_DB = "gemini_cache.db"       
VERIFIED_DB = "verified_shlokas.db" 

def init_dbs():
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_input TEXT,
            bot_response TEXT,
            language TEXT,
            source TEXT,
            score REAL,
            session_id TEXT
        )
    ''')
    try: cursor.execute("ALTER TABLE chat_history ADD COLUMN source TEXT DEFAULT 'gemini'")
    except: pass 
    try: cursor.execute("ALTER TABLE chat_history ADD COLUMN score REAL DEFAULT 0")
    except: pass
    try: cursor.execute("ALTER TABLE chat_history ADD COLUMN session_id TEXT DEFAULT 'global'")
    except: pass 

    conn.commit()
    conn.close()

    conn2 = sqlite3.connect(VERIFIED_DB)
    cursor2 = conn2.cursor()
    cursor2.execute('''
        CREATE TABLE IF NOT EXISTS verified_data (
            id TEXT,
            chapter INTEGER,
            verse INTEGER,
            shloka TEXT,
            transliteration TEXT,
            hin_meaning TEXT,
            eng_meaning TEXT,
            word_meaning TEXT
        )
    ''')
    conn2.commit()
    conn2.close()

init_dbs()

@app.route('/')
def index():
    return render_template('index.html')

# ==========================================
# RAG PIPELINE: CACHE -> VERIFIED DB -> GEMINI
# ==========================================
@app.route('/chat', methods=['POST'])
def chat_with_pandit():
    global current_key_index
    data = request.json
    user_text = data.get('text', '').strip()
    target_lang = data.get('lang', 'auto')
    session_id = data.get('session_id', 'global')

    # --- FIX: SMART AUTO-DETECT VIA UNICODE SCRIPT SCANNING ---
    if target_lang == 'auto':
        if re.search(r'[\u0900-\u097F]', user_text): target_lang = 'hi'  # Devanagari (Sanskrit/Hindi)
        elif re.search(r'[\u0A00-\u0A7F]', user_text): target_lang = 'pa' # Gurmukhi
        elif re.search(r'[\u0C00-\u0C7F]', user_text): target_lang = 'te' # Telugu
        elif re.search(r'[\u0B80-\u0BFF]', user_text): target_lang = 'ta' # Tamil
        else: target_lang = 'en' # Default to English

    lang_map = {
        'en': 'English', 'hi': 'Hindi', 'pa': 'Punjabi',
        'ur': 'Urdu', 'mr': 'Marathi', 'gu': 'Gujarati',
        'ta': 'Tamil', 'te': 'Telugu', 'ml': 'Malayalam'
    }
    language_name = lang_map.get(target_lang, 'English')

    # FIX: Mahavakya is strictly False until PROVEN by the verified DB or a verified Cache hit!
    is_mahavakya = False

    # ----------------------------------------------------
    # STEP 1: Check Gemini Cache
    # ----------------------------------------------------
    conn_cache = sqlite3.connect(CACHE_DB)
    cursor_cache = conn_cache.cursor()
    cursor_cache.execute("SELECT user_input, bot_response, source, score FROM chat_history WHERE language=?", (target_lang,))
    cache_rows = cursor_cache.fetchall()
    
    best_cache_score = 0.0
    db_match_response = ""
    cached_source = "db"
    cached_original_score = 0

    for row in cache_rows:
        prev_input, prev_response, p_source, p_score = row
        score = difflib.SequenceMatcher(None, user_text.lower(), prev_input.lower()).ratio()
        if score > best_cache_score:
            best_cache_score = score
            db_match_response = prev_response
            cached_source = p_source if p_source else "db"
            cached_original_score = p_score if p_score else 0

    if best_cache_score >= 0.85:
        display_source = 'verified_db' if cached_source == 'verified_db' else 'db'
        display_score = cached_original_score if cached_source == 'verified_db' else round(best_cache_score * 100, 1)
        
        cursor_cache.execute("INSERT INTO chat_history (user_input, bot_response, language, source, score, session_id) VALUES (?, ?, ?, ?, ?, ?)", 
                       (user_text, db_match_response, target_lang, display_source, display_score, session_id))
        conn_cache.commit()
        conn_cache.close()
        
        # EXACT FIX: Only trigger Easter Egg if the cached response came directly from the VERIFIED DB
        if cached_source == 'verified_db' and ("karmanye" in user_text.lower() or "कर्मण्ये" in user_text):
            is_mahavakya = True

        return jsonify({'reply': db_match_response, 'source': display_source, 'score': display_score, 'is_mahavakya': is_mahavakya})

    # ----------------------------------------------------
    # STEP 2: Check Verified Gita Dataset
    # ----------------------------------------------------
    conn_verified = sqlite3.connect(VERIFIED_DB)
    cursor_verified = conn_verified.cursor()
    cursor_verified.execute("SELECT shloka, transliteration, hin_meaning, eng_meaning, word_meaning FROM verified_data")
    verified_rows = cursor_verified.fetchall()
    conn_verified.close()

    verified_context_string = "No pre-verified scripture found. Rely on your own authentic Pandit knowledge."
    best_v_score = 0.0
    matched_data = None
    final_source = 'gemini'
    final_score = 0

    for row in verified_rows:
        shloka, trans, hin, eng, word = row
        score_sanskrit = difflib.SequenceMatcher(None, user_text.lower(), str(shloka).lower()).ratio()
        score_trans = difflib.SequenceMatcher(None, user_text.lower(), str(trans).lower()).ratio()
        highest_score = max(score_sanskrit, score_trans)
        
        if highest_score > best_v_score:
            best_v_score = highest_score
            matched_data = row

    if best_v_score >= 0.80 and matched_data:
        verified_context_string = f"""
        VERIFIED SCRIPTURE MATCH FOUND FROM DATABASE:
        Original Sanskrit: {matched_data[0]}
        Hindi Meaning: {matched_data[2]}
        English Meaning: {matched_data[3]}
        Word-by-Word Analysis: {matched_data[4]}
        INSTRUCTION: You are Baba. The user is asking about this exact Shloka. 
        Base your explanation STRICTLY on this verified data.
        """
        final_source = 'verified_db'
        final_score = round(best_v_score * 100, 1)

        # EXACT FIX: Strict Mahavakya Easter Egg Detection
        if "karmanye" in str(matched_data[1]).lower() or "कर्मण्ये" in str(matched_data[0]):
            is_mahavakya = True

    # ----------------------------------------------------
    # STEP 3: Prompt Gemini (Expanded Memory)
    # ----------------------------------------------------
    cursor_cache.execute("SELECT user_input, bot_response FROM chat_history WHERE session_id=? ORDER BY id DESC LIMIT 4", (session_id,))
    recent_history = cursor_cache.fetchall()
    history_context = "\n".join([f"User: {r[0]}\nBaba: {r[1]}" for r in reversed(recent_history)])

    system_prompt = f"""
    You are Baba, a highly revered, wise, and authentic Hindu spiritual Guru representing 'VedaSync'.
    
    CRITICAL RULES (FOLLOW STRICTLY):
    1. TARGET LANGUAGE: Generate your ENTIRE response strictly in {language_name}. NEVER ask the user to speak English unless they spoke English.
    2. PERSONA: You MUST ALWAYS speak completely in character as a spiritual Guru. Begin your responses with a warm, spiritual greeting (e.g., 'My child', 'Dear seeker', 'Blessings to you').
    3. SHLOKAS: If the user provides a Shloka or asks for meaning, do NOT just act like a translator. Say something like, "The profound wisdom of this verse teaches us..." and provide the translation and deep meaning. DO NOT echo the original Sanskrit back.
    4. LENGTH: Keep answers CONCISE (Max 2 to 3 short sentences). Do not give long sermons.
    5. FORMAT: Pure plain text ONLY. NO emojis, NO markdown, NO formatting.
    
    Database Context: {verified_context_string}
    Chat History: {history_context}
    User Input: "{user_text}"
    """

    safe_config = types.GenerateContentConfig(
        safety_settings=[
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_NONE),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
        ]
    )

    attempts = 0
    bot_reply = ""
    model_name = 'gemini-3.6-flash'

    while attempts < 3:
        try:
            active_key = API_KEYS[current_key_index]
            print(f"--- Attempt {attempts+1} | Model: {model_name} | Key ending in ...{active_key[-4:]} ---")
            
            client = genai.Client(api_key=active_key)
            response = client.models.generate_content(model=model_name, contents=system_prompt, config=safe_config)
            bot_reply = response.text.strip()
            break 
        except Exception as e:
            err_str = str(e).lower()
            print(f"API Error: {err_str}")
            
            if "429" in err_str or "quota" in err_str or "exhausted" in err_str:
                current_key_index = (current_key_index + 1) % len(API_KEYS)
                print(f"Rotated to API Key Index: {current_key_index}")
                attempts += 1
                if attempts == 2: model_name = 'gemini-3.5-flash'
                continue
            elif "503" in err_str or "overloaded" in err_str:
                print("Server overloaded. Instantly downgrading to gemini-3.5-flash...")
                model_name = 'gemini-3.5-flash'
                attempts += 1
                continue
            else:
                return jsonify({'error': "My mind is cloudy. Please check the network."}), 500

    if not bot_reply: 
        return jsonify({'error': "The cosmic energies are too heavy right now. Please try again later."}), 500

    cursor_cache.execute("INSERT INTO chat_history (user_input, bot_response, language, source, score, session_id) VALUES (?, ?, ?, ?, ?, ?)", 
                    (user_text, bot_reply, target_lang, final_source, final_score, session_id))
    conn_cache.commit()
    conn_cache.close()

    return jsonify({'reply': bot_reply, 'source': final_source, 'score': final_score, 'is_mahavakya': is_mahavakya})

def sanitize_for_tts(text, lang):
    text = text.replace('।', ',').replace('॥', ',').replace('.', ',').replace('-', ' ')
    if lang == 'auto': lang = 'hi' 
        
    if lang == 'pa': text = re.sub(r'[^\u0A00-\u0A7F\s,]', ' ', text)  
    elif lang in ['hi', 'mr']: text = re.sub(r'[^\u0900-\u097F\s,a-zA-Z]', ' ', text)  
    elif lang == 'gu': text = re.sub(r'[^\u0A80-\u0AFF\s,a-zA-Z]', ' ', text)  
    elif lang == 'ta': text = re.sub(r'[^\u0B80-\u0BFF\s,a-zA-Z]', ' ', text)  
    elif lang == 'te': text = re.sub(r'[^\u0C00-\u0C7F\s,a-zA-Z]', ' ', text)  
    elif lang == 'ml': text = re.sub(r'[^\u0D00-\u0D7F\s,a-zA-Z]', ' ', text)  
    elif lang == 'ur': text = re.sub(r'[^\u0600-\u06FF\s,a-zA-Z]', ' ', text)  
    else: text = re.sub(r'[^a-zA-Z\s,]', ' ', text)         
    return re.sub(r'\s+', ' ', text).strip()

def run_edge_tts_thread(text, voice, audio_queue):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async def _stream():
        try:
            communicate = edge_tts.Communicate(text, voice)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_queue.put(chunk["data"])
        except Exception as e: print("Edge TTS Exception:", e)
        finally: audio_queue.put(None)
    loop.run_until_complete(_stream())
    loop.close()

@app.route('/tts', methods=['POST'])
def generate_tts():
    data = request.json
    raw_text = data.get('text', '').strip()
    lang = data.get('lang', 'en')
    
    # 1. SMART TTS AUTO-DETECT: Scan Gemini's output script FIRST!
    if lang == 'auto':
        if re.search(r'[\u0A00-\u0A7F]', raw_text): lang = 'pa'
        elif re.search(r'[\u0900-\u097F]', raw_text): lang = 'hi'
        elif re.search(r'[\u0B80-\u0BFF]', raw_text): lang = 'ta'
        elif re.search(r'[\u0C00-\u0C7F]', raw_text): lang = 'te'
        elif re.search(r'[\u0D00-\u0D7F]', raw_text): lang = 'ml'
        elif re.search(r'[\u0A80-\u0AFF]', raw_text): lang = 'gu'
        elif re.search(r'[\u0600-\u06FF]', raw_text): lang = 'ur'
        else: lang = 'en'

    # 2. NOW sanitize using the correctly detected language
    clean_text = sanitize_for_tts(raw_text, lang)
    
    # 3. BULLETPROOF CRASH FIX: Checks if there are actually any letters left after sanitizing
    if not clean_text.replace(',', '').strip(): 
        clean_text = "Translation complete"
    
    voice_map = {
        'en': 'en-IN-PrabhatNeural', 'hi': 'hi-IN-MadhurNeural', 'pa': 'pa-IN-OjasNeural',
        'ur': 'ur-IN-SalmanNeural', 'mr': 'mr-IN-ManoharNeural', 'gu': 'gu-IN-NiranjanNeural',
        'ta': 'ta-IN-ValluvarNeural', 'te': 'te-IN-MohanNeural', 'ml': 'ml-IN-MidhunNeural'
    }
    voice = voice_map.get(lang, 'en-US-ChristopherNeural')

    if lang == 'pa':
        transliterated_text = "".join([chr(ord(c) - 0x0100) if 0x0A00 <= ord(c) <= 0x0A7F else c for c in clean_text])
        clean_text = transliterated_text
        voice = 'hi-IN-MadhurNeural'
    
    audio_queue = queue.Queue()
    threading.Thread(target=run_edge_tts_thread, args=(clean_text, voice, audio_queue), daemon=True).start()

    def generate():
        while True:
            chunk = audio_queue.get()
            if chunk is None: break
            yield chunk
    return Response(generate(), mimetype="audio/mpeg")

@app.route('/get_history', methods=['GET'])
def get_history():
    session_id = request.args.get('session_id', 'global')
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT user_input, bot_response, source, score FROM chat_history WHERE session_id=? ORDER BY id ASC", (session_id,))
    rows = cursor.fetchall()
    conn.close()
    return jsonify([{"user": r[0], "bot": r[1], "source": r[2] if r[2] else 'gemini', "score": r[3] if r[3] else 0} for r in rows])

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'logo.png', mimetype='image/png')

#if __name__ == '__main__':
#    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
#        threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
#    app.run(debug=True, port=5000)

if __name__ == '__main__':
    # Fix: Dynamically grab the port Render assigns, or default to 5000 locally!
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
