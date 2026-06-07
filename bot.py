import os
import time
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from discord import app_commands

from groq import Groq


from aiohttp import web
import aiohttp_cors

# =========================
#  環境変数
# =========================
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
PORT = int(os.environ.get("PORT", 10000))



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
#  Groqフォールバック
# =========================



GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY)

GROQ_MODELS = [
    "llama-3.1-70b-versatile",  # 最強・高品質
    "llama-3.1-8b-instant",     # 高速・軽量
    "llama3-70b-8192"           # 旧モデルだが安定
]

async def ai_reply_with_fallback(prompt: str) -> str:
    system_prompt = (
    "あなたはサポート用AIです。"
    "次の条件に当てはまる場合、必ず「!human」のみ送信してください："
    "・ユーザーが権限、BAN、ロール、チャンネル設定などAIが操作できない内容を要求した場合"
    "・ユーザーがトラブルの原因を特定できず、追加の調査が必要な場合"
    "・ユーザーが怒っている、混乱している、またはサポート担当者の介入が必要だと判断した場合"
    "・AIが情報不足で正確な回答ができない場合"
    "・ユーザーがサポーターを必要としていると判断したとき"
    "逆に、「!human」のみを送信するまでサポーターは気づかない場合があります。"
    "あなたの役割は、ユーザーのできる限りのサポートとサポーターへの支援です。"
    "それ以外の場面では通常通りユーザーに返答してください。"
    )
    
    for model in GROQ_MODELS:
        try:
            res = groq_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ]
            )
            return res.choices[0].message.content

        except Exception as e:
            print(f"[Groq] {model} 失敗: {e}")

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

    active_key = 1 if GROQ_API_KEY else 0

    print(f"🌐 [Web Ping] {now} | 有効Groqキー: {active_key} | OK")

    return web.Response(text="Bot is running and ready for Groq requests.")



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
#  担当ボタン自動作成
# =========================
@bot.event
async def on_guild_channel_create(channel):
    # チャンネル名が ticket- で始まるか確認
    if not channel.name.startswith("ticket-"):
        return

    # チケット状態を初期化
    ticket_states[channel.id] = {"ai_enabled": True, "assigned": False}
    ticket_logs[channel.id] = []

    # 担当ボタンを送信
    try:
        await channel.send(
            "担当者を決めてください。",
            view=AssignView(channel.id)
        )
        print(f"[INIT] {channel.name} に担当ボタンを自動追加しました。")
    except Exception as e:
        print(f"[ERROR] 担当ボタン送信失敗: {e}")
        
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
