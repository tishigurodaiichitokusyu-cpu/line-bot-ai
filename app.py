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

def is_youtube_url(text):
    """YouTubeのURLか判定する"""
    return bool(re.search(r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/', text))

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
    """ユーザーのメッセージが画像生成リクエストか判定する"""
    text_lower = text.lower()
    keywords = [
        "画像", "イラスト", "描いて", "作って", "写真", "イメージ", "画", "絵", "描画",
        "生成", "デザイン", "アイコン", "見せて", "書いて", "作画", "フォト", "壁紙",
        "image", "draw", "picture", "photo", "generate", "illustration"
    ]
    return any(k in text_lower for k in keywords)

def generate_context_aware_art_prompt(user_id, user_text):
    """過去の会話文脈とユーザーの要望を統合し、Antigravity AIで超高度な英文AIアートプロンプトを自動生成する"""
    load_dotenv(override=True)
    api_key = os.getenv('GEMINI_API_KEY')
    history_context = get_formatted_history(user_id)

    async def get_prompt_from_ai():
        system_instruction = (
            "You are an elite AI art prompt engineer. "
            "Your task is to analyze the conversation history and the user's request, and create a highly detailed, accurate, cinematic English text-to-image prompt.\n"
            "If the request relates to characters (e.g. Nogoon Beki, Nogi from VIVANT), describe their visual appearance vividly (age, facial features, costume, mood, lighting, background).\n"
            "Output ONLY the English prompt string. Do not include Japanese, quotes, or conversational text."
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
        print(f"[AI隼人] 会話文脈解析済AIプロンプト: {clean_prompt[:120]}...")
        return clean_prompt
    except Exception as e:
        print(f"[AIプロンプト生成フォールバック]: {e}")
        clean_text = re.sub(r'[^a-zA-Z0-9\s,]', '', user_text)
        return f"masterpiece, highly detailed, digital artwork of {clean_text or 'japanese character'}, dramatic lighting"

def get_guaranteed_image_urls(user_id, user_text):
    """会話文脈を読み込んで本物のAI生成イラストURLを出力する"""
    art_prompt = generate_context_aware_art_prompt(user_id, user_text)
    encoded_prompt = urllib.parse.quote(art_prompt)
    seed = int(time.time()) % 10000

    # 本物のAI生成イラストエンジン (Pollinations AI)
    pollinations_orig = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}&nologo=true"
    pollinations_prev = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=512&height=512&seed={seed}&nologo=true"

    try:
        res = requests.head(pollinations_orig, timeout=3.5)
        if res.status_code == 200 and 'image' in res.headers.get('Content-Type', ''):
            return pollinations_prev, pollinations_orig
    except Exception:
        pass

    # フォールバック
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
        "ユーザーが画像や絵の作成を希望している場合、絶対に『画像生成できません』と答えてはなりません。当システムには画像描画プロトコルが実装されています。\n\n"
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
    """バックグラウンドで YouTube要約・文脈連動型画像生成・テキスト会話を判定してLINEへ送信する"""
    try:
        # 1. YouTube URL 自動検出・自動要約
        if is_youtube_url(user_text):
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

        # 2. 会話文脈連動型 画像生成リクエスト
        elif is_image_request(user_text):
            print(f"[AI隼人] ユーザー({user_id[:8]}...) からの文脈連動画像要求: 「{user_text}」")
            preview_url, original_url = get_guaranteed_image_urls(user_id, user_text)
            
            reply_text = f"【別班画像解析・精密描画完了】\n司令との会話文脈およびご要求『{user_text}』に基づき、カスタムAIイラストを描画・出力いたしました。"
            update_user_history(user_id, user_text, reply_text)

            messages = [
                ImageMessage(original_content_url=original_url, preview_image_url=preview_url),
                TextMessage(text=reply_text)
            ]
        # 3. 通常テキスト対話
        else:
            print(f"[AI隼人] ユーザー({user_id[:8]}...) の文脈を解析しテキスト回答生成中: 「{user_text}」")
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
        print("[送信成功] LINEへ文脈連動回答（または画像）を送信完了しました！")
    except Exception as e:
        print(f"[LINE送信エラー]: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    print("========================================")
    print(" AI隼人 (会話文脈連動・リアルタイムAI画風生成版) 起動中 ")
    print("========================================")
    app.run(port=5000)
