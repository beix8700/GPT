# coding: utf-8
"""Telegram bot powered by GPT responses and keyword triggers.

This script helps automate chatting in Telegram groups. On the first run it
collects configuration from the user, supports OTP and two-factor
authentication, and periodically sends casual prompts into a random group.
"""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any, Dict, List

from apscheduler.schedulers.background import BackgroundScheduler
from openai import OpenAI
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

# --------------------------------------------------------------------------------------
# File paths
# --------------------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = ROOT_DIR / "config.json"
KEYWORDS_FILE = ROOT_DIR / "keywords_default.json"
SESSION_NAME = "tg_session"


# --------------------------------------------------------------------------------------
# Configuration helpers
# --------------------------------------------------------------------------------------
def _prompt_for_config() -> Dict[str, Any]:
    """Collect configuration data interactively from the terminal."""
    print("\n🔧 [首次启动配置] 未检测到 config.json，开始配置 Telegram 登录信息：")
    print("请确保您已在 https://my.telegram.org 获取 API ID 和 API HASH\n")
    api_id = int(input("👉 请输入 API ID（纯数字）: "))
    api_hash = input("👉 请输入 API HASH（字母+数字）: ").strip()
    openai_key = input("👉 请输入 OpenAI API KEY（sk-开头）: ").strip()
    phone = input("👉 请输入 Telegram 手机号（含国家码，如 +8613812345678）: ").strip()

    config: Dict[str, Any] = {
        "api_id": api_id,
        "api_hash": api_hash,
        "openai_api_key": openai_key,
        "phone": phone,
        "persona": "技术宅",
        "interval_min": 30,
        "interval_max": 90,
    }
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n✅ 配置保存成功，下一步将登录 Telegram 获取验证码…\n")
    return config


def ensure_config() -> Dict[str, Any]:
    """Return configuration data, prompting the user on first run."""
    if not CONFIG_FILE.exists():
        return _prompt_for_config()

    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except json.JSONDecodeError as exc:
        raise RuntimeError("配置文件损坏，请删除 config.json 后重新运行。") from exc

    missing = {key for key in ("api_id", "api_hash", "openai_api_key", "phone") if key not in config}
    if missing:
        raise RuntimeError(f"配置文件缺少必要字段: {', '.join(sorted(missing))}")

    interval_min = int(config.get("interval_min", 30))
    interval_max = int(config.get("interval_max", 90))
    if interval_min <= 0 or interval_max <= 0 or interval_min > interval_max:
        interval_min, interval_max = 30, 90
        config["interval_min"], config["interval_max"] = interval_min, interval_max
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def ensure_keywords() -> List[str]:
    """Return the keyword list, generating defaults if needed."""
    default_keywords = ["加班", "代码", "吃饭", "火锅", "原神", "emo", "下雨", "涨工资"]
    if not KEYWORDS_FILE.exists():
        KEYWORDS_FILE.write_text(json.dumps(default_keywords, ensure_ascii=False, indent=2), encoding="utf-8")
        return default_keywords

    try:
        with KEYWORDS_FILE.open("r", encoding="utf-8") as handle:
            keywords = json.load(handle)
    except json.JSONDecodeError as exc:
        raise RuntimeError("关键词文件损坏，请删除 keywords_default.json 后重新运行。") from exc

    if not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords):
        raise RuntimeError("关键词文件格式错误，需为字符串列表。")
    return keywords


# --------------------------------------------------------------------------------------
# GPT persona and client initialisation
# --------------------------------------------------------------------------------------
config = ensure_config()
KEYWORDS = ensure_keywords()
openai_client = OpenAI(api_key=config["openai_api_key"])

persona_prompts: Dict[str, str] = {
    "技术宅": "你是一个26岁的程序员，搞技术的，爱摸鱼，喜欢在群里用轻松、幽默的语气聊天。"
             "用口语化的表达，比如：\"卷麻了\"，\"笑死我了\"，\"这谁顶得住啊\"，可以适当加些 emoji。"
             "请用中文回复群聊中提到的内容。",
    "社牛搞笑型": "你是个话痨社牛，爱插科打诨，语气夸张，用搞笑表情包风格发言，句子里可以有很多表情和网络语气词，制造热闹气氛。",
    "温柔女生": "你是一个温柔、善解人意的女生，喜欢关心别人，用柔和的语气回复，用 emoji 点缀，回复中带有情绪共鸣。",
}


def get_prompt_template(name: str) -> str:
    """Return the persona prompt text."""
    return persona_prompts.get(name, persona_prompts["技术宅"])


async def generate_reply(user_text: str) -> str:
    """Generate a chat response using OpenAI's Chat Completions API."""
    prompt = get_prompt_template(config.get("persona", "技术宅"))

    try:
        response = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text},
            ],
        )
    except Exception as exc:  # pragma: no cover - network errors cannot be tested reliably
        return f"[生成出错] {exc}"

    choice = response.choices[0]
    message = choice.message.content if choice.message and choice.message.content else ""
    return message.strip() or "[生成出错] 未获取到回复内容"


# --------------------------------------------------------------------------------------
# Telegram client setup and event handlers
# --------------------------------------------------------------------------------------
client = TelegramClient(SESSION_NAME, config["api_id"], config["api_hash"])
scheduler = BackgroundScheduler()


@client.on(events.NewMessage)
async def handle_message(event: events.NewMessage.Event) -> None:
    """Respond when incoming message contains any keyword."""
    try:
        text = event.raw_text or ""
        if text and any(keyword in text for keyword in KEYWORDS):
            print(f"🎯 命中关键词：{text}")
            reply = await generate_reply(text)
            await event.reply(reply)
            print(f"🤖 回复发送成功：{reply}")
    except Exception as exc:  # pragma: no cover - runtime safety net
        print(f"[错误] {exc}")


async def random_chat() -> None:
    """Send a random chat message in the first group dialog."""
    dialogs = await client.get_dialogs()
    target = next((dialog for dialog in dialogs if dialog.is_group), None)
    if target is None:
        return

    text_seed = random.choice([
        "今天真卷啊…加班到几点？",
        "兄弟们吃啥了，外卖太贵了…",
        "原神真好玩，但我打不过 boss",
        "emo 了，这天一到晚上就不想写代码…",
    ])
    reply = await generate_reply(text_seed)
    await client.send_message(target.id, reply)
    print(f"⏱️ 定时发言：{reply}")


async def _login() -> None:
    """Login to Telegram, handling OTP and 2FA as required."""
    if await client.is_user_authorized():
        return

    print("🔐 [登录步骤 1] 正在向 Telegram 发送验证码…")
    await client.send_code_request(config["phone"])
    print("📨 [登录步骤 2] 请查看你的 Telegram（或短信），输入 6 位验证码：")
    code = input("✏️ 请输入验证码 >>> ")

    try:
        await client.sign_in(config["phone"], code)
    except SessionPasswordNeededError:
        print("🔒 [登录步骤 3] 账号启用了两步验证，请输入你的 Telegram 密码：")
        password = input("🔑 请输入密码 >>> ")
        await client.sign_in(password=password)


async def main() -> None:
    """Program entry point."""
    await client.connect()
    await _login()
    print("✅ 登录成功！开始监听群聊消息…\n")

    interval = random.randint(config["interval_min"], config["interval_max"])
    scheduler.add_job(lambda: asyncio.ensure_future(random_chat()), "interval", minutes=interval)
    scheduler.start()

    try:
        await client.run_until_disconnected()
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
