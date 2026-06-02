import os
import time
import discord
from discord.ext import commands
from discord import app_commands
import google.generativeai as genai

# =========================
#  4つの Gemini API キー
# =========================
GEMINI_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
    os.environ.get("GEMINI_API_KEY_4"),
]

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

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

# ticket_states[channel_id] = {"ai_enabled": bool, "assigned": bool}
ticket_states = {}

AI_STOP_WORD = "!human"


# =========================
#  Gemini フォールバック関数
# =========================
async def ai_reply_with_fallback(prompt: str) -> str:
    for index, key in enumerate(GEMINI_KEYS):
        if key is None:
            continue

        try:
            genai.configure(api_key=key)
            model = genai.GenerativeModel("gemini-pro")
            res = model.generate_content(prompt)

            if hasattr(res, "text"):
                return res.text

            return "すみません、返答を生成できませんでした。"

        except Exception as e:
            err = str(e)

            if "429" in err or "rate" in err.lower():
                print(f"[Gemini] APIキー {index+1} がレート制限 → 次へ")
                time.sleep(0.5)
                continue

            print(f"[Gemini] APIキー {index+1} でエラー: {err}")
            continue

    return "現在AIが利用できません。後ほどもう一度お試しください。"


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

        # 状態更新
        state = ticket_states.get(self.ticket_channel_id, {"ai_enabled": True, "assigned": False})
        state["assigned"] = True
        state["ai_enabled"] = False
        ticket_states[self.ticket_channel_id] = state

        ticket_channel = guild.get_channel(self.ticket_channel_id)
        support_channel = guild.get_channel(support_channel_id)

        # チケット内通知
        await ticket_channel.send(
            f"{ticket_channel.mention} は今後 {interaction.user.mention} が対応します。"
        )

        # サポートチャンネル通知
        await support_channel.send(
            f"{ticket_channel.mention} は {interaction.user.mention} が担当します。"
        )

        # ボタン無効化
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

    await interaction.response.send_message(
        "担当者を決めてください。",
        view=AssignView(channel.id)
    )


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

    # 停止ワード
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

    # AI応答
    if state["ai_enabled"] and not state["assigned"]:
        reply = await ai_reply_with_fallback(message.content)
        await channel.send(reply)


# =========================
#  Bot 起動
# =========================
bot.run(DISCORD_TOKEN)
