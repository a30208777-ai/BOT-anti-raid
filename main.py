"""Bot anti-raid: confere a tabela "user-bang" do Supabase e bane automaticamente."""
import asyncio
import logging
import os

import discord
from aiohttp import web
from discord.ext import tasks
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("antiraid")

TABELA = "user-bang"
MOTIVO = (
    "Você é uma pessoa que faz Raid, por conta disso você foi "
    "automaticamente banido deste servidor"
)
INTERVALO_SEGUNDOS = 60

for var in ("DISCORD_TOKEN", "SUPABASE_URL", "SUPABASE_KEY"):
    if not os.getenv(var):
        raise SystemExit(f"Variável de ambiente ausente: {var}")

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

intents = discord.Intents.default()
intents.members = True  # ative "Server Members Intent" no Portal de Desenvolvedores


class AntiRaid(discord.Client):
    async def setup_hook(self):
        # Mini servidor HTTP: o Render (Web Service) exige uma porta aberta
        async def saude(_request):
            return web.Response(text="Bot online" if self.is_ready() else "Bot iniciando")

        app = web.Application()
        app.router.add_get("/", saude)
        runner = web.AppRunner(app)
        await runner.setup()
        porta = int(os.getenv("PORT", "3000"))
        await web.TCPSite(runner, "0.0.0.0", porta).start()
        log.info("[HTTP] Servidor de saúde na porta %s", porta)


bot = AntiRaid(intents=intents)


def _consulta_esta_na_lista(user_id: int) -> bool:
    resp = supabase.rpc("usuario_esta_banido", {"p_user_id": str(user_id)}).execute()
    return resp.data is True


async def esta_na_lista(user_id: int) -> bool:
    try:
        return await asyncio.to_thread(_consulta_esta_na_lista, user_id)
    except Exception as e:
        log.error("[ERRO] Supabase (%s): %s", user_id, e)
        return False


async def banir(guild: discord.Guild, user_id: int, origem: str):
    if bot.user and user_id == bot.user.id:
        return
    try:
        await guild.ban(discord.Object(id=user_id), reason=MOTIVO)
        log.info('[BAN] %s banido em "%s" (%s)', user_id, guild.name, origem)
    except Exception as e:
        log.error('[ERRO] Falha ao banir %s em "%s": %s', user_id, guild.name, e)


@bot.event
async def on_member_join(member: discord.Member):
    log.info('[ENTROU] %s (%s) em "%s"', member, member.id, member.guild.name)
    if await esta_na_lista(member.id):
        try:
            await member.send(MOTIVO)  # a DM pode estar fechada
        except Exception:
            pass
        await banir(member.guild, member.id, "entrada")


def _busca_ids_banidos() -> set[int]:
    resp = supabase.table(TABELA).select("user_id").execute()
    return {int(linha["user_id"]) for linha in resp.data}


@tasks.loop(seconds=INTERVALO_SEGUNDOS)
async def varredura():
    """Bane quem já está no servidor e foi adicionado à tabela depois."""
    try:
        ids = await asyncio.to_thread(_busca_ids_banidos)
    except Exception as e:
        log.error("[ERRO] Varredura Supabase: %s", e)
        return
    if not ids:
        return
    for guild in bot.guilds:
        for membro in guild.members:
            if membro.id in ids:
                await banir(guild, membro.id, "varredura")


@bot.event
async def on_ready():
    log.info("[ONLINE] Conectado como %s em %s servidor(es)", bot.user, len(bot.guilds))
    if not varredura.is_running():
        varredura.start()


bot.run(os.environ["DISCORD_TOKEN"], log_handler=None)
