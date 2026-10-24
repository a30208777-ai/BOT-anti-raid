"""Bot anti-raid (Discord) + Supabase em um único serviço."""
import asyncio
import logging
import os
import re

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("antiraid")

MOTIVO = (
    "Você é uma pessoa que faz Raid, por conta disso você foi "
    "automaticamente banido deste servidor"
)
INTERVALO_SEGUNDOS = 60
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL", "https://bot-anti-raid-icvm.onrender.com/")
KEEPALIVE_SEGUNDOS = 15
CARGO_GERENTE = os.getenv("CARGO_GERENTE", "gerente").strip().lower()

RE_ID = re.compile(r"^\d{15,25}$")
RE_USERNAME = re.compile(r"^[a-z0-9_.]{2,32}$")

for var in ("DISCORD_TOKEN", "SUPABASE_URL", "SUPABASE_KEY"):
    if not os.getenv(var):
        raise SystemExit(f"Variável de ambiente ausente: {var}")

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

intents = discord.Intents.default()
intents.members = True  # ative "Server Members Intent" no Portal de Desenvolvedores


# ---------- Supabase (funções do banco) ----------
async def rpc(nome: str, params: dict):
    return await asyncio.to_thread(lambda: supabase.rpc(nome, params).execute().data)


async def esta_na_lista(guild_id: int, user_id: int, username: str) -> bool:
    try:
        r = await rpc(
            "usuario_esta_banido_no_servidor",
            {"p_guild_id": guild_id, "p_user_id": str(user_id), "p_username": username},
        )
        return r is True
    except Exception as e:
        log.error("[ERRO] Verificação de @%s (%s): %s", username, user_id, e)
        return False


async def banidos_do_servidor(guild_id: int) -> tuple[set[int], set[str]]:
    linhas = await rpc("banidos_do_servidor", {"p_guild_id": guild_id})
    ids: set[int] = set()
    nomes: set[str] = set()
    for linha in linhas or []:
        uid = (linha.get("user_id") or "").strip()
        if uid.isdigit():
            ids.add(int(uid))
        nome = (linha.get("username") or "").strip().lstrip("@").lower()
        if nome:
            nomes.add(nome)
    return ids, nomes


# ---------- bot ----------
class AntiRaid(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(add)
        await self.tree.sync()
        log.info("[COMANDOS] Comandos de barra sincronizados")

        # Mini servidor HTTP: o Render (Web Service) exige uma porta aberta
        async def saude(_request):
            return web.Response(text="Bot online" if self.is_ready() else "Bot iniciando")

        app = web.Application()
        app.router.add_get("/", saude)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        porta = int(os.getenv("PORT", "3000"))
        await web.TCPSite(runner, "0.0.0.0", porta).start()
        log.info("[HTTP] Servidor de saúde na porta %s", porta)


bot = AntiRaid()


async def banir(guild: discord.Guild, user_id: int, origem: str):
    if (bot.user and user_id == bot.user.id) or user_id == guild.owner_id:
        return
    try:
        await guild.ban(discord.Object(id=user_id), reason=MOTIVO)
        log.info('[BAN] %s banido em "%s" (%s)', user_id, guild.name, origem)
    except Exception as e:
        log.error('[ERRO] Falha ao banir %s em "%s": %s', user_id, guild.name, e)


# ---------- comando /add auto ban:@nome ----------
add = app_commands.Group(
    name="add", description="Adicionar à lista do servidor", guild_only=True
)


def pode_usar(membro: discord.Member) -> bool:
    """Dono do servidor ou quem tem o cargo de gerente."""
    if membro.id == membro.guild.owner_id:
        return True
    return any(cargo.name.strip().lower() == CARGO_GERENTE for cargo in membro.roles)


@add.command(name="auto", description="Adiciona uma pessoa ao auto ban deste servidor")
@app_commands.describe(ban="@ da pessoa (ex: @fulano) ou o ID do Discord")
async def add_auto(interaction: discord.Interaction, ban: str):
    if not pode_usar(interaction.user):
        await interaction.response.send_message(
            "Só o dono do servidor ou quem tem o cargo **Gerente** pode usar este comando.",
            ephemeral=True,
        )
        return

    alvo = ban.strip().lstrip("@").lower()
    if RE_ID.match(alvo):
        user_id, username = alvo, None
    elif RE_USERNAME.match(alvo):
        user_id, username = None, alvo
    else:
        await interaction.response.send_message(
            "Alvo inválido. Use o @ (ex: `@fulano`) ou o ID do Discord.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        resultado = await rpc(
            "registrar_auto_ban",
            {
                "p_guild_id": interaction.guild_id,
                "p_user_id": user_id,
                "p_username": username,
                "p_motivo": "Adicionado via /add auto ban",
                "p_por": str(interaction.user.id),
            },
        )
    except Exception as e:
        log.error("[ERRO] /add auto ban (%s): %s", alvo, e)
        await interaction.followup.send(
            "Não consegui salvar agora. Tente de novo em instantes.", ephemeral=True
        )
        return

    log.info(
        '[ADD] %s adicionou "%s" em "%s" (%s)',
        interaction.user, alvo, interaction.guild.name, resultado,
    )
    mensagens = {
        "tabela_criada": f"Primeira vez neste servidor: criei a tabela e adicionei **@{alvo}**.",
        "inserido": f"**@{alvo}** foi adicionado à lista de auto ban.",
        "ja_existia": f"**@{alvo}** já estava na lista.",
    }
    await interaction.followup.send(mensagens.get(resultado, "Feito."), ephemeral=True)

    # bane na hora se a pessoa já estiver no servidor
    guild = interaction.guild
    if user_id:
        if guild.get_member(int(user_id)):
            await banir(guild, int(user_id), "comando")
    else:
        membro = discord.utils.find(lambda m: m.name.lower() == username, guild.members)
        if membro:
            await banir(guild, membro.id, "comando")


# ---------- eventos ----------
@bot.event
async def on_member_join(member: discord.Member):
    log.info('[ENTROU] @%s (%s) em "%s"', member.name, member.id, member.guild.name)
    if await esta_na_lista(member.guild.id, member.id, member.name):
        try:
            await member.send(MOTIVO)  # a DM pode estar fechada
        except Exception:
            pass
        await banir(member.guild, member.id, "entrada")


@tasks.loop(seconds=INTERVALO_SEGUNDOS)
async def varredura():
    """Bane quem já está no servidor e foi adicionado à lista depois."""
    for guild in bot.guilds:
        try:
            ids, nomes = await banidos_do_servidor(guild.id)
        except Exception as e:
            log.error('[ERRO] Varredura em "%s": %s', guild.name, e)
            continue
        if not ids and not nomes:
            continue
        for membro in guild.members:
            if membro.id in ids or membro.name.lower() in nomes:
                await banir(guild, membro.id, "varredura")


@tasks.loop(seconds=KEEPALIVE_SEGUNDOS)
async def keep_alive():
    """Acessa a própria URL a cada 15s (só registra no log se falhar)."""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sessao:
            async with sessao.get(KEEPALIVE_URL) as resp:
                await resp.read()
                if resp.status >= 400:
                    log.warning("[KEEPALIVE] Resposta %s de %s", resp.status, KEEPALIVE_URL)
    except Exception as e:
        log.warning("[KEEPALIVE] Falha ao acessar %s: %s", KEEPALIVE_URL, e)


@bot.event
async def on_ready():
    log.info("[ONLINE] Conectado como %s em %s servidor(es)", bot.user, len(bot.guilds))
    if not varredura.is_running():
        varredura.start()
    if not keep_alive.is_running():
        keep_alive.start()


bot.run(os.environ["DISCORD_TOKEN"], log_handler=None)
