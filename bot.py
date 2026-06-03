import os
import time
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from discord import app_commands

from google.genai import Client
from google.genai.types import Content, Part

from aiohttp import web
import aiohttp_cors

# =========================
#  環境変数
# =========================
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
PORT = int(os.environ.get("PORT", 10000))

GEMINI_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
    os.environ.get("GEMINI_API_KEY_4"),
]

# =========================
#  Bot 基本設定
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
#  設定保存
# =========================
support_channel_id = None
supporter_role_id = None

ticket_states = {}  # {channel_id: {"ai_enabled": bool, "assigned": bool}}
ticket_logs = {}    # {channel_id: ["User: ...", "AI: ..."]}

AI_STOP_WORD = "!human"


# =========================
#  Gemini フォールバック（google.genai）
# =========================


async def ai_reply_with_fallback(prompt: str) -> str:
    for index, key in enumerate(GEMINI_KEYS):
        if not key:
            print(f"[Gemini] APIキー {index+1} が設定されていません")
            continue

        print(f"[Gemini] APIキー {index+1} を使用します")

        try:
            client = Client(api_key=key)

            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[
                    Content(
                        role="user",
                        parts=[Part.from_text(prompt)]
                    )
                ]
            )

            print(f"[Gemini] APIキー {index+1} 成功")
            return response.text

        except Exception as e:
            print(f"[Gemini] APIキー {index+1} で例外発生:")
            print("----- ERROR START -----")
            print(e)
            print("----- ERROR END -----")

            err = str(e)

            if "429" in err or "rate" in err.lower():
                print(f"[Gemini] APIキー {index+1} がレート制限 → 次へ")
                time.sleep(0.5)
                continue

            print(f"[Gemini] APIキー {index+1} が通常エラー → 次へ")
            continue

    print("[Gemini] 全APIキー失敗 → フォールバック終了")
    return "現在AIが利用できません。後ほどもう一度お試しください。"



# =========================
#  AI 会話履歴付き応答
# =========================
async def ai_reply_with_history(channel_id: int, user_message: str) -> str:
    if channel_id not in ticket_logs:
        ticket_logs[channel_id] = []

    history_text = "\n".join(ticket_logs[channel_id])

    prompt = f"""
以下はこのチケットの会話履歴です。
これを踏まえて、最後のユーザー発言に返答してください。

--- 会話履歴 ---
{history_text}

--- 今回のユーザー発言 ---
User: {user_message}
"""

    reply = await ai_reply_with_fallback(prompt)

    ticket_logs[channel_id].append(f"User: {user_message}")
    ticket_logs[channel_id].append(f"AI: {reply}")

    return reply


# =========================
#  Webサーバー（Render用）
# =========================
async def handle_ping(request):
    JST = timezone(timedelta(hours=+9), "JST")
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S %Z")

    active_keys = len([k for k in GEMINI_KEYS if k])

    print(f"🌐 [Web Ping] {now} | 有効Geminiキー: {active_keys} | OK")

    return web.Response(text="Bot is running and ready for Gemini requests.")


def setup_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            allow_methods=["GET"],
            allow_headers=("X-Requested-With", "Content-Type"),
        )
    })

    for route in list(app.router.routes()):
        cors.add(route)

    return app


async def start_web_server():
    web_app = setup_web_server()
    runner = web.AppRunner(web_app)
    await runner.setup()

    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    print(f"Webサーバーをポート {PORT} で起動します (Render対応)...")

    try:
        await site.start()
    except Exception as e:
        print(f"Webサーバー起動失敗: {e}")

    await asyncio.Future()


# =========================
#  起動時
# =========================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        await bot.tree.sync()
        print("Slash commands synced")
    except Exception as e:
        print("Sync error:", e)


# =========================
#  /supportchannel
# =========================
@bot.tree.command(name="supportchannel", description="サポート通知を送るチャンネルを設定します")
@app_commands.checks.has_permissions(administrator=True)
async def supportchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    global support_channel_id
    support_channel_id = channel.id
    await interaction.response.send_message(
        f"サポートチャンネルを {channel.mention} に設定しました。",
        ephemeral=True
    )


# =========================
#  /supporterrole
# =========================
@bot.tree.command(name="supporterrole", description="サポート担当ロールを設定します")
@app_commands.checks.has_permissions(administrator=True)
async def supporterrole(interaction: discord.Interaction, role: discord.Role):
    global supporter_role_id
    supporter_role_id = role.id
    await interaction.response.send_message(
        f"サポートロールを {role.mention} に設定しました。",
        ephemeral=True
    )


# =========================
#  担当ボタン
# =========================
class AssignView(discord.ui.View):
    def __init__(self, ticket_channel_id: int):
        super().__init__(timeout=None)
        self.ticket_channel_id = ticket_channel_id

    @discord.ui.button(label="担当する", style=discord.ButtonStyle.green, custom_id="assign_button")
    async def assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        global supporter_role_id, support_channel_id

        guild = interaction.guild
        supporter_role = guild.get_role(supporter_role_id)

        if supporter_role not in interaction.user.roles:
            return await interaction.response.send_message(
                "あなたはサポートロールを持っていません。",
                ephemeral=True
            )

        state = ticket_states.get(self.ticket_channel_id, {"ai_enabled": True, "assigned": False})
        state["assigned"] = True
        state["ai_enabled"] = False
        ticket_states[self.ticket_channel_id] = state

        ticket_channel = guild.get_channel(self.ticket_channel_id)
        support_channel = guild.get_channel(support_channel_id)

        await ticket_channel.send(
            f"{ticket_channel.mention} は今後 {interaction.user.mention} が対応します。"
        )

        await support_channel.send(
            f"{ticket_channel.mention} は {interaction.user.mention} が担当します。"
        )

        button.disabled = True
        await interaction.response.edit_message(view=self)


# =========================
#  /addassign
# =========================
@bot.tree.command(name="addassign", description="現在のチケットに担当ボタンを追加します")
async def addassign(interaction: discord.Interaction):
    channel = interaction.channel

    if not channel.name.startswith("ticket-"):
        return await interaction.response.send_message(
            "このチャンネルは ticket- ではありません。",
            ephemeral=True
        )

    ticket_states[channel.id] = {"ai_enabled": True, "assigned": False}
    ticket_logs[channel.id] = []

    await interaction.response.send_message(
        "担当者を決めてください。",
        view=AssignView(channel.id)
    )


# =========================
#  チャンネル削除時 → 会話ログ削除
# =========================
@bot.event
async def on_guild_channel_delete(channel):
    if channel.id in ticket_logs:
        del ticket_logs[channel.id]
        print(f"[LOG] チケット {channel.name} の会話履歴を削除しました。")


# =========================
#  メッセージ監視（AI応答）
# =========================
@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    if message.author.bot:
        return

    channel = message.channel

    if not channel.name.startswith("ticket-"):
        return

    ticket_id = channel.id
    state = ticket_states.get(ticket_id, {"ai_enabled": True, "assigned": False})

    if message.content.strip() == AI_STOP_WORD:
        state["ai_enabled"] = False
        ticket_states[ticket_id] = state

        guild = message.guild
        support_channel = guild.get_channel(support_channel_id)
        supporter_role = guild.get_role(supporter_role_id)

        await support_channel.send(
            f"{supporter_role.mention}\n"
            f"{channel.mention} で対応をお願いします。"
        )
        return

    if state["ai_enabled"] and not state["assigned"]:
        reply = await ai_reply_with_history(ticket_id, message.content)
        await channel.send(reply)


# =========================
#  メイン（Bot + Webサーバー同時起動）
# =========================
async def main():
    if not DISCORD_TOKEN:
        print("DISCORD_TOKEN が設定されていません。")
        return

    web_task = asyncio.create_task(start_web_server())
    bot_task = asyncio.create_task(bot.start(DISCORD_TOKEN))

    await asyncio.gather(web_task, bot_task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot and Web Server stopped.")
    except Exception as e:
        print(f"メイン実行中にエラー: {e}")
