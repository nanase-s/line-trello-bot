import os
import json
import re
import logging
from collections import deque
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
import anthropic
import requests
from datetime import datetime

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
TRELLO_API_KEY            = os.environ["TRELLO_API_KEY"]
TRELLO_TOKEN              = os.environ["TRELLO_TOKEN"]
TRELLO_BOARD_ID           = os.environ.get("TRELLO_BOARD_ID", "eruSRtjT")
ANTHROPIC_API_KEY         = os.environ["ANTHROPIC_API_KEY"]

line_bot_api = MessagingApi(ApiClient(Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)))
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
claude       = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

message_history: dict = {}  # group_id -> deque(maxlen=10) of {"sender": str, "text": str}

PERSON_TO_LIST = {
    "長内":  "長内ToDo",
    "管":    "管ToDo",
    "板垣":  "板垣ToDo",
    "小田":  "小田 ToDo",
    "新野":  "新野 ToDo",
    "瀧澤":  "瀧澤ToDo",
    "原田":  "原田ToDo",
}

DISPLAY_NAME_MAP = {
    "長内":  ["長内", "おさない", "えりかちゃん", "erika", "Erika", "エリカ"],
    "管":    ["管", "かん", "すぐる"],
    "板垣":  ["板垣", "かっちゃん", "いたがき"],
    "小田":  ["小田", "おだ", "おださん", "浩貴"],
    "新野":  ["新野", "七瀬", "ななせ"],
    "瀧澤":  ["瀧澤", "滝澤", "たっきー", "たきざわ", "圭太"],
    "原田":  ["原田", "はらだ", "さや姉", "さやか", "さやねぇ"],
}

def infer_person_from_sender(sender: str):
    for person, aliases in DISPLAY_NAME_MAP.items():
        for alias in aliases:
            if alias in sender:
                return person
    return None

def get_board_lists():
    res = requests.get(
        f"https://api.trello.com/1/boards/{TRELLO_BOARD_ID}/lists",
        params={"key": TRELLO_API_KEY, "token": TRELLO_TOKEN, "fields": "id,name"},
    )
    return res.json()

def get_list_id_by_name(name: str):
    for lst in get_board_lists():
        if lst["name"] == name:
            return lst["id"]
    return None

def find_matching_cards(cards: list, keyword: str) -> list:
    if not cards:
        return []
    exact = [c for c in cards if keyword.lower() in c["name"].lower()]
    if exact:
        return exact
    card_list = "\n".join([f"{i+1}. {c['name']}" for i, c in enumerate(cards)])
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{
                "role": "user",
                "content":
f"""以下のTrelloカード一覧から、キーワード「{keyword}」に関連するカードの番号を答えてください。
関連するカードがなければ「なし」と返してください。番号のみカンマ区切りで返してください。

{card_list}"""
            }]
        )
        answer = resp.content[0].text.strip()
        logger.info(f"Card matching result for '{keyword}': {answer}")
        if "なし" in answer:
            return []
        indices = [int(x.strip()) - 1 for x in answer.split(",") if x.strip().isdigit()]
        return [cards[i] for i in indices if 0 <= i < len(cards)]
    except Exception as e:
        logger.error(f"find_matching_cards error: {e}")
        return []

def create_trello_card(person: str, task: str) -> bool:
    list_name = PERSON_TO_LIST.get(person)
    if not list_name:
        return False
    list_id = get_list_id_by_name(list_name)
    if not list_id:
        return False
    cards = requests.get(
        f"https://api.trello.com/1/lists/{list_id}/cards",
        params={"key": TRELLO_API_KEY, "token": TRELLO_TOKEN, "fields": "id,name"},
    ).json()
    if find_matching_cards(cards, task):
        logger.info(f"Duplicate skipped: [{list_name}] {task}")
        return False
    requests.post(
        "https://api.trello.com/1/cards",
        data={"key": TRELLO_API_KEY, "token": TRELLO_TOKEN, "idList": list_id, "name": task},
    )
    logger.info(f"Created card: [{list_name}] {task}")
    return True

def move_card_to_done(person: str, keyword: str) -> int:
    list_name = PERSON_TO_LIST.get(person)
    if not list_name:
        return 0
    list_id  = get_list_id_by_name(list_name)
    done_id  = get_list_id_by_name("DONE")
    if not list_id or not done_id:
        return 0
    cards = requests.get(
        f"https://api.trello.com/1/lists/{list_id}/cards",
        params={"key": TRELLO_API_KEY, "token": TRELLO_TOKEN},
    ).json()
    matched = find_matching_cards(cards, keyword)
    moved = 0
    for card in matched:
        if card["idList"] != done_id:
            requests.put(
                f"https://api.trello.com/1/cards/{card['id']}",
                data={"key": TRELLO_API_KEY, "token": TRELLO_TOKEN, "idList": done_id},
            )
            moved += 1
    return moved

def append_to_card_description(person: str, keyword: str, update: str) -> int:
    list_name = PERSON_TO_LIST.get(person)
    if not list_name:
        return 0
    list_id = get_list_id_by_name(list_name)
    if not list_id:
        return 0
    cards = requests.get(
        f"https://api.trello.com/1/lists/{list_id}/cards",
        params={"key": TRELLO_API_KEY, "token": TRELLO_TOKEN, "fields": "id,name,desc"},
    ).json()
    matched = find_matching_cards(cards, keyword)
    updated = 0
    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    for card in matched:
        current_desc = card.get("desc", "")
        new_desc = f"{current_desc}\n[{now}] {update}".strip()
        requests.put(
            f"https://api.trello.com/1/cards/{card['id']}",
            data={"key": TRELLO_API_KEY, "token": TRELLO_TOKEN, "desc": new_desc},
        )
        updated += 1
    return updated

def classify(message: str, sender: str, history=None) -> list:
    truncated = message[:600] + "…(省略)" if len(message) > 600 else message
    history_section = ""
    if history:
        recent = history[-5:]
        lines = "\n".join(f"{h['sender']}: {h['text']}" for h in recent)
        history_section = f"【直前の会話履歴（最新5件）】\n{lines}\n"
    prompt = f"""あなたは映画制作チームの情報管理アシスタントです。
以下のLINEメッセージを分析し、JSONの配列のみを返してください（説明文不要）。
タスクが複数あれば複数のオブジェクトを返してください。
【チームメンバー】長内、管、板垣、小田、新野、瀧澤、原田
【分類タイプ】
- TASK: タスクの依頼・割り当て（「〇〇さんに△△お願い」「<人名>」付きのタスクなど）
- DONE: タスク完了報告（「〇〇完了」「終わりました」など）
- UPDATE: 進捗報告（「確認中」「対応中」「連絡済み」「〜しました」など）
- IGNORE: 雑談・挨拶・質問・ボットへの命令など
【重要ルール】
- <人名> 表記があればその人をpersonにする
- UPDATE/DONEでpersonが不明な場合は発言者({sender})を担当者とする
- personは必ず長内/管/板垣/小田/新野/瀧澤/原田から選ぶ
- 全体がIGNOREなら [{{"type":"IGNORE"}}] を返す
{history_section}発言者: {sender}
メッセージ: {truncated}
JSONフォーマット（配列）:
[{{"type":"TASK|DONE|UPDATE|IGNORE","person":"担当者名","task":"タスク内容","keyword":"キーワード","update":"進捗内容"}}]"""
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip().strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            return [{"type": "IGNORE"}]
        raw = match.group(0)
        raw = re.sub(r',\s*}', '}', raw)
        raw = re.sub(r',\s*]', ']', raw)
        results = json.loads(raw)
        for r in results:
            if not r.get("person") and r.get("type") in ("UPDATE", "DONE", "TASK"):
                r["person"] = infer_person_from_sender(sender)
        return results
    except Exception as e:
        logger.error(f"classify error: {e}")
        return [{"type": "IGNORE"}]

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    msg    = event.message.text.strip()
    sender = ""
    try:
        if event.source.type == "group":
            profile = line_bot_api.get_group_member_profile(
                event.source.group_id, event.source.user_id
            )
        elif event.source.type == "room":
            profile = line_bot_api.get_room_member_profile(
                event.source.room_id, event.source.user_id
            )
        else:
            profile = line_bot_api.get_profile(event.source.user_id)
        sender = profile.display_name
    except Exception as e:
        logger.error(f"profile error: {e}")

    group_id = event.source.group_id if event.source.type == "group" else "direct"
    if group_id not in message_history:
        message_history[group_id] = deque(maxlen=10)
    message_history[group_id].append({"sender": sender, "text": msg})

    results = classify(msg, sender, history=list(message_history.get(group_id, [])))
    reply_texts = []

    for result in results:
        typ = result.get("type", "IGNORE")
        logger.info(f"Classified: {typ} | sender={sender} | {result}")

        if typ == "TASK":
            person = result.get("person")
            task   = result.get("task")
            if person and task:
                ok = create_trello_card(person, task)
                if ok:
                    reply_texts.append(f"✅ {person}さんのToDoに追加：{task}")
        elif typ == "DONE":
            person  = result.get("person")
            keyword = result.get("keyword")
            if person and keyword:
                moved = move_card_to_done(person, keyword)
                if moved > 0:
                    reply_texts.append(f"✅ {person}さんの「{keyword}」をDONEに移動しました")
        elif typ == "UPDATE":
            person  = result.get("person")
            keyword = result.get("keyword")
            update  = result.get("update")
            if person and keyword and update:
                updated = append_to_card_description(person, keyword, update)
                if updated > 0:
                    reply_texts.append(f"📝 {person}さんの「{keyword}」の説明を更新しました")

    if reply_texts:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="\n".join(reply_texts))],
            )
        )

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body      = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
