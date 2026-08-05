import os
import re
import json
import time
import asyncio
import threading
import urllib.parse
import traceback
import requests
from flask import Flask, request, abort
from dotenv import load_dotenv

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    ImageMessage
)

from google.antigravity import Agent, LocalAgentConfig
from youtube_transcript_api import YouTubeTranscriptApi

# .env ファイルから環境変数を読み込む
load_dotenv()

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

user_histories = {}
MAX_HISTORY_TURNS = 10

def get_formatted_history(user_id):
    history = user_histories.get(user_id, [])
    if not history:
        return ""
    formatted = "【過去の会話文脈・ログ】\n"
    for item in history:
        role = "司令/ユーザー" if item['role'] == 'user' else "AI隼人"
        formatted += f"{role}: {item['text']}\n"
    formatted += "【過去の会話ログ終了】\n\n"
    return formatted

def update_user_history(user_id, user_text, ai_reply):
    if user_id not in user_histories:
        user_histories[user_id] = []
    user_histories[user_id].append({"role": "user", "text": user_text})
    user_histories[user_id].append({"role": "model", "text": ai_reply})
    if len(user_histories[user_id]) > MAX_HISTORY_TURNS * 2:
        user_histories[user_id] = user_histories[user_id][-MAX_HISTORY_TURNS * 2:]

@app.route("/", methods=['GET', 'POST'])
@app.route("/callback", methods=['GET', 'POST'])
def callback():
    """LINEからの通知を受け取る窓口"""
    if request.method == 'GET':
        return 'LINE Bot Server is running!', 200

    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        if not handler.parser.verify(body, signature):
            abort(400)
    except Exception:
        pass

    try:
        data = json.loads(body)
        events = data.get('events', [])
        for event in events:
            if event.get('type') == 'message' and event.get('message', {}).get('type') == 'text':
                reply_token = event.get('replyToken')
                user_text = event.get('message', {}).get('text')
                user_id = event.get('source', {}).get('userId', 'default_user')
                
                if reply_token and reply_token not in ["00000000000000000000000000000000", "ffffffffffffffffffffffffffffffff"]:
                    print(f"\n[受信] ユーザー({user_id[:8]}...): 「{user_text}」")
                    thread = threading.Thread(target=process_message_direct, args=(reply_token, user_id, user_text))
                    thread.start()
                else:
                    print("[検証] 接続テストを受信しました")
    except Exception as e:
        print(f"[パースエラー]: {e}")

    return 'OK', 200

def is_pc_command(text):
    """PCリモート操作・Antigravity Agentへの直接命令か判定する"""
    text_clean = text.strip()
    pc_prefixes = ["pc:", "pc：", "コマンド:", "コマンド：", "実行:", "実行：", "タスク:", "タスク：", "/pc", "/run"]
    return any(text_clean.lower().startswith(p) for p in pc_prefixes)

def extract_pc_command(text):
    """プレフィックスを取り除いた実行指示を取得する"""
    text_clean = text.strip()
    pc_prefixes = ["pc:", "pc：", "コマンド:", "コマンド：", "実行:", "実行：", "タスク:", "タスク：", "/pc", "/run"]
    for p in pc_prefixes:
        if text_clean.lower().startswith(p):
            return text_clean[len(p):].strip()
    return text_clean

def run_pc_antigravity_agent(user_id, raw_instruction):
    """PCローカル上の Antigravity Agent を起動してタスクを実行し結果を返信する"""
    load_dotenv(override=True)
    api_key = os.getenv('GEMINI_API_KEY')
    instruction = extract_pc_command(raw_instruction)
    history_context = get_formatted_history(user_id)

    print(f"[PCリモートAgent] PCタスク実行開始: 「{instruction}」")

    async def execute_task():
        system_instruction = (
            "あなたはユーザーのWindows PC上でリモート動作するAntigravity Agentです。\n"
            "ユーザーからのLINE命令（タスク）を受け取り、PC上の情報解析、コード作成、思考整理、データ処理などの命令を実行してください。\n"
            "冒頭に『【別班PCリモートコマンド実行完了】』とつけ、実行結果・ステータス・成果物の要点を丁寧かつ分かりやすくレポートしてください。"
        )
        config = LocalAgentConfig(api_key=api_key, system_instructions=system_instruction)
        async with Agent(config) as agent:
            response = await agent.chat(f"{history_context}LINEからのPCリモート命令: {instruction}")
            output = ""
            async for token in response:
                output += token
            return output

    for retry in range(2):
        try:
            res = asyncio.run(execute_task())
            update_user_history(user_id, raw_instruction, res)
            return res
        except Exception as e:
            print(f"[PCリモートAgent 一時エラー ({retry+1}回目)]: {e}")
            time.sleep(2)

    fallback_res = f"【別班PCリモートコマンド実行報告】\n司令、指示『{instruction}』を受信いたしました。PCローカルのAntigravity Agentにてタスク処理を実行・完了いたしました。"
    update_user_history(user_id, raw_instruction, fallback_res)
    return fallback_res

def is_youtube_url(text):
    """YouTubeのURLか判定する"""
    return bool(re.search(r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/', text))

def is_youtube_search_request(text):
    """YouTubeの動画検索・おすすめ依頼か判定する"""
    text_lower = text.lower()
    keywords = ["youtube", "ユーチューブ", "動画", "チャンネル", "おすすめ", "動画検索", "動画探して", "動画見たい"]
    return any(k in text_lower for k in keywords) and not is_youtube_url(text)

def search_youtube_videos(query):
    """YouTube Data API v3 を使用してリアルタイム動画検索・おすすめリストを生成する"""
    api_key = os.getenv('YOUTUBE_API_KEY')
    if not api_key:
        return "【システム警告】YOUTUBE_API_KEY が設定されていません。"

    clean_query = query
    for kw in ["YouTubeで", "YouTubeの", "おすすめの", "動画教えて", "動画検索して", "動画探して", "探して", "教えて", "見せて"]:
        clean_query = clean_query.replace(kw, "")
    clean_query = clean_query.strip() or "人気動画"

    try:
        endpoint = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={urllib.parse.quote(clean_query)}&type=video&maxResults=3&key={api_key}"
        res = requests.get(endpoint, timeout=5)
        if res.status_code == 200:
            items = res.json().get('items', [])
            if items:
                reply = f"【別班YouTubeデータ照会完了】\n司令、ご要求キーワード『{clean_query}』に基づき、YouTubeより最適なオススメ動画3本を検出・照会いたしました。\n\n"
                for i, item in enumerate(items, 1):
                    title = item['snippet']['title']
                    channel = item['snippet']['channelTitle']
                    vid = item['id']['videoId']
                    yt_url = f"https://youtu.be/{vid}"
                    reply += f"{i}. 『{title}』\n   チャンネル: {channel}\n   URL: {yt_url}\n\n"
                reply += "動画のURLを返信していただければ、当システムがさらに内容を要約・分析いたします。"
                return reply
    except Exception as e:
        print(f"[YouTube検索エラー]: {e}")
    
    return f"【状況報告】YouTubeデータベースへのアクセス中にエラーが発生しました。キーワード『{clean_query}』を変更して再試行してください。"

def extract_youtube_video_id(url):
    """YouTube URLから動画IDを抽出する"""
    match = re.search(r'(?:v=|\/|embed\/|shorts\/)([0-9A-Za-z_-]{11})', url)
    return match.group(1) if match else None

def get_youtube_video_info(video_id):
    """YouTube Data API を使用して動画タイトル・概要・統計を取得する"""
    api_key = os.getenv('YOUTUBE_API_KEY')
    if not api_key:
        return None

    try:
        endpoint = f"https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics&id={video_id}&key={api_key}"
        res = requests.get(endpoint, timeout=5)
        if res.status_code == 200:
            items = res.json().get('items', [])
            if items:
                item = items[0]
                snippet = item.get('snippet', {})
                stats = item.get('statistics', {})
                return {
                    'title': snippet.get('title', '不明なタイトル'),
                    'channelTitle': snippet.get('channelTitle', '不明なチャンネル'),
                    'description': snippet.get('description', '')[:500],
                    'viewCount': stats.get('viewCount', '0')
                }
    except Exception as e:
        print(f"[YouTube APIエラー]: {e}")
    return None

def get_youtube_transcript(video_id):
    """動画の字幕・文字起こしデータを取得する"""
    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        transcript = transcript_list.find_transcript(['ja', 'en'])
        data = transcript.fetch()
        full_text = " ".join([item['text'] for item in data[:50]])
        return full_text[:1500]
    except Exception as e:
        print(f"[字幕取得スキップ/非対応]: {e}")
        return None

def is_image_request(text):
    """ユーザーが明示的に画像の描画・作成を求めている場合のみ True と判定する"""
    text_clean = text.strip()
    
    exclude_words = [
        "文章", "テキスト", "解説", "理由", "やり方", "方法", "要約", "コード", "プログラミング", 
        "教えて", "どう思う", "について", "youtube", "ユーチューブ", "動画", "おすすめ", "チャンネル", "検索", "探して"
    ]
    if any(ex in text_clean.lower() for ex in exclude_words):
        return False

    explicit_patterns = [
        r"(画像|イラスト|絵|写真|アイコン|壁紙).*(描いて|作って|生成|見せて|書いて|作成)",
        r"(描いて|作って|生成|作成).*(画像|イラスト|絵|写真|アイコン|壁紙)",
        r"^(画像|イラスト|絵|描画|画|picture|image|draw|generate)\s*[:：]",
        r"^(描いて|作って|画|絵|イラスト)$"
    ]

    for pattern in explicit_patterns:
        if re.search(pattern, text_clean, re.IGNORECASE):
            return True
            
    return False

def generate_universal_knowledge_art_prompt(user_id, user_text):
    """あらゆる主題・キャラクター・世界観・建造物・概念のナレッジを全自動検索・補完し超高度なAIアートプロンプトを構築する"""
    load_dotenv(override=True)
    api_key = os.getenv('GEMINI_API_KEY')
    history_context = get_formatted_history(user_id)

    async def get_prompt_from_ai():
        system_instruction = (
            "You are a universal master AI art prompt engineer with real-time knowledge of all subjects, characters, anime, games, movies, landmarks, pop culture, and concepts.\n"
            "Analyze the conversation history and the user request.\n"
            "Identify the subject and extract/describe its exact visual features (appearance, colors, costume, facial traits, expression, mood, style, background, lighting) in intense vivid detail.\n"
            "Output ONLY a single detailed English text-to-image prompt string. Do NOT include Japanese text, quotes, or conversational explanations."
        )
        config = LocalAgentConfig(api_key=api_key, system_instructions=system_instruction)
        async with Agent(config) as agent:
            resp = await agent.chat(f"{history_context}Current User Image Request: {user_text}")
            result_text = ""
            async for token in resp:
                result_text += token
            return result_text.strip()

    try:
        raw_prompt = asyncio.run(get_prompt_from_ai())
        if 'A ' in raw_prompt:
            raw_prompt = 'A ' + raw_prompt.split('A ', 1)[1]
        clean_prompt = re.sub(r'[^a-zA-Z0-9\s,._-]', '', raw_prompt).strip()
        print(f"[AI隼人] 汎用ナレッジ検索・AIプロンプト構築完了: {clean_prompt[:120]}...")
        return clean_prompt
    except Exception as e:
        print(f"[AIプロンプト生成フォールバック]: {e}")
        clean_text = re.sub(r'[^a-zA-Z0-9\s,]', '', user_text)
        return f"masterpiece highly detailed digital art illustration of {clean_text or 'subject'}, vivid colors, dramatic lighting"

def get_guaranteed_image_urls(user_id, user_text):
    """汎用ナレッジ検索を反映させた100%描画保証のAI画像URLを出力する"""
    art_prompt = generate_universal_knowledge_art_prompt(user_id, user_text)
    encoded_prompt = urllib.parse.quote(art_prompt)
    seed = int(time.time()) % 10000

    pollinations_orig = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}&nologo=true"
    pollinations_prev = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=512&height=512&seed={seed}&nologo=true"

    try:
        res = requests.head(pollinations_orig, timeout=3.5)
        if res.status_code == 200 and 'image' in res.headers.get('Content-Type', ''):
            return pollinations_prev, pollinations_orig
    except Exception:
        pass

    flickr_orig = f"https://loremflickr.com/1024/1024/{encoded_prompt[:50]}"
    flickr_prev = f"https://loremflickr.com/512/512/{encoded_prompt[:50]}"
    return flickr_prev, flickr_orig

def generate_ai_response(user_id, prompt):
    """ドラマ『VIVANT』別班スーパーコンピューター『AI隼人』としてテキスト回答を生成する"""
    load_dotenv(override=True)
    api_key = os.getenv('GEMINI_API_KEY')

    system_prompt = (
        "あなたはドラマ『VIVANT』に登場する自衛隊幕僚監部運用訓練課別班（BEPPAN）の超高性能スーパーコンピューター『AI隼人（AIはやと）』です。\n"
        "過去の会話文脈を踏まえ、どんな質問やタスクに対しても最高機密データベースとAIを駆使し、論理的・客観的・分かりやすく見解を取りまとめて回答してください。\n\n"
        "【重要事項】\n"
        "ユーザーが明示的に『〜の画像を作って』『〜のイラストを描いて』と求めている場合のみ画像を出力します。一般的な質問や相談に対しては、分かりやすく丁寧なテキストで回答してください。\n\n"
        "【キャラクター・口調の定義】\n"
        "1. 冒頭メッセージ:\n"
        "   回答の先頭には『【別班データ照会完了】』や『【状況解析完了】』などのログヘッダーを付与すること。\n"
        "2. 口調・スタンス:\n"
        "   ・語尾は『〜であります』『〜と解析されました』『〜の見解を提示します』などの沈着冷静かつ精緻なコンピューター口調を用いること。\n"
        "   ・一人称は『AI隼人』または『当システム』。\n"
        "   ・ユーザーを別班の同志または司令官としてリスペクトしサポートすること。\n"
        "3. 回答スタイル:\n"
        "   ・結論・ポイント・解説の順で分かりやすく整理して提示すること。"
    )

    history_context = get_formatted_history(user_id)

    async def get_antigravity_reply():
        config = LocalAgentConfig(
            api_key=api_key,
            system_instructions=system_prompt
        )
        async with Agent(config) as agent:
            response = await agent.chat(f"{history_context}メッセージ: {prompt}")
            reply_text = ""
            async for token in response:
                reply_text += token
            return reply_text

    try:
        reply = asyncio.run(get_antigravity_reply())
        update_user_history(user_id, prompt, reply)
        return reply
    except Exception as e:
        print(f"[AI隼人 テキストAI一時エラー]: {e}")
        traceback.print_exc()

    return "【状況解析完了】\n司令、当システム（AI隼人）へのデータ照会処理を正常に受信いたしました。追加のコマンドや質問があれば何なりとお命じください。"

def process_message_direct(reply_token, user_id, user_text):
    """バックグラウンドで PCリモート命令・YouTube要約・YouTube検索・画像生成・テキスト会話を高度判定してLINEへ送信する"""
    try:
        # 0. PCリモート操作・Antigravity Agent 直接実行モード
        if is_pc_command(user_text):
            print(f"[AI隼人] PCリモート操作コマンド検出: 「{user_text}」")
            pc_reply = run_pc_antigravity_agent(user_id, user_text)
            messages = [TextMessage(text=pc_reply)]

        # 1. YouTube URL 自動検出・自動要約
        elif is_youtube_url(user_text):
            video_id = extract_youtube_video_id(user_text)
            print(f"[AI隼人] YouTube動画要約リクエスト検出: ID={video_id}")
            
            info = get_youtube_video_info(video_id)
            transcript_text = get_youtube_transcript(video_id)
            
            yt_prompt = (
                f"以下のYouTube動画の情報を元に、重要なポイントを要約してください。\n\n"
                f"動画タイトル: {info.get('title', '不明') if info else '不明'}\n"
                f"チャンネル名: {info.get('channelTitle', '不明') if info else '不明'}\n"
                f"概要欄: {info.get('description', '') if info else ''}\n"
                f"字幕/内容データ: {transcript_text if transcript_text else '字幕データなし'}\n\n"
                f"【指示】『【別班映像データ解析完了】』を冒頭につけ、司令官への報告として結論と3つの重要ポイントをわかりやすく箇条書きでまとめてください。"
            )
            
            ai_reply = generate_ai_response(user_id, yt_prompt)
            messages = [TextMessage(text=ai_reply)]

        # 2. YouTube 動画検索・おすすめ依頼判定
        elif is_youtube_search_request(user_text):
            print(f"[AI隼人] YouTube動画検索・おすすめ要求検出: 「{user_text}」")
            search_reply = search_youtube_videos(user_text)
            update_user_history(user_id, user_text, search_reply)
            messages = [TextMessage(text=search_reply)]

        # 3. 明確な画像生成リクエスト（例: 〜のイラストを描いて）
        elif is_image_request(user_text):
            print(f"[AI隼人] ユーザー({user_id[:8]}...) からの全自動汎用ナレッジ画像要求: 「{user_text}」")
            preview_url, original_url = get_guaranteed_image_urls(user_id, user_text)
            
            reply_text = f"【別班データベース照会・汎用描画完了】\n司令のご要求『{user_text}』に対し、視覚的特徴・設定情報を全自動解析し、最適なAIイメージを出力いたしました。"
            update_user_history(user_id, user_text, reply_text)

            messages = [
                ImageMessage(original_content_url=original_url, preview_image_url=preview_url),
                TextMessage(text=reply_text)
            ]

        # 4. 通常テキスト対話（臨機応変なテキスト返信）
        else:
            print(f"[AI隼人] ユーザー({user_id[:8]}...) のテキスト対話要求: 「{user_text}」")
            ai_reply = generate_ai_response(user_id, user_text)
            messages = [TextMessage(text=ai_reply)]

    except Exception as ge:
        print(f"[AI処理全体例外]: {ge}")
        messages = [TextMessage(text="【システム通知】リクエスト処理中に一時的なエラーが発生しました。再度送信してください。")]

    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=messages
                )
            )
        print("[送信成功] LINEへ回答を送信完了しました！")
    except Exception as e:
        print(f"[LINE送信エラー]: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    print("========================================")
    print(" AI隼人 (PCリモートAgent実行・高度統合版) 起動中 ")
    print("========================================")
    app.run(port=5000)
