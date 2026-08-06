import os
import sys
import time
import asyncio
import requests
import re
from io import StringIO
from dotenv import load_dotenv

from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    TextMessage
)
from google import genai

# 環境変数のロード
load_dotenv()

# 設定値の取得
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
# ポーリング対象サーバー (環境変数かデフォルトでローカルを指定)
POLLING_URL = os.getenv('POLLING_URL', 'http://127.0.0.1:5000')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

def execute_local_code_task(command, history_context):
    """指示を元にGeminiでPythonコードを1回だけ生成し、ローカルPC上で実行する（シングルターン方式）"""
    print(f"[AI] コード生成開始: 「{command}」")
    
    # Clientの初期化
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
あなたはユーザーのWindows PC上で動作するPythonコードを生成するエキスパートAI（AI隼人）です。
ユーザーからのLINEによる指示を元に、タスクを実行するための完全なPythonコードを生成してください。

【ユーザーからの指示】
{command}

【会話履歴文脈】
{history_context}

【厳守事項】
1. 返答は「実行可能なPythonコードのみ」とし、余分な解説文や挨拶などは一切含めないでください。
2. コードは必ず markdown の ```python ... ``` ブロックで囲んで出力してください。
3. 実行結果（ユーザーに報告したいテキスト）は、必ず `print()` 関数を用いて標準出力に出力するようにコードを作成してください。
4. ファイルの操作（読み書き、作成、リストアップなど）、コマンドの実行、requestsを用いたAPI連携などは制限なく自由に行って構いません。
5. エラーが発生した場合は、エラーメッセージを分かりやすく `print()` して終了するように、適切に try-except で囲んでください。
6. `os`, `sys`, `subprocess`, `requests`, `gspread` など必要なライブラリは、コード内で必ず `import` してください。
"""

    try:
        # 1回だけのコンテンツ生成 (無料枠で安定動作する gemini-3.5-flash を使用)
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=prompt
        )
        raw_text = response.text
    except Exception as e:
        print(f"[Gemini APIエラー]: {e}")
        return f"【別班PCリモートコマンド実行エラー】\n司令、Gemini APIによるコード生成中にエラーが発生いたしました。\n詳細: {e}"

    print(f"[AI] コード生成完了。パース中...")
    
    # markdownのコードブロックからPythonコードを抽出
    code_match = re.search(r'```python\s*(.*?)\s*```', raw_text, re.DOTALL)
    if not code_match:
        # コードブロックが見つからない場合はテキスト全体をコードとする
        code = raw_text.strip()
    else:
        code = code_match.group(1).strip()
        
    print("\n----- 実行コード -----")
    print(code)
    print("----------------------\n")
    
    # 標準出力をキャプチャしてコードを実行する
    old_stdout = sys.stdout
    redirected_output = sys.stdout = StringIO()
    
    loc = {}
    exec_error = None
    
    try:
        # グローバル名前空間とローカル名前空間を渡して実行
        exec(code, globals(), loc)
    except Exception as e:
        exec_error = e
    finally:
        # 標準出力を元に戻す
        sys.stdout = old_stdout
        
    exec_output = redirected_output.getvalue()
    
    if exec_error:
        print(f"[実行エラー]: {exec_error}")
        return (
            f"【別班PCリモートコマンド実行エラー】\n"
            f"司令、指示コードのローカル実行中に例外エラーが発生いたしました。\n\n"
            f"■ 例外内容:\n{exec_error}\n\n"
            f"■ 実行中ログ:\n{exec_output}"
        )
        
    header = "【別班PCリモートコマンド実行完了】\n司令、ご要求の処理が完了いたしました。\n\n[実行結果]\n"
    if not exec_output.strip():
        exec_output = "(出力はありませんでした。処理は正常に終了しました)"
        
    return f"{header}{exec_output}"

def send_line_push_message(user_id, text):
    """LINEのPush APIを使って非同期で結果を送信する"""
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            push_message_request = PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=text)]
            )
            line_bot_api.push_message(push_message_request)
        print(f"[LINE] 送信完了: {user_id[:8]}...")
    except Exception as e:
        print(f"[LINE] プッシュ送信エラー: {e}")

def run_worker():
    print("========================================")
    print(" AI隼人 ローカルPCワーカー（シングルターン版） ")
    print(f" 接続先: {POLLING_URL}")
    print("========================================")
    
    while True:
        try:
            # タスクの取得をリクエスト
            response = requests.get(f"{POLLING_URL}/api/get_task", timeout=5)
            if response.status_code == 200:
                data = response.json()
                if 'task_id' in data:
                    task_id = data['task_id']
                    user_id = data['user_id']
                    command = data['command']
                    history_context = data.get('history_context', '')
                    
                    print(f"\n[タスク受信] ID={task_id}, Command=「{command}」")
                    
                    # 1回だけのコード生成＆実行方式で処理
                    result = execute_local_code_task(command, history_context)
                    
                    # LINEに結果をプッシュ送信
                    send_line_push_message(user_id, result)
                    
                    # 完了報告
                    report_resp = requests.post(
                        f"{POLLING_URL}/api/complete_task",
                        json={'task_id': task_id},
                        timeout=5
                    )
                    print(f"[ステータス] 完了報告送信: {report_resp.status_code}")
            else:
                print(f"[警告] サーバー接続エラー: HTTP {response.status_code}")
                
        except requests.exceptions.ConnectionError:
            # サーバーが起動していないか接続できない場合は静かに待つ
            pass
        except Exception as e:
            print(f"[エラー]: {e}")
            
        time.sleep(3)

if __name__ == "__main__":
    run_worker()
