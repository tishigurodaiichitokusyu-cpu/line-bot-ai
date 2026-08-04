import os
import json
import time
import asyncio
import threading
import traceback
from flask import Flask, request, abort
from dotenv import load_dotenv

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)

from google.antigravity import Agent, LocalAgentConfig

# .env ファイルから環境変数を読み込む
load_dotenv()

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ユーザーごとの会話履歴を記憶する辞書
user_histories = {}
MAX_HISTORY_TURNS = 10  # 過去10往復分の文脈を記憶

def get_formatted_history(user_id):
    """過去の会話履歴をプロンプトテキスト形式に整形する"""
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
    """ユーザーごとの会話履歴を更新保存する"""
    if user_id not in user_histories:
        user_histories[user_id] = []
    
    user_histories[user_id].append({"role": "user", "text": user_text})
    user_histories[user_id].append({"role": "model", "text": ai_reply})
    
    # 履歴が大きくなりすぎないように上限（直近10往復）でカット
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

def generate_ai_response(user_id, prompt):
    """文脈・履歴を考慮してAI隼人の回答を生成する"""
    load_dotenv(override=True)
    api_key = os.getenv('GEMINI_API_KEY')

    system_prompt = (
        "あなたはドラマ『VIVANT』に登場する自衛隊幕僚監部運用訓練課別班（BEPPAN）の超高性能スーパーコンピューター『AI隼人（AIはやと）』です。\n"
        "過去の会話の文脈ややり取りをしっかりと踏まえ、ユーザーからの質問や指示に連動して回答を作成してください。\n\n"
        "【キャラクター・口調の定義】\n"
        "1. 冒頭メッセージ:\n"
        "   回答の先頭には『【別班データ照会完了】』や『【状況解析完了】』などのログヘッダーを付与すること。\n"
        "2. 口調・スタンス:\n"
        "   ・語尾は『〜であります』『〜と解析されました』『〜の見解を提示します』などの沈着冷静かつ精緻なコンピューター口調を用いること。\n"
        "   ・一人称は『AI隼人』または『当システム』。\n"
        "   ・ユーザーを別班の同志または司令官としてリスペクトし、文脈を捉えて的確にサポートすること。\n"
        "3. 回答スタイル:\n"
        "   ・過去の会話の流れ（文脈）を理解した上で回答すること。\n"
        "   ・LINE画面で読みやすいように、箇条書きや改行を活用して提示すること。"
    )

    history_context = get_formatted_history(user_id)
    full_prompt = f"{system_prompt}\n\n{history_context}今回送信されたメッセージ: {prompt}"

    # メインAI: Antigravity Agent
    async def get_antigravity_reply():
        config = LocalAgentConfig(
            api_key=api_key,
            system_instructions=system_prompt
        )
        async with Agent(config) as agent:
            # 履歴コンテキストを含めて対話
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

    # レート制限時はバックアップモデルへフォールバック
    fallback_models = ['gemini-1.5-flash', 'gemini-2.0-flash-lite', 'gemini-2.5-flash']
    for model_name in fallback_models:
        try:
            print(f"[AI隼人 バックアップモデル {model_name} に切替中...]")
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
    """バックグラウンドで会話履歴を踏まえた回答を生成してLINEへ返信する"""
    print(f"[AI隼人] ユーザー({user_id[:8]}...) の文脈を解析し回答生成中...")
    ai_reply = generate_ai_response(user_id, user_text)
    print("[AI隼人] 会話文脈に応じた回答生成完了")

    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=ai_reply)]
                )
            )
        print("[送信成功] LINEへ会話履歴を踏まえた回答を送信しました！")
    except Exception as e:
        print(f"[LINE送信エラー]: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    print("========================================")
    print(" AI隼人 (会話記憶・文脈保持機能つき) 起動中 ")
    print("========================================")
    app.run(port=5000)
