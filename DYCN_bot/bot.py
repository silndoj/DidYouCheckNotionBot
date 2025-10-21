import os
import json
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from rapidfuzz import fuzz

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
JSON_PATH = os.getenv("JSON_PATH", "notion_data.json")

# Flask setup
app = Flask(__name__)

# Load the Notion data once on startup
with open(JSON_PATH, "r", encoding="utf-8") as f:
    notion_data = json.load(f)

def verify_token():
    if request.path != "/respond":
        return  # only check /respond endpoint
    token = request.headers.get("X-Auth-Token")
    if token != os.getenv("BOT_SECRET"):
        print("[SECURITY] Invalid or missing token.")
        return jsonify({"error": "Unauthorized"}), 403

def weighted_similarity(user_msg, item):
    """
    Weighted fuzzy similarity:
    """
    msg = user_msg.lower()

    # Topic similarity (most important)
    topic_score = fuzz.partial_ratio(msg, item["topic"].lower()) * 6

    # Keyword similarity (take best keyword match)
    keyword_score = (
        max(fuzz.partial_ratio(msg, kw.lower()) for kw in item["keywords"])
        * 4 if item.get("keywords") else 0
    )

    # Summary similarity (least important)
    summary_score = fuzz.partial_ratio(msg, item["summary"].lower()) * 2

    total_score = topic_score + keyword_score + summary_score
    return total_score


def classify_message(user_message):
    scored = []
    for item in notion_data:
        score = weighted_similarity(user_message, item)
        scored.append((score, item))

    top_candidates = sorted(scored, key=lambda x: x[0], reverse=True)
    print("\n[DEBUG] Top 5 candidate topics for this message:")
    for rank, (score, item) in enumerate(top_candidates[:5], 1):
        print(f"  {rank}. {item['topic']} — weighted score {score:.2f}")
    print("-" * 60)

    best_score = top_candidates[0][0]
# it was 700 mane
    if best_score < 700:
        print("[LOCAL MATCH] Low confidence — skipping AI call.")
        return "Scholarship"

    print(f"[LOCAL MATCH] Potential match ({best_score:.2f}) — sending to AI.")
    selected = top_candidates[:4]  # top 3 candidates sent to AI

    slim_candidates = [
        {
            "topic": c[1]["topic"],
            "summary": c[1]["summary"],
            "keywords": c[1]["keywords"],
        }
        for c in selected
    ]

    prompt = f"""
You are a topic classifier for 42 Heilbronn's internal Notion wiki.

Below are several candidate topics with summaries and keywords.
You must decide which topic best matches the user message.

### Your Rules:
1. Respond ONLY with the **exact topic name** (as written below) if it matches clearly.
2. If the user's message is NOT about any of these topics, respond with **none**.
3. Do NOT explain your reasoning or add extra text.
4. The output must be **exactly one word or phrase** — the topic name or 'none'.

### Candidates:
{json.dumps([c['topic'] for c in slim_candidates], indent=2)}

### Full Candidate Details:
{json.dumps(slim_candidates, indent=2)}

### User message:
"{user_message}"

Now respond ONLY with the best matching topic name from the list above, or 'none' if no match is clear.
"""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    body = {
        "model": "mistralai/mistral-7b-instruct",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 10,
        "temperature": 0.0,
    }

    try:
        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=body,
            timeout=30,
        )

        if res.status_code != 200:
            print(f"[ERROR] OpenRouter returned {res.status_code}: {res.text[:200]}")
            return "none"

        data = res.json()
        if "choices" not in data or not data["choices"]:
            print("[ERROR] No choices returned from API.")
            print(f"[DEBUG] Raw response: {data}")
            return "none"

        msg = data["choices"][0].get("message", {}).get("content", "").strip()
        if not msg:
            print("[ERROR] No message content found in API response.")
            print(f"[DEBUG] Raw response: {data}")
            return "none"

        # 🧹 Clean + enforce single-word return
        choice = msg.split("\n")[0].strip().strip('"').replace("**", "")
        print(f"[AI MATCH] → {choice}")
        return choice

    except requests.exceptions.RequestException as e:
        print(f"[NETWORK ERROR] {e}")
        return "none"
    except Exception as e:
        print(f"[UNEXPECTED ERROR] {e}")
        return "none"


@app.route("/respond", methods=["POST"])
def respond():
    """
    n8n will POST here with {"message": "user text from Slack"}
    We'll classify it, grab the right Notion entry, and return a smart reply + link.
    """
    data = request.get_json()
    user_message = data.get("message", "").strip()
    user_id = data.get("user", "unknown")

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    topic = classify_message(user_message)

    if topic.lower() == "none":
        return jsonify({
            "reply": "none",
            "link": None,
            "user": user_id
        })

    entry = next((item for item in notion_data if item["topic"].lower() == topic.lower()), None)

    if not entry:
        return jsonify({
            "reply": "none",
            "link": None,
            "user": user_id
        })

    reply = f"{entry['reply']}\n👉 {entry['link']}"
    return jsonify({"reply": reply, "topic": topic, "user": user_id})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
