import os
import json
import time
import asyncio
import threading
import urllib.parse
import traceback
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

# .env ファイルから環境変数を読み込む
load_dotenv()

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')

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

def is_image_request(text):
    """ユーザーのメッセージが画像生成リクエストか判定する"""
    text_lower = text.lower()
    keywords = [
        "画像", "イラスト", "描いて", "作って", "写真", "イメージ", "画", "絵", "描画",
        "生成", "デザイン", "アイコン", "見せて", "書いて", "作画", "フォト", "壁紙",
        "image", "draw", "picture", "photo", "generate", "illustration"
    ]
    return any(k in text_lower for k in keywords)

def translate_to_english_prompt(text):
    """Geminiを使って画像生成用に最適化された英語プロンプトに変換する"""
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return f"masterpiece, highly detailed, {text}"

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        res = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=f"以下の日本語を、AI画像生成ツール用の詳細な英語プロンプトに変換してください。解説や前置きは一切不要で、英語のプロンプトのみを出力してください。\n\n日本語: {text}"
        )
        clean_prompt = res.text.strip().replace("\n", ", ")
        return clean_prompt
    except Exception:
        return f"masterpiece, highly detailed, {text}"

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
    full_prompt = f"{system_prompt}\n\n{history_context}今回送信されたメッセージ: {prompt}"

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
        print(f"[AI隼人 メインAI一時エラー/レート制限]: {e}")

    fallback_models = ['gemini-1.5-flash', 'gemini-2.0-flash-lite', 'gemini-2.5-flash']
    for model_name in fallback_models:
        try:
            time.sleep(1.5)
            from google import genai
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=model_name,
                contents=full_prompt
            )
            reply = response.text
            update_user_history(user_id, prompt, reply)
            return reply
        except Exception as fe:
            print(f"[モデル {model_name} エラー]: {fe}")
            continue

    return "【状況報告】現在、別班データベースへのアクセスが一時的に集中しております。15秒ほど空けて再度コマンドを送信してください。"

def process_message_direct(reply_token, user_id, user_text):
    """バックグラウンドで画像生成またはAI思考回答を行いLINEへ確実返信する"""
    if is_image_request(user_text):
        print(f"[AI隼人] ユーザー({user_id[:8]}...) からの画像生成要求を検出: 「{user_text}」")
        try:
            english_prompt = translate_to_english_prompt(user_text)
            encoded_prompt = urllib.parse.quote(english_prompt)
            seed = int(time.time()) % 10000

            original_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}&nologo=true"
            preview_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=512&height=512&seed={seed}&nologo=true"
            
            reply_text = f"【別班画像解析・生成完了】\n司令のご要求『{user_text}』に基づき、高精度イメージを描画・出力いたしました。"
            update_user_history(user_id, user_text, reply_text)

            messages = [
                ImageMessage(original_content_url=original_url, preview_image_url=preview_url),
                TextMessage(text=reply_text)
            ]
        except Exception as ie:
            print(f"[画像生成処理エラー]: {ie}")
            messages = [TextMessage(text="【画像生成エラー】画像の描画処理中に問題が発生しました。再度送信してください。")]
    else:
        print(f"[AI隼人] ユーザー({user_id[:8]}...) の文脈を解析しテキスト回答生成中...")
        ai_reply = generate_ai_response(user_id, user_text)
        messages = [TextMessage(text=ai_reply)]

    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=messages
                )
            )
        print("[送信成功] LINEへ回答（または画像メッセージ）を送信完了しました！")
    except Exception as e:
        print(f"[LINE送信エラー]: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    print("========================================")
    print(" AI隼人 (非ブロック型・高信頼性画像対応) 起動中 ")
    print("========================================")
    app.run(port=5000)
